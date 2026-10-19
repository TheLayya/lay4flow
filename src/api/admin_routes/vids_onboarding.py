"""Cookie-authenticated, explicit Vids account onboarding actions."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ...services.vids.gateway import GatewayError
from ..openai_common import APIError, api_error_response, read_json_body


def create_vids_onboarding_router(authenticate, get_service):
    router = APIRouter(dependencies=[Depends(authenticate)], tags=["Vids console"])

    def service():
        value = get_service()
        if value is None:
            raise GatewayError(
                503, "profile_unavailable", "Existing account Profiles are unavailable"
            )
        return value

    def failed(error):
        return JSONResponse(
            {"error": {"code": error.code, "message": str(error)}},
            status_code=error.status,
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/api/admin/vids/onboard/candidates")
    async def candidates():
        try:
            rows = await service().candidates()
        except GatewayError as error:
            return failed(error)
        return JSONResponse({"accounts": rows}, headers={"Cache-Control": "no-store"})

    @router.post("/api/admin/vids/onboard/{token_id}")
    async def onboard(token_id: int, request: Request):
        try:
            body = await read_json_body(request, 4096)
            if set(body) - {"create_blank"} or type(body.get("create_blank", False)) is not bool:
                raise GatewayError(400, "invalid_request", "Only create_blank is accepted")
            result = await service().onboard(token_id, create_blank=body.get("create_blank", False))
        except GatewayError as error:
            return failed(error)
        except APIError as error:
            return api_error_response(error)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.post("/api/admin/vids/onboard/{token_id}/reconnect")
    async def reconnect(token_id: int):
        try:
            result = await service().reconnect(token_id)
        except GatewayError as error:
            return failed(error)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    return router
