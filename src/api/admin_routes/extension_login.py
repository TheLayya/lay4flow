"""One-time, administrator-bound browser extension login; captcha stays remote."""

import asyncio
import hashlib
import hmac
import json
import secrets
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ...core.config import normalize_public_origin
from ...services.browser.extension_import_jobs import ExtensionImportJobs, get_extension_import_jobs
from ...services.browser.profile_transfer import ProfileTransferError

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class ExtensionLoginSessions:
    """Bounded process-local metadata only; no browser credentials are retained."""

    def __init__(self, ttl_seconds: int = 300, max_sessions: int = 100):
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions
        self.items = {}

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def issue(self, owner, origin):
        now = time.time()
        self.items = {
            key: item
            for key, item in self.items.items()
            if item["expires_at"] > now or item["lock"].locked()
        }
        if len(self.items) >= self.max_sessions:
            raise HTTPException(429, "登录会话过多，请稍后重试", headers=NO_STORE)
        session_id, nonce = secrets.token_urlsafe(18), secrets.token_urlsafe(32)
        self.items[session_id] = {
            "owner": self.digest(owner),
            "nonce": self.digest(nonce),
            "origin": origin,
            "expires_at": int(now) + self.ttl_seconds,
            "lock": asyncio.Lock(),
        }
        return session_id, nonce, self.items[session_id]["expires_at"]

    def get(self, session_id, nonce, owner, origin):
        item = self.items.get(session_id)
        if item is None or not all(
            (
                hmac.compare_digest(item["nonce"], self.digest(nonce)),
                hmac.compare_digest(item["owner"], self.digest(owner)),
                item["origin"] == origin,
            )
        ):
            raise HTTPException(403, "扩展登录凭据无效，请重新开始", headers=NO_STORE)
        if item["expires_at"] <= time.time():
            raise HTTPException(410, "扩展登录已到期，请重新开始", headers=NO_STORE)
        if item.get("cancelled"):
            raise HTTPException(410, "扩展登录已取消", headers=NO_STORE)
        return item


def create_extension_login_router(
    get_tokens: Callable,
    verify_token: Callable,
    *,
    sessions: ExtensionLoginSessions | None = None,
    import_login: Callable | None = None,
    job_store: ExtensionImportJobs | None = None,
):
    router = APIRouter()
    sessions = sessions or ExtensionLoginSessions()

    def origin(request):
        try:
            value = normalize_public_origin(str(request.base_url).rstrip("/"))
        except ValueError:
            raise HTTPException(400, "管理页地址无效", headers=NO_STORE) from None
        # Bearer download clients may omit Origin. Browser writes require it.
        if request.method != "GET" and request.headers.get("origin") != value:
            raise HTTPException(403, "请从当前管理页开始扩展登录", headers=NO_STORE)
        return value

    async def payload(request, limit=1024 * 1024):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > limit:
                raise HTTPException(413, "登录数据过大", headers=NO_STORE)
        try:
            value = json.loads(body or b"{}")
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "登录数据格式无效", headers=NO_STORE) from None

    async def run_import(value):
        if import_login is not None:
            return await import_login(value)
        from ...services.browser.extension_login import ExtensionLogin

        return await ExtensionLogin(get_tokens()).import_login(value)

    def jobs():
        nonlocal job_store
        if job_store is None:
            manager = get_tokens()
            if import_login is None:
                job_store = get_extension_import_jobs(manager)
            else:
                job_store = ExtensionImportJobs(
                    Path(manager.db.db_path).resolve().parent, run_import
                )
        return job_store

    def job_response(operation, *args):
        try:
            return operation(*args)
        except ProfileTransferError as error:
            raise HTTPException(error.status, str(error), headers=NO_STORE) from None

    @router.get("/api/plugin/import/jobs")
    async def extension_job_list(response: Response, owner: str = Depends(verify_token)):
        response.headers.update(NO_STORE)
        return {"success": True, "jobs": job_response(jobs().list, owner)}

    @router.get("/api/plugin/import/jobs/{job_id}")
    async def extension_job_status(
        job_id: str, response: Response, owner: str = Depends(verify_token)
    ):
        response.headers.update(NO_STORE)
        return {"success": True, "job": job_response(jobs().get, owner, job_id)}

    @router.post("/api/plugin/import/jobs/{job_id}/retry")
    async def extension_job_retry(
        job_id: str, request: Request, owner: str = Depends(verify_token)
    ):
        origin(request)
        await payload(request, 16384)
        job = job_response(jobs().retry, owner, job_id)
        return Response(
            json.dumps({"success": True, "job": job}),
            media_type="application/json",
            headers=NO_STORE,
        )

    @router.get("/api/plugin/import/extension.zip")
    async def download_extension(request: Request, owner: str = Depends(verify_token)):
        from scripts.build_account_import_extension import build_extension

        with tempfile.TemporaryDirectory(prefix="lay4flow-login-extension-") as directory:
            path = Path(directory) / "extension.zip"
            build_extension(origin(request), path)
            content = path.read_bytes()
        return Response(
            content,
            media_type="application/zip",
            headers={
                **NO_STORE,
                "Content-Disposition": 'attachment; filename="lay4flow-remote-login-extension.zip"',
            },
        )

    @router.post("/api/plugin/import/start")
    async def start_extension_login(request: Request, owner: str = Depends(verify_token)):
        management_origin = origin(request)
        # The extension opens Flow in the user's fingerprint browser. Its own
        # network settings control that login; no server proxy is selected here.
        await payload(request, 16384)
        session_id, nonce, expires_at = sessions.issue(owner, management_origin)
        return Response(
            json.dumps(
                {
                    "success": True,
                    "session_id": session_id,
                    "nonce": nonce,
                    "expires_at": expires_at,
                    "complete_url": management_origin + "/api/plugin/import/complete",
                    "return_url": management_origin + "/manage?account_import=complete#/accounts",
                    "extension_expected": "lay4flow-import-extension",
                    "browser_profile_source": "cloak",
                }
            ),
            media_type="application/json",
            headers=NO_STORE,
        )

    @router.post("/api/plugin/import/complete")
    async def complete_extension_login(request: Request, owner: str = Depends(verify_token)):
        management_origin = origin(request)
        authorization = request.headers.get("authorization", "")
        prefix = "Bearer import:"
        session_id, separator, nonce = authorization.removeprefix(prefix).partition(".")
        if not authorization.startswith(prefix) or not separator or not nonce or "." in nonce:
            raise HTTPException(403, "扩展登录凭据无效", headers=NO_STORE)
        value = await payload(request)
        if value.get("session_id") != session_id or "nonce" in value:
            raise HTTPException(400, "扩展登录会话不匹配", headers=NO_STORE)
        binding = tuple(value.get(key) for key in ("tab_id", "cookie_store_id"))
        if any(not isinstance(part, str) or not part or len(part) > 128 for part in binding):
            raise HTTPException(400, "扩展登录窗口无效", headers=NO_STORE)
        # An accepted durable receipt outlives the process-local login handshake.
        job = job_response(
            jobs().restore_receipt, session_id, nonce, owner, management_origin, binding
        )
        idempotent = job is not None
        if job is None:
            item = sessions.get(session_id, nonce, owner, management_origin)
            if item.get("binding", binding) != binding:
                raise HTTPException(403, "扩展登录窗口已变化", headers=NO_STORE)
            item["binding"] = binding
            job = job_response(
                jobs().submit,
                owner,
                session_id,
                nonce,
                management_origin,
                binding,
                {
                    "google_cookies": value.get("google_cookies"),
                    "browser_identity": value.get("browser_identity"),
                },
            )
        if job["state"] == "completed":
            result = {
                "success": True,
                "session_id": session_id,
                "email": job["email"],
                "token_id": job["token_id"],
                "action": job["action"],
                "job": job,
                "job_id": job["id"],
                "idempotent": idempotent,
            }
            status_code = 200
        else:
            # Accepted means durably stored, not identity-verified. The explicit
            # job remains visible even when the extension is closed or restarted.
            result = {
                "success": True,
                "accepted": True,
                "action": "queued",
                "session_id": session_id,
                "email": "",
                "token_id": None,
                "job_id": job["id"],
                "job": job,
                "status_url": "/api/plugin/import/jobs/" + job["id"],
                "idempotent": idempotent,
            }
            status_code = 202
        return Response(
            json.dumps(result),
            status_code=status_code,
            media_type="application/json",
            headers=NO_STORE,
        )

    return router
