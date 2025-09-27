"""Tests for the Slack interaction handler."""

from __future__ import annotations

import json
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

        def get_value(self, doctype, filters, fieldname):
            if isinstance(filters, dict):
                key = (doctype, json.dumps(filters, sort_keys=True))
            else:
                key = (doctype, str(filters))
            result = self.values.get((key[0], key[1]))
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

    fake_db.values[("Employee", "EMP-0001")] = True

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
