"""Pause new writes while the host updater drains this application instance."""

import json
from pathlib import Path


class UpdateActivity:
    def __init__(self, marker: Path):
        self.marker = marker
        self.active_requests = 0


class UpdateGuardMiddleware:
    def __init__(self, app, activity: UpdateActivity):
        self.app = app
        self.activity = activity

    async def __call__(self, scope, receive, send):
        mutation = scope["type"] == "http" and scope["method"] in {"POST", "PUT", "PATCH", "DELETE"}
        if not mutation:
            return await self.app(scope, receive, send)
        if self.activity.marker.exists():
            body = json.dumps(
                {"error": {"code": "update_in_progress", "message": "服务正在升级，请稍后重试"}},
                ensure_ascii=False,
            ).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [
                        (b"content-type", b"application/json; charset=utf-8"),
                        (b"retry-after", b"30"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        self.activity.active_requests += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self.activity.active_requests -= 1
