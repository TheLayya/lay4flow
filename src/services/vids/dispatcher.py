"""Explicit Vids account bindings with shared account admission and durable receipts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ...core.config import config
from ...core.logger import debug_logger
from ..proxy_health import bound_proxy_unavailable, proxy_pool_snapshot
from .client import VidsProtocolError, VidsTransportError, video_feature
from .gateway import GatewayError, VidsGateway

QUOTA_FRESHNESS_SECONDS = 15 * 60
POOL_SUCCESSOR_FIELD = "pool_successor"
POOL_CHAIN_MAX_ATTEMPTS = 1000
_POOL_FAILOVER_ERRORS = frozenset({"upstream_http_401", "upstream_http_403", "upstream_http_429"})


class VidsDispatchError(GatewayError):
    """A routing or admission failure never requires another account to submit."""


@dataclass(frozen=True)
class VidsAccount:
    token_id: int
    gateway: VidsGateway
    identity_hash: str


@dataclass(frozen=True)
class QuotaObservation:
    account: VidsAccount
    client: object
    feature: int
    quota: dict
    observed_at: float
    queried_at: str


class VidsProviderDispatcher:
    """Bind each request to an explicit account; never balance or retry paid work."""

    def __init__(self, db, repair_session=None):
        self.db = db
        self._accounts: dict[int, VidsAccount] = {}
        self._roots: dict[int, str] = {}
        self.unavailable_receipt_roots: dict[int, Path] = {}
        # ponytail: retain claims for this process; persist/index them when the pool grows.
        self._claims: dict[str, int] = {}
        self._document_refs: dict[str, tuple[int, str]] = {}
        self.repair_session = repair_session
        self._preflight_locks: dict[int, asyncio.Lock] = {}
        # Both public and admin pool routers share this account selection state.
        self._pool_selection_lock = asyncio.Lock()
        self._pool_last_selected: dict[str, int] = {}
        self._pool_pending: set[int] = set()
        # Routing observations belong to this binding, never to Flow credits.
        self._quota_observations: dict[tuple[int, str], QuotaObservation] = {}
        self._quota_reads: dict[tuple[int, str], object] = {}
        # One logical pool key at a time; accounts and other keys stay concurrent.
        self._pool_key_locks: dict[str, asyncio.Lock] = {}
        self.account_policy = None

    async def _observe_receipt(self, token_id, gateway, identifier, *, body=None, started_at=None):
        if self.account_policy is None or not gateway._path(identifier).is_file():
            return
        summary = None
        if isinstance(body, dict):
            summary = {
                name: body[name]
                for name in (
                    "kind",
                    "mode",
                    "seconds",
                    "size",
                    "aspect_ratio",
                    "style",
                    "n",
                    "voice",
                )
                if name in body and type(body[name]) in (str, int, bool)
            }
        try:
            await self.account_policy.observe(
                token_id, gateway.get(identifier), request_summary=summary, started_at=started_at
            )
        except Exception as error:
            # Accepted media/receipts remain available even if diagnostics storage fails.
            debug_logger.log_warning(f"[VIDS_RECEIPT_LOG] {type(error).__name__}")

    @staticmethod
    def _root(gateway):
        return os.path.normcase(str(Path(gateway.root).resolve()))

    def register(self, account: VidsAccount) -> None:
        if (
            not isinstance(account, VidsAccount)
            or type(account.token_id) is not int
            or account.token_id <= 0
            or not isinstance(account.gateway, VidsGateway)
            or not isinstance(account.identity_hash, str)
            or not re.fullmatch(r"[a-f0-9]{64}", account.identity_hash)
            or not account.gateway._ready
            or getattr(account.gateway.client, "initialization_valid", True) is False
        ):
            raise ValueError("A started Vids gateway and validated account binding are required")
        root = self._root(account.gateway)
        if account.token_id in self._accounts or any(
            existing.gateway is account.gateway
            or self._roots[existing.token_id] == root
            or existing.identity_hash == account.identity_hash
            for existing in self._accounts.values()
        ):
            raise ValueError("Vids account, identity and runtime must be unique")
        try:
            self._verify_identity(account, root)
        except VidsDispatchError:
            raise ValueError("Vids account identity does not match its binding") from None
        self._accounts[account.token_id] = account
        self._roots[account.token_id] = root

    def replace(self, accounts) -> None:
        temporary = VidsProviderDispatcher(self.db)
        for account in accounts:
            temporary.register(account)
        self._accounts = temporary._accounts
        self._roots = temporary._roots
        self.unavailable_receipt_roots.clear()
        self._claims.clear()
        self._document_refs.clear()
        self._pool_last_selected.clear()
        self._quota_observations.clear()
        self._quota_reads.clear()

    @staticmethod
    def _quota_feature(client, kind):
        return (
            video_feature(getattr(client, "flags", {}))
            if kind == "video"
            else {"image": 205, "speech": 254, "music": 242}.get(kind)
        )

    def _quota_context_matches(self, token_id, kind, account, client, feature):
        return (
            self._accounts.get(token_id) is account
            and account.gateway.client is client
            and getattr(account.gateway, "_ready", True)
            and getattr(client, "initialization_valid", True) is not False
            and (
                not getattr(account.gateway, "identity", None)
                or getattr(client, "flags", {}).get("docs-hue") == account.gateway.identity
            )
            and self._quota_feature(client, kind) == feature
        )

    async def read_quota(self, token_id, kind):
        """Share existing free quota reads with admission without adding polling."""
        account = self._account(token_id, network=True)
        key = (token_id, kind)
        client = account.gateway.client
        feature = self._quota_feature(client, kind)
        read = object()
        self._quota_reads[key] = read
        try:
            quota = await account.gateway.quota(kind)
            if (
                not isinstance(quota, dict)
                or type(quota.get("available")) is not int
                or quota["available"] < 0
            ):
                raise GatewayError(502, "schema_changed", "Upstream quota structure changed")
        except BaseException:
            # A failed/cancelled refresh must not extend a previous zero balance.
            observation = self._quota_observations.get(key)
            if (
                self._quota_reads.get(key) is read
                and observation is not None
                and observation.account is account
                and observation.client is client
            ):
                self._quota_observations.pop(key, None)
            raise
        if self._quota_reads.get(key) is read and self._quota_context_matches(
            token_id, kind, account, client, feature
        ):
            self._quota_observations[key] = QuotaObservation(
                account,
                client,
                feature,
                dict(quota),
                time.monotonic(),
                datetime.now(timezone.utc).isoformat(),
            )
        return quota

    def quota_snapshot(self, token_id, kind):
        observation = self._quota_observations.get((token_id, kind))
        if observation is None or not self._quota_context_matches(
            token_id, kind, observation.account, observation.client, observation.feature
        ):
            return None
        age = time.monotonic() - observation.observed_at
        return {
            **observation.quota,
            "queried_at": observation.queried_at,
            "error": None,
            "stale": not 0 <= age < QUOTA_FRESHNESS_SECONDS,
        }

    def quota_exhausted(self, token_id, kind):
        observation = self.quota_snapshot(token_id, kind)
        return (
            observation is not None and not observation["stale"] and observation["available"] == 0
        )

    def forget_quota(self, token_id):
        for key in list(self._quota_observations):
            if key[0] == token_id:
                self._quota_observations.pop(key, None)
        for key in list(self._quota_reads):
            if key[0] == token_id:
                self._quota_reads.pop(key, None)

    def _verify_identity(self, account, root, *, check_session=True):
        gateway = account.gateway
        identity = gateway.identity
        try:
            valid = (
                self._root(gateway) == root
                and isinstance(identity, str)
                and bool(identity)
                and hashlib.sha256(identity.encode()).hexdigest() == account.identity_hash
                and (not check_session or gateway.client.flags.get("docs-hue") == identity)
                and (gateway.root / ".account").read_text(encoding="utf-8") == account.identity_hash
            )
        except (AttributeError, OSError, TypeError, ValueError):
            valid = False
        if not valid:
            raise VidsDispatchError(409, "account_mismatch", "Vids account identity changed")

    def _account(self, token_id, *, network=False):
        account = self._accounts.get(token_id) if type(token_id) is int else None
        if account is None:
            if type(token_id) is int and token_id in self.unavailable_receipt_roots:
                raise VidsDispatchError(503, "session_unavailable", "Vids session is unavailable")
            raise VidsDispatchError(404, "account_not_found", "Vids account is not registered")
        self._verify_identity(account, self._roots[token_id])
        if network and (
            not account.gateway._ready
            or getattr(account.gateway.client, "initialization_valid", True) is False
        ):
            raise VidsDispatchError(503, "session_unavailable", "Vids session is unavailable")
        return account

    async def generate(self, token_id, body, idempotency_key):
        account = self._account(token_id)
        kind = body.get("kind") if isinstance(body, dict) else None
        if not isinstance(kind, str) or kind not in {"image", "video", "speech"}:
            raise VidsDispatchError(400, "invalid_kind", "Unsupported Vids generation kind")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
            raise VidsDispatchError(
                400, "idempotency_required", "Idempotency-Key must contain 1 to 200 characters"
            )
        account.gateway.normalize(body)
        key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
        generation_id = "vids_" + key_hash
        self._claim_receipt(token_id, generation_id)

        # Durable receipts are pure replays. Gateway normalization still verifies
        # exact parameters, while expired credentials cannot trigger another POST.
        if account.gateway._path(generation_id).exists():
            result = await account.gateway.generate(body, idempotency_key)
            await self._observe_receipt(token_id, account.gateway, generation_id)
            return result

        if kind == "image" and "image_base64" in body:
            insertion_id = (
                "vids_" + hashlib.sha256(("document-image:" + idempotency_key).encode()).hexdigest()
            )
            # The nested upload shares the pool's receipt namespace and account claim.
            self._claim_receipt(token_id, insertion_id)

        if self.repair_session is not None:
            await self._generation_preflight(token_id, kind, generation_id)
        else:
            self._account(token_id, network=True)

        async def admission():
            await self._admission(token_id, kind, check_quota=True)

        started_at = time.time()
        if self.account_policy is not None:
            tracking = getattr(account.gateway, "_vids_outcome_tracking", None)
            if tracking is None:
                tracking = account.gateway._vids_outcome_tracking = set()
            tracking.add(generation_id)
        try:
            return await account.gateway.generate(body, idempotency_key, admission=admission)
        finally:
            # Cancellation also leaves an unknown durable receipt; record its
            # evidence without treating uncertain acceptance as an account fault.
            await self._observe_receipt(
                token_id, account.gateway, generation_id, body=body, started_at=started_at
            )
            if self.account_policy is not None:
                account.gateway._vids_outcome_tracking.discard(generation_id)

    def _receipt_owners(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"vids_[a-f0-9]{64}", identifier):
            raise VidsDispatchError(404, "not_found", "Generation not found")
        # Failed sessions still reserve their durable receipts after a restart.
        roots = {**self.unavailable_receipt_roots, **self._roots}
        return [
            token_id
            for token_id, root in sorted(roots.items())
            if (Path(root) / (identifier + ".json")).is_file()
        ]

    def _claim_receipt(self, token_id, identifier):
        key_hash = identifier.removeprefix("vids_")
        owner = self._claims.get(key_hash)
        # ponytail: scan registered receipt directories; index if the account pool grows.
        for candidate_id in self._receipt_owners(identifier):
            if owner is not None and owner != candidate_id:
                raise VidsDispatchError(
                    409, "account_affinity_conflict", "Receipt belongs to another Vids account"
                )
            owner = candidate_id
        if owner is not None and owner != token_id:
            raise VidsDispatchError(
                409, "account_affinity_conflict", "Idempotency-Key belongs to another Vids account"
            )
        # Claim before the first await; concurrent calls cannot switch the paid account.
        self._claims[key_hash] = token_id

    async def _generation_preflight(self, token_id, kind, generation_id):
        lock = self._preflight_locks.setdefault(token_id, asyncio.Lock())
        async with lock:
            account = self._account(token_id)
            if generation_id is not None and account.gateway._path(generation_id).exists():
                return
            await self._admission(
                token_id, kind, check_session=False, check_proxy=generation_id is not None
            )
            if not account.gateway._ready:
                raise VidsDispatchError(503, "session_unavailable", "Vids session is unavailable")
            if account.gateway.lock.locked():
                raise VidsDispatchError(429, "busy", "The selected Vids account is busy")
            repaired = False
            if getattr(account.gateway.client, "initialization_valid", True) is False:
                await self._repair_generation_session(token_id)
                repaired = True
                account = self._account(token_id, network=True)
            try:
                quota = await self.read_quota(token_id, kind)
            except VidsProtocolError as error:
                if error.status in {401, 403}:
                    account.gateway.client.initialization_valid = False
                if error.status not in {401, 403} or repaired:
                    raise VidsDispatchError(
                        503, "session_unavailable", "Vids session verification failed"
                    ) from None
                await self._repair_generation_session(token_id)
                account = self._account(token_id, network=True)
                try:
                    quota = await self.read_quota(token_id, kind)
                except Exception as error:
                    if isinstance(error, VidsProtocolError) and error.status in {401, 403}:
                        account.gateway.client.initialization_valid = False
                    raise VidsDispatchError(
                        503, "session_unavailable", "Vids session verification failed"
                    ) from None
            except Exception:
                raise VidsDispatchError(
                    503, "session_unavailable", "Vids session verification failed"
                ) from None
            await self._admission(token_id, kind)
            if generation_id is not None and quota["available"] <= 0:
                raise VidsDispatchError(
                    429, "quota_exhausted", "Vids generation quota is exhausted"
                )

    async def _repair_generation_session(self, token_id):
        try:
            await self.repair_session(token_id)
        except GatewayError:
            raise
        except Exception:
            raise VidsDispatchError(
                503, "session_unavailable", "Vids session repair failed"
            ) from None

    async def _admission(
        self, token_id, kind, *, check_session=True, check_quota=False, check_proxy=False
    ):
        self._account(token_id, network=check_session)
        try:
            token = await self.db.get_token(token_id)
        except Exception:
            raise VidsDispatchError(
                503, "account_lookup_failed", "Vids account admission is unavailable"
            ) from None
        self._account(token_id, network=check_session)
        if (
            token is None
            or not token.is_active
            or kind in {"image", "video"}
            and not getattr(token, f"{kind}_enabled", False)
        ):
            raise VidsDispatchError(
                503, "account_disabled", "The selected Vids account is unavailable"
            )
        if check_quota and self.account_policy is not None:
            reason = await self.account_policy.availability(token_id)
            if reason is not None:
                raise VidsDispatchError(429, "vids_account_protected", reason)
        if (check_proxy or check_quota) and bound_proxy_unavailable(
            token, await proxy_pool_snapshot(self.db)
        ):
            raise VidsDispatchError(
                503, "account_proxy_unavailable", "账号绑定代理连接异常、已停用或正在冷却"
            )
        if check_quota and self.quota_exhausted(token_id, kind):
            raise VidsDispatchError(429, "quota_exhausted", "Vids generation quota is exhausted")

    def document_reference(self, token_id, identifier, *, scenes=False):
        account = self._account(token_id)
        prefix = "vids_scene_" if scenes else "vids_image_"
        digest = hashlib.sha256(
            json.dumps([account.identity_hash, prefix, identifier]).encode()
        ).hexdigest()
        reference = prefix + digest
        self._document_refs[reference] = (token_id, identifier)
        return reference

    def resolve_document_reference(self, reference, *, scenes=False):
        prefix = "vids_scene_" if scenes else "vids_image_"
        if not isinstance(reference, str) or not re.fullmatch(prefix + r"[a-f0-9]{64}", reference):
            raise VidsDispatchError(400, "invalid_source", "Select a saved document source")
        if reference not in self._document_refs:
            # Rebuild stable references from restored snapshots and durable insertion receipts.
            from .client import document_image_shapes
            from .document import document_scenes

            for token_id in sorted(self._accounts):
                try:
                    account = self._account(token_id, network=True)
                except VidsDispatchError:
                    continue
                model = getattr(account.gateway.client, "model_chunk", None)
                if model is not None:
                    try:
                        identifiers = (
                            document_scenes(model)
                            if scenes
                            else [row["shape_id"] for row in document_image_shapes(model)]
                        )
                    except ValueError:
                        identifiers = []
                    for identifier in identifiers:
                        self.document_reference(token_id, identifier, scenes=scenes)
                if not scenes:
                    for record in self.list(token_id):
                        if record.get("operation") == "insert" and record["status"] == "completed":
                            shape_id = record.get("metadata", {}).get("shape_id")
                            if isinstance(shape_id, str):
                                self.document_reference(token_id, shape_id)
        resolved = self._document_refs.get(reference)
        if resolved is None:
            raise VidsDispatchError(400, "invalid_source", "Refresh saved document sources")
        self._account(resolved[0])
        return resolved

    def get(self, token_id, generation_id):
        return self._account(token_id).gateway.get(generation_id)

    async def public_accounts(self):
        """Read local bindings and DB switches without touching upstream sessions."""
        switches = await self.account_switches(self._accounts)
        return self._public_accounts(switches)

    async def account_switches(self, token_ids):
        """Fetch only safe console fields for the requested accounts in one connection."""
        identifiers = sorted({value for value in token_ids if type(value) is int and value > 0})
        if not identifiers:
            return {}
        result = {}
        async with self.db._connect() as db:
            for offset in range(0, len(identifiers), 900):
                chunk = identifiers[offset : offset + 900]
                rows = await (
                    await db.execute(
                        "SELECT id,email,is_active,image_enabled,video_enabled FROM tokens WHERE id IN ("
                        + ",".join("?" for _ in chunk)
                        + ")",
                        chunk,
                    )
                ).fetchall()
                result.update(
                    {
                        row[0]: {
                            "token_id": row[0],
                            "email": row[1] or "",
                            "enabled": bool(row[2]),
                            "image_enabled": bool(row[3]),
                            "video_enabled": bool(row[4]),
                        }
                        for row in rows
                    }
                )
        return result

    def _public_accounts(self, switches):
        result = []
        for token_id in sorted(self._accounts):
            try:
                self._account(token_id, network=True)
                ready = True
            except VidsDispatchError:
                ready = False
            result.append(
                {
                    "token_id": token_id,
                    "email": "",
                    "enabled": False,
                    "image_enabled": False,
                    "video_enabled": False,
                    **switches.get(token_id, {}),
                    "ready": ready,
                }
            )
        return result

    def list(self, token_id):
        gateway = self._account(token_id).gateway
        # ponytail: scan local receipts; index storage when the runtime grows.
        return [gateway.get(path.stem) for path in gateway.root.glob("vids_*.json")]

    def content(self, token_id, generation_id, index=0):
        gateway = self._account(token_id).gateway
        path, mime = (
            gateway.content(generation_id)
            if type(index) is int and index == 0
            else gateway.content(generation_id, index)
        )
        suffix = {"image/jpeg": ".jpg", "audio/wav": ".wav", "video/mp4": ".mp4"}.get(mime)
        if (
            path.resolve().parent != gateway.root
            or suffix is None
            or path.name != generation_id + (f"_{index}" if index else "") + suffix
        ):
            raise VidsDispatchError(410, "asset_expired", "Generation content is unavailable")
        return path, mime

    async def retry_download(self, token_id, generation_id):
        gateway = self._account(token_id, network=True).gateway
        try:
            return await gateway.retry_download(generation_id)
        finally:
            await self._observe_receipt(token_id, gateway, generation_id)

    async def reconcile_outcomes(self, token_id):
        if self.account_policy is None:
            return
        account = self._account(token_id)
        await self.account_policy.reconcile(token_id, account.gateway.root)


class VidsPoolDispatcher:
    """Select eligible accounts automatically and preserve every physical receipt.

    A new unbound pool request may move to another eligible account only after
    a confirmed generate-side 401/403/429 refusal. Each attempt remains a real
    receipt on the account that sent it; the rejected receipt stores a bounded
    internal successor link so the original key can replay the final attempt.

    Public/admin routers share this wrapper. Explicit account routes and
    provider-resource-bound source operations stay on their original account.
    """

    def __init__(self, dispatcher: VidsProviderDispatcher):
        self.dispatcher = dispatcher

    def _owners(self, generation_id):
        owners = self.dispatcher._receipt_owners(generation_id)
        if len(set(owners)) > 1:
            raise VidsDispatchError(
                409, "account_affinity_conflict", "Receipt belongs to multiple accounts"
            )
        return owners[0] if owners else None

    def _receipt_record(self, token_id, identifier):
        if token_id in self.dispatcher._accounts:
            return self.dispatcher._account(token_id).gateway.get(identifier)
        root = self.dispatcher.unavailable_receipt_roots.get(token_id)
        if root is None:
            raise VidsDispatchError(404, "not_found", "Generation not found")
        return VidsGateway(None, root).get(identifier)

    @staticmethod
    def _successor_key(base_id, attempt):
        base_hash = base_id.removeprefix("vids_")
        return "vids-pool-" + hashlib.sha256((base_hash + ":" + str(attempt)).encode()).hexdigest()

    @staticmethod
    def _receipt_id(idempotency_key):
        return "vids_" + hashlib.sha256(idempotency_key.encode()).hexdigest()

    @staticmethod
    def _max_attempts():
        return max(1, min(int(config.flow_max_retries), POOL_CHAIN_MAX_ATTEMPTS))

    @staticmethod
    def _can_failover(record):
        if not isinstance(record, dict) or record.get("status") != "failed":
            return False
        if record.get("error_operation") != "generate":
            return False
        if record.get("error") not in _POOL_FAILOVER_ERRORS:
            return False
        upstream = record.get("upstream_error")
        return not (
            isinstance(upstream, dict) and upstream.get("reason") == "LANGUAGE_NOT_SUPPORTED"
        )

    def _validate_request_hash(self, owner, record, body):
        expected = record.get("request_hash")
        if not isinstance(expected, str):
            if record.get(POOL_SUCCESSOR_FIELD) is not None:
                raise VidsDispatchError(
                    409, "receipt_chain_invalid", "Vids receipt chain cannot be verified"
                )
            return
        account = self.dispatcher._accounts.get(owner)
        if account is not None:
            normalized = account.gateway.normalize(body)
        else:
            root = self.dispatcher.unavailable_receipt_roots.get(owner)
            if root is None:
                raise VidsDispatchError(404, "not_found", "Generation not found")
            normalized = VidsGateway(None, root).normalize(body)
        digest = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
        if digest != expected:
            raise VidsDispatchError(
                409, "idempotency_conflict", "Idempotency-Key has different parameters"
            )

    def _successor(self, record, base_id, base_key, current_owner, attempted, expected_attempt):
        link = record.get(POOL_SUCCESSOR_FIELD)
        if link is None:
            return None
        if not isinstance(link, dict) or set(link) != {
            "attempt",
            "base_receipt_id",
            "token_id",
            "idempotency_key",
            "receipt_id",
        }:
            raise VidsDispatchError(409, "receipt_chain_invalid", "Vids receipt chain is invalid")
        attempt = link.get("attempt")
        base_receipt_id = link.get("base_receipt_id")
        token_id = link.get("token_id")
        key = link.get("idempotency_key")
        receipt_id = link.get("receipt_id")
        if (
            type(attempt) is not int
            or attempt != expected_attempt
            or not 2 <= attempt <= POOL_CHAIN_MAX_ATTEMPTS
            or not isinstance(base_receipt_id, str)
            or not re.fullmatch(r"vids_[a-f0-9]{64}", base_receipt_id)
            or (base_key is not None and base_receipt_id != base_id)
            or type(token_id) is not int
            or token_id <= 0
            or token_id == current_owner
            or token_id in attempted
            or not isinstance(key, str)
            or len(key) > 200
            or key != self._successor_key(base_receipt_id, attempt)
            or not isinstance(receipt_id, str)
            or receipt_id != self._receipt_id(key)
            or not self._can_failover(record)
        ):
            raise VidsDispatchError(409, "receipt_chain_invalid", "Vids receipt chain is invalid")
        if base_key is None:
            base_owner = self._owner_or_error(base_receipt_id)
            self._receipt_record(base_owner, base_receipt_id)
        return link

    def _chain_state(
        self, identifier, *, base_key, base_owner=None, base_record=None, base_id=None
    ):
        current_owner = base_owner if base_owner is not None else self._owner_or_error(identifier)
        current_key = base_key
        current_attempt = 1
        current_record = base_record or self._receipt_record(current_owner, identifier)
        if base_key is None:
            hint = current_record.get(POOL_SUCCESSOR_FIELD)
            if isinstance(hint, dict) and type(hint.get("attempt")) is int:
                current_attempt = max(1, hint["attempt"] - 1)
        attempted = {current_owner}
        seen = {identifier}
        chain_base = base_id if base_key is not None else None
        while True:
            link = self._successor(
                current_record,
                chain_base or identifier,
                base_key if chain_base is not None else None,
                current_owner,
                attempted,
                current_attempt + 1,
            )
            if link is None:
                return {
                    "terminal_owner": current_owner,
                    "terminal_record": current_record,
                    "terminal_key": current_key,
                    "resume": None,
                    "attempted": attempted,
                    "attempt": current_attempt,
                }
            next_id = link["receipt_id"]
            next_owner = link["token_id"]
            chain_base = link["base_receipt_id"]
            if base_key is None and chain_base not in seen:
                base_owner = self._owner_or_error(chain_base)
                self._receipt_record(base_owner, chain_base)
                attempted.add(base_owner)
                seen.add(chain_base)
            if next_id in seen:
                raise VidsDispatchError(
                    409, "receipt_chain_invalid", "Vids receipt chain contains a cycle"
                )
            owners = self.dispatcher._receipt_owners(next_id)
            if len(set(owners)) > 1:
                raise VidsDispatchError(
                    409, "account_affinity_conflict", "Receipt belongs to multiple accounts"
                )
            if not owners:
                return {
                    "terminal_owner": current_owner,
                    "terminal_record": current_record,
                    "terminal_key": current_key,
                    "resume": link,
                    "attempted": attempted,
                    "attempt": current_attempt,
                }
            if owners[0] != next_owner:
                raise VidsDispatchError(
                    409, "receipt_chain_invalid", "Vids receipt successor owner changed"
                )
            seen.add(next_id)
            current_owner = owners[0]
            current_key = link["idempotency_key"]
            current_attempt = link["attempt"]
            current_record = self._receipt_record(current_owner, next_id)
            attempted.add(current_owner)

    async def _eligible(self, kind, exclude=None):
        excluded = exclude or set()
        busy = False
        exhausted = False
        protected = False
        configured = False
        recoverable = None
        proxy_blocked = False
        proxy_entries = await proxy_pool_snapshot(self.dispatcher.db)
        last = self.dispatcher._pool_last_selected.get(kind, 0)
        candidates = sorted(self.dispatcher._accounts)
        candidates = [identifier for identifier in candidates if identifier > last] + [
            identifier for identifier in candidates if identifier <= last
        ]
        for token_id in candidates:
            if token_id in excluded:
                continue
            try:
                account = self.dispatcher._account(token_id)
            except VidsDispatchError as error:
                if error.code in {"session_unavailable", "account_mismatch"}:
                    continue
                raise
            try:
                token = await self.dispatcher.db.get_token(token_id)
            except Exception:
                raise VidsDispatchError(
                    503, "account_lookup_failed", "Vids account admission is unavailable"
                ) from None
            configured = True
            if not account.gateway._ready:
                continue
            if token is None or not getattr(token, "is_active", False):
                continue
            if bound_proxy_unavailable(token, proxy_entries):
                proxy_blocked = True
                continue
            if kind in {"image", "video"} and not getattr(token, f"{kind}_enabled", False):
                continue
            if (
                self.dispatcher.account_policy is not None
                and await self.dispatcher.account_policy.availability(token_id) is not None
            ):
                protected = True
                continue
            if self.dispatcher.quota_exhausted(token_id, kind):
                exhausted = True
                continue
            preflight = self.dispatcher._preflight_locks.get(token_id)
            if (
                account.gateway.lock.locked()
                or token_id in self.dispatcher._pool_pending
                or preflight is not None
                and preflight.locked()
            ):
                busy = True
                continue
            if getattr(account.gateway.client, "initialization_valid", True) is False:
                if self.dispatcher.repair_session is not None and recoverable is None:
                    recoverable = token_id
                continue
            return token_id
        if recoverable is not None:
            return recoverable
        if busy:
            raise VidsDispatchError(429, "busy", "All eligible Vids accounts are busy")
        if exhausted:
            raise VidsDispatchError(429, "quota_exhausted", "Vids generation quota is exhausted")
        if protected:
            raise VidsDispatchError(
                429,
                "vids_account_protected",
                "Vids accounts are protected; check their recovery time",
            )
        if proxy_blocked:
            raise VidsDispatchError(
                503, "account_proxy_unavailable", "账号绑定代理连接异常、已停用或正在冷却"
            )
        if configured:
            raise VidsDispatchError(
                503, "account_disabled", "No eligible Vids account is available"
            )
        raise VidsDispatchError(503, "session_unavailable", "No Vids account is available")

    async def _reserve_new(self, kind, body, identifier, exclude):
        async with self.dispatcher._pool_selection_lock:
            owner = self._claimed_owner(identifier)
            if owner is not None:
                raise VidsDispatchError(
                    409, "account_affinity_conflict", "Generation belongs to another Vids account"
                )
            selected = await self._eligible(kind, exclude=exclude)
            self.dispatcher._account(selected).gateway.normalize(body)
            self.dispatcher._claim_receipt(selected, identifier)
            self.dispatcher._pool_last_selected[kind] = selected
            self.dispatcher._pool_pending.add(selected)
            return selected

    async def _reserve_fixed(self, kind, owner, identifier):
        async with self.dispatcher._pool_selection_lock:
            claimed = self._claimed_owner(identifier)
            if claimed is not None and claimed != owner:
                raise VidsDispatchError(
                    409, "account_affinity_conflict", "Generation belongs to another Vids account"
                )
            self.dispatcher._claim_receipt(owner, identifier)
            self.dispatcher._pool_pending.add(owner)
            self.dispatcher._pool_last_selected[kind] = owner

    async def _run_reserved(self, owner, body, idempotency_key):
        try:
            return await self.dispatcher.generate(owner, body, idempotency_key)
        finally:
            self.dispatcher._pool_pending.discard(owner)

    def _write_successor(self, owner, parent_id, link):
        record = self._receipt_record(owner, parent_id)
        if not self._can_failover(record):
            raise VidsDispatchError(409, "receipt_chain_invalid", "Vids receipt chain is invalid")
        existing = record.get(POOL_SUCCESSOR_FIELD)
        if existing is not None:
            if existing != link:
                raise VidsDispatchError(
                    409, "receipt_chain_invalid", "Vids receipt successor already differs"
                )
            return
        account = self.dispatcher._accounts.get(owner)
        if account is None:
            raise VidsDispatchError(503, "session_unavailable", "Vids session is unavailable")
        record[POOL_SUCCESSOR_FIELD] = link
        account.gateway._save(record)

    async def _continue_failover(
        self,
        *,
        body,
        kind,
        base_id,
        current_owner,
        current_id,
        current_attempt,
        attempted,
        last_result,
    ):
        while current_attempt < self._max_attempts() and self._can_failover(last_result):
            attempted.add(current_owner)
            next_attempt = current_attempt + 1
            next_key = self._successor_key(base_id, next_attempt)
            next_id = self._receipt_id(next_key)
            try:
                next_owner = await self._reserve_new(kind, body, next_id, attempted)
            except (GatewayError, VidsDispatchError):
                return last_result
            link = {
                "attempt": next_attempt,
                "base_receipt_id": base_id,
                "token_id": next_owner,
                "idempotency_key": next_key,
                "receipt_id": next_id,
            }
            try:
                self._write_successor(current_owner, current_id, link)
            except Exception:
                self.dispatcher._pool_pending.discard(next_owner)
                self.dispatcher._claims.pop(next_id.removeprefix("vids_"), None)
                return last_result
            current_owner = next_owner
            current_id = next_id
            current_attempt = next_attempt
            try:
                last_result = await self._run_reserved(next_owner, body, next_key)
            except (GatewayError, VidsDispatchError):
                return last_result
        return last_result

    async def _replay_chain(self, owner, body, idempotency_key, generation_id, kind):
        record = self._receipt_record(owner, generation_id)
        self._validate_request_hash(owner, record, body)
        state = self._chain_state(
            generation_id,
            base_key=idempotency_key,
            base_owner=owner,
            base_record=record,
            base_id=generation_id,
        )
        if state["resume"] is None:
            return await self.dispatcher.generate(
                state["terminal_owner"], body, state["terminal_key"]
            )
        link = state["resume"]
        try:
            await self._reserve_fixed(kind, link["token_id"], link["receipt_id"])
            result = await self._run_reserved(link["token_id"], body, link["idempotency_key"])
        except (GatewayError, VidsDispatchError):
            return VidsGateway.public(state["terminal_record"])
        return await self._continue_failover(
            body=body,
            kind=kind,
            base_id=generation_id,
            current_owner=link["token_id"],
            current_id=link["receipt_id"],
            current_attempt=link["attempt"],
            attempted=state["attempted"],
            last_result=result,
        )

    async def _generate_locked(self, body, idempotency_key, kind, generation_id):
        owner = self._claimed_owner(generation_id)
        if kind == "image" and isinstance(body, dict):
            if "shape_id" in body:
                raise VidsDispatchError(400, "invalid_source", "Select a saved document image")
            if body.get("mode") == "edit" and "image_base64" not in body:
                source_owner, shape_id = self.dispatcher.resolve_document_reference(
                    body.get("source_id")
                )
                if owner is not None and owner != source_owner:
                    raise VidsDispatchError(
                        409, "account_affinity_conflict", "Source belongs to another Vids account"
                    )
                owner = source_owner
                body = {key: value for key, value in body.items() if key != "source_id"}
                body["shape_id"] = shape_id
            elif "source_id" in body:
                raise VidsDispatchError(400, "invalid_source", "Image source requires edit mode")
        elif isinstance(body, dict) and isinstance(body.get("source_id"), str):
            source_owner = self._owners(body["source_id"])
            if source_owner is None:
                if owner is not None:
                    self.dispatcher.get(owner, body["source_id"])
                raise VidsDispatchError(400, "invalid_source", "A completed Vids video is required")
            if owner is not None and owner != source_owner:
                raise VidsDispatchError(
                    409, "account_affinity_conflict", "Source belongs to another Vids account"
                )
            owner = source_owner

        if owner is not None:
            if self._owners(generation_id) is not None:
                return await self._replay_chain(owner, body, idempotency_key, generation_id, kind)
            return await self.dispatcher.generate(owner, body, idempotency_key)

        fixed_unbound = (
            kind == "video"
            and isinstance(body, dict)
            and body.get("mode")
            in {
                "edit",
                "extend",
                "upscale",
            }
        )
        current_owner = await self._reserve_new(kind, body, generation_id, set())
        last_result = await self._run_reserved(current_owner, body, idempotency_key)
        if fixed_unbound:
            return last_result
        return await self._continue_failover(
            body=body,
            kind=kind,
            base_id=generation_id,
            current_owner=current_owner,
            current_id=generation_id,
            current_attempt=1,
            attempted=set(),
            last_result=last_result,
        )

    async def generate(self, _token_id, body, idempotency_key):
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
            # Preserve the explicit dispatcher error before any account lookup.
            raise VidsDispatchError(
                400, "idempotency_required", "Idempotency-Key must contain 1 to 200 characters"
            )
        kind = body.get("kind") if isinstance(body, dict) else None
        if not isinstance(kind, str) or kind not in {"image", "video", "speech"}:
            raise VidsDispatchError(400, "invalid_kind", "Unsupported Vids generation kind")
        key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
        generation_id = "vids_" + key_hash
        lock = self.dispatcher._pool_key_locks.setdefault(key_hash, asyncio.Lock())
        async with lock:
            return await self._generate_locked(body, idempotency_key, kind, generation_id)

    def latest_receipt(self, generation_id):
        """Read the last real receipt in a recorded successor chain.

        Read-only: validates account, ordinal, deterministic child id and link
        shape; never creates a receipt, follows a new attempt or calls upstream.
        A link whose child was never sent resolves to its last sent parent.
        """
        owner = self._owner_or_error(generation_id)
        record = self._receipt_record(owner, generation_id)
        state = self._chain_state(
            generation_id, base_key=None, base_owner=owner, base_record=record
        )
        return state["terminal_record"]

    async def document_images(self, _token_id, scenes=False):
        data = []
        for token_id in sorted(self.dispatcher._accounts):
            try:
                account = self.dispatcher._account(
                    token_id, network=self.dispatcher.repair_session is None
                )
                if self.dispatcher.repair_session is not None:
                    await self.dispatcher._generation_preflight(token_id, "image", None)
                    account = self.dispatcher._account(token_id, network=True)
                await self.dispatcher._admission(token_id, "image")
                result = await account.gateway.document_images(scenes)
            except (GatewayError, VidsProtocolError, VidsTransportError) as error:
                if getattr(error, "code", None) == "account_lookup_failed":
                    raise
                continue
            for row in result["data"]:
                field = "scene_id" if scenes else "source_id"
                identifier = row["scene_id"] if scenes else row["shape_id"]
                data.append(
                    {
                        field: self.dispatcher.document_reference(
                            token_id, identifier, scenes=scenes
                        ),
                        "label": ("场景 " if scenes else "图片 ") + str(len(data) + 1),
                    }
                )
        return {"object": "list", "data": data}

    def public_document_image(self, token_id, record):
        result = VidsGateway.public(record)
        result["metadata"] = {}
        result["source_id"] = None
        if record["status"] == "completed":
            shape_id = record.get("metadata", {}).get("shape_id")
            if isinstance(shape_id, str):
                result["source_id"] = self.dispatcher.document_reference(token_id, shape_id)
        return result

    async def insert_document_image(self, _token_id, body, idempotency_key):
        if not isinstance(body, dict) or set(body) != {"image_base64", "scene_id"}:
            raise VidsDispatchError(
                400, "invalid_request", "image_base64 and scene_id are required"
            )
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
            raise VidsDispatchError(400, "idempotency_required", "Idempotency-Key is required")
        token_id, scene_id = self.dispatcher.resolve_document_reference(
            body["scene_id"], scenes=True
        )
        identifier = (
            "vids_" + hashlib.sha256(("document-image:" + idempotency_key).encode()).hexdigest()
        )
        owner = self._claimed_owner(identifier)
        if owner is not None and owner != token_id:
            raise VidsDispatchError(
                409, "account_affinity_conflict", "Upload belongs to another Vids account"
            )
        account = self.dispatcher._account(token_id, network=True)
        self.dispatcher._claims[identifier.removeprefix("vids_")] = token_id

        async def admission():
            await self.dispatcher._admission(token_id, "image")

        await account.gateway.insert_document_image(
            {"image_base64": body["image_base64"], "scene_id": scene_id},
            idempotency_key,
            admission=admission,
        )
        return self.public_document_image(token_id, account.gateway.get(identifier))

    async def verify_document_image(self, _token_id, identifier):
        token_id = self._owner_or_error(identifier)
        account = self.dispatcher._account(token_id, network=True)
        await account.gateway.verify_document_image(identifier)
        return self.public_document_image(token_id, account.gateway.get(identifier))

    def _claimed_owner(self, generation_id):
        owner = self._owners(generation_id)
        claimed = self.dispatcher._claims.get(generation_id.removeprefix("vids_"))
        if claimed is not None and owner is not None and claimed != owner:
            raise VidsDispatchError(
                409, "account_affinity_conflict", "Receipt account differs from its claim"
            )
        return claimed if claimed is not None else owner

    def models(self):
        for token_id in sorted(self.dispatcher._accounts):
            try:
                self.dispatcher._account(token_id, network=True)
            except VidsDispatchError:
                continue

    def _owner_or_error(self, generation_id):
        owner = self._owners(generation_id)
        if owner is None:
            raise VidsDispatchError(404, "not_found", "Generation not found")
        return owner

    def get(self, _token_id, generation_id):
        return self.dispatcher.get(self._owner_or_error(generation_id), generation_id)

    def list(self, _token_id):
        records = []
        seen = set()
        for token_id in sorted(self.dispatcher._accounts):
            try:
                self.dispatcher._account(token_id, network=True)
            except VidsDispatchError:
                continue
            for record in self.dispatcher.list(token_id):
                if record["id"] in seen:
                    raise VidsDispatchError(
                        409, "account_affinity_conflict", "Receipt belongs to multiple accounts"
                    )
                seen.add(record["id"])
                records.append(record)
        return records

    def content(self, _token_id, generation_id, index=0):
        return self.dispatcher.content(self._owner_or_error(generation_id), generation_id, index)

    async def retry_download(self, _token_id, generation_id):
        return await self.dispatcher.retry_download(
            self._owner_or_error(generation_id), generation_id
        )
