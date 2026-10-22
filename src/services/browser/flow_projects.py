"""Prepare account projects through the persisted Flow HTTP session."""

from typing import Any

from ...core.models import Project
from ..flow.flow_rpc import FlowRPCError, flow_rpc
from ..project_pool import flow_session_binding
from .flow_session import account_http_session
from .token_account_probe import TokenManagerAccountProbe


async def create_profile_project(tokens: Any, token: Any, title: str) -> Project:
    refresher = tokens.profile_refresher
    if refresher is None:
        raise FlowRPCError("account_probe_error", "flow_projects")
    expected = flow_session_binding(token)
    proxy = await refresher._proxy_url(token)

    async def check_binding():
        current = await tokens.db.get_token(token.id)
        if (
            current is None
            or current.browser_profile_source != "cloak"
            or flow_session_binding(current) != expected
            or await refresher._proxy_url(current) != proxy
        ):
            raise FlowRPCError("account_probe_error", "flow_projects")

    await check_binding()
    expected_proxy_binding = (
        (token.proxy_pool_id, token.proxy_url) if token.proxy_pool_id is not None else None
    )
    async with account_http_session(tokens, token, proxy) as context:
        # Verify identity in the same context before the write, including for
        # a disabled account that is still completing its initial login.
        probe = TokenManagerAccountProbe(tokens, token)
        result = await probe.verify(token.email, context)
        if result.get("reason"):
            raise FlowRPCError(result["reason"], probe.network_step)
        projects = await flow_rpc(
            context, "UpteDb", ["projects/*", 21], network_step="flow_projects"
        )
        if not isinstance(projects, list) or not projects or not isinstance(projects[0], list):
            raise FlowRPCError("account_probe_error", "flow_projects")
        local_ids = {row.project_id for row in await tokens.db.get_projects_by_token(token.id)}
        for row in projects[0]:
            if not isinstance(row, list) or not row or not isinstance(row[0], str) or not row[0]:
                raise FlowRPCError("account_probe_error", "flow_projects")
            if row[0] in local_ids:
                continue
            metadata = row[1] if len(row) > 1 else None
            remote_title = metadata[0] if isinstance(metadata, list) and metadata else None
            project = Project(
                project_id=row[0],
                token_id=token.id,
                project_name=remote_title if isinstance(remote_title, str) else title,
            )
            break
        else:
            await check_binding()
            payload = await flow_rpc(
                context,
                "jHPbke",
                ["projects/*", [None, [title]], [None, 22]],
                network_step="flow_projects",
            )
            identifier = payload[0] if isinstance(payload, list) and payload else None
            if not isinstance(identifier, str) or not identifier.strip():
                raise FlowRPCError("account_probe_error", "flow_projects")
            project = Project(project_id=identifier, token_id=token.id, project_name=title)
    project.id = await tokens.db.add_project_if_credentials_match(
        project, expected, expected_proxy_binding=expected_proxy_binding
    )
    if project.id is None:
        raise FlowRPCError("account_probe_error", "flow_projects")
    return project
