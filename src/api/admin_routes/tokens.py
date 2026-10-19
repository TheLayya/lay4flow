"""Account administration routes with explicit service and auth dependencies."""

from collections.abc import Callable
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from ...services.proxy_manager import ProxyManager
from ..admin_schemas import AddTokenRequest, ImportTokensRequest, ST2ATRequest, UpdateTokenRequest


def create_token_router(
    get_database: Callable,
    get_token_manager: Callable,
    get_concurrency_manager: Callable,
    verify_token: Callable,
):
    """Keep account identity checks in this group; resolve services at call time."""
    router = APIRouter()

    @router.get("/api/tokens")
    async def get_tokens(token: str = Depends(verify_token)):
        """Get all tokens with statistics"""
        token_rows = await get_database().get_all_tokens_with_stats()

        def to_iso(value):
            return value.isoformat() if hasattr(value, "isoformat") else value

        now = datetime.now(timezone.utc)

        def normalize_dt(value):
            if not value:
                return None
            if isinstance(value, str):
                try:
                    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except Exception:
                    return None
            if getattr(value, "tzinfo", None) is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)

        return [
            {
                "id": row.get("id"),
                "st": row.get("st"),  # Session Token for editing
                "at": row.get("at"),  # Access Token for editing (从ST转换而来)
                "at_expires": to_iso(row.get("at_expires"))
                if row.get("at_expires")
                else None,  # 🆕 AT过期时间
                "at_expired": bool(
                    normalize_dt(row.get("at_expires"))
                    and normalize_dt(row.get("at_expires")) <= now
                ),
                "at_expiring_within_1h": bool(
                    normalize_dt(row.get("at_expires"))
                    and normalize_dt(row.get("at_expires")) > now
                    and (normalize_dt(row.get("at_expires")) - now).total_seconds() < 3600
                ),
                "token": row.get("at"),  # 兼容前端 token.token 的访问方式
                "email": row.get("email"),
                "name": row.get("name"),
                "remark": row.get("remark"),
                "is_active": bool(row.get("is_active")),
                "created_at": to_iso(row.get("created_at")) if row.get("created_at") else None,
                "last_used_at": to_iso(row.get("last_used_at"))
                if row.get("last_used_at")
                else None,
                "use_count": row.get("use_count"),
                "credits": row.get("credits"),  # 🆕 余额
                "user_paygate_tier": row.get("user_paygate_tier"),
                "current_project_id": row.get("current_project_id"),  # 🆕 项目ID
                "current_project_name": row.get("current_project_name"),  # 🆕 项目名称
                "captcha_proxy_url": row.get("captcha_proxy_url") or "",
                "protocol_mode": row.get("protocol_mode") or "session",
                "google_cookies": row.get("google_cookies") or "",
                "login_account": row.get("login_account") or "",
                "login_password": row.get("login_password") or "",
                "proxy_url": row.get("proxy_url") or "",
                "proxy_pool_id": row.get("proxy_pool_id"),
                "auto_refresh_enabled": bool(row.get("auto_refresh_enabled", True)),
                "refresh_interval_minutes": row.get("refresh_interval_minutes") or 120,
                "browser_profile_source": row.get("browser_profile_source") or "cookies",
                "browser_profile_epoch": row.get("browser_profile_epoch") or "",
                "browser_profile_status": row.get("browser_profile_status") or "uninitialized",
                "browser_profile_error": row.get("browser_profile_error") or "",
                "browser_profile_verified_at": to_iso(row.get("browser_profile_verified_at"))
                if row.get("browser_profile_verified_at")
                else None,
                "browser_profile_next_at": to_iso(row.get("browser_profile_next_at"))
                if row.get("browser_profile_next_at")
                else None,
                "browser_profile_failures": row.get("browser_profile_failures") or 0,
                "last_st_refresh_at": to_iso(row.get("last_st_refresh_at"))
                if row.get("last_st_refresh_at")
                else None,
                "last_st_refresh_result": row.get("last_st_refresh_result") or "",
                "refresh_status": row.get("refresh_status") or "idle",
                "refresh_error": row.get("refresh_error") or "",
                "refresh_failures": row.get("refresh_failures") or 0,
                "next_refresh_at": to_iso(row.get("next_refresh_at")),
                "last_refresh_at": to_iso(row.get("last_refresh_at")),
                "image_enabled": bool(row.get("image_enabled")),
                "video_enabled": bool(row.get("video_enabled")),
                "image_concurrency": row.get("image_concurrency"),
                "video_concurrency": row.get("video_concurrency"),
                "image_count": row.get("image_count", 0),
                "video_count": row.get("video_count", 0),
                "error_count": row.get("error_count", 0),
                "today_error_count": row.get("today_error_count", 0),
                "consecutive_error_count": row.get("consecutive_error_count", 0),
                "last_error_at": to_iso(row.get("last_error_at"))
                if row.get("last_error_at")
                else None,
                "ban_reason": row.get("ban_reason"),
                "banned_at": to_iso(row.get("banned_at")) if row.get("banned_at") else None,
            }
            for row in token_rows
        ]  # 直接返回数组,兼容前端

    @router.post("/api/tokens")
    async def add_token(request: AddTokenRequest, token: str = Depends(verify_token)):
        """Add a new token"""
        try:
            new_token = await get_token_manager().add_token(
                st=request.st,
                project_id=request.project_id,  # 🆕 支持用户指定project_id
                project_name=request.project_name,
                remark=request.remark,
                captcha_proxy_url=request.captcha_proxy_url.strip()
                if request.captcha_proxy_url is not None
                else None,
                image_enabled=request.image_enabled,
                video_enabled=request.video_enabled,
                image_concurrency=request.image_concurrency,
                video_concurrency=request.video_concurrency,
                protocol_mode=request.protocol_mode,
                google_cookies=request.google_cookies,
                login_account=request.login_account,
                login_password=request.login_password,
                proxy_url=request.proxy_url,
                proxy_pool_id=request.proxy_pool_id,
                auto_refresh_enabled=request.auto_refresh_enabled,
                refresh_interval_minutes=request.refresh_interval_minutes,
                browser_profile_source=request.browser_profile_source,
            )

            # 热更新并发限制，避免必须重启服务
            if get_concurrency_manager():
                await get_concurrency_manager().reset_token(
                    new_token.id,
                    image_concurrency=new_token.image_concurrency,
                    video_concurrency=new_token.video_concurrency,
                )

            return {
                "success": True,
                "message": "Token添加成功",
                "token": {
                    "id": new_token.id,
                    "email": new_token.email,
                    "credits": new_token.credits,
                    "project_id": new_token.current_project_id,
                    "project_name": new_token.current_project_name,
                },
            }
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"添加Token失败: {str(e)}")

    @router.put("/api/tokens/{token_id}")
    async def update_token(
        token_id: int, request: UpdateTokenRequest, token: str = Depends(verify_token)
    ):
        """Save settings even when the existing ST has expired."""
        try:
            existing = await get_token_manager().get_token(token_id)
            if not existing:
                raise HTTPException(status_code=404, detail="Token不存在")
            st = request.st.strip() if request.st is not None else None
            at = None
            at_expires = None
            if st is not None and st != existing.st:
                if not st:
                    raise HTTPException(status_code=400, detail="Session Token不能为空")
                proxy_fields = {}
                if request.proxy_pool_id is not None:
                    selected_id, selected_url = await ProxyManager(
                        get_database()
                    ).select_account_proxy(request.proxy_pool_id)
                    if (
                        existing.browser_profile_source == "cloak"
                        and get_token_manager()._normalize_token_proxy_url(selected_url)
                        != get_token_manager()._normalize_token_proxy_url(existing.proxy_url)
                    ):
                        raise HTTPException(
                            status_code=400,
                            detail="请先单独切换账号代理，再编辑登录凭据",
                        )
                    proxy_fields = {"proxy_pool_id": selected_id, "proxy_url": selected_url}
                elif request.proxy_url is not None:
                    changed_url = get_token_manager()._normalize_token_proxy_url(request.proxy_url)
                    if (
                        existing.browser_profile_source == "cloak"
                        and changed_url
                        != get_token_manager()._normalize_token_proxy_url(existing.proxy_url)
                    ):
                        raise HTTPException(
                            status_code=400,
                            detail="请先单独切换账号代理，再编辑登录凭据",
                        )
                    proxy_fields = {
                        "proxy_pool_id": None,
                        "proxy_url": changed_url,
                    }
                validation_token = existing.model_copy(update=proxy_fields)
                try:
                    result = await get_token_manager()._st_to_at_for_token(validation_token, st)
                    at = result["access_token"]
                    if not at:
                        raise ValueError("Missing access token")
                except Exception:
                    raise HTTPException(
                        status_code=400, detail="新Session Token验证失败，请检查登录状态和代理"
                    )
                email = str(result.get("user", {}).get("email") or "").strip()
                if email.casefold() != existing.email.strip().casefold():
                    raise HTTPException(status_code=400, detail="新Session Token与当前账号不一致")
                st = result.get("_session_token") or st
                at_expires = get_token_manager()._parse_at_expires(result.get("expires"))

            # 更新token (包含AT、ST、AT过期时间、project_id和project_name)
            await get_token_manager().update_token(
                token_id=token_id,
                st=st,
                at=at,
                at_expires=at_expires,  # 🆕 更新AT过期时间
                project_id=request.project_id,
                project_name=request.project_name,
                remark=request.remark,
                captcha_proxy_url=request.captcha_proxy_url.strip()
                if request.captcha_proxy_url is not None
                else None,
                image_enabled=request.image_enabled,
                video_enabled=request.video_enabled,
                image_concurrency=request.image_concurrency,
                video_concurrency=request.video_concurrency,
                protocol_mode=request.protocol_mode,
                google_cookies=request.google_cookies,
                login_account=request.login_account,
                login_password=request.login_password,
                proxy_url=request.proxy_url,
                proxy_pool_id=request.proxy_pool_id,
                auto_refresh_enabled=request.auto_refresh_enabled,
                refresh_interval_minutes=request.refresh_interval_minutes,
                browser_profile_source=request.browser_profile_source,
            )

            # Only an explicit limit change may alter live concurrency state.
            if get_concurrency_manager() and (
                request.image_concurrency is not None or request.video_concurrency is not None
            ):
                updated_token = await get_token_manager().get_token(token_id)
                if updated_token:
                    await get_concurrency_manager().reset_token(
                        token_id,
                        image_concurrency=updated_token.image_concurrency,
                        video_concurrency=updated_token.video_concurrency,
                    )

            return {"success": True, "message": "Token更新成功"}
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @router.delete("/api/tokens/{token_id}")
    async def delete_token(token_id: int, token: str = Depends(verify_token)):
        """Delete token"""
        try:
            await get_token_manager().delete_token(token_id)
            if get_concurrency_manager():
                await get_concurrency_manager().remove_token(token_id)
            return {"success": True, "message": "Token删除成功"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/api/tokens/{token_id}/enable")
    async def enable_token(token_id: int, token: str = Depends(verify_token)):
        """Enable token"""
        try:
            await get_token_manager().enable_token(token_id)
        except ValueError:
            raise HTTPException(
                status_code=400, detail="账号验证失败，请修复Cookie或重新登录后再启用"
            )
        return {"success": True, "message": "Token已启用"}

    @router.post("/api/tokens/{token_id}/disable")
    async def disable_token(token_id: int, token: str = Depends(verify_token)):
        """Disable token"""
        await get_token_manager().disable_token(token_id)
        return {"success": True, "message": "Token已禁用"}

    @router.post("/api/tokens/{token_id}/refresh-credits")
    async def refresh_credits(token_id: int, token: str = Depends(verify_token)):
        """刷新Token余额 🆕"""
        try:
            credits = await get_token_manager().refresh_credits(token_id)
            return {"success": True, "message": "余额刷新成功", "credits": credits}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"刷新余额失败: {str(e)}")

    @router.post("/api/tokens/{token_id}/refresh-at")
    async def refresh_at(token_id: int, token: str = Depends(verify_token)):
        """Refresh now, bypassing the scheduled retry time."""
        from ...core.logger import debug_logger

        try:
            existing_token = await get_token_manager().get_token(token_id)
            if not existing_token:
                raise HTTPException(status_code=404, detail="Token不存在")
            success = await get_token_manager()._refresh_at(token_id, force=True, refresh_st=True)
            updated_token = await get_token_manager().get_token(token_id)
            if success:
                return {
                    "success": True,
                    "message": "AT刷新成功",
                    "token": {
                        "id": updated_token.id,
                        "email": updated_token.email,
                        "at_expires": updated_token.at_expires.isoformat()
                        if updated_token.at_expires
                        else None,
                        "refresh_status": updated_token.refresh_status,
                    },
                }
            return JSONResponse(
                status_code=502,
                content={
                    "success": False,
                    "detail": updated_token.refresh_error or "刷新失败，请检查刷新状态",
                    "refresh_status": updated_token.refresh_status,
                    "refresh_failures": updated_token.refresh_failures,
                    "next_refresh_at": updated_token.next_refresh_at.isoformat()
                    if updated_token.next_refresh_at
                    else None,
                },
            )
        except HTTPException:
            raise
        except Exception as e:
            debug_logger.log_error(f"[API] 刷新AT异常: token_id={token_id}, {type(e).__name__}")
            raise HTTPException(status_code=500, detail="刷新AT异常，请稍后重试并检查刷新状态")

    @router.post("/api/tokens/st2at")
    async def st_to_at(request: ST2ATRequest, token: str = Depends(verify_token)):
        """Convert Session Token to Access Token (仅转换,不添加到数据库)"""
        try:
            result = await get_token_manager().flow_client.st_to_at(request.st)
            return {
                "success": True,
                "message": "ST converted to AT successfully",
                "access_token": result["access_token"],
                "email": result.get("user", {}).get("email"),
                "expires": result.get("expires"),
            }
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.post("/api/tokens/import")
    async def import_tokens(request: ImportTokensRequest, token: str = Depends(verify_token)):
        """批量导入Token"""
        from datetime import datetime, timezone

        added = 0
        updated = 0
        errors = []
        imported = []
        # 保持与历史逻辑一致：按 created_at DESC 的结果中，优先命中同邮箱“最新一条”
        existing_by_email = {}
        for existing_token in await get_token_manager().get_all_tokens():
            if existing_token.email and existing_token.email not in existing_by_email:
                existing_by_email[existing_token.email] = existing_token

        for idx, item in enumerate(request.tokens):
            try:
                st = item.session_token

                if not st:
                    errors.append(f"第{idx + 1}项: 缺少 session_token")
                    continue

                # 使用 ST 转 AT 获取用户信息
                try:
                    result = await get_token_manager().flow_client.st_to_at(st)
                    at = result["access_token"]
                    email = result.get("user", {}).get("email")
                    expires = result.get("expires")

                    if not email:
                        errors.append(f"第{idx + 1}项: 无法获取邮箱信息")
                        continue

                    st = result.get("_session_token") or st

                    # 解析过期时间
                    at_expires = None
                    is_expired = False
                    if expires:
                        try:
                            at_expires = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                            # 判断是否过期
                            now = datetime.now(timezone.utc)
                            is_expired = at_expires <= now
                        except:  # noqa: E722 - preserve the legacy per-item import contract
                            pass

                    # 使用邮箱检查是否已存在
                    existing = existing_by_email.get(email)

                    if existing:
                        # 更新现有Token
                        await get_token_manager().update_token(
                            token_id=existing.id,
                            st=st,
                            at=at,
                            at_expires=at_expires,
                            captcha_proxy_url=item.captcha_proxy_url.strip()
                            if item.captcha_proxy_url is not None
                            else None,
                            image_enabled=item.image_enabled,
                            video_enabled=item.video_enabled,
                            image_concurrency=item.image_concurrency,
                            video_concurrency=item.video_concurrency,
                            protocol_mode=item.protocol_mode,
                            google_cookies=item.google_cookies,
                            login_account=item.login_account,
                            login_password=item.login_password,
                            proxy_url=item.proxy_url,
                            auto_refresh_enabled=item.auto_refresh_enabled,
                            refresh_interval_minutes=item.refresh_interval_minutes,
                        )
                        # 如果过期则禁用
                        if is_expired:
                            await get_token_manager().disable_token(existing.id)
                            existing.is_active = False
                        existing.st = st
                        existing.at = at
                        existing.at_expires = at_expires
                        existing.captcha_proxy_url = item.captcha_proxy_url
                        existing.image_enabled = item.image_enabled
                        existing.video_enabled = item.video_enabled
                        existing.image_concurrency = item.image_concurrency
                        existing.video_concurrency = item.video_concurrency
                        existing.protocol_mode = item.protocol_mode
                        existing.google_cookies = item.google_cookies or ""
                        existing.login_account = item.login_account or ""
                        existing.login_password = item.login_password or ""
                        existing.proxy_url = item.proxy_url or ""
                        existing.auto_refresh_enabled = item.auto_refresh_enabled
                        existing.refresh_interval_minutes = item.refresh_interval_minutes
                        if get_concurrency_manager():
                            await get_concurrency_manager().reset_token(
                                existing.id,
                                image_concurrency=existing.image_concurrency,
                                video_concurrency=existing.video_concurrency,
                            )
                        updated += 1
                    else:
                        # 添加新Token
                        new_token = await get_token_manager().add_token(
                            st=st,
                            captcha_proxy_url=item.captcha_proxy_url.strip()
                            if item.captcha_proxy_url is not None
                            else None,
                            image_enabled=item.image_enabled,
                            video_enabled=item.video_enabled,
                            image_concurrency=item.image_concurrency,
                            video_concurrency=item.video_concurrency,
                            protocol_mode=item.protocol_mode,
                            google_cookies=item.google_cookies,
                            login_account=item.login_account,
                            login_password=item.login_password,
                            proxy_url=item.proxy_url,
                            auto_refresh_enabled=item.auto_refresh_enabled,
                            refresh_interval_minutes=item.refresh_interval_minutes,
                        )
                        # 如果过期则禁用
                        if is_expired:
                            await get_token_manager().disable_token(new_token.id)
                            new_token.is_active = False
                        existing_by_email[email] = new_token
                        if get_concurrency_manager():
                            await get_concurrency_manager().reset_token(
                                new_token.id,
                                image_concurrency=new_token.image_concurrency,
                                video_concurrency=new_token.video_concurrency,
                            )
                        added += 1

                    imported.append(
                        {
                            "index": idx,
                            "token_id": existing.id if existing else new_token.id,
                            "email": email,
                        }
                    )

                except Exception as e:
                    errors.append(f"第{idx + 1}项: {str(e)}")

            except Exception as e:
                errors.append(f"第{idx + 1}项: {str(e)}")

        return {
            "success": True,
            "added": added,
            "updated": updated,
            "imported": imported,
            "errors": errors if errors else None,
            "message": f"导入完成: 新增 {added} 个, 更新 {updated} 个"
            + (f", {len(errors)} 个失败" if errors else ""),
        }

    return router, (
        get_tokens,
        add_token,
        update_token,
        delete_token,
        enable_token,
        disable_token,
        refresh_credits,
        refresh_at,
        st_to_at,
        import_tokens,
    )
