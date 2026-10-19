"""Explicit Vids account bindings with shared account admission and durable receipts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .client import VidsProtocolError, VidsTransportError
from .gateway import GatewayError, VidsGateway


class VidsDispatchError(GatewayError):
    """A routing or admission failure never requires another account to submit."""


@dataclass(frozen=True)
class VidsAccount:
    token_id: int
    gateway: VidsGateway
    identity_hash: str


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
            return await account.gateway.generate(body, idempotency_key)

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
            await self._admission(token_id, kind)

        return await account.gateway.generate(body, idempotency_key, admission=admission)

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
            await self._admission(token_id, kind, check_session=False)
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
                quota = await account.gateway.quota(kind)
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
                    quota = await account.gateway.quota(kind)
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

    async def _admission(self, token_id, kind, *, check_session=True):
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
        result = []
        for token_id in sorted(self._accounts):
            token = await self.db.get_token(token_id)
            try:
                self._account(token_id, network=True)
                ready = True
            except VidsDispatchError:
                ready = False
            result.append(
                {
                    "token_id": token_id,
                    "email": getattr(token, "email", ""),
                    "enabled": bool(token and token.is_active),
                    "image_enabled": bool(token and getattr(token, "image_enabled", False)),
                    "video_enabled": bool(token and getattr(token, "video_enabled", False)),
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
        return await self._account(token_id, network=True).gateway.retry_download(generation_id)


class VidsPoolDispatcher:
    """Select one eligible account once, then preserve that receipt affinity.

    New requests rotate eligible accounts independently for each media kind.
    Public and admin routers share the rotation. Once claimed, a receipt
    remains on its original account, including failed and unknown results.
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

    async def _eligible(self, kind):
        busy = False
        configured = False
        recoverable = None
        last = self.dispatcher._pool_last_selected.get(kind, 0)
        candidates = sorted(self.dispatcher._accounts)
        candidates = [identifier for identifier in candidates if identifier > last] + [
            identifier for identifier in candidates if identifier <= last
        ]
        for token_id in candidates:
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
            if kind in {"image", "video"} and not getattr(token, f"{kind}_enabled", False):
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
        if configured:
            raise VidsDispatchError(
                503, "account_disabled", "No eligible Vids account is available"
            )
        raise VidsDispatchError(503, "session_unavailable", "No Vids account is available")

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
        reserved = False
        if owner is None:
            async with self.dispatcher._pool_selection_lock:
                owner = self._claimed_owner(generation_id)
                if owner is None:
                    selected = await self._eligible(kind)
                    # Explicit account routes may claim this key during the DB read.
                    owner = self._claimed_owner(generation_id)
                    if owner is None:
                        self.dispatcher._account(selected).gateway.normalize(body)
                        self.dispatcher._claim_receipt(selected, generation_id)
                        owner = selected
                        self.dispatcher._pool_last_selected[kind] = owner
                        self.dispatcher._pool_pending.add(owner)
                        reserved = True
        try:
            return await self.dispatcher.generate(owner, body, idempotency_key)
        finally:
            if reserved:
                self.dispatcher._pool_pending.discard(owner)

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
