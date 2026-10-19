"""Account, project, and usage persistence using the owning Database policy."""

from typing import TYPE_CHECKING, Any, Dict, List, Optional

import aiosqlite

from ..models import Project, Token, TokenStats

if TYPE_CHECKING:
    from ..database import Database


class AccountRepository:
    """Preserve account ordering, daily counters, and existing transaction boundaries."""

    def __init__(self, database: "Database"):
        self._database = database

    async def add_token(self, token: Token) -> int:
        """Add a new token"""
        async with self._database._connect(write=True) as db:
            cursor = await db.execute(
                """
                INSERT INTO tokens (st, at, at_expires, email, name, remark, is_active,
                                   credits, user_paygate_tier, current_project_id, current_project_name,
                                   image_enabled, video_enabled, image_concurrency, video_concurrency,
                                   captcha_proxy_url,
                                   protocol_mode, google_cookies, login_account, login_password,
                                   proxy_url, proxy_pool_id, auto_refresh_enabled, refresh_interval_minutes,
                                   browser_profile_source, last_st_refresh_at, last_st_refresh_result,
                                   refresh_status, refresh_error, refresh_failures,
                                   next_refresh_at, last_refresh_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    token.st,
                    token.at,
                    token.at_expires,
                    token.email,
                    token.name,
                    token.remark,
                    token.is_active,
                    token.credits,
                    token.user_paygate_tier,
                    token.current_project_id,
                    token.current_project_name,
                    token.image_enabled,
                    token.video_enabled,
                    token.image_concurrency,
                    token.video_concurrency,
                    token.captcha_proxy_url,
                    token.protocol_mode,
                    token.google_cookies,
                    token.login_account,
                    token.login_password,
                    token.proxy_url,
                    token.proxy_pool_id,
                    token.auto_refresh_enabled,
                    token.refresh_interval_minutes,
                    token.browser_profile_source,
                    token.last_st_refresh_at,
                    token.last_st_refresh_result,
                    token.refresh_status,
                    token.refresh_error,
                    token.refresh_failures,
                    token.next_refresh_at,
                    token.last_refresh_at,
                ),
            )
            await db.commit()
            token_id = cursor.lastrowid

            # Create stats entry
            await db.execute(
                """
                INSERT INTO token_stats (token_id) VALUES (?)
            """,
                (token_id,),
            )
            await db.commit()

            return token_id

    async def get_token(self, token_id: int) -> Optional[Token]:
        """Get token by ID"""
        async with self._database._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tokens WHERE id = ?", (token_id,))
            row = await cursor.fetchone()
            if row:
                return Token(**dict(row))
            return None

    async def get_token_by_st(self, st: str) -> Optional[Token]:
        """Get token by ST"""
        async with self._database._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tokens WHERE st = ?", (st,))
            row = await cursor.fetchone()
            if row:
                return Token(**dict(row))
            return None

    async def get_token_by_email(self, email: str) -> Optional[Token]:
        """Get token by email"""
        async with self._database._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM tokens WHERE email = ? COLLATE NOCASE", (email,)
            )
            row = await cursor.fetchone()
            if row:
                return Token(**dict(row))
            return None

    async def get_all_tokens(self) -> List[Token]:
        """Get all tokens"""
        async with self._database._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tokens ORDER BY created_at DESC")
            rows = await cursor.fetchall()
            return [Token(**dict(row)) for row in rows]

    async def get_all_tokens_with_stats(self) -> List[Dict[str, Any]]:
        """Get all tokens with merged statistics in one query"""
        async with self._database._connect() as db:
            db.row_factory = aiosqlite.Row
            today = self._database._current_stats_date()
            cursor = await db.execute(
                """
                SELECT
                    t.*,
                    COALESCE(ts.image_count, 0) AS image_count,
                    COALESCE(ts.video_count, 0) AS video_count,
                    COALESCE(ts.error_count, 0) AS error_count,
                    COALESCE(CASE WHEN ts.today_date = ? THEN ts.today_image_count ELSE 0 END, 0) AS today_image_count,
                    COALESCE(CASE WHEN ts.today_date = ? THEN ts.today_video_count ELSE 0 END, 0) AS today_video_count,
                    COALESCE(CASE WHEN ts.today_date = ? THEN ts.today_error_count ELSE 0 END, 0) AS today_error_count,
                    COALESCE(ts.consecutive_error_count, 0) AS consecutive_error_count,
                    ts.last_error_at AS last_error_at
                FROM tokens t
                LEFT JOIN token_stats ts ON ts.token_id = t.id
                ORDER BY t.created_at DESC
            """,
                (today, today, today),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_dashboard_stats(self) -> Dict[str, int]:
        """Get dashboard counters with aggregated SQL queries"""
        async with self._database._connect() as db:
            db.row_factory = aiosqlite.Row
            today = self._database._current_stats_date()

            token_cursor = await db.execute("""
                SELECT
                    COUNT(*) AS total_tokens,
                    COALESCE(SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END), 0) AS active_tokens
                FROM tokens
            """)
            token_row = await token_cursor.fetchone()

            stats_cursor = await db.execute(
                """
                SELECT
                    COALESCE(SUM(image_count), 0) AS total_images,
                    COALESCE(SUM(video_count), 0) AS total_videos,
                    COALESCE(SUM(error_count), 0) AS total_errors,
                    COALESCE(SUM(CASE WHEN today_date = ? THEN today_image_count ELSE 0 END), 0) AS today_images,
                    COALESCE(SUM(CASE WHEN today_date = ? THEN today_video_count ELSE 0 END), 0) AS today_videos,
                    COALESCE(SUM(CASE WHEN today_date = ? THEN today_error_count ELSE 0 END), 0) AS today_errors
                FROM token_stats
            """,
                (today, today, today),
            )
            stats_row = await stats_cursor.fetchone()

            token_data = dict(token_row) if token_row else {}
            stats_data = dict(stats_row) if stats_row else {}

            return {
                "total_tokens": int(token_data.get("total_tokens") or 0),
                "active_tokens": int(token_data.get("active_tokens") or 0),
                "total_images": int(stats_data.get("total_images") or 0),
                "total_videos": int(stats_data.get("total_videos") or 0),
                "total_errors": int(stats_data.get("total_errors") or 0),
                "today_images": int(stats_data.get("today_images") or 0),
                "today_videos": int(stats_data.get("today_videos") or 0),
                "today_errors": int(stats_data.get("today_errors") or 0),
            }

    async def get_system_info_stats(self) -> Dict[str, int]:
        """Get lightweight system counters used by admin dashboard"""
        async with self._database._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT
                    COUNT(*) AS total_tokens,
                    COALESCE(SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END), 0) AS active_tokens,
                    COALESCE(SUM(CASE WHEN is_active = 1 THEN credits ELSE 0 END), 0) AS total_credits
                FROM tokens
            """)
            row = await cursor.fetchone()
            data = dict(row) if row else {}
            return {
                "total_tokens": int(data.get("total_tokens") or 0),
                "active_tokens": int(data.get("active_tokens") or 0),
                "total_credits": int(data.get("total_credits") or 0),
            }

    async def get_active_tokens(self) -> List[Token]:
        """Get all active tokens"""
        async with self._database._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM tokens WHERE is_active = 1 ORDER BY last_used_at ASC"
            )
            rows = await cursor.fetchall()
            return [Token(**dict(row)) for row in rows]

    async def update_token(self, token_id: int, **kwargs):
        """Update token fields"""
        async with self._database._connect(write=True) as db:
            updates = []
            params = []

            for key, value in kwargs.items():
                updates.append(f"{key} = ?")
                params.append(value)

            if updates:
                params.append(token_id)
                query = f"UPDATE tokens SET {', '.join(updates)} WHERE id = ?"
                await db.execute(query, params)
                await db.commit()

    async def update_token_if_credentials_match(
        self,
        token_id: int,
        expected: Dict[str, Any],
        *,
        expected_proxy_binding: tuple[int, str] | None = None,
        **kwargs,
    ) -> bool:
        """Update refresh state only if the credential snapshot is unchanged."""
        if not kwargs or not expected:
            return False
        async with self._database._connect(write=True) as db:
            assignments = ", ".join(f"{key} = ?" for key in kwargs)
            where = " AND ".join(f"{key} IS ?" for key in expected)
            params = [*kwargs.values(), token_id, *expected.values()]
            if expected_proxy_binding is not None:
                where += " AND EXISTS (SELECT 1 FROM proxy_pool p WHERE p.id = ? AND p.enabled = 1 AND p.proxy_url IS ?)"
                params.extend(expected_proxy_binding)
            cursor = await db.execute(
                f"UPDATE tokens SET {assignments} WHERE id = ? AND {where}", params
            )
            await db.commit()
            return cursor.rowcount == 1

    async def delete_token(self, token_id: int):
        """Delete token and related data"""
        async with self._database._connect(write=True) as db:
            await db.execute(
                "UPDATE request_logs SET token_id = NULL WHERE token_id = ?", (token_id,)
            )
            await db.execute("DELETE FROM tasks WHERE token_id = ?", (token_id,))
            await db.execute("DELETE FROM token_stats WHERE token_id = ?", (token_id,))
            await db.execute("DELETE FROM projects WHERE token_id = ?", (token_id,))
            await db.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
            await db.commit()

    async def add_project(self, project: Project) -> int:
        """Add a new project"""
        async with self._database._connect(write=True) as db:
            cursor = await db.execute(
                """
                INSERT INTO projects (project_id, token_id, project_name, tool_name, is_active)
                VALUES (?, ?, ?, ?, ?)
            """,
                (
                    project.project_id,
                    project.token_id,
                    project.project_name,
                    project.tool_name,
                    project.is_active,
                ),
            )
            await db.commit()
            return cursor.lastrowid

    async def get_project_by_id(self, project_id: str) -> Optional[Project]:
        """Get project by UUID"""
        async with self._database._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,))
            row = await cursor.fetchone()
            if row:
                return Project(**dict(row))
            return None

    async def get_projects_by_token(self, token_id: int) -> List[Project]:
        """Get all projects for a token"""
        async with self._database._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM projects WHERE token_id = ? ORDER BY created_at DESC", (token_id,)
            )
            rows = await cursor.fetchall()
            return [Project(**dict(row)) for row in rows]

    async def delete_project(self, project_id: str):
        """Delete project"""
        async with self._database._connect(write=True) as db:
            await db.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
            await db.commit()

    async def get_token_stats(self, token_id: int) -> Optional[TokenStats]:
        """Get token statistics"""
        async with self._database._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM token_stats WHERE token_id = ?", (token_id,))
            row = await cursor.fetchone()
            if row:
                return TokenStats(**dict(row))
            return None

    async def increment_image_count(self, token_id: int):
        """Increment image generation count with daily reset"""
        async with self._database._connect(write=True) as db:
            today = self._database._current_stats_date()
            # Get current stats
            cursor = await db.execute(
                "SELECT today_date FROM token_stats WHERE token_id = ?", (token_id,)
            )
            row = await cursor.fetchone()

            # If date changed, reset all daily counters before recording today's image usage.
            if row and row[0] != today:
                await db.execute(
                    """
                    UPDATE token_stats
                    SET image_count = image_count + 1,
                        today_image_count = 1,
                        today_video_count = 0,
                        today_error_count = 0,
                        today_date = ?
                    WHERE token_id = ?
                """,
                    (today, token_id),
                )
            else:
                # Same day, just increment both
                await db.execute(
                    """
                    UPDATE token_stats
                    SET image_count = image_count + 1,
                        today_image_count = today_image_count + 1,
                        today_date = ?
                    WHERE token_id = ?
                """,
                    (today, token_id),
                )
            await db.commit()

    async def increment_video_count(self, token_id: int):
        """Increment video generation count with daily reset"""
        async with self._database._connect(write=True) as db:
            today = self._database._current_stats_date()
            # Get current stats
            cursor = await db.execute(
                "SELECT today_date FROM token_stats WHERE token_id = ?", (token_id,)
            )
            row = await cursor.fetchone()

            # If date changed, reset all daily counters before recording today's video usage.
            if row and row[0] != today:
                await db.execute(
                    """
                    UPDATE token_stats
                    SET video_count = video_count + 1,
                        today_image_count = 0,
                        today_video_count = 1,
                        today_error_count = 0,
                        today_date = ?
                    WHERE token_id = ?
                """,
                    (today, token_id),
                )
            else:
                # Same day, just increment both
                await db.execute(
                    """
                    UPDATE token_stats
                    SET video_count = video_count + 1,
                        today_video_count = today_video_count + 1,
                        today_date = ?
                    WHERE token_id = ?
                """,
                    (today, token_id),
                )
            await db.commit()

    async def increment_error_count(self, token_id: int):
        """Increment error count with daily reset

        Updates two counters:
        - error_count: Historical total errors (never reset)
        - consecutive_error_count: Consecutive errors (reset on success/enable)
        - today_error_count: Today's errors (reset on date change)
        """
        async with self._database._connect(write=True) as db:
            today = self._database._current_stats_date()
            # Get current stats
            cursor = await db.execute(
                "SELECT today_date FROM token_stats WHERE token_id = ?", (token_id,)
            )
            row = await cursor.fetchone()

            # If date changed, reset all daily counters before recording today's error.
            if row and row[0] != today:
                await db.execute(
                    """
                    UPDATE token_stats
                    SET error_count = error_count + 1,
                        consecutive_error_count = consecutive_error_count + 1,
                        today_image_count = 0,
                        today_video_count = 0,
                        today_error_count = 1,
                        today_date = ?,
                        last_error_at = CURRENT_TIMESTAMP
                    WHERE token_id = ?
                """,
                    (today, token_id),
                )
            else:
                # Same day, just increment all counters
                await db.execute(
                    """
                    UPDATE token_stats
                    SET error_count = error_count + 1,
                        consecutive_error_count = consecutive_error_count + 1,
                        today_error_count = today_error_count + 1,
                        today_date = ?,
                        last_error_at = CURRENT_TIMESTAMP
                    WHERE token_id = ?
                """,
                    (today, token_id),
                )
            await db.commit()

    async def reset_error_count(self, token_id: int):
        """Reset consecutive error count (only reset consecutive_error_count, keep error_count and today_error_count)

        This is called when:
        - Token is manually enabled by admin
        - Request succeeds (resets consecutive error counter)

        Note: error_count (total historical errors) is NEVER reset
        """
        async with self._database._connect(write=True) as db:
            await db.execute(
                """
                UPDATE token_stats SET consecutive_error_count = 0 WHERE token_id = ?
            """,
                (token_id,),
            )
            await db.commit()
