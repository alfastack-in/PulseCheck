"""Tests for Slack prompt and digest jobs."""
from __future__ import annotations

import logging
import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


class DummyCache:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get_value(self, key: str):
        return self.store.get(key)

    def set_value(self, key: str, value: str, expires_in_sec: int | None = None):
        self.store[key] = value

    def clear(self) -> None:
        self.store.clear()


_FAKE_SETTINGS: SimpleNamespace | None = None
_FAKE_EMPLOYEES: list[dict] = []
_FAKE_CHECKINS: list[dict] = []
_TABLES: set[str] = set()
_COLUMNS: set[tuple[str, str]] = set()
_CACHE = DummyCache()


fake_frappe = types.ModuleType("frappe")
fake_frappe.logger = lambda name: logging.getLogger(name)
fake_frappe.cache = lambda: _CACHE
fake_frappe.utils = SimpleNamespace(now_datetime=lambda: datetime(2024, 1, 1, 10, 0))


def _table_exists(table: str) -> bool:
    return table in _TABLES


def _has_column(doctype: str, column: str) -> bool:
    return (doctype, column) in _COLUMNS


fake_frappe.db = SimpleNamespace(table_exists=_table_exists, has_column=_has_column)


def _get_all(doctype: str, **_kwargs):
    if doctype == "Employee":
        return list(_FAKE_EMPLOYEES)
    if doctype == "Weekly Checkin":
        return list(_FAKE_CHECKINS)
    return []


def _get_single(_doctype: str):
    return _FAKE_SETTINGS


fake_frappe.get_all = _get_all
fake_frappe.get_single = _get_single

sys.modules.setdefault("frappe", fake_frappe)

from pulsecheck.pulse_check import digests, notifications, prompts


def _reset_state():
    global _FAKE_SETTINGS, _FAKE_EMPLOYEES, _FAKE_CHECKINS
    _FAKE_SETTINGS = None
    _FAKE_EMPLOYEES = []
    _FAKE_CHECKINS = []
    _TABLES.clear()
    _COLUMNS.clear()
    _CACHE.clear()


@pytest.fixture(autouse=True)
def _prepare_environment(monkeypatch):
    _reset_state()
    monkeypatch.setattr(fake_frappe.utils, "now_datetime", lambda: datetime(2024, 1, 1, 10, 0))
    yield
    _reset_state()


def _basic_settings(**overrides) -> SimpleNamespace:
    values = {
        "enable_weekly_prompts": 1,
        "notification_day": "Monday",
        "notification_time": "10:00:00",
        "slack_bot_token": "xoxb-test",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _enable_employee_directory():
    _TABLES.add("tabEmployee")
    _COLUMNS.add(("Employee", "slack_user_id"))


def _enable_checkins_table():
    _TABLES.add("tabWeekly Checkin")


def test_should_run_now_matches_schedule():
    settings = _basic_settings()
    now = datetime(2024, 1, 1, 10, 15)  # Monday
    assert notifications.should_run_now(settings, now)

    later = datetime(2024, 1, 1, 12, 0)
    assert not notifications.should_run_now(settings, later)

    aware_now = datetime(2024, 1, 1, 10, 15, tzinfo=timezone.utc)
    assert notifications.should_run_now(settings, aware_now)

    aware_later = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert not notifications.should_run_now(settings, aware_later)


def test_send_weekly_prompts_respects_window_with_timezone(monkeypatch):
    global _FAKE_SETTINGS
    _FAKE_SETTINGS = _basic_settings()
    _enable_employee_directory()
    _FAKE_EMPLOYEES.append({"employee_name": "Ada", "slack_user_id": "U01"})

    payloads: list[dict] = []

    def _capture(token, payload):
        payloads.append(payload)

    monkeypatch.setattr(notifications, "post_to_slack", _capture)

    outside_window = datetime(2024, 1, 1, 9, 0, tzinfo=timezone.utc)
    sent = prompts.send_weekly_prompts(now=outside_window)
    assert sent is False
    assert not payloads

    inside_window = datetime(2024, 1, 1, 10, 5, tzinfo=timezone.utc)
    sent = prompts.send_weekly_prompts(now=inside_window)
    assert sent is True
    assert payloads

    prior_week_start, prior_week_end = notifications.get_week_bounds(inside_window, offset_weeks=-1)
    expected_range = f"{prior_week_start:%b %d} - {prior_week_end:%b %d}"
    message = payloads[-1]["text"]
    assert f"last week ({expected_range})" in message


def test_should_run_now_handles_timezone_awareness():
    settings = _basic_settings()
    now = datetime(2024, 1, 1, 10, 15, tzinfo=timezone.utc)
    assert notifications.should_run_now(settings, now)

    early = datetime(2024, 1, 1, 9, 30, tzinfo=timezone.utc)
    assert not notifications.should_run_now(settings, early)

    late = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert not notifications.should_run_now(settings, late)


def test_send_weekly_prompts_skips_when_disabled(monkeypatch):
    global _FAKE_SETTINGS
    _FAKE_SETTINGS = _basic_settings(enable_weekly_prompts=0)
    _enable_employee_directory()
    _FAKE_EMPLOYEES.append({"employee_name": "Ada", "slack_user_id": "U01"})

    called = []
    monkeypatch.setattr(notifications, "post_to_slack", lambda *args, **kwargs: called.append((args, kwargs)))

    sent = prompts.send_weekly_prompts(now=datetime(2024, 1, 1, 10, 5))
    assert sent is False
    assert not called


def test_send_weekly_prompts_requires_token(monkeypatch):
    global _FAKE_SETTINGS
    _FAKE_SETTINGS = _basic_settings(slack_bot_token="   ")
    _enable_employee_directory()
    _FAKE_EMPLOYEES.append({"employee_name": "Ada", "slack_user_id": "U01"})

    called = []
    monkeypatch.setattr(notifications, "post_to_slack", lambda *args, **kwargs: called.append((args, kwargs)))

    sent = prompts.send_weekly_prompts(now=datetime(2024, 1, 1, 10, 5))
    assert sent is False
    assert not called


def test_send_weekly_prompts_marks_execution(monkeypatch):
    global _FAKE_SETTINGS
    _FAKE_SETTINGS = _basic_settings()
    _enable_employee_directory()
    _FAKE_EMPLOYEES.append({"employee_name": "Ada", "slack_user_id": "U01"})

    monkeypatch.setattr(notifications, "post_to_slack", lambda *args, **kwargs: None)

    first_run = prompts.send_weekly_prompts(now=datetime(2024, 1, 1, 10, 5, tzinfo=timezone.utc))
    second_run = prompts.send_weekly_prompts(now=datetime(2024, 1, 1, 10, 10, tzinfo=timezone.utc))

    assert first_run is True
    assert second_run is False


def test_send_weekly_prompts_skips_outside_window_with_timezone(monkeypatch):
    global _FAKE_SETTINGS
    _FAKE_SETTINGS = _basic_settings()
    _enable_employee_directory()
    _FAKE_EMPLOYEES.append({"employee_name": "Ada", "slack_user_id": "U01"})

    called = []
    monkeypatch.setattr(notifications, "post_to_slack", lambda *args, **kwargs: called.append((args, kwargs)))

    sent = prompts.send_weekly_prompts(now=datetime(2024, 1, 1, 9, 30, tzinfo=timezone.utc))
    assert sent is False
    assert not called


def test_send_weekly_digest_summarises_checkins(monkeypatch):
    global _FAKE_SETTINGS
    _FAKE_SETTINGS = _basic_settings()
    _enable_employee_directory()
    _enable_checkins_table()
    _FAKE_EMPLOYEES.append({"employee_name": "Ada", "slack_user_id": "U01"})
    _FAKE_CHECKINS.append(
        {
            "employee_name": "Ada Lovelace",
            "goal": "Grow pipeline",
            "progress_reported": 80,
            "confidence": "On Track",
            "blockers": "None",
        }
    )

    payloads: list[dict] = []

    def _capture(token, payload):
        payloads.append(payload)

    monkeypatch.setattr(notifications, "post_to_slack", _capture)

    sent = digests.send_weekly_digest(now=datetime(2024, 1, 8, 10, 5, tzinfo=timezone.utc))

    assert sent is True
    assert payloads, "Expected a digest payload to be sent"
    assert "Pulse Check digest" in payloads[0]["text"]
    assert "Ada Lovelace" in payloads[0]["text"]


def test_send_weekly_digest_respects_window_with_timezone(monkeypatch):
    global _FAKE_SETTINGS
    _FAKE_SETTINGS = _basic_settings()
    _enable_employee_directory()
    _enable_checkins_table()
    _FAKE_EMPLOYEES.append({"employee_name": "Ada", "slack_user_id": "U01"})
    _FAKE_CHECKINS.append(
        {
            "employee_name": "Ada Lovelace",
            "goal": "Grow pipeline",
            "progress_reported": 80,
            "confidence": "On Track",
            "blockers": "None",
        }
    )

    payloads: list[dict] = []

    def _capture(token, payload):
        payloads.append(payload)

    monkeypatch.setattr(notifications, "post_to_slack", _capture)

    outside_window = datetime(2024, 1, 8, 9, 0, tzinfo=timezone.utc)
    sent = digests.send_weekly_digest(now=outside_window)
    assert sent is False
    assert not payloads

    inside_window = datetime(2024, 1, 8, 10, 5, tzinfo=timezone.utc)
    sent = digests.send_weekly_digest(now=inside_window)
    assert sent is True
    assert payloads


def test_send_weekly_digest_handles_missing_checkins(monkeypatch):
    global _FAKE_SETTINGS
    _FAKE_SETTINGS = _basic_settings()
    _enable_employee_directory()
    _enable_checkins_table()
    _FAKE_EMPLOYEES.append({"employee_name": "Ada", "slack_user_id": "U01"})

    monkeypatch.setattr(notifications, "post_to_slack", lambda *args, **kwargs: None)

    sent = digests.send_weekly_digest(now=datetime(2024, 1, 8, 10, 5, tzinfo=timezone.utc))
    assert sent is False


def test_send_weekly_digest_skips_outside_window_with_timezone(monkeypatch):
    global _FAKE_SETTINGS
    _FAKE_SETTINGS = _basic_settings()
    _enable_employee_directory()
    _enable_checkins_table()
    _FAKE_EMPLOYEES.append({"employee_name": "Ada", "slack_user_id": "U01"})
    _FAKE_CHECKINS.append(
        {
            "employee_name": "Ada Lovelace",
            "goal": "Grow pipeline",
            "progress_reported": 80,
            "confidence": "On Track",
            "blockers": "None",
        }
    )

    called = []

    def _capture(token, payload):
        called.append(payload)

    monkeypatch.setattr(notifications, "post_to_slack", _capture)

    sent = digests.send_weekly_digest(now=datetime(2024, 1, 8, 9, 30, tzinfo=timezone.utc))

    assert sent is False


def test_get_slack_token_uses_decrypted_value():
    calls: list[str] = []

    class Settings(SimpleNamespace):
        def get_password(self, key: str) -> str:
            calls.append(key)
            return "  decrypted-token  "

    settings = Settings(slack_bot_token="should-not-be-used")

    token = notifications.get_slack_token(settings)

    assert token == "decrypted-token"
    assert calls == ["slack_bot_token"]


def test_get_slack_token_falls_back_on_error():
    class Settings(SimpleNamespace):
        def get_password(self, key: str) -> str:
            raise RuntimeError("boom")

    settings = Settings(slack_bot_token="  fallback-token  ")

    token = notifications.get_slack_token(settings)

    assert token == "fallback-token"


