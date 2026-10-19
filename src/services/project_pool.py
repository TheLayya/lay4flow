"""Per-account Flow project-pool policy and coordination."""

import asyncio
from datetime import datetime
from typing import Any, Awaitable, Callable, List, Optional

from ..core.database import Database
from ..core.logger import debug_logger
from ..core.models import Project, Token


class ProjectPool:
    """Maintain a bounded, round-robin project pool for each account."""

    def __init__(
        self,
        db: Database,
        flow_client: Any,
        pool_size_getter: Optional[Callable[[], Any]] = None,
    ):
        self.db = db
        self.flow_client = flow_client
        self._pool_size_getter = pool_size_getter
        self._lock_guard = asyncio.Lock()
        self._locks: dict[int, asyncio.Lock] = {}

    def pool_size(self) -> int:
        try:
            value = self._pool_size_getter() if self._pool_size_getter else 4
            return max(1, min(50, int(value or 4)))
        except Exception:
            return 4

    def clear_token(self, token_id: int) -> None:
        self._locks.pop(int(token_id), None)

    async def _get_lock(self, token_id: int) -> asyncio.Lock:
        async with self._lock_guard:
            lock = self._locks.get(token_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[token_id] = lock
            return lock

    @staticmethod
    def sort_projects(projects: List[Project]) -> List[Project]:
        return sorted(projects, key=lambda project: (project.id or 0, project.project_id))

    @staticmethod
    def normalize_project_name_base(project_name: Optional[str] = None) -> str:
        raw_name = (project_name or "").strip()
        if raw_name:
            parts = raw_name.rsplit(" ", 1)
            if len(parts) == 2 and parts[1].startswith("P") and parts[1][1:].isdigit():
                return parts[0]
            return raw_name
        return datetime.now().strftime("%b %d - %H:%M")

    @classmethod
    def build_project_name(cls, pool_index: int, base_name: Optional[str] = None) -> str:
        return f"{cls.normalize_project_name_base(base_name)} P{pool_index}"

    @classmethod
    def select_next_project(cls, token: Token, projects: List[Project]) -> Project:
        ordered_projects = cls.sort_projects(projects)
        if not ordered_projects:
            raise ValueError("No available projects for token")
        if len(ordered_projects) == 1:
            return ordered_projects[0]
        if token.current_project_id:
            for index, project in enumerate(ordered_projects):
                if project.project_id == token.current_project_id:
                    return ordered_projects[(index + 1) % len(ordered_projects)]
        return ordered_projects[0]

    async def get_personal_warmup_project_ids(
        self,
        tokens: List[Token],
        limit: Optional[int] = None,
        *,
        sort_projects: Optional[Callable[[List[Project]], List[Project]]] = None,
    ) -> List[str]:
        pool_size = self.pool_size()
        warmup_ids: List[str] = []
        seen_projects: set[str] = set()
        try:
            warmup_limit = None if limit is None else max(1, int(limit))
        except Exception:
            warmup_limit = None
        sorter = sort_projects or self.sort_projects

        for token in tokens:
            if not token or not token.is_active:
                continue
            candidate_ids: List[str] = []
            current_project_id = str(token.current_project_id or "").strip()
            if current_project_id:
                candidate_ids.append(current_project_id)
            projects = [
                project
                for project in await self.db.get_projects_by_token(token.id)
                if project.is_active
            ]
            for project in sorter(projects):
                project_id = str(project.project_id or "").strip()
                if project_id and project_id not in candidate_ids:
                    candidate_ids.append(project_id)
            for project_id in candidate_ids[:pool_size]:
                if project_id in seen_projects:
                    continue
                seen_projects.add(project_id)
                warmup_ids.append(project_id)
                if warmup_limit is not None and len(warmup_ids) >= warmup_limit:
                    return warmup_ids
        return warmup_ids

    async def create_project_for_token(
        self,
        token: Token,
        pool_index: int,
        base_name: Optional[str] = None,
        *,
        build_name: Optional[Callable[[int, Optional[str]], str]] = None,
    ) -> Project:
        name_builder = build_name or self.build_project_name
        project_name = name_builder(pool_index, base_name)
        project_id = await self.flow_client.create_project(token.st, project_name)
        debug_logger.log_info(
            f"[PROJECT] Created pooled project for token {token.id}: {project_name} ({project_id})"
        )
        project = Project(project_id=project_id, token_id=token.id, project_name=project_name)
        project.id = await self.db.add_project(project)
        return project

    async def ensure_project_exists(
        self,
        token_id: int,
        *,
        create_project: Optional[Callable[..., Awaitable[Project]]] = None,
        select_project: Optional[Callable[[Token, List[Project]], Project]] = None,
        sort_projects: Optional[Callable[[List[Project]], List[Project]]] = None,
        pool_size: Optional[int] = None,
    ) -> str:
        lock = await self._get_lock(token_id)
        async with lock:
            token = await self.db.get_token(token_id)
            if not token:
                raise ValueError("Token not found")
            sorter = sort_projects or self.sort_projects
            projects = [
                project
                for project in await self.db.get_projects_by_token(token_id)
                if project.is_active
            ]
            projects = sorter(projects)
            try:
                target_size = pool_size if pool_size is not None else self.pool_size()
                creator = create_project or self.create_project_for_token
                while len(projects) < target_size:
                    project = await creator(token, len(projects) + 1)
                    projects.append(project)
                    projects = sorter(projects)
                selector = select_project or self.select_next_project
                selected_project = selector(token, projects[:target_size])
                await self.db.update_token(
                    token_id,
                    current_project_id=selected_project.project_id,
                    current_project_name=selected_project.project_name,
                )
                return selected_project.project_id
            except Exception as exc:
                raise ValueError(f"Failed to prepare project pool: {exc}") from exc
