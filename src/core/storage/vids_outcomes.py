"""Atomic Vids receipt settlement, safe logs, and provider-only account protection."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from datetime import datetime, timezone

import aiosqlite

_STATUSES = {
    "generating",
    "received",
    "completed",
    "unknown",
    "failed",
    "no_result",
    "specification_mismatch",
    "download_failed",
}
_KINDS = {"image", "video", "speech", "music"}
_ACTIONS = {"LIMIT", "DEACTIVE", "DISABLE"}
_SAFE_ERRORS = {
    "invalid_source",
    "acceptance_unknown",
    "interrupted_acceptance_unknown",
    "image_upload_unknown",
    "upstream_returned_no_media",
    "upstream_image_count",
    "upstream_image_specification",
    "upstream_video_specification",
    "download_failed",
    "download_interrupted",
    "session_unavailable",
    "session_persistence_failed",
    "account_mismatch",
    "account_disabled",
    "account_lookup_failed",
    "account_protected",
    "quota_exhausted",
    "busy",
    "runtime_busy",
}
_COUNTERS = (
    "image_count",
    "video_count",
    "speech_count",
    "music_count",
    "success_count",
    "error_count",
    "consecutive_error_count",
    "today_success_count",
    "today_error_count",
)


class VidsOutcomeRepository:
    """Commit the receipt index, final counter and log in a single transaction.

    The caller classifies a receipt as success/failure only when its paid outcome
    is known. Unknown acceptance, downloads, uploads and session probes use None.
    Flow token_stats and manual account switches are never written here.
    """

    def __init__(self, database):
        self._database = database
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def ensure_schema(self):
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            async with self._database._connect(write=True) as db:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS vids_account_stats ("
                    "token_id INTEGER PRIMARY KEY, "
                    + ", ".join(f"{field} INTEGER NOT NULL DEFAULT 0" for field in _COUNTERS)
                    + ", today_date TEXT, last_success_at REAL, last_error_at REAL, "
                    "FOREIGN KEY(token_id) REFERENCES tokens(id) ON DELETE CASCADE)"
                )
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS vids_account_protection ("
                    "token_id INTEGER PRIMARY KEY, action TEXT NOT NULL, reason TEXT NOT NULL, "
                    "until_at REAL, created_at REAL NOT NULL, source TEXT NOT NULL DEFAULT 'rule', "
                    "FOREIGN KEY(token_id) REFERENCES tokens(id) ON DELETE CASCADE)"
                )
                columns = await (
                    await db.execute("PRAGMA table_info(vids_account_protection)")
                ).fetchall()
                if not any(column[1] == "source" for column in columns):
                    await db.execute(
                        "ALTER TABLE vids_account_protection ADD COLUMN source TEXT NOT NULL DEFAULT 'rule'"
                    )
                candidates_existed = await (
                    await db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vids_protection_candidates'"
                    )
                ).fetchone()
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS vids_protection_candidates ("
                    "candidate_id TEXT PRIMARY KEY, token_id INTEGER NOT NULL, action TEXT NOT NULL, "
                    "reason TEXT NOT NULL, until_at REAL, created_at REAL NOT NULL, source TEXT NOT NULL, "
                    "FOREIGN KEY(token_id) REFERENCES tokens(id) ON DELETE CASCADE)"
                )
                if not candidates_existed:
                    await db.execute(
                        "INSERT INTO vids_protection_candidates "
                        "SELECT 'legacy_' || token_id,token_id,action,reason,until_at,created_at,source "
                        "FROM vids_account_protection"
                    )
                # Keep an index even if its account or log is later deleted. A
                # retained receipt can never be credited to a replacement account.
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS vids_receipt_outcomes ("
                    "receipt_id TEXT PRIMARY KEY, token_id INTEGER, kind TEXT NOT NULL, "
                    "status TEXT NOT NULL, settlement TEXT, log_id INTEGER, "
                    "created_at REAL NOT NULL, updated_at REAL NOT NULL, "
                    "FOREIGN KEY(token_id) REFERENCES tokens(id) ON DELETE SET NULL, "
                    "FOREIGN KEY(log_id) REFERENCES request_logs(id) ON DELETE SET NULL)"
                )
                await db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_vids_receipt_token "
                    "ON vids_receipt_outcomes(token_id)"
                )
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS vids_fault_events (receipt_id TEXT PRIMARY KEY, token_id INTEGER NOT NULL, event_at REAL NOT NULL, success INTEGER NOT NULL, FOREIGN KEY(token_id) REFERENCES tokens(id) ON DELETE CASCADE)"
                )
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS vids_fault_resets (token_id INTEGER PRIMARY KEY, reset_at REAL NOT NULL, FOREIGN KEY(token_id) REFERENCES tokens(id) ON DELETE CASCADE)"
                )
                await db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_vids_fault_window ON vids_fault_events(token_id,success,event_at)"
                )
                await db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_vids_protection_candidate_token ON vids_protection_candidates(token_id)"
                )
                await db.commit()
            self._initialized = True

    @staticmethod
    def _receipt(record):
        if not isinstance(record, dict):
            raise ValueError("Invalid Vids receipt")
        if record.get("operation") == "insert":
            return None
        identifier = record.get("id")
        kind, status = record.get("kind"), record.get("status")
        if (
            not isinstance(identifier, str)
            or not re.fullmatch(r"vids_[a-f0-9]{64}", identifier)
            or not isinstance(kind, str)
            or kind not in _KINDS
            or not isinstance(status, str)
            or status not in _STATUSES
        ):
            raise ValueError("Invalid Vids receipt")
        return identifier, kind, status

    @staticmethod
    def _timestamp(value, fallback):
        return float(value) if type(value) in (int, float) and math.isfinite(value) else fallback

    @staticmethod
    def _safe_error(value):
        return (
            value
            if isinstance(value, str)
            and (value in _SAFE_ERRORS or re.fullmatch(r"upstream_http_[1-5][0-9]{2}", value))
            else None
        )

    @staticmethod
    def _safe_upstream_error(value):
        """Log only bounded RPC status/reason, never the raw provider message."""
        if not isinstance(value, dict):
            return None
        result = {}
        for key in ("status", "reason"):
            item = value.get(key)
            if isinstance(item, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", item):
                result[key] = item
        return result or None

    @staticmethod
    def _decision(decision):
        if decision is None:
            return None
        if not isinstance(decision, dict):
            raise ValueError("Invalid Vids protection decision")
        action, seconds = decision.get("action"), decision.get("seconds", 0)
        if action in {"RETRY", "DELAY"}:
            return None
        if (
            not isinstance(action, str)
            or action not in _ACTIONS
            or type(seconds) is not int
            or not 0 <= seconds <= 7 * 86400
        ):
            raise ValueError("Invalid Vids protection decision")
        if action == "LIMIT" and seconds == 0:
            return None
        keyword = decision.get("keyword")
        if (
            not isinstance(keyword, str)
            or not 1 <= len(keyword) <= 256
            or any(ord(char) < 32 or ord(char) == 127 for char in keyword)
        ):
            raise ValueError("Invalid Vids protection reason")
        return {"action": action, "seconds": seconds, "keyword": keyword}

    async def record_receipt(
        self,
        token_id,
        record,
        *,
        outcome=None,
        decision=None,
        consecutive_failure_threshold=3,
        failure_cooldown_seconds=300,
        account_fault=True,
        allow_insert=True,
    ):
        """Index one receipt; settlement succeeds once across calls and restarts.

        A pending/download-failed log may become completed later, while a settled
        receipt keeps its original outcome. Cleared logs remain cleared.
        """
        receipt = self._receipt(record)
        if receipt is None:
            return {"inserted": False, "settled": False, "log_id": None}
        if (
            type(token_id) is not int
            or token_id <= 0
            or outcome not in {None, "success", "failure"}
        ):
            raise ValueError("Invalid Vids settlement")
        if (
            type(consecutive_failure_threshold) is not int
            or not 1 <= consecutive_failure_threshold <= 100
            or type(failure_cooldown_seconds) is not int
            or not 1 <= failure_cooldown_seconds <= 86400
        ):
            raise ValueError("Invalid Vids protection settings")
        if outcome == "success" and record["status"] != "completed":
            raise ValueError("A completed Vids receipt is required for success")
        if outcome == "failure" and record["status"] not in {
            "failed",
            "no_result",
            "specification_mismatch",
        }:
            raise ValueError("A known Vids failure is required")
        decision = self._decision(decision)
        await self.ensure_schema()
        identifier, kind, status = receipt
        now = time.time()
        created = min(now, max(0, self._timestamp(record.get("created_at"), now)))
        event_time = min(
            now,
            max(
                created, self._timestamp(record.get("updated_at", record.get("completed_at")), now)
            ),
        )
        async with self._database._connect(write=True) as db:
            await db.execute("BEGIN IMMEDIATE")
            existing = await (
                await db.execute(
                    "SELECT token_id,kind,settlement,log_id,status FROM vids_receipt_outcomes "
                    "WHERE receipt_id=?",
                    (identifier,),
                )
            ).fetchone()
            if existing is None and not allow_insert:
                await db.commit()
                return {"inserted": False, "settled": False, "log_id": None}
            if existing is not None and (existing[0] != token_id or existing[1] != kind):
                raise ValueError("Vids receipt account affinity changed")
            token = await (
                await db.execute("SELECT 1 FROM tokens WHERE id=?", (token_id,))
            ).fetchone()
            if token is None:
                raise ValueError("Vids account does not exist")
            inserted = existing is None
            newly_settled = outcome is not None and (existing is None or existing[2] is None)
            log_id = existing[3] if existing else None
            # Older concurrent observations cannot downgrade a final log.
            if existing is not None and existing[2] is not None:
                await db.commit()
                return {"inserted": False, "settled": False, "log_id": log_id}
            # Neutral final outcomes (unknown acceptance, schema/media faults)
            # also keep a stable final log. Re-reading an unchanged receipt
            # must not inflate latency, nor may an older pending snapshot win.
            if (
                existing is not None
                and outcome is None
                and (
                    existing[4] == status
                    or existing[4] not in {"generating", "received"}
                    and status in {"generating", "received"}
                )
            ):
                await db.commit()
                return {"inserted": False, "settled": False, "log_id": log_id}
            request = json.dumps({"provider": "vids", "kind": kind, "generation_id": identifier})
            response = {"provider": "vids", "id": identifier, "kind": kind, "status": status}
            error = self._safe_error(record.get("error"))
            if error:
                response["error"] = {"code": error}
            upstream = self._safe_upstream_error(record.get("upstream_error"))
            if upstream:
                response["upstream_error"] = upstream
            response = json.dumps(response)
            protocol_status = (
                int(error.removeprefix("upstream_http_"))
                if error and re.fullmatch(r"upstream_http_[1-5][0-9]{2}", error)
                else None
            )
            status_code = (
                200
                if status == "completed"
                else 102
                if status in {"generating", "received"}
                else protocol_status
                if protocol_status is not None
                else 502
            )
            progress = 100 if status == "completed" else 50 if status == "received" else 0
            values = (response, status_code, max(0, event_time - created), status, progress)
            if inserted:
                cursor = await db.execute(
                    "INSERT INTO request_logs(token_id,operation,request_body,response_body,"
                    "status_code,duration,status_text,progress,attempt_count,retry_count,account_count) "
                    "VALUES(?,?,?,?,?,?,?,?,1,0,1)",
                    (token_id, "vids_generate_" + kind, request, *values),
                )
                log_id = cursor.lastrowid
                await db.execute(
                    "INSERT INTO vids_receipt_outcomes(receipt_id,token_id,kind,status,settlement,"
                    "log_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (identifier, token_id, kind, status, outcome, log_id, created, now),
                )
            else:
                if log_id is not None:
                    await db.execute(
                        "UPDATE request_logs SET response_body=?,status_code=?,duration=?,status_text=?,"
                        "progress=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (*values, log_id),
                    )
                await db.execute(
                    "UPDATE vids_receipt_outcomes SET status=?,settlement=?,updated_at=? WHERE receipt_id=?",
                    (status, outcome, now, identifier),
                )
            if newly_settled:
                if outcome == "success" or account_fault:
                    await db.execute(
                        "INSERT OR IGNORE INTO vids_fault_events(receipt_id,token_id,event_at,success) VALUES(?,?,?,?)",
                        (identifier, token_id, event_time, int(outcome == "success")),
                    )
                await self._settle(
                    db,
                    token_id,
                    kind,
                    outcome,
                    now,
                    decision,
                    consecutive_failure_threshold,
                    failure_cooldown_seconds,
                    event_time,
                    "updated_at" in record or "completed_at" in record,
                    account_fault,
                    identifier,
                )
                # Recovered failures retain their original deadline; a restart
                # never turns an expired historical failure into a new ban.
                await self._expire(db, now, token_id)
            await db.commit()
            return {"inserted": inserted, "settled": newly_settled, "log_id": log_id}

    async def _settle(
        self,
        db,
        token_id,
        kind,
        outcome,
        now,
        decision,
        threshold,
        cooldown,
        event_time,
        has_event_time,
        account_fault,
        receipt_id,
    ):
        await self._expire(db, now, token_id)
        await db.execute(
            "INSERT OR IGNORE INTO vids_account_stats(token_id) VALUES(?)", (token_id,)
        )
        prior = await (
            await db.execute(
                "SELECT last_success_at,last_error_at FROM vids_account_stats WHERE token_id=?",
                (token_id,),
            )
        ).fetchone()
        previous_success = prior[0] or 0
        previous_event = max(previous_success, prior[1] or 0)
        reset = await (
            await db.execute("SELECT reset_at FROM vids_fault_resets WHERE token_id=?", (token_id,))
        ).fetchone()
        boundary = max(previous_success, reset[0] if reset else 0)
        today = (
            datetime.fromtimestamp(event_time).date().isoformat()
            if has_event_time
            else self._database._current_stats_date()
        )
        await db.execute(
            "UPDATE vids_account_stats SET today_success_count=0,today_error_count=0,today_date=? "
            "WHERE token_id=? AND (today_date IS NULL OR today_date<?)",
            (today, token_id, today),
        )
        if outcome == "success":
            await db.execute(
                f"UPDATE vids_account_stats SET {kind}_count={kind}_count+1,success_count=success_count+1,"
                "today_success_count=today_success_count+CASE WHEN today_date=? THEN 1 ELSE 0 END,"
                "consecutive_error_count=CASE WHEN ?>=? THEN 0 ELSE consecutive_error_count END,last_success_at=MAX(COALESCE(last_success_at,0),?) "
                "WHERE token_id=?",
                (today, event_time, previous_event, event_time, token_id),
            )
            await db.execute(
                "DELETE FROM vids_protection_candidates WHERE token_id=? AND action='LIMIT' AND created_at<=?",
                (token_id, event_time),
            )
            count = await self._recount_faults(db, token_id)
            # A late success can split a previously observed failure streak.
            # Revoke only protection derived from that streak; keyword rules
            # retain their own deadlines until a later success or explicit clear.
            if count < threshold:
                await db.execute(
                    "DELETE FROM vids_protection_candidates WHERE token_id=? AND source='threshold'",
                    (token_id,),
                )
            await self._project_protection(db, token_id)
            return
        await db.execute(
            "UPDATE vids_account_stats SET error_count=error_count+1,"
            "today_error_count=today_error_count+CASE WHEN today_date=? THEN 1 ELSE 0 END,"
            "consecutive_error_count=consecutive_error_count+?,"
            "last_error_at=MAX(COALESCE(last_error_at,0),?) WHERE token_id=?",
            (today, int(account_fault and event_time > boundary), event_time, token_id),
        )
        if not account_fault or event_time <= boundary:
            return
        count = await self._recount_faults(db, token_id)
        source = "rule"
        if decision is None and count >= threshold:
            decision = {"action": "LIMIT", "seconds": cooldown, "keyword": "连续 Vids 生成失败保护"}
            source = "threshold"
            # Recovery may deliver older failures after newer ones. A streak's
            # cooldown starts at its latest failure, independent of arrival order.
            event_time = (
                await (
                    await db.execute(
                        "SELECT MAX(event_at) FROM vids_fault_events WHERE token_id=? AND success=0 AND event_at>?",
                        (token_id, boundary),
                    )
                ).fetchone()
            )[0]
        if decision is None:
            return
        action = decision["action"]
        until = event_time + decision["seconds"] if action == "LIMIT" else None
        await db.execute(
            "INSERT INTO vids_protection_candidates(candidate_id,token_id,action,reason,until_at,created_at,source) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(candidate_id) DO UPDATE SET reason=excluded.reason,"
            "until_at=excluded.until_at,created_at=excluded.created_at",
            (
                f"threshold_{token_id}" if source == "threshold" else receipt_id,
                token_id,
                action,
                decision["keyword"],
                until,
                event_time,
                source,
            ),
        )
        await self._project_protection(db, token_id)

    @staticmethod
    async def _project_protection(db, token_id):
        # Keep all candidates: a late success may remove the strongest LIMIT
        # while a later, shorter keyword rule still needs to remain effective.
        row = await (
            await db.execute(
                "SELECT action,reason,until_at,created_at,source FROM vids_protection_candidates "
                "WHERE token_id=? ORDER BY CASE action WHEN 'DISABLE' THEN 3 WHEN 'DEACTIVE' THEN 2 ELSE 1 END DESC, "
                "until_at DESC, created_at DESC LIMIT 1",
                (token_id,),
            )
        ).fetchone()
        if row is None:
            await db.execute("DELETE FROM vids_account_protection WHERE token_id=?", (token_id,))
        else:
            await db.execute(
                "INSERT INTO vids_account_protection(token_id,action,reason,until_at,created_at,source) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(token_id) DO UPDATE SET action=excluded.action,reason=excluded.reason,"
                "until_at=excluded.until_at,created_at=excluded.created_at,source=excluded.source",
                (token_id, *row),
            )

    @staticmethod
    async def _recount_faults(db, token_id):
        success = (
            await (
                await db.execute(
                    "SELECT MAX(event_at) FROM vids_fault_events WHERE token_id=? AND success=1",
                    (token_id,),
                )
            ).fetchone()
        )[0] or 0
        reset = await (
            await db.execute("SELECT reset_at FROM vids_fault_resets WHERE token_id=?", (token_id,))
        ).fetchone()
        boundary = max(success, reset[0] if reset else 0)
        count = (
            await (
                await db.execute(
                    "SELECT COUNT(*) FROM vids_fault_events WHERE token_id=? AND success=0 AND event_at>?",
                    (token_id, boundary),
                )
            ).fetchone()
        )[0]
        await db.execute(
            "UPDATE vids_account_stats SET consecutive_error_count=? WHERE token_id=?",
            (count, token_id),
        )
        return count

    @staticmethod
    async def _expire(db, now, token_id=None):
        query = "SELECT token_id,until_at FROM vids_account_protection WHERE action='LIMIT' AND until_at<=?"
        values = [now]
        if token_id is not None:
            query += " AND token_id=?"
            values.append(token_id)
        rows = await (await db.execute(query, values)).fetchall()
        for expired_id, until in rows:
            await db.execute(
                "INSERT INTO vids_fault_resets(token_id,reset_at) VALUES(?,?) ON CONFLICT(token_id) DO UPDATE SET reset_at=MAX(reset_at,excluded.reset_at)",
                (expired_id, until),
            )
            await db.execute(
                "DELETE FROM vids_protection_candidates WHERE token_id=? AND action='LIMIT' AND until_at<=?",
                (expired_id, now),
            )
            await VidsOutcomeRepository._project_protection(db, expired_id)
            await VidsOutcomeRepository._recount_faults(db, expired_id)

    async def stats(self, token_id):
        return (await self.stats_many([token_id]))[token_id]

    async def stats_many(self, token_ids):
        """Read a console's provider counters with one connection, not one per row."""
        identifiers = sorted(set(token_ids))
        if not identifiers:
            return {}
        await self.ensure_schema()
        async with self._database._connect(write=True) as db:
            await self._expire(db, time.time())
            db.row_factory = aiosqlite.Row
            rows = {}
            for offset in range(0, len(identifiers), 900):
                chunk = identifiers[offset : offset + 900]
                found = await (
                    await db.execute(
                        "SELECT * FROM vids_account_stats WHERE token_id IN ("
                        + ",".join("?" for _ in chunk)
                        + ")",
                        chunk,
                    )
                ).fetchall()
                rows.update({row["token_id"]: dict(row) for row in found})
            await db.commit()
        today = self._database._current_stats_date()
        return {
            token_id: self._public_stats(token_id, rows.get(token_id), today)
            for token_id in identifiers
        }

    @staticmethod
    def _public_stats(token_id, row, today):
        result = (
            dict(row)
            if row
            else {
                "token_id": token_id,
                **dict.fromkeys(_COUNTERS, 0),
                "today_date": None,
                "last_success_at": None,
                "last_error_at": None,
            }
        )
        if result["today_date"] != today:
            result.update(today_success_count=0, today_error_count=0)
        timestamps = [
            value
            for value in (result["last_success_at"], result["last_error_at"])
            if value is not None
        ]
        result["last_used_at"] = (
            datetime.fromtimestamp(max(timestamps), timezone.utc).isoformat()
            if timestamps
            else None
        )
        return result

    async def public_states(self):
        await self.ensure_schema()
        async with self._database._connect(write=True) as db:
            await self._expire(db, time.time())
            rows = await (
                await db.execute(
                    "SELECT token_id,action,reason,until_at FROM vids_account_protection"
                )
            ).fetchall()
            await db.commit()
        return {
            token_id: {
                "action": action,
                "reason": reason,
                "status": {"LIMIT": "cooldown", "DEACTIVE": "abnormal", "DISABLE": "disabled"}[
                    action
                ],
                "until": datetime.fromtimestamp(until, timezone.utc).isoformat() if until else None,
            }
            for token_id, action, reason, until in rows
        }

    async def availability(self, token_id):
        state = (await self.public_states()).get(token_id)
        if state is None:
            return None
        if state["action"] == "LIMIT":
            return f"Vids 账号冷却至 {state['until']}：{state['reason']}"
        return (
            f"Vids 账号{'异常' if state['action'] == 'DEACTIVE' else '已停用'}：{state['reason']}"
        )

    async def clear(self, token_id):
        """Clear Vids protection without changing manual switches or Flow state."""
        await self.ensure_schema()
        async with self._database._connect(write=True) as db:
            await db.execute(
                "INSERT INTO vids_fault_resets(token_id,reset_at) VALUES(?,?) ON CONFLICT(token_id) DO UPDATE SET reset_at=MAX(reset_at,excluded.reset_at)",
                (token_id, time.time()),
            )
            await db.execute("DELETE FROM vids_account_protection WHERE token_id=?", (token_id,))
            await db.execute("DELETE FROM vids_protection_candidates WHERE token_id=?", (token_id,))
            await db.execute(
                "UPDATE vids_account_stats SET consecutive_error_count=0 WHERE token_id=?",
                (token_id,),
            )
            await db.commit()
