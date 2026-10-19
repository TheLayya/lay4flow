"""Proxy routes with explicit runtime dependencies."""

import asyncio
import re
import time
from collections.abc import Callable

from fastapi import APIRouter, Depends

from ..admin_schemas import (
    ProxyConfigRequest,
    ProxyPoolAddRequest,
    ProxyPoolBatchRequest,
    ProxyPoolImportRequest,
    ProxyPoolUpdateRequest,
    ProxyTestRequest,
)


def _pool_entry_to_dict(entry) -> dict:
    return {
        "id": entry.id,
        "proxy_url": entry.proxy_url,
        "enabled": entry.enabled,
        "captcha_enabled": entry.captcha_enabled,
        "bound_account_count": entry.bound_account_count,
        "note": entry.note,
        "last_check_ok": entry.last_check_ok,
        "last_check_at": entry.last_check_at.isoformat() if entry.last_check_at else None,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
    }


def create_proxy_router(
    get_proxy_manager: Callable, make_session: Callable, verify_token: Callable
):
    router = APIRouter()

    @router.get("/api/config/proxy")
    async def get_proxy_config(token: str = Depends(verify_token)):
        """Get proxy configuration"""
        config = await get_proxy_manager().get_proxy_config()
        return {
            "success": True,
            "config": {
                "enabled": config.enabled,
                "proxy_url": config.proxy_url,
                "media_proxy_enabled": config.media_proxy_enabled,
                "media_proxy_url": config.media_proxy_url,
            },
        }

    @router.get("/api/proxy/config")
    async def get_proxy_config_alias(token: str = Depends(verify_token)):
        """Get proxy configuration (alias for frontend compatibility)"""
        config = await get_proxy_manager().get_proxy_config()
        return {
            "proxy_enabled": config.enabled,
            "proxy_url": config.proxy_url,
            "media_proxy_enabled": config.media_proxy_enabled,
            "media_proxy_url": config.media_proxy_url,
        }

    @router.post("/api/proxy/config")
    async def update_proxy_config_alias(
        request: ProxyConfigRequest, token: str = Depends(verify_token)
    ):
        """Update proxy configuration (alias for frontend compatibility)"""
        try:
            await get_proxy_manager().update_proxy_config(
                enabled=request.proxy_enabled,
                proxy_url=request.proxy_url,
                media_proxy_enabled=request.media_proxy_enabled,
                media_proxy_url=request.media_proxy_url,
            )
        except ValueError as e:
            return {"success": False, "message": str(e)}
        return {"success": True, "message": "代理配置更新成功"}

    @router.post("/api/config/proxy")
    async def update_proxy_config(request: ProxyConfigRequest, token: str = Depends(verify_token)):
        """Update proxy configuration"""
        try:
            await get_proxy_manager().update_proxy_config(
                enabled=request.proxy_enabled,
                proxy_url=request.proxy_url,
                media_proxy_enabled=request.media_proxy_enabled,
                media_proxy_url=request.media_proxy_url,
            )
        except ValueError as e:
            return {"success": False, "message": str(e)}
        return {"success": True, "message": "代理配置更新成功"}

    @router.post("/api/proxy/test")
    async def test_proxy_connectivity(
        request: ProxyTestRequest, token: str = Depends(verify_token)
    ):
        """测试代理是否可访问目标站点（默认 https://labs.google/）"""
        proxy_input = (request.proxy_url or "").strip()
        test_url = (request.test_url or "https://labs.google/").strip()
        timeout_seconds = int(request.timeout_seconds or 15)
        timeout_seconds = max(5, min(timeout_seconds, 60))
        if not proxy_input:
            return {"success": False, "message": "代理地址为空", "test_url": test_url}
        try:
            proxy_url = get_proxy_manager().normalize_proxy_url(proxy_input)
        except ValueError as e:
            return {"success": False, "message": str(e), "test_url": test_url}
        start_time = time.time()
        try:
            proxies = {"http": proxy_url, "https": proxy_url}
            async with make_session() as session:
                resp = await session.get(
                    test_url,
                    proxies=proxies,
                    timeout=timeout_seconds,
                    impersonate="chrome120",
                    allow_redirects=True,
                    verify=False,
                )
            elapsed_ms = int((time.time() - start_time) * 1000)
            status_code = resp.status_code
            final_url = str(resp.url)
            ok = 200 <= status_code < 400
            return {
                "success": ok,
                "message": "代理可用" if ok else f"代理可连通，但目标返回状态码 {status_code}",
                "test_url": test_url,
                "final_url": final_url,
                "status_code": status_code,
                "elapsed_ms": elapsed_ms,
            }
        except Exception as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            return {
                "success": False,
                "message": f"代理测试失败: {str(e)}",
                "test_url": test_url,
                "elapsed_ms": elapsed_ms,
            }

    @router.get("/api/proxy/pool")
    async def list_proxy_pool(token: str = Depends(verify_token)):
        """List the mint egress proxy pool."""
        entries = await get_proxy_manager().list_proxy_pool()
        return {"success": True, "entries": [_pool_entry_to_dict(e) for e in entries]}

    @router.post("/api/proxy/pool")
    async def add_proxy_pool_entry(
        request: ProxyPoolAddRequest, token: str = Depends(verify_token)
    ):
        """Add a proxy to the mint egress pool."""
        try:
            entry_id = await get_proxy_manager().add_proxy_pool_entry(
                request.proxy_url, request.note, captcha_enabled=request.captcha_enabled
            )
        except ValueError as error:
            return {"success": False, "message": str(error)}
        return {"success": True, "id": entry_id}

    @router.post("/api/proxy/pool/import")
    async def import_proxy_pool_entries(
        request: ProxyPoolImportRequest, token: str = Depends(verify_token)
    ):
        """Default to remote-DNS SOCKS5, retaining existing pool rows and bindings."""
        if len(request.text) > 2_100_000:
            return {
                "success": False,
                "message": "导入内容过大，每次最多 500 行，每行最多 4096 字符",
            }
        raw_lines = re.split(r"\r\n|\n|\r", request.text)
        lines = [(index, line.strip()) for index, line in enumerate(raw_lines, 1) if line.strip()]
        if not lines:
            return {"success": False, "message": "请输入代理地址，每行一个"}
        if len(lines) > 500:
            return {"success": False, "message": "每次最多导入 500 个代理"}
        if any(len(line) > 4096 for line in raw_lines):
            return {"success": False, "message": "每行最多 4096 字符，请检查后重试"}

        manager = get_proxy_manager()
        try:
            existing = await manager.list_proxy_pool()
            known = {}
            for entry in existing:
                try:
                    normalized = manager.normalize_proxy_url(entry.proxy_url)
                except ValueError:
                    continue
                if normalized:
                    known.setdefault(normalized, entry.id)
        except Exception:
            return {"success": False, "message": "读取代理池失败，请重试"}

        results = []
        for line_number, line in lines:
            try:
                if "://" not in line and not line.lower().startswith("st5 "):
                    line = f"socks5h://{line}"
                normalized = manager.normalize_proxy_url(line)
                if not normalized:
                    raise ValueError("empty proxy")
            except ValueError:
                results.append(
                    {
                        "line": line_number,
                        "status": "failed",
                        "error_code": "invalid_format",
                        "message": "代理地址格式错误",
                    }
                )
                continue
            except Exception:
                results.append(
                    {
                        "line": line_number,
                        "status": "failed",
                        "error_code": "import_failed",
                        "message": "导入失败，请重试",
                    }
                )
                continue
            if normalized in known:
                results.append({"line": line_number, "status": "skipped", "id": known[normalized]})
                continue
            try:
                entry_id = await manager.add_proxy_pool_entry(normalized)
            except Exception:
                # Storage/transport exceptions can contain the credential-bearing URL.
                results.append(
                    {
                        "line": line_number,
                        "status": "failed",
                        "error_code": "import_failed",
                        "message": "导入失败，请重试",
                    }
                )
                continue
            known[normalized] = entry_id
            results.append({"line": line_number, "status": "added", "id": entry_id})
        return {
            "success": True,
            "total": len(results),
            "added": sum(item["status"] == "added" for item in results),
            "skipped": sum(item["status"] == "skipped" for item in results),
            "failed": sum(item["status"] == "failed" for item in results),
            "results": results,
        }

    @router.delete("/api/proxy/pool/{entry_id}")
    async def remove_proxy_pool_entry(entry_id: int, token: str = Depends(verify_token)):
        """Remove a proxy from the mint egress pool."""
        try:
            await get_proxy_manager().remove_proxy_pool_entry(entry_id)
        except ValueError as error:
            return {"success": False, "message": str(error)}
        return {"success": True}

    @router.put("/api/proxy/pool/{entry_id}")
    async def update_proxy_pool_entry(
        entry_id: int, request: ProxyPoolUpdateRequest, token: str = Depends(verify_token)
    ):
        """Edit a pool entry without replacing its ID."""
        if not request.model_fields_set:
            return {"success": False, "message": "至少提供一个要修改的字段"}
        if "proxy_url" in request.model_fields_set and not request.proxy_url:
            return {"success": False, "message": "代理地址不能为空"}
        try:
            await get_proxy_manager().update_proxy_pool_entry(
                entry_id,
                proxy_url=request.proxy_url,
                note=request.note,
                captcha_enabled=request.captcha_enabled,
            )
        except ValueError as error:
            return {"success": False, "message": str(error)}
        return {"success": True, "id": entry_id}

    @router.post("/api/proxy/pool/batch")
    async def batch_proxy_pool_action(
        request: ProxyPoolBatchRequest, token: str = Depends(verify_token)
    ):
        """Apply an operation to explicit IDs; bound health-check concurrency."""
        manager = get_proxy_manager()
        ids = list(dict.fromkeys(request.ids))
        existing = {entry.id for entry in await manager.list_proxy_pool()}
        limit = asyncio.Semaphore(5 if request.action == "check" else 1)

        async def perform(entry_id):
            if entry_id not in existing:
                return {"id": entry_id, "success": False, "message": "代理不存在"}
            async with limit:
                try:
                    if request.action in {"enable", "disable"}:
                        enabled = request.action == "enable"
                        await manager.set_proxy_pool_enabled(entry_id, enabled)
                        return {"id": entry_id, "success": True, "enabled": enabled}
                    if request.action == "delete":
                        await manager.remove_proxy_pool_entry(entry_id)
                        return {"id": entry_id, "success": True, "deleted": True}
                    ok = await manager.check_proxy_pool_entry(entry_id)
                    return {
                        "id": entry_id,
                        "success": ok,
                        "check_ok": ok,
                        "message": "代理可用" if ok else "代理不可用",
                    }
                except Exception:
                    # Transport/storage errors may include a credential-bearing URL.
                    return {"id": entry_id, "success": False, "message": "操作失败，请重试"}

        results = await asyncio.gather(*(perform(entry_id) for entry_id in ids))
        succeeded = sum(item["success"] for item in results)
        return {
            "success": True,
            "action": request.action,
            "total": len(results),
            "succeeded": succeeded,
            "failed": len(results) - succeeded,
            "partial": 0 < succeeded < len(results),
            "results": results,
        }

    @router.post("/api/proxy/pool/{entry_id}/toggle")
    async def toggle_proxy_pool_entry(entry_id: int, token: str = Depends(verify_token)):
        """Enable or disable a proxy in the mint egress pool."""
        entries = await get_proxy_manager().list_proxy_pool()
        entry = next((e for e in entries if e.id == entry_id), None)
        if entry is None:
            return {"success": False, "message": "代理不存在"}
        await get_proxy_manager().set_proxy_pool_enabled(entry_id, not entry.enabled)
        return {"success": True, "enabled": not entry.enabled}

    @router.post("/api/proxy/pool/{entry_id}/check")
    async def check_proxy_pool_entry(entry_id: int, token: str = Depends(verify_token)):
        """Health-check a proxy against Google and record the result."""
        ok = await get_proxy_manager().check_proxy_pool_entry(entry_id)
        return {"success": ok, "message": "代理可用" if ok else "代理不可用"}

    return router, (
        get_proxy_config,
        get_proxy_config_alias,
        update_proxy_config_alias,
        update_proxy_config,
        test_proxy_connectivity,
    )
