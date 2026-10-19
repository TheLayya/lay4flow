"""Authenticated account migration and background import endpoints."""

from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from ...services.browser.profile_import_jobs import ProfileImportJobs
from ...services.browser.profile_transfer import ProfileTransfer, transfer_error_detail
from ..admin_schemas import ProfileImportJobRequest, ProfileTransferImportRequest

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class ProfileTransferRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def wrapped(request: Request):
            try:
                response = await handler(request)
            except RequestValidationError:
                # Pydantic's default response echoes input, including exported cookies.
                raise HTTPException(
                    422,
                    detail={"error_code": "invalid_import_request", "message": "导入请求格式有误"},
                    headers=NO_STORE,
                ) from None
            except HTTPException as error:
                error.headers = {**(error.headers or {}), **NO_STORE}
                raise
            response.headers.update(NO_STORE)
            return response

        return wrapped


def create_profile_transfer_router(
    get_token_manager: Callable, verify_token: Callable, legacy_importer: Callable | None = None
) -> APIRouter:
    router = APIRouter(route_class=ProfileTransferRoute)

    def service():
        manager = get_token_manager()
        if not hasattr(manager, "_profile_transfer"):
            manager._profile_transfer = ProfileTransfer(manager)
        return manager._profile_transfer

    def jobs():
        manager = get_token_manager()
        if not hasattr(manager, "_profile_import_jobs"):
            manager._profile_import_jobs = ProfileImportJobs(service(), legacy_importer)
        return manager._profile_import_jobs

    def failed(error):
        status, detail = transfer_error_detail(error)
        return HTTPException(status, detail=detail, headers=NO_STORE)

    async def run(operation):
        try:
            return await operation
        except Exception as error:
            raise failed(error) from None

    def job_result(response, field, operation, *args, **kwargs):
        response.headers.update(NO_STORE)
        try:
            return {"success": True, field: operation(*args, **kwargs)}
        except Exception as error:
            raise failed(error) from None

    @router.get("/api/tokens/{token_id}/profile-export")
    async def profile_transfer_export(
        token_id: int, response: Response, token: str = Depends(verify_token)
    ):
        bundle = await run(service().export(token_id))
        response.headers.update(NO_STORE)
        return {"success": True, "bundle": bundle}

    @router.post("/api/profile-transfer/import")
    async def profile_transfer_import(
        payload: ProfileTransferImportRequest,
        response: Response,
        token: str = Depends(verify_token),
    ):
        response.headers.update(NO_STORE)
        kwargs = {"proxy_pool_id": payload.proxy_pool_id} if payload.proxy_pool_id else {}
        return await run(service().import_bundle(payload.bundle, payload.proxy_url, **kwargs))

    @router.post("/api/profile-transfer/jobs")
    async def profile_import_submit(
        payload: ProfileImportJobRequest, response: Response, token: str = Depends(verify_token)
    ):
        return job_result(
            response,
            "job",
            jobs().submit,
            token,
            payload.bundles,
            payload.request_id,
            proxy_pool_id=payload.proxy_pool_id,
            proxy_url=payload.proxy_url,
        )

    @router.get("/api/profile-transfer/jobs")
    async def profile_import_list(response: Response, token: str = Depends(verify_token)):
        return job_result(response, "jobs", jobs().list, token)

    @router.get("/api/profile-transfer/jobs/{job_id}")
    async def profile_import_get(
        job_id: str, response: Response, token: str = Depends(verify_token)
    ):
        return job_result(response, "job", jobs().get, token, job_id)

    @router.post("/api/profile-transfer/jobs/{job_id}/retry")
    async def profile_import_retry(
        job_id: str, response: Response, token: str = Depends(verify_token)
    ):
        return job_result(response, "job", jobs().retry, token, job_id)

    return router
