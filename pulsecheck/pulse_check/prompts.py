"""Compose and send weekly prompt messages to Slack users."""

from __future__ import annotations

from datetime import datetime

from . import notifications

logger = notifications.get_logger("pulsecheck.prompts")
_CACHE_KEY = "pulsecheck_weekly_prompts_last_run"


def enqueue_weekly_prompts(now: datetime | None = None) -> bool:
    """Entry point for the scheduler. Returns True if any prompts were sent."""

    return send_weekly_prompts(now=now)


def send_weekly_prompts(now: datetime | None = None, *, force: bool = False) -> bool:
    """Send weekly reminders to all configured Slack recipients."""

    settings = notifications.get_settings()
    if not settings:
        notifications.log_event(
            "Weekly Prompts",
            step="missing_settings",
            force=force,
            now=now.isoformat() if isinstance(now, datetime) else None,
        )
        logger.warning("Skipping weekly prompts because PulseCheck Settings are unavailable.")
        return False

    if not force and not notifications.notifications_enabled(settings):
        notifications.log_event(
            "Weekly Prompts",
            step="disabled",
            enable_weekly_prompts=getattr(settings, "enable_weekly_prompts", None),
        )
        logger.info("Weekly prompts are disabled in PulseCheck Settings; skipping run.")
        return False

    if not force and not notifications.should_run_now(settings, now=now):
        notifications.log_event(
            "Weekly Prompts",
            step="outside_window",
            now=now.isoformat() if isinstance(now, datetime) else None,
        )
        return False

    if not force and notifications.already_executed(_CACHE_KEY, now=now):
        notifications.log_event(
            "Weekly Prompts",
            step="already_executed",
            cache_key=_CACHE_KEY,
            now=now.isoformat() if isinstance(now, datetime) else None,
        )
        return False

    token = notifications.get_slack_token(settings)
    if not token:
        notifications.log_event("Weekly Prompts", step="missing_token")
        logger.warning("Slack bot token is missing; prompts cannot be delivered.")
        return False

    recipients = notifications.get_slack_recipients()
    if not recipients:
        notifications.log_event("Weekly Prompts", step="no_recipients")
        logger.info("No Slack recipients were found; nothing to send.")
        return False

    week_start, week_end = notifications.get_week_bounds(now, offset_weeks=-1)
    notifications.log_event(
        "Weekly Prompts",
        step="sending",
        recipient_count=len(recipients),
        week_start=str(week_start),
        week_end=str(week_end),
    )
    messages_sent = 0

    for recipient in recipients:
        channel = notifications.resolve_slack_user_id(token, recipient)
        if not channel:
            notifications.log_event(
                "Weekly Prompts",
                step="lookup_failed",
                recipient=recipient.get("name"),
            )
            continue

        recipient["slack_user_id"] = channel

        notifications.log_event(
            "Weekly Prompts",
            step="sending_recipient",
            recipient=recipient.get("name"),
            channel=channel,
        )

        text = _compose_prompt(recipient.get("employee_name") or recipient.get("name"), week_start, week_end)
        try:
            notifications.post_to_slack(
                token,
                {
                    "channel": channel,
                    "text": text,
                },
            )
        except notifications.SlackDeliveryError as exc:
            logger.exception("Failed to send prompt to %s: %s", channel, exc)
            notifications.log_event(
                "Weekly Prompts",
                step="slack_error",
                recipient=recipient.get("name"),
                channel=channel,
                error=str(exc),
            )
            continue

        messages_sent += 1
        notifications.log_event(
            "Weekly Prompts",
            step="sent",
            recipient=recipient.get("name"),
            channel=channel,
        )

    if messages_sent:
        notifications.mark_executed(_CACHE_KEY, now=now)
        notifications.record_settings_timestamp(
            "last_prompt_run",
            now=now,
            settings=settings,
        )
        notifications.log_event(
            "Weekly Prompts",
            step="completed",
            messages_sent=messages_sent,
            cache_key=_CACHE_KEY,
        )
    else:
        notifications.log_event(
            "Weekly Prompts",
            step="nothing_sent",
        )

    return bool(messages_sent)


def _compose_prompt(employee_name: str | None, week_start, week_end) -> str:
    friendly_name = employee_name or "there"
    return (
        f"Hi {friendly_name}! It's time for your weekly pulse check.\n"
        f"Use `/pulsecheck` to submit your update for last week ({week_start:%b %d} - {week_end:%b %d})."
    )


def get_last_prompt_run() -> datetime | None:
    """Return the cached datetime of the last successful prompts run."""

    cached = notifications.get_last_execution(_CACHE_KEY)
    if cached:
        return cached
    return notifications.get_settings_timestamp("last_prompt_run")
