"""Tests for the Slack interaction handler."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from pulsecheck.pulse_check import api


@pytest.fixture(autouse=True)
def fake_frappe(monkeypatch):
    """Provide a minimal frappe namespace so the handler can run in unit tests."""

    class _FakeDB:
        def __init__(self):
            self.values: Dict[tuple[str, str], Any] = {}

        def get_value(self, doctype, filters, fieldname=None, *, as_dict: bool = False):
            if isinstance(filters, dict):
                key = (doctype, json.dumps(filters, sort_keys=True))
            else:
                key = (doctype, str(filters))
            result = self.values.get((key[0], key[1]))
            if as_dict:
                if isinstance(fieldname, (list, tuple)):
                    data = {}
                    for item in fieldname:
                        data[item] = result.get(item) if isinstance(result, dict) else None
                    return data
                if isinstance(result, dict):
                    return result
                return {fieldname: result}

            if isinstance(fieldname, (list, tuple)):
                if isinstance(result, dict):
                    return [result.get(item) for item in fieldname]
                return [result for _ in fieldname]

            if isinstance(result, dict):
                return result.get(fieldname)
            return result

        def exists(self, doctype, name):
            return (doctype, str(name)) in self.values

    fake_db = _FakeDB()

    class FakeDoc:
        def __init__(self, **values):
            self.__dict__.update(values)
            if "meta" not in values:
                self.meta = SimpleNamespace(get_field=lambda _field: True)

        def insert(self, ignore_permissions=True):
            return self

        def submit(self):
            return self

        def set(self, fieldname, value):
            setattr(self, fieldname, value)
            return self

        def save(self, ignore_permissions=True):
            return self

    fake_local = SimpleNamespace(flags=SimpleNamespace(), response={})
    fake_request = SimpleNamespace(data=None, method="POST")

    fake_db.values[("Employee", "EMP-0001")] = {
        "name": "EMP-0001",
        "employee_name": "Ada Lovelace",
        "company_email": "ada@example.com",
    }
    fake_db.values[("Employee", json.dumps({"slack_user_id": "U123"}, sort_keys=True))] = "EMP-0001"
    fake_db.values[("Goal", "GOAL-0001")] = True

    def _fake_get_doc(arg, second=None):
        if isinstance(arg, dict):
            return FakeDoc(**arg)
        if second is not None:
            return FakeDoc(name=second)
        return FakeDoc(name=arg)

    fake = SimpleNamespace(
        db=fake_db,
        form_dict={},
        local=fake_local,
        log_error=lambda **_: None,
        request=fake_request,
        get_doc=_fake_get_doc,
        only_for=lambda _roles: None,
    )

    monkeypatch.setattr(api, "frappe", fake)

    # Ensure helper functions rely on fake metadata
    return fake


def build_payload(**overrides):
    payload = {
        "type": "view_submission",
        "user": {"id": "U123"},
        "view": {
            "private_metadata": json.dumps({"employee": "EMP-0001", "goal": "GOAL-0001"}),
            "state": {
                "values": {
                    "progress_block": {
                        "progress_input": {"type": "plain_text_input", "value": "72"}
                    },
                    "confidence_block": {
                        "confidence_select": {
                            "type": "static_select",
                            "selected_option": {"value": "On Track"},
                        }
                    },
                    "context_block": {
                        "context_input": {"type": "plain_text_input", "value": "Shipping beta."}
                    },
                    "blockers_block": {
                        "blockers_input": {
                            "type": "plain_text_input",
                            "value": "Need marketing assets.",
                        }
                    },
                    "plan_block": {
                        "plan_input": {
                            "type": "plain_text_input",
                            "value": "Coordinate launch with sales.",
                        }
                    },
                }
            },
        },
    }

    for key, value in overrides.items():
        payload[key] = value
    return payload


def test_handle_slack_interaction_processes_modal(monkeypatch, fake_frappe):
    payload = build_payload()
    fake_frappe.form_dict["payload"] = json.dumps(payload)

    created_docs = {}

    def _capture_checkin(employee, submission):
        created_docs["employee"] = employee
        created_docs["submission"] = submission
        return SimpleNamespace(goal=submission.goal)

    monkeypatch.setattr(api, "_create_weekly_checkin", _capture_checkin)

    updated_progress = {}

    def _capture_goal_update(goal_name, progress):
        updated_progress["goal"] = goal_name
        updated_progress["progress"] = progress

    monkeypatch.setattr(api, "_update_goal_progress", _capture_goal_update)
    monkeypatch.setattr(api, "_build_confirmation_message", lambda *args: "All set!")

    response = api.handle_slack_interaction()

    assert created_docs["employee"] == "EMP-0001"
    assert created_docs["submission"].progress == pytest.approx(72.0)
    assert updated_progress == {"goal": "GOAL-0001", "progress": pytest.approx(72.0)}

    assert response["response_action"] == "clear"
    assert response["messages"][0]["text"]["text"] == "All set!"


def test_handle_slack_interaction_returns_error_for_missing_employee(monkeypatch, fake_frappe):
    payload = build_payload()
    payload["view"]["private_metadata"] = json.dumps({"goal": "GOAL-0001"})
    fake_frappe.db.values.pop(("Employee", "EMP-0001"), None)
    fake_frappe.form_dict["payload"] = json.dumps(payload)

    response = api.handle_slack_interaction()

    assert response["response_action"] == "errors"
    assert "Employee" in response["errors"]["general"] or "employee" in response["errors"]["general"].lower()
    assert fake_frappe.local.response["http_status_code"] == 400


def test_open_checkin_modal_launches_view(monkeypatch, fake_frappe):
    fake_frappe.form_dict.clear()
    fake_frappe.form_dict.update(
        {
            "trigger_id": "1337.abc",
            "user_id": "U123",
            "user_name": "ada",
            "command": "/pulsecheck",
        }
    )

    payloads: list[dict] = []

    settings = SimpleNamespace(enable_weekly_prompts=1, notification_day="Monday", notification_time="10:00:00")

    monkeypatch.setattr(api.notifications, "get_settings", lambda: settings)
    monkeypatch.setattr(api.notifications, "get_slack_token", lambda _settings: "xoxb-test")
    monkeypatch.setattr(api.notifications, "open_slack_modal", lambda token, trigger, view: payloads.append({"token": token, "trigger": trigger, "view": view}))
    monkeypatch.setattr(api, "_fetch_employee_goals", lambda _employee: [{"name": "GOAL-0001", "goal_name": "Grow pipeline"}])

    response = api.open_checkin_modal()

    assert response["response_type"] == "ephemeral"
    assert payloads
    view = payloads[0]["view"]
    assert view["type"] == "modal"
    assert json.loads(view["private_metadata"]) == {"employee": "EMP-0001"}


def test_open_checkin_modal_returns_error_when_token_missing(monkeypatch, fake_frappe):
    fake_frappe.form_dict.clear()
    fake_frappe.form_dict.update(
        {
            "trigger_id": "1337.abc",
            "user_id": "U123",
            "user_name": "ada",
            "command": "/pulsecheck",
        }
    )

    settings = SimpleNamespace(enable_weekly_prompts=1, notification_day="Monday", notification_time="10:00:00")

    monkeypatch.setattr(api.notifications, "get_settings", lambda: settings)
    monkeypatch.setattr(api.notifications, "get_slack_token", lambda _settings: None)
    monkeypatch.setattr(api, "_fetch_employee_goals", lambda _employee: [])

    response = api.open_checkin_modal()

    assert response["response_type"] == "ephemeral"
    assert "token" in response["text"].lower()


def test_trigger_weekly_prompts_respects_force(monkeypatch, fake_frappe):
    fake_frappe.utils.now_datetime = lambda: datetime(2024, 1, 1, 10, 0)

    captured: Dict[str, Any] = {}

    def _fake_send(now=None, force=False):
        captured["force"] = force
        return True

    monkeypatch.setattr(api.prompts, "send_weekly_prompts", _fake_send)

    response = api.trigger_weekly_prompts(force="1")

    assert captured["force"] is True
    assert response["sent"] is True
    assert response["timestamp"].startswith("2024-01-01T10:00:00")


def test_trigger_weekly_digest_respects_force(monkeypatch, fake_frappe):
    fake_frappe.utils.now_datetime = lambda: datetime(2024, 1, 8, 10, 0)

    captured: Dict[str, Any] = {}

    def _fake_send(now=None, force=False):
        captured["force"] = force
        return False

    monkeypatch.setattr(api.digests, "send_weekly_digest", _fake_send)

    response = api.trigger_weekly_digest(force="1")

    assert captured["force"] is True
    assert response["sent"] is False
    assert response["timestamp"].startswith("2024-01-08T10:00:00")


def test_get_job_status_formats_datetimes(monkeypatch, fake_frappe):
    prompts_run = datetime(2024, 1, 1, 10, 0)
    digest_run = datetime(2024, 1, 8, 10, 0)

    monkeypatch.setattr(api.prompts, "get_last_prompt_run", lambda: prompts_run)
    monkeypatch.setattr(api.digests, "get_last_digest_run", lambda: digest_run)

    response = api.get_job_status()

    assert response == {
        "prompts_last_run": prompts_run.isoformat(),
        "digests_last_run": digest_run.isoformat(),
    }
