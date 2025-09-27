"""Compose and send weekly prompt messages to Slack users."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from . import notifications

logger = notifications.get_logger("pulsecheck.prompts")
_CACHE_KEY = "pulsecheck_weekly_prompts_last_run"


def enqueue_weekly_prompts(now: Optional[datetime] = None) -> bool:
    """Entry point for the scheduler. Returns True if any prompts were sent."""

    return send_weekly_prompts(now=now)


def send_weekly_prompts(now: Optional[datetime] = None) -> bool:
    """Send weekly reminders to all configured Slack recipients."""

    settings = notifications.get_settings()
    if not settings:
        logger.warning("Skipping weekly prompts because PulseCheck Settings are unavailable.")
        return False

    if not notifications.notifications_enabled(settings):
        logger.info("Weekly prompts are disabled in PulseCheck Settings; skipping run.")
        return False

    if not notifications.should_run_now(settings, now=now):
        return False

    if notifications.already_executed(_CACHE_KEY, now=now):
        return False

    token = notifications.get_slack_token(settings)
    if not token:
        logger.warning("Slack bot token is missing; prompts cannot be delivered.")
        return False

    recipients = notifications.get_slack_recipients()
    if not recipients:
        logger.info("No Slack recipients were found; nothing to send.")
        return False

    week_start, week_end = notifications.get_week_bounds(now)
    messages_sent = 0

    for recipient in recipients:
        text = _compose_prompt(recipient.get("employee_name") or recipient.get("name"), week_start, week_end)
        try:
            notifications.post_to_slack(
                token,
                {
                    "channel": recipient.get("slack_user_id"),
                    "text": text,
                },
            )
        except notifications.SlackDeliveryError as exc:
            logger.exception("Failed to send prompt to %s: %s", recipient.get("slack_user_id"), exc)
            continue

        messages_sent += 1

    if messages_sent:
        notifications.mark_executed(_CACHE_KEY, now=now)

    return bool(messages_sent)


def _compose_prompt(employee_name: Optional[str], week_start, week_end) -> str:
    friendly_name = employee_name or "there"
    return (
        f"Hi {friendly_name}! It's time for your weekly pulse check.\n"
        f"Please submit your update for {week_start:%b %d} – {week_end:%b %d}."
    )
