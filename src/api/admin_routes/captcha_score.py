"""Run optional score diagnostics entirely inside the remote browser worker."""

import time
from typing import Optional

from fastapi import APIRouter, Depends

from ..admin_schemas import CaptchaScoreTestRequest


def create_captcha_score_router(verify_token, remote_score, mask_token):
    router = APIRouter()

    @router.post("/api/captcha/score-test")
    async def test_captcha_score(
        _request: Optional[CaptchaScoreTestRequest] = None, _token: str = Depends(verify_token)
    ):
        request = _request or CaptchaScoreTestRequest()
        website_url = (request.website_url or "https://antcpt.com/score_detector/").strip()
        website_key = (request.website_key or "6LcR_okUAAAAAPYrPe-HK_0RULO1aZM15ENyM-Mf").strip()
        action = (request.action or "homepage").strip()
        verify_url = (request.verify_url or "https://antcpt.com/score_detector/verify.php").strip()
        started = time.monotonic()
        result = {
            "captcha_method": "remote_browser",
            "website_url": website_url,
            "website_key": website_key,
            "action": action,
            "verify_url": verify_url,
            "enterprise": bool(request.enterprise),
        }
        try:
            payload = await remote_score(
                website_url=website_url,
                website_key=website_key,
                verify_url=verify_url,
                action=action,
                enterprise=bool(request.enterprise),
            )
            if payload.get("success") is False:
                raise RuntimeError("Remote captcha score check failed")
            verify_result = payload.get("verify_result")
            verify_result = verify_result if isinstance(verify_result, dict) else {}
            token = payload.get("token")
            token = token if isinstance(token, str) else ""
            fingerprint = payload.get("fingerprint")
            fingerprint = fingerprint if isinstance(fingerprint, dict) else None
            success = verify_result.get("success") is True
            result.update(
                {
                    "success": success,
                    "message": "Score verified" if success else "Score verification did not pass",
                    "token_acquired": bool(token) or success,
                    "token_preview": mask_token(token),
                    "token_elapsed_ms": payload.get("token_elapsed_ms", 0),
                    "verify_elapsed_ms": payload.get("verify_elapsed_ms", 0),
                    "verify_http_status": payload.get("verify_http_status"),
                    "score": verify_result.get("score"),
                    "verify_result": verify_result,
                    "fingerprint": fingerprint,
                    "verify_request_meta": {
                        "mode": payload.get("verify_mode") or "remote_browser_page"
                    },
                }
            )
        except Exception as error:
            result.update(
                {
                    "success": False,
                    "message": f"Remote score test failed ({type(error).__name__})",
                    "token_acquired": False,
                }
            )
        result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return result

    return router, (test_captcha_score,)
