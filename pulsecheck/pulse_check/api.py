"""Public API endpoints for the Pulse Check app."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

try:  # pragma: no cover - frappe is only available in a bench environment
    import frappe  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - fallback that allows unit tests to stub frappe
    frappe = None  # type: ignore


def _get_whitelist_decorator():
    if frappe is None:
        def _whitelist(*_args, **_kwargs):
            def _decorator(func):
                return func

            return _decorator

        return _whitelist

    return frappe.whitelist  # type: ignore[return-value]


_whitelist = _get_whitelist_decorator()


class SlackPayloadError(Exception):
    """Raised when an incoming Slack interaction payload cannot be processed."""


@dataclass(slots=True)
class Submission:
    """Normalized submission payload extracted from a Slack modal."""

    goal: Optional[str]
    progress: Optional[float]
    confidence: Optional[str]
    context: Optional[str]
    blockers: Optional[str]
    next_week_plan: Optional[str]


def _load_payload() -> Dict[str, Any]:
    """Return the JSON payload sent by Slack."""

    if frappe is None:  # pragma: no cover - validated during runtime
        raise RuntimeError("Frappe must be installed to handle Slack interactions.")

    raw_payload: Optional[str | bytes] = None

    if getattr(frappe, "form_dict", None):
        raw_payload = frappe.form_dict.get("payload")  # type: ignore[attr-defined]

    if not raw_payload and getattr(getattr(frappe, "request", None), "data", None):
        raw_payload = frappe.request.data  # type: ignore[attr-defined]

    if isinstance(raw_payload, bytes):
        raw_payload = raw_payload.decode("utf-8")

    if not raw_payload:
        raise SlackPayloadError("Slack payload is missing from the request body.")

    try:
        return json.loads(raw_payload)
    except json.JSONDecodeError as exc:  # pragma: no cover - guarded by tests
        raise SlackPayloadError("Slack payload is not valid JSON.") from exc


def _parse_private_metadata(metadata: Optional[str]) -> Dict[str, Any]:
    if not metadata:
        return {}

    metadata = metadata.strip()
    if not metadata:
        return {}

    if metadata.startswith("{"):
        try:
            parsed = json.loads(metadata)
        except json.JSONDecodeError:  # pragma: no cover - metadata is controlled by us
            return {}
        return parsed if isinstance(parsed, dict) else {}

    return {"value": metadata}


def _flatten_state_values(state_values: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    flattened: Dict[str, Any] = {}
    for block in state_values.values():
        if not isinstance(block, dict):
            continue
        for action_id, action_payload in block.items():
            if not isinstance(action_payload, dict):
                continue

            if "value" in action_payload and action_payload["value"] not in (None, ""):
                flattened[action_id] = action_payload["value"]
                continue

            selected_option = action_payload.get("selected_option")
            if isinstance(selected_option, dict):
                flattened[action_id] = selected_option.get("value") or selected_option.get("text", {}).get("text")
                continue

            if action_payload.get("selected_options"):
                flattened[action_id] = [
                    option.get("value")
                    for option in action_payload.get("selected_options", [])
                    if isinstance(option, dict)
                ]
                continue

            if "selected_user" in action_payload:
                flattened[action_id] = action_payload.get("selected_user")

    return flattened


def _extract_submission(view: Dict[str, Any]) -> Submission:
    state = view.get("state", {}) if isinstance(view, dict) else {}
    values = state.get("values", {}) if isinstance(state, dict) else {}
    flattened = _flatten_state_values(values)

    metadata = _parse_private_metadata(view.get("private_metadata")) if isinstance(view, dict) else {}

    goal = metadata.get("goal") or metadata.get("value")
    for key in ("goal", "goal_select", "goal_input", "selected_goal"):
        if key in flattened and flattened[key]:
            goal = flattened[key]
            break

    progress_raw = None
    for key in ("progress", "progress_reported", "progress_input", "progress_slider"):
        if key in flattened and flattened[key] not in (None, ""):
            progress_raw = flattened[key]
            break

    progress_value: Optional[float] = None
    if progress_raw not in (None, ""):
        try:
            progress_value = float(progress_raw)
        except (TypeError, ValueError) as exc:
            raise SlackPayloadError("Progress value must be a number between 0 and 100.") from exc

    confidence = None
    for key in ("confidence", "confidence_select"):
        if key in flattened and flattened[key]:
            confidence = flattened[key]
            break

    return Submission(
        goal=goal,
        progress=progress_value,
        confidence=confidence,
        context=flattened.get("context") or flattened.get("context_input"),
        blockers=flattened.get("blockers") or flattened.get("blockers_input"),
        next_week_plan=flattened.get("next_week_plan") or flattened.get("plan_input"),
    )


def _resolve_employee(payload: Dict[str, Any], metadata: Dict[str, Any]) -> str:
    if frappe is None:  # pragma: no cover - runtime guard
        raise RuntimeError("Frappe must be installed to resolve employees.")

    employee_identifier = metadata.get("employee") or metadata.get("employee_id") or metadata.get("value")
    if employee_identifier:
        if frappe.db.exists("Employee", employee_identifier):  # type: ignore[attr-defined]
            return employee_identifier

    user_section = payload.get("user", {})
    if isinstance(user_section, dict):
        slack_user_id = user_section.get("id")
        if slack_user_id:
            employee = frappe.db.get_value("Employee", {"slack_user_id": slack_user_id}, "name")  # type: ignore[attr-defined]
            if employee:
                return employee

        user_email = user_section.get("email")
        if user_email:
            employee = frappe.db.get_value("Employee", {"company_email": user_email}, "name")  # type: ignore[attr-defined]
            if not employee:
                employee = frappe.db.get_value("Employee", {"personal_email": user_email}, "name")  # type: ignore[attr-defined]
            if employee:
                return employee

        username = user_section.get("username") or user_section.get("name")
        if username:
            employee = frappe.db.get_value("Employee", {"employee_name": username}, "name")  # type: ignore[attr-defined]
            if employee:
                return employee

    raise SlackPayloadError("Unable to map the Slack user to an Employee record.")


def _create_weekly_checkin(employee: str, submission: Submission) -> Any:
    if frappe is None:  # pragma: no cover - runtime guard
        raise RuntimeError("Frappe must be installed to create Weekly Checkins.")

    if not submission.goal:
        raise SlackPayloadError("Goal selection is required to submit a check-in.")

    if submission.progress is None:
        raise SlackPayloadError("Progress value is required to submit a check-in.")

    doc = frappe.get_doc(  # type: ignore[attr-defined]
        {
            "doctype": "Weekly Checkin",
            "employee": employee,
            "goal": submission.goal,
            "progress_reported": submission.progress,
            "confidence": submission.confidence,
            "context": submission.context,
            "blockers": submission.blockers,
            "next_week_plan": submission.next_week_plan,
        }
    )
    doc.insert(ignore_permissions=True)
    doc.submit()
    return doc


def _update_goal_progress(goal_name: str, progress: float) -> None:
    if frappe is None:  # pragma: no cover - runtime guard
        raise RuntimeError("Frappe must be installed to update Goal progress.")

    goal_doc = frappe.get_doc("Goal", goal_name)  # type: ignore[attr-defined]
    target_field = _first_existing_field(goal_doc, ("progress", "current", "value", "percent_complete"))
    if target_field:
        goal_doc.set(target_field, progress)
        goal_doc.save(ignore_permissions=True)


def _first_existing_field(doc: Any, fieldnames: Iterable[str]) -> Optional[str]:
    meta = getattr(doc, "meta", None)
    for fieldname in fieldnames:
        if meta and getattr(meta, "get_field", None) and meta.get_field(fieldname):
            return fieldname
        if hasattr(doc, fieldname):
            return fieldname
    return None


def _build_confirmation_message(employee_name: str, goal_name: str, progress: float) -> str:
    if frappe is None:  # pragma: no cover - runtime guard
        raise RuntimeError("Frappe must be installed to build confirmation messages.")

    employee_title = frappe.db.get_value("Employee", employee_name, "employee_name") or employee_name  # type: ignore[attr-defined]

    goal_title_fields = ["title", "subject", "goal_name", "name"]
    goal_title = None
    for field in goal_title_fields:
        goal_title = frappe.db.get_value("Goal", goal_name, field)  # type: ignore[attr-defined]
        if goal_title:
            break
    goal_title = goal_title or goal_name

    return (
        f"Thanks, {employee_title}! Your weekly check-in for *{goal_title}* has been recorded "
        f"with progress set to {int(progress)}%."
    )


def _success_response(message: str) -> Dict[str, Any]:
    return {
        "response_action": "clear",
        "messages": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": message,
                },
            }
        ],
    }


def _error_response(message: str) -> Dict[str, Any]:
    if frappe is not None:
        frappe.local.response["http_status_code"] = 400  # type: ignore[attr-defined]
    return {
        "response_action": "errors",
        "errors": {"general": message},
    }


@_whitelist(allow_guest=True, methods=["POST"])
def handle_slack_interaction() -> Dict[str, Any]:
    """Webhook handler invoked by Slack interactive components."""

    if frappe is None:  # pragma: no cover - runtime guard
        raise RuntimeError("Frappe must be installed to handle Slack interactions.")

    frappe.local.flags.ignore_csrf = True  # type: ignore[attr-defined]

    try:
        payload = _load_payload()
        if payload.get("type") != "view_submission":
            raise SlackPayloadError("Only Slack modal submissions are supported.")

        view = payload.get("view", {})
        if not isinstance(view, dict):
            raise SlackPayloadError("Slack modal submission payload is malformed.")

        metadata = _parse_private_metadata(view.get("private_metadata"))
        employee = _resolve_employee(payload, metadata)
        submission = _extract_submission(view)

        checkin_doc = _create_weekly_checkin(employee, submission)
        if submission.progress is not None:
            _update_goal_progress(checkin_doc.goal, submission.progress)

        message = _build_confirmation_message(employee, checkin_doc.goal, submission.progress or 0)
        return _success_response(message)
    except SlackPayloadError as exc:
        if frappe is not None:
            frappe.log_error(message=str(exc), title="PulseCheck Slack Interaction")  # type: ignore[attr-defined]
        return _error_response(str(exc))

