"""Aggregate Weekly Check-in data and send Slack digests."""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

import frappe

from . import notifications

logger = notifications.get_logger("pulsecheck.digests")
_CACHE_KEY = "pulsecheck_weekly_digest_last_run"


def enqueue_weekly_digest(now: Optional[datetime] = None) -> bool:
    """Scheduler entry point for sending weekly digests."""

    return send_weekly_digest(now=now)


def send_weekly_digest(now: Optional[datetime] = None) -> bool:
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

    recipients = notifications.get_slack_recipients()
    if not recipients:
        logger.info("No Slack recipients were found; nothing to send.")
        return False

    week_start, week_end = notifications.get_week_bounds(now, offset_weeks=-1)
    checkins = _fetch_weekly_checkins(week_start, week_end)

    digest_message = _compose_digest_message(checkins, week_start, week_end)
    if not digest_message:
        return False

    messages_sent = 0
    for recipient in recipients:
        try:
            notifications.post_to_slack(
                token,
                {
                    "channel": recipient.get("slack_user_id"),
                    "text": digest_message,
                },
            )
        except notifications.SlackDeliveryError as exc:
            logger.exception("Failed to send digest to %s: %s", recipient.get("slack_user_id"), exc)
            continue

        messages_sent += 1

    if messages_sent:
        notifications.mark_executed(_CACHE_KEY, now=now)

    return bool(messages_sent)


def _fetch_weekly_checkins(week_start, week_end) -> List[dict]:
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


def _compose_digest_message(checkins: List[dict], week_start, week_end) -> str:
    if not checkins:
        logger.info(
            "No Weekly Check-in submissions found for the %s – %s window; skipping digest.",
            week_start,
            week_end,
        )
        return ""

    header = f"*Pulse Check digest* for {week_start:%b %d} – {week_end:%b %d}"
    lines = [header, ""]

    for checkin in checkins:
        employee_name = checkin.get("employee_name") or checkin.get("employee") or "Unknown employee"
        goal = checkin.get("goal") or "No goal linked"
        progress = checkin.get("progress_reported")
        progress_fragment = f"{int(progress)}% complete" if progress not in (None, "") else "Progress not reported"
        confidence = checkin.get("confidence")
        if confidence:
            progress_fragment = f"{progress_fragment} · {confidence}"

        lines.append(f"• *{employee_name}* – {goal}: {progress_fragment}")

        blockers = (checkin.get("blockers") or "").strip()
        if blockers:
            lines.append(f"  Blockers: {blockers}")

    return "\n".join(lines)
