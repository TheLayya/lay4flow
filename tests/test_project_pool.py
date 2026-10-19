"""Project-pool policy stays deterministic and isolated per account."""

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock

from src.core.models import Project, Token
from src.services.project_pool import ProjectPool


class ProjectPoolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = Mock()
        self.flow = Mock()
        self.pool = ProjectPool(self.db, self.flow, lambda: 2)

    @staticmethod
    def token(token_id=1, project_id=None, active=True):
        return Token(
            id=token_id,
            st=f"st-{token_id}",
            email=f"account-{token_id}@example.invalid",
            current_project_id=project_id,
            is_active=active,
        )

    def test_normalization_sorting_and_round_robin(self):
        self.assertEqual(self.pool.normalize_project_name_base("Demo P2"), "Demo")
        self.assertEqual(self.pool.build_project_name(3, "Demo P2"), "Demo P3")
        projects = [
            Project(id=2, project_id="b", token_id=1, project_name="B"),
            Project(id=1, project_id="a", token_id=1, project_name="A"),
        ]
        ordered = self.pool.sort_projects(projects)
        self.assertEqual([project.project_id for project in ordered], ["a", "b"])
        self.assertEqual(
            self.pool.select_next_project(self.token(project_id="a"), projects).project_id, "b"
        )
        self.assertEqual(
            self.pool.select_next_project(self.token(project_id="unknown"), projects).project_id,
            "a",
        )

    async def test_warmup_filters_inactive_deduplicates_and_applies_limit(self):
        self.db.get_projects_by_token = AsyncMock(
            side_effect=[
                [
                    Project(id=2, project_id="shared", token_id=1, project_name="Shared"),
                    Project(id=1, project_id="one", token_id=1, project_name="One"),
                ],
                [Project(id=1, project_id="two", token_id=2, project_name="Two")],
            ]
        )
        tokens = [self.token(1, project_id="shared"), self.token(2), self.token(3, active=False)]
        self.assertEqual(
            await self.pool.get_personal_warmup_project_ids(tokens, limit=2),
            ["shared", "one"],
        )
        self.assertEqual(self.db.get_projects_by_token.await_count, 1)

    async def test_create_persists_project_and_uses_pool_name(self):
        self.flow.create_project = AsyncMock(return_value="created")
        self.db.add_project = AsyncMock(return_value=7)
        project = await self.pool.create_project_for_token(self.token(), 1, "Demo")
        self.assertEqual(
            project, Project(id=7, project_id="created", token_id=1, project_name="Demo P1")
        )
        self.flow.create_project.assert_awaited_once_with("st-1", "Demo P1")
        self.db.add_project.assert_awaited_once_with(project)

    async def test_ensure_creates_pool_selects_next_and_updates_token(self):
        token = self.token(project_id="one")
        self.db.get_token = AsyncMock(return_value=token)
        self.db.get_projects_by_token = AsyncMock(
            return_value=[
                Project(id=1, project_id="one", token_id=1, project_name="One"),
            ]
        )
        self.db.update_token = AsyncMock()
        created = Project(id=2, project_id="two", token_id=1, project_name="Two")
        create = AsyncMock(return_value=created)
        selected = await self.pool.ensure_project_exists(1, create_project=create)
        self.assertEqual(selected, "two")
        create.assert_awaited_once_with(token, 2)
        self.db.update_token.assert_awaited_once_with(
            1, current_project_id="two", current_project_name="Two"
        )

    async def test_ensure_serializes_same_account_but_not_different_accounts(self):
        tokens = {1: self.token(1), 2: self.token(2)}
        self.db.get_token = AsyncMock(side_effect=lambda token_id: tokens[token_id])
        self.db.get_projects_by_token = AsyncMock(return_value=[])
        self.db.update_token = AsyncMock()
        active = 0
        peak = 0
        barrier = asyncio.Event()

        async def create(token, index):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if peak == 2:
                barrier.set()
            await asyncio.wait_for(barrier.wait(), 1)
            active -= 1
            return Project(project_id=f"{token.id}-{index}", token_id=token.id, project_name="P")

        results = await asyncio.gather(
            self.pool.ensure_project_exists(1, create_project=create),
            self.pool.ensure_project_exists(1, create_project=create),
            self.pool.ensure_project_exists(2, create_project=create),
        )
        self.assertEqual(len(results), 3)
        self.assertEqual(peak, 2)


if __name__ == "__main__":
    unittest.main()
