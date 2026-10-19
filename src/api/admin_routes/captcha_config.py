"""Configure the external headed captcha service."""

from fastapi import APIRouter, Depends, HTTPException

from ...core.config import REMOTE_BROWSER_BASE_URL


def create_captcha_config_router(get_database, verify_token, normalize_url):
    router = APIRouter()

    @router.post("/api/captcha/config")
    async def update_captcha_config(request: dict, token: str = Depends(verify_token)):
        if request.get("captcha_method", "remote_browser") != "remote_browser":
            raise HTTPException(status_code=400, detail="Only remote_browser captcha is supported")
        allowed = {
            "captcha_method",
            "remote_browser_base_url",
            "remote_browser_api_key",
            "remote_browser_timeout",
        }
        if set(request) - allowed:
            raise HTTPException(status_code=400, detail="Unsupported captcha configuration fields")
        current = await get_database().get_captcha_config()
        # Older clients may still send a service URL; the provider remains fixed.
        service_url = request.get("remote_browser_base_url", current.remote_browser_base_url)
        api_key = request.get("remote_browser_api_key", current.remote_browser_api_key)
        timeout = request.get("remote_browser_timeout", current.remote_browser_timeout)
        if not isinstance(service_url, str) or not isinstance(api_key, str):
            raise HTTPException(status_code=400, detail="Service URL and API Key must be strings")
        if not api_key.strip():
            raise HTTPException(
                status_code=400, detail="Remote captcha service API Key is required"
            )
        if type(timeout) is not int or not 5 <= timeout <= 600:
            raise HTTPException(
                status_code=400, detail="Remote captcha timeout must be 5–600 integer seconds"
            )
        await get_database().update_captcha_config(
            captcha_method="remote_browser",
            remote_browser_base_url=REMOTE_BROWSER_BASE_URL,
            remote_browser_api_key=api_key.strip(),
            remote_browser_timeout=timeout,
        )
        await get_database().reload_config_to_memory()
        return {"success": True, "message": "Remote captcha configuration saved"}

    @router.get("/api/captcha/config")
    async def get_captcha_config(token: str = Depends(verify_token)):
        settings = await get_database().get_captcha_config()
        return {
            "captcha_method": "remote_browser",
            "remote_browser_base_url": REMOTE_BROWSER_BASE_URL,
            "remote_browser_api_key": settings.remote_browser_api_key,
            "remote_browser_timeout": settings.remote_browser_timeout,
        }

    return router, (update_captcha_config, get_captcha_config)
