"""One persisted Profile maintenance policy; account identities stay independent."""

from datetime import datetime, timedelta, timezone

DEFAULT_PROFILE_MAINTENANCE = {"enabled": True, "refresh_interval_minutes": 30, "revision": 0}


def profile_policy(database):
    value = getattr(database, "_profile_maintenance_policy", None)
    return value if isinstance(value, dict) else dict(DEFAULT_PROFILE_MAINTENANCE)


def utc(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def cycle_anchor(token, policy):
    """Recognize modern browser cycles, including their pre-global interval."""
    anchor = utc(token.last_st_refresh_at)
    due = utc(token.browser_profile_next_at)
    if (
        token.browser_profile_source == "cloak"
        and token.last_st_refresh_result == "success"
        and token.at is None
        and token.at_expires is None
        and anchor
        and (
            due is None
            and (not policy["enabled"] or not token.auto_refresh_enabled)
            or due == anchor + timedelta(minutes=policy["refresh_interval_minutes"])
            or due == anchor + timedelta(minutes=token.refresh_interval_minutes)
        )
    ):
        return anchor
    return None


def schedule(token, policy, now=None, *, anchor_policy=None):
    """Recalculate from this account's own successful cycle, never all from save time."""
    now = now or datetime.now(timezone.utc)
    if not policy["enabled"] or not token.auto_refresh_enabled:
        return {"next_refresh_at": None, "browser_profile_next_at": None}
    if token.refresh_status in {"config_error", "login_required", "retrying"}:
        return {}
    if token.browser_profile_status != "ready":
        return {}
    anchor = (
        cycle_anchor(token, anchor_policy or policy)
        or utc(token.browser_profile_verified_at)
        or (utc(token.last_refresh_at) if token.refresh_status == "ready" else None)
    )
    due = (
        min(anchor, now) + timedelta(minutes=policy["refresh_interval_minutes"]) if anchor else now
    )
    return {"next_refresh_at": max(now, due), "browser_profile_next_at": due}
