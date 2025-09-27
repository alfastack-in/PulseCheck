"""Aggregate Weekly Check-in data and send Slack digests."""

from __future__ import annotations

from datetime import datetime

import frappe

from . import notifications

logger = notifications.get_logger("pulsecheck.digests")
_CACHE_KEY = "pulsecheck_weekly_digest_last_run"


def enqueue_weekly_digest(now: datetime | None = None) -> bool:
    """Scheduler entry point for sending weekly digests."""

    return send_weekly_digest(now=now)


def send_weekly_digest(now: datetime | None = None) -> bool:
    """Send a Slack digest summarising submitted Weekly Check-ins."""

    settings = notifications.get_settings()
    if not settings:
        logger.warning("Skipping weekly digest because PulseCheck Settings are unavailable.")
        return False

    if not notifications.notifications_enabled(settings):
        logger.info("Weekly digests are disabled in PulseCheck Settings; skipping run.")
        return False

    if not notifications.should_run_now(settings, now=now):
        return False

    if notifications.already_executed(_CACHE_KEY, now=now):
        return False

    token = notifications.get_slack_token(settings)
    if not token:
        logger.warning("Slack bot token is missing; digest cannot be delivered.")
        return False

    week_start, week_end = notifications.get_week_bounds(now, offset_weeks=-1)
    checkins = _fetch_weekly_checkins(week_start, week_end)

    if not checkins:
        logger.info(
            "No Weekly Check-in submissions found for the %s - %s window; skipping digest.",
            week_start,
            week_end,
        )
        return False

    employees_by_id, employees_by_display = _build_employee_directory()
    if not employees_by_id:
        logger.info("No employees with manager relationships were found; skipping digest.")
        return False

    manager_map, unassigned = _group_checkins_by_manager(
        checkins, employees_by_id, employees_by_display
    )
    if not manager_map:
        logger.info("No managers with Slack identifiers were found for digest delivery.")
        return False

    messages_sent = 0
    for manager_name in sorted(
        manager_map,
        key=lambda name: (employees_by_id.get(name, {}).get("employee_name") or name or "").lower(),
    ):
        manager = employees_by_id.get(manager_name)
        if not manager:
            continue
        slack_id = (manager.get("slack_user_id") or "").strip()
        if not slack_id:
            continue

        digest_message = _compose_manager_digest(
            manager,
            manager_map[manager_name],
            week_start,
            week_end,
        )
        if not digest_message:
            continue

        try:
            notifications.post_to_slack(
                token,
                {
                    "channel": slack_id,
                    "text": digest_message,
                },
            )
        except notifications.SlackDeliveryError as exc:
            logger.exception("Failed to send digest to %s: %s", slack_id, exc)
            continue

        messages_sent += 1

    if messages_sent:
        notifications.mark_executed(_CACHE_KEY, now=now)

    if unassigned:
        logger.info(
            "Skipped %s Weekly Check-in entries without a manager Slack recipient.",
            len(unassigned),
        )

    return bool(messages_sent)


def _fetch_weekly_checkins(week_start, week_end) -> list[dict]:
    """Pull approved Weekly Check-in records for the provided date range."""

    if not _weekly_checkin_table_exists():
        return []

    try:
        return frappe.get_all(  # type: ignore[call-arg]
            "Weekly Checkin",
            filters={
                "docstatus": 1,
                "posting_date": ["between", [str(week_start), str(week_end)]],
            },
            fields=[
                "name",
                "employee",
                "employee_name",
                "goal",
                "progress_reported",
                "confidence",
                "blockers",
            ],
            order_by="employee_name asc",
        )
    except Exception:  # pragma: no cover - frappe raises detailed errors in production
        logger.warning("Unable to load Weekly Check-in documents for digest generation.")
        return []


def _weekly_checkin_table_exists() -> bool:
    db = getattr(frappe, "db", None)
    table_exists = getattr(db, "table_exists", None)
    if callable(table_exists):
        try:
            return bool(table_exists("tabWeekly Checkin"))
        except Exception:  # pragma: no cover - rely on fallback
            return False
    return False


def _build_employee_directory() -> tuple[dict[str, dict], dict[str, dict]]:
    """Return employee directory data structures for lookup."""

    employees = notifications.get_employee_directory(
        extra_fields=list(_MANAGER_FIELDS)
    )

    if not employees:
        return {}, {}

    by_id: dict[str, dict] = {}
    by_display: dict[str, dict] = {}
    for employee in employees:
        name = employee.get("name")
        if not name:
            continue
        by_id[name] = employee
        display_key = _normalize(employee.get("employee_name") or name)
        if display_key and display_key not in by_display:
            by_display[display_key] = employee

    return by_id, by_display


def _group_checkins_by_manager(
    checkins: list[dict],
    employees_by_id: dict[str, dict],
    employees_by_display: dict[str, dict],
) -> tuple[dict[str, list[tuple[dict, dict]]], list[tuple[dict | None, dict]]]:
    """Group Weekly Check-ins by the Slack-enabled managers responsible for them."""

    manager_map: dict[str, list[tuple[dict, dict]]] = {}
    unassigned: list[tuple[dict | None, dict]] = []

    for checkin in checkins:
        employee = _resolve_employee(checkin, employees_by_id, employees_by_display)
        if not employee:
            unassigned.append((None, checkin))
            continue

        managers = _resolve_managers(employee, employees_by_id, employees_by_display)
        delivered = False

        for manager in managers:
            slack_id = (manager.get("slack_user_id") or "").strip()
            manager_name = manager.get("name")
            if not slack_id or not manager_name:
                continue
            manager_map.setdefault(manager_name, []).append((employee, checkin))
            delivered = True

        if not delivered:
            unassigned.append((employee, checkin))

    return manager_map, unassigned


def _resolve_employee(
    checkin: dict,
    employees_by_id: dict[str, dict],
    employees_by_display: dict[str, dict],
) -> dict | None:
    employee_id = (checkin.get("employee") or "").strip()
    if employee_id:
        employee = employees_by_id.get(employee_id)
        if employee:
            return employee

    display_key = _normalize(checkin.get("employee_name"))
    if display_key:
        return employees_by_display.get(display_key)

    return None


_MANAGER_FIELDS = ("reports_to", "leave_approver")


def _resolve_managers(
    employee: dict,
    employees_by_id: dict[str, dict],
    employees_by_display: dict[str, dict],
) -> list[dict]:
    manager_entries: list[dict] = []
    seen: set[str] = set()

    for field in _MANAGER_FIELDS:
        reference = employee.get(field)
        if isinstance(reference, str):
            reference = reference.strip()
        if not reference:
            continue

        manager = employees_by_id.get(reference)
        if not manager:
            manager = employees_by_display.get(_normalize(reference))
        if not manager:
            continue

        manager_name = manager.get("name")
        if not manager_name or manager_name == employee.get("name"):
            continue

        if manager_name in seen:
            continue

        seen.add(manager_name)
        manager_entries.append(manager)

    return manager_entries


def _compose_manager_digest(
    manager: dict,
    entries: list[tuple[dict, dict]],
    week_start,
    week_end,
) -> str:
    if not entries:
        return ""

    manager_display = manager.get("employee_name") or manager.get("name") or "your team"
    header = f"*Pulse Check digest* for {week_start:%b %d} - {week_end:%b %d}"
    intro = f"Updates for your team, {manager_display}:"

    lines = [header, intro, ""]

    for employee, checkin in sorted(
        entries,
        key=lambda pair: _normalize(
            pair[0].get("employee_name") or pair[0].get("name") or pair[1].get("employee_name")
        ),
    ):
        employee_display = (
            checkin.get("employee_name")
            or employee.get("employee_name")
            or employee.get("name")
            or "Unknown employee"
        )
        goal = checkin.get("goal") or "No goal linked"
        progress = checkin.get("progress_reported")
        progress_fragment = (
            f"{int(progress)}% complete" if progress not in (None, "") else "Progress not reported"
        )
        confidence = checkin.get("confidence")
        if confidence:
            progress_fragment = f"{progress_fragment} · {confidence}"

        lines.append(f"• *{employee_display}* - {goal}: {progress_fragment}")

        blockers = (checkin.get("blockers") or "").strip()
        if blockers:
            lines.append(f"  ⚠️ *Blockers:* {blockers}")

    return "\n".join(lines)


def _normalize(value: str | None) -> str:
    return (value or "").strip().lower()
