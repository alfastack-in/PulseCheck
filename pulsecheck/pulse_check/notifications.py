"""Common helpers for Slack notifications sent by Pulse Check jobs."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta

import frappe

try:  # pragma: no cover - urllib is always available, but the import is guarded for clarity
    from urllib import error as urllib_error
    from urllib import request as urllib_request
except ImportError:  # pragma: no cover
    urllib_request = None
    urllib_error = None

__all__ = [
    "SlackDeliveryError",
    "already_executed",
    "extract_schedule",
    "get_logger",
    "get_settings",
    "get_slack_recipients",
    "get_slack_token",
    "get_week_bounds",
    "mark_executed",
    "notifications_enabled",
    "post_to_slack",
    "should_run_now",
]

_WEEKDAY_TO_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


class _FallbackCache:
    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def get_value(self, key: str) -> str | None:
        return self._store.get(key)

    def set_value(self, key: str, value: str, expires_in_sec: int | None = None) -> None:
        self._store[key] = value


_FALLBACK_CACHE = _FallbackCache()


class SlackDeliveryError(RuntimeError):
    """Raised when a Slack API call fails."""


def get_logger(name: str) -> logging.Logger:
    """Return a frappe-backed logger if available, otherwise a stdlib logger."""

    logger_getter = getattr(frappe, "logger", None)
    if callable(logger_getter):
        try:
            return logger_getter(name)
        except Exception:  # pragma: no cover - fall back to stdlib logging
            pass

    return logging.getLogger(name)


logger = get_logger("pulsecheck.notifications")


def _now() -> datetime:
    utils = getattr(frappe, "utils", None)
    if utils:
        now_datetime = getattr(utils, "now_datetime", None)
        if callable(now_datetime):
            try:
                return now_datetime()
            except Exception:  # pragma: no cover - fall back when frappe has issues
                pass

    return datetime.utcnow()


def get_settings():
    """Return the PulseCheck Settings document, handling missing doctypes gracefully."""

    try:
        return frappe.get_single("PulseCheck Settings")
    except Exception:  # pragma: no cover - frappe raises specific exceptions in production
        logger.warning("Unable to load PulseCheck Settings document.")
        return None


def notifications_enabled(settings) -> bool:
    """Check if weekly notifications are enabled inside the settings document."""

    return bool(getattr(settings, "enable_weekly_prompts", False))


def extract_schedule(settings) -> tuple[int, time] | None:
    """Extract the configured weekday and time from settings."""

    day = (getattr(settings, "notification_day", "") or "").strip().lower()
    time_value = (getattr(settings, "notification_time", "") or "").strip()

    if not day or not time_value:
        return None

    weekday = _WEEKDAY_TO_INDEX.get(day)
    if weekday is None:
        logger.warning("Unsupported notification day: %s", day)
        return None

    parsed_time = _parse_time(time_value)
    if not parsed_time:
        logger.warning("Unable to parse notification time: %s", time_value)
        return None

    return weekday, parsed_time


def should_run_now(settings, now: datetime | None = None, window_minutes: int = 30) -> bool:
    """Return True when the scheduler should execute the job right now."""

    schedule = extract_schedule(settings)
    if not schedule:
        return False

    if now is None:
        now = _now()

    weekday, target_time = schedule
    if now.weekday() != weekday:
        return False

    window_start = datetime.combine(now.date(), target_time)
    if now.tzinfo is not None and window_start.tzinfo is None:
        window_start = window_start.replace(tzinfo=now.tzinfo)
    window_end = window_start + timedelta(minutes=window_minutes)

    return window_start <= now < window_end


def get_slack_token(settings) -> str | None:
    token = getattr(settings, "slack_bot_token", None)
    if token:
        token = token.strip()
    return token or None


def _get_cache():
    cache_getter = getattr(frappe, "cache", None)
    if callable(cache_getter):
        try:
            return cache_getter()
        except Exception:  # pragma: no cover - fall back when frappe cache fails
            pass

    return _FALLBACK_CACHE


def already_executed(cache_key: str, now: datetime | None = None) -> bool:
    """Check whether a job already ran today."""

    if now is None:
        now = _now()

    cache = _get_cache()
    last_run = cache.get_value(cache_key)
    if not last_run:
        return False

    try:
        last_run_dt = datetime.fromisoformat(last_run)
    except ValueError:  # pragma: no cover - corrupt cache entries are ignored
        return False

    return last_run_dt.date() == now.date()


def mark_executed(cache_key: str, now: datetime | None = None) -> None:
    if now is None:
        now = _now()

    cache = _get_cache()
    cache.set_value(cache_key, now.isoformat())


def _employee_table_exists() -> bool:
    db = getattr(frappe, "db", None)
    table_exists = getattr(db, "table_exists", None)
    if callable(table_exists):
        try:
            return bool(table_exists("tabEmployee"))
        except Exception:  # pragma: no cover - rely on fallback
            return False
    return False


def _employee_has_slack_field() -> bool:
    db = getattr(frappe, "db", None)
    has_column = getattr(db, "has_column", None)
    if callable(has_column):
        try:
            return bool(has_column("Employee", "slack_user_id"))
        except Exception:  # pragma: no cover - rely on fallback
            return False
    return False


def get_slack_recipients() -> list[dict]:
    """Return active employees that have a Slack user identifier configured."""

    if not _employee_table_exists() or not _employee_has_slack_field():
        return []

    try:
        employees = frappe.get_all(  # type: ignore[call-arg]
            "Employee",
            filters={"status": "Active", "slack_user_id": ["!=", ""]},
            fields=["name", "employee_name", "slack_user_id"],
            order_by="employee_name asc",
        )
    except Exception:  # pragma: no cover - frappe provides richer errors
        logger.warning("Unable to load Slack recipients from Employee records.")
        return []

    recipients = []
    for employee in employees:
        slack_id = (employee.get("slack_user_id") or "").strip()
        if not slack_id:
            continue
        recipients.append(
            {
                "name": employee.get("name"),
                "employee_name": employee.get("employee_name") or employee.get("name"),
                "slack_user_id": slack_id,
            }
        )

    return recipients


def get_week_bounds(now: datetime | None = None, *, offset_weeks: int = 0) -> tuple[date, date]:
    """Return the start and end dates (Monday-Sunday) for a week offset from the current one."""

    if now is None:
        now = _now()

    current_date = now.date()
    start_of_week = current_date - timedelta(days=current_date.weekday()) + timedelta(weeks=offset_weeks)
    end_of_week = start_of_week + timedelta(days=6)
    return start_of_week, end_of_week


def post_to_slack(token: str, payload: dict) -> None:
    """Send a message payload to Slack's chat.postMessage API."""

    if urllib_request is None or urllib_error is None:  # pragma: no cover
        raise SlackDeliveryError("The urllib library is not available in this environment.")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }

    data = json.dumps(payload).encode("utf-8")
    request = urllib_request.Request(
        "https://slack.com/api/chat.postMessage",
        data=data,
        headers=headers,
        method="POST",
    )

    try:
        with urllib_request.urlopen(request, timeout=10) as response:
            raw_body = response.read()
    except urllib_error.URLError as exc:  # pragma: no cover - difficult to emulate in tests
        raise SlackDeliveryError(str(exc)) from exc

    try:
        data = json.loads(raw_body.decode("utf-8") if raw_body else "{}")
    except json.JSONDecodeError:
        data = {"ok": False, "error": raw_body.decode("utf-8", errors="ignore")}

    if not data.get("ok"):
        raise SlackDeliveryError(data.get("error") or "Unknown Slack API error")


def _parse_time(value: str) -> time | None:
    """Parse a time string in HH:MM or HH:MM:SS format."""

    for pattern in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(value, pattern).time()
        except ValueError:
            continue
    return None
