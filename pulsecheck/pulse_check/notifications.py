"""Common helpers for Slack notifications sent by Pulse Check jobs."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta, timezone

import frappe

try:  # pragma: no cover - urllib is always available, but the import is guarded for clarity
    from urllib import error as urllib_error
    from urllib import request as urllib_request
    from urllib import parse as urllib_parse
except ImportError:  # pragma: no cover
    urllib_request = None
    urllib_error = None
    urllib_parse = None

__all__ = [
    "SlackDeliveryError",
    "already_executed",
    "extract_schedule",
    "get_last_execution",
    "get_settings_timestamp",
    "record_settings_timestamp",
    "log_event",
    "get_logger",
    "get_employee_directory",
    "get_settings",
    "get_slack_recipients",
    "get_slack_token",
    "get_week_bounds",
    "get_completed_week_bounds",
    "get_employee_goals",
    "mark_executed",
    "notifications_enabled",
    "open_slack_modal",
    "post_to_slack",
    "update_slack_view",
    "should_run_now",
    "resolve_slack_user_id",
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

_EMPLOYEE_IDENTIFIER_FIELDS = ("user_id", "company_email", "personal_email")


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


def log_event(event: str, **details) -> None:
    """Emit structured debug information via ``frappe.log_error`` for diagnostics."""

    payload = {"event": event, **details}

    try:
        message = json.dumps(payload, default=str, sort_keys=True)
    except TypeError:
        message = repr(payload)

    try:
        frappe.log_error(message=message, title=f"PulseCheck {event}")  # type: ignore[attr-defined]
    except Exception:
        logger.info("PulseCheck %s | %s", event, message)


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

    if now.tzinfo is not None and now.tzinfo.utcoffset(now) is not None:

        window_start = window_start.replace(tzinfo=now.tzinfo)
    window_end = window_start + timedelta(minutes=window_minutes)

    return window_start <= now < window_end


def get_slack_token(settings) -> str | None:
    if not settings:
        return None

    def _clean(value) -> str | None:
        if isinstance(value, str):
            value = value.strip()
        elif value is not None:
            value = str(value).strip()
        return value or None

    get_password = getattr(settings, "get_password", None)
    if callable(get_password):
        try:
            token = _clean(get_password("slack_bot_token"))
        except Exception:
            token = None
        else:
            if token:
                return token

    return _clean(getattr(settings, "slack_bot_token", None))


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


def get_slack_recipients() -> list[dict]:
    """Return active employees that can be contacted on Slack by user identifier.

    The implementation assumes Slack member identifiers match the employee's
    linked Frappe user or their email address. This keeps provisioning simple for
    teams that enforce matching emails across systems.
    """

    directory = get_employee_directory(
        extra_fields=list(_EMPLOYEE_IDENTIFIER_FIELDS),
        require_slack=True,
    )

    recipients = []
    for employee in directory:
        slack_id = _resolve_employee_slack_identifier(employee)
        if not slack_id:
            continue

        recipients.append(
            {
                "name": employee.get("name"),
                "employee_name": employee.get("employee_name") or employee.get("name"),
                "slack_user_id": slack_id,
                "user_id": employee.get("user_id"),
                "company_email": employee.get("company_email"),
                "personal_email": employee.get("personal_email"),
            }
        )

    return recipients


def get_employee_directory(
    *, extra_fields: list[str] | None = None, require_slack: bool = False
) -> list[dict]:
    """Return active employees with optional extra fields.

    When ``require_slack`` is True the result only includes employees that have a
    linked User ID, which we treat as the Slack recipient identifier.
    """

    try:
        meta = frappe.get_meta("Employee")  # type: ignore[attr-defined]
        available_fields = {df.fieldname for df in getattr(meta, "fields", [])}
    except Exception:
        available_fields = set()

    fields: set[str] = {"name", "employee_name", "user_id"}
    fallback_fields = [field for field in _EMPLOYEE_IDENTIFIER_FIELDS if field in available_fields]
    fields.update(fallback_fields)

    if extra_fields:
        for field in extra_fields:
            if field in available_fields:
                fields.add(field)
            else:
                log_event("Directory", step="missing_field", field=field)

    filters: dict[str, object] = {"status": "Active"}
    if require_slack:
        filters["user_id"] = ["!=", ""]

    try:
        employees = frappe.get_all(  # type: ignore[call-arg]
            "Employee",
            filters=filters,
            fields=sorted(fields),
            order_by="employee_name asc",
            ignore_permissions=True,
        )
    except Exception as exc:
        logger.warning("Unable to load Employee directory for Slack notifications: %s", exc)
        log_event(
            "Directory",
            step="get_all_failed",
            requested_fields=sorted(fields),
            filters=filters,
            error=str(exc),
        )
        return []

    cleaned: list[dict] = []
    for employee in employees:
        entry = {
            key: (value.strip() if isinstance(value, str) else value)
            for key, value in dict(employee).items()
        }
        if not require_slack or entry.get("user_id"):
            cleaned.append(entry)

    log_event(
        "Directory",
        step="loaded",
        requested_fields=sorted(fields),
        total=len(employees),
        included=len(cleaned),
        require_slack=require_slack,
    )

    return cleaned


def _resolve_employee_slack_identifier(employee: dict) -> str | None:
    """Return the stored identifier that might map to a Slack user."""

    for field in _EMPLOYEE_IDENTIFIER_FIELDS:
        value = _clean_identifier(employee.get(field))
        if value:
            return value

    return None


def resolve_slack_user_id(token: str, recipient: dict) -> str | None:
    """Ensure the recipient has a Slack member ID, performing lookup by email when needed."""

    candidates = [
        _clean_identifier(recipient.get("slack_user_id")),
        _clean_identifier(recipient.get("user_id")),
        _clean_identifier(recipient.get("company_email")),
        _clean_identifier(recipient.get("personal_email")),
    ]

    for candidate in candidates:
        if not candidate:
            continue
        if candidate.startswith("U"):
            return candidate

    for candidate in candidates:
        if not candidate or "@" not in candidate:
            continue
        try:
            user_id = _lookup_slack_user_by_email(token, candidate)
        except SlackDeliveryError as exc:
            log_event(
                "Slack Lookup",
                step="lookup_failed",
                email=candidate,
                error=str(exc),
            )
            continue
        if user_id:
            return user_id

    return None


def _clean_identifier(value) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        cleaned = value.strip()
    else:
        cleaned = str(value).strip()
    return cleaned or None


def get_week_bounds(now: datetime | None = None, *, offset_weeks: int = 0) -> tuple[date, date]:
    """Return the start and end dates (Monday-Sunday) for a week offset from the current one."""

    if now is None:
        now = _now()

    current_date = now.date()
    start_of_week = current_date - timedelta(days=current_date.weekday()) + timedelta(weeks=offset_weeks)
    end_of_week = start_of_week + timedelta(days=6)
    return start_of_week, end_of_week


def get_completed_week_bounds(now: datetime | None = None) -> tuple[date, date]:
    """Return the Sunday-Saturday window for the week that just completed."""

    if now is None:
        now = _now()

    reference = (now - timedelta(days=1)).date()
    days_since_sunday = (reference.weekday() + 1) % 7
    start_of_week = reference - timedelta(days=days_since_sunday)
    end_of_week = start_of_week + timedelta(days=6)
    return start_of_week, end_of_week


def post_to_slack(token: str, payload: dict) -> None:
    """Send a message payload to Slack's chat.postMessage API."""

    _call_slack_api(token, "chat.postMessage", payload)


def open_slack_modal(token: str, trigger_id: str, view: dict) -> None:
    """Open a Slack modal for an interactive command or shortcut."""

    if not trigger_id:
        raise SlackDeliveryError("Slack trigger_id is required to open a modal.")

    _call_slack_api(
        token,
        "views.open",
        {
            "trigger_id": trigger_id,
            "view": view,
        },
    )


def update_slack_view(token: str, view_id: str, view: dict) -> None:
    """Update an existing Slack modal view."""

    if not view_id:
        raise SlackDeliveryError("Slack view_id is required to update a modal.")

    _call_slack_api(
        token,
        "views.update",
        {
            "view_id": view_id,
            "view": view,
        },
    )


def _parse_time(value: str) -> time | None:
    """Parse a time string in HH:MM or HH:MM:SS format."""

    for pattern in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(value, pattern).time()
        except ValueError:
            continue
    return None


def _call_slack_api(token: str, method: str, payload: dict) -> None:
    """Invoke a Slack Web API method with shared error handling."""

    if urllib_request is None or urllib_error is None:  # pragma: no cover
        raise SlackDeliveryError("The urllib library is not available in this environment.")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }

    request = urllib_request.Request(
        f"https://slack.com/api/{method}",
        data=json.dumps(payload).encode("utf-8"),
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


def _lookup_slack_user_by_email(token: str, email: str) -> str | None:
    """Return the Slack user ID for the provided email, if available."""

    if urllib_request is None or urllib_error is None or urllib_parse is None:  # pragma: no cover
        raise SlackDeliveryError("The urllib library is not available in this environment.")

    query = urllib_parse.urlencode({"email": email})
    request = urllib_request.Request(
        f"https://slack.com/api/users.lookupByEmail?{query}",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )

    try:
        with urllib_request.urlopen(request, timeout=10) as response:
            raw_body = response.read()
    except urllib_error.URLError as exc:  # pragma: no cover
        raise SlackDeliveryError(str(exc)) from exc

    try:
        data = json.loads(raw_body.decode("utf-8") if raw_body else "{}")
    except json.JSONDecodeError:
        data = {"ok": False, "error": raw_body.decode("utf-8", errors="ignore")}

    if not data.get("ok"):
        if data.get("error") == "users_not_found":
            return None
        raise SlackDeliveryError(data.get("error") or "Unknown Slack API error")

    return (data.get("user") or {}).get("id")


def resolve_slack_user_id(token: str, recipient: dict) -> str | None:
    """Return a Slack member ID for ``recipient`` using stored identifiers or Slack lookups."""

    candidates = [
        _clean_identifier(recipient.get("slack_user_id")),
        _clean_identifier(recipient.get("user_id")),
        _clean_identifier(recipient.get("company_email")),
        _clean_identifier(recipient.get("personal_email")),
    ]

    for candidate in candidates:
        if candidate and candidate.startswith("U"):
            return candidate

    for candidate in candidates:
        if not candidate or "@" not in candidate:
            continue
        try:
            user_id = _lookup_slack_user_by_email(token, candidate)
        except SlackDeliveryError as exc:
            log_event(
                "Slack Lookup",
                step="lookup_failed",
                email=candidate,
                error=str(exc),
            )
            continue
        if user_id:
            return user_id

    return None


def get_employee_goals(employee_name: str, *, limit: int = 5) -> list[dict]:
    """Fetch the most recent active goals for an employee."""

    try:
        goals = frappe.get_all(  # type: ignore[call-arg]
            "Goal",
            filters={
                "employee": employee_name,
                "is_group": 0,
                "status": ["!=", "Archived"],
            },
            fields=["name", "goal_name", "status", "progress"],
            order_by="modified desc",
            limit_page_length=limit,
            ignore_permissions=True,
        )
    except Exception as exc:  # pragma: no cover - logged for diagnostics
        log_event(
            "Goals",
            step="fetch_failed",
            employee=employee_name,
            error=str(exc),
        )
        return []

    return list(goals)


def record_settings_timestamp(
    fieldname: str,
    *,
    now: datetime | None = None,
    settings=None,
) -> None:
    """Persist the provided timestamp on the PulseCheck Settings DocType.

    The field is expected to be a read-only Datetime field. Failures are logged
    but otherwise ignored so job execution is never blocked by metadata
    mismatch.
    """

    if now is None:
        now = _now()

    target_settings = settings or get_settings()
    if not target_settings or not hasattr(target_settings, "db_set"):
        return

    timestamp = _normalise_datetime(now)

    try:
        target_settings.db_set(fieldname, timestamp)
    except Exception:
        logger.warning("Unable to store %s on PulseCheck Settings.", fieldname, exc_info=True)


def _normalise_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def get_settings_timestamp(fieldname: str) -> datetime | None:
    """Fetch a stored timestamp from the PulseCheck Settings DocType."""

    settings = get_settings()
    if not settings:
        return None

    return _coerce_datetime(getattr(settings, fieldname, None))


def _coerce_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def get_last_execution(cache_key: str) -> datetime | None:
    """Return the datetime of the last execution recorded for ``cache_key``."""

    cache = _get_cache()
    value = cache.get_value(cache_key)
    if not value:
        return None

    try:
        return datetime.fromisoformat(value)
    except ValueError:  # pragma: no cover - invalid cache entries are ignored
        return None
