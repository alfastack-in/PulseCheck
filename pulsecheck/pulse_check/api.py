"""Public API endpoints for the Pulse Check app."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional
from types import SimpleNamespace

from . import digests, notifications, prompts

def _noop_whitelist(*_args, **_kwargs):
    def _decorator(func):
        return func

    return _decorator


try:  # pragma: no cover - frappe is only available in a bench environment
    import frappe  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - fallback that allows unit tests to stub frappe
    frappe = SimpleNamespace(whitelist=_noop_whitelist)  # type: ignore
else:
    if not getattr(frappe, "whitelist", None):  # type: ignore[attr-defined]
        setattr(frappe, "whitelist", _noop_whitelist)


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


def _coerce_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


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


def _parse_slack_command_payload() -> Dict[str, Any]:
    if frappe is None:  # pragma: no cover - runtime guard
        raise RuntimeError("Frappe must be installed to parse Slack commands.")

    form_dict = getattr(frappe, "form_dict", None) or {}

    if "payload" in form_dict:
        raw_payload = form_dict.get("payload")
        if isinstance(raw_payload, bytes):
            raw_payload = raw_payload.decode("utf-8")
        if not raw_payload:
            raise SlackPayloadError("Slack payload is empty.")
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError as exc:
            raise SlackPayloadError("Slack payload is not valid JSON.") from exc
        if not isinstance(payload, dict):
            raise SlackPayloadError("Slack payload is malformed.")
        payload.setdefault("interaction_type", payload.get("type") or "interaction")
        return payload

    command_payload = dict(form_dict)
    if not command_payload:
        raise SlackPayloadError("Slack command payload is missing.")

    user_section = {
        "id": command_payload.get("user_id"),
        "username": command_payload.get("user_name"),
        "name": command_payload.get("user_name"),
        "email": command_payload.get("user_email"),
    }

    command_payload.update({
        "interaction_type": "command",
        "user": user_section,
    })

    return command_payload


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

    debug_details: list[str] = []

    user_section = payload.get("user", {})
    if isinstance(user_section, dict):
        slack_user_id = user_section.get("id")
        if slack_user_id:
            debug_details.append(f"Slack ID {slack_user_id}")
            employee = frappe.db.get_value("Employee", {"slack_user_id": slack_user_id}, "name")  # type: ignore[attr-defined]
            if employee:
                return employee

        user_email = user_section.get("email")
        if user_email:
            debug_details.append(f"email {user_email}")
            employee = frappe.db.get_value("Employee", {"company_email": user_email}, "name")  # type: ignore[attr-defined]
            if not employee:
                employee = frappe.db.get_value("Employee", {"personal_email": user_email}, "name")  # type: ignore[attr-defined]
            if employee:
                return employee

        username = user_section.get("username") or user_section.get("name")
        if username:
            debug_details.append(f"username {username}")
            employee = frappe.db.get_value("Employee", {"employee_name": username}, "name")  # type: ignore[attr-defined]
            if employee:
                return employee

    detail_fragment = "; ".join(debug_details)
    message = "Unable to map the Slack user to an Employee record."
    if detail_fragment:
        message = f"{message} (checked {detail_fragment})."
    if frappe is not None:
        frappe.log_error(  # type: ignore[attr-defined]
            message=message,
            title="PulseCheck Employee Resolution",
        )

    raise SlackPayloadError(message)


def _fetch_employee_goals(employee_name: str) -> List[Dict[str, Any]]:
    if frappe is None:  # pragma: no cover - runtime guard
        return []

    try:
        return frappe.get_all(  # type: ignore[call-arg]
            "Goal",
            filters={
                "employee": employee_name,
                "is_group": 0,
                "status": ["!=", "Archived"],
            },
            fields=["name", "goal_name", "status", "progress"],
            order_by="modified desc",
            limit_page_length=50,
        )
    except Exception:  # pragma: no cover - frappe surfaces detailed errors in production
        return []


def _get_employee_details(employee_name: str) -> Dict[str, Any]:
    if frappe is None:  # pragma: no cover - runtime guard
        return {"name": employee_name}

    details = frappe.db.get_value(  # type: ignore[attr-defined]
        "Employee",
        employee_name,
        ["name", "employee_name", "company_email", "personal_email"],
        as_dict=True,
    )

    if not isinstance(details, dict):
        return {"name": employee_name}

    details.setdefault("name", employee_name)
    return details


def _build_checkin_modal(
    employee: Dict[str, Any],
    goals: List[Dict[str, Any]],
    *,
    initial_goal: Optional[str] = None,
) -> Dict[str, Any]:
    employee_display = employee.get("employee_name") or employee.get("name") or "your team"

    blocks: List[Dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"Submit your weekly update for *{_truncate_text(employee_display, 150)}*.",
            },
        }
    ]

    options: List[Dict[str, Any]] = []
    for goal in goals:
        option = _goal_option(goal)
        if option:
            options.append(option)
    initial_option = None
    if initial_goal:
        initial_option = next((opt for opt in options if opt["value"] == initial_goal), None)

    if options:
        blocks.append(
            {
                "type": "input",
                "block_id": "goal_block",
                "label": {"type": "plain_text", "text": "Goal"},
                "element": {
                    "type": "static_select",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Select a goal",
                    },
                    "action_id": "goal_select",
                    "options": options,
                    **({"initial_option": initial_option} if initial_option else {}),
                },
            }
        )
    else:
        blocks.append(
            {
                "type": "input",
                "block_id": "goal_block",
                "label": {"type": "plain_text", "text": "Goal"},
                "hint": {
                    "type": "plain_text",
                    "text": "Start typing the Goal ID (for example HR-GOAL-2025-0001).",
                },
                "element": {
                    "type": "plain_text_input",
                    "action_id": "goal_input",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Enter the Goal ID",
                    },
                },
            }
        )

    blocks.extend(
        [
            {
                "type": "input",
                "block_id": "progress_block",
                "label": {"type": "plain_text", "text": "Progress (%)"},
                "hint": {
                    "type": "plain_text",
                    "text": "Provide a number between 0 and 100.",
                },
                "element": {
                    "type": "plain_text_input",
                    "action_id": "progress_input",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "e.g. 75",
                    },
                },
            },
            {
                "type": "input",
                "block_id": "confidence_block",
                "label": {"type": "plain_text", "text": "Confidence"},
                "optional": True,
                "element": {
                    "type": "static_select",
                    "action_id": "confidence_select",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "How confident are you?",
                    },
                    "options": [
                        _static_option("On Track"),
                        _static_option("At Risk"),
                        _static_option("Blocked"),
                    ],
                },
            },
            {
                "type": "input",
                "block_id": "context_block",
                "label": {"type": "plain_text", "text": "Context"},
                "optional": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "context_input",
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "What did you accomplish?",
                    },
                },
            },
            {
                "type": "input",
                "block_id": "blockers_block",
                "label": {"type": "plain_text", "text": "Blockers"},
                "optional": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "blockers_input",
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Is anything slowing you down?",
                    },
                },
            },
            {
                "type": "input",
                "block_id": "plan_block",
                "label": {"type": "plain_text", "text": "Next Week Plan"},
                "optional": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "plan_input",
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "What will you focus on next week?",
                    },
                },
            },
        ]
    )

    metadata = {"employee": employee.get("name")}

    return {
        "type": "modal",
        "callback_id": "pulsecheck_weekly_checkin",
        "title": {"type": "plain_text", "text": "Weekly Pulse Check"},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": json.dumps(metadata),
        "blocks": blocks,
    }


def _static_option(label: str) -> Dict[str, Any]:
    return {
        "text": {"type": "plain_text", "text": _truncate_text(label, 75)},
        "value": label,
    }


def _goal_option(goal: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    name = (goal.get("name") or "").strip()
    if not name:
        return None

    title = goal.get("goal_name") or name
    status = goal.get("status")
    progress = goal.get("progress")

    fragments = [_truncate_text(str(title), 60)]
    extra = []
    if progress not in (None, ""):
        try:
            extra.append(f"{int(float(progress))}%")
        except (TypeError, ValueError):
            pass
    if status and isinstance(status, str):
        extra.append(status)
    if extra:
        fragments.append(f"({' · '.join(extra)})")

    return {
        "text": {"type": "plain_text", "text": _truncate_text(" ".join(fragments), 75)},
        "value": name,
    }


def _truncate_text(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    if max_length <= 1:
        return value[:max_length]
    return value[: max_length - 1] + "…"


def _create_weekly_checkin(employee: str, submission: Submission) -> Any:
    if frappe is None:  # pragma: no cover - runtime guard
        raise RuntimeError("Frappe must be installed to create Weekly Checkins.")

    if not submission.goal:
        raise SlackPayloadError("Goal selection is required to submit a check-in.")

    if submission.progress is None:
        raise SlackPayloadError("Progress value is required to submit a check-in.")

    if not frappe.db.exists("Goal", submission.goal):  # type: ignore[attr-defined]
        raise SlackPayloadError(
            f"The selected goal ({submission.goal}) could not be found. Please pick a valid goal."
        )

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
    payload = {
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
    if frappe is not None:
        frappe.response["type"] = "json"  # type: ignore[attr-defined]
        frappe.response["message"] = payload  # type: ignore[attr-defined]
        frappe.response.pop("http_status_code", None)  # type: ignore[attr-defined]
    return payload


def _error_response(message: str) -> Dict[str, Any]:
    payload = {"response_action": "errors", "errors": {"general": message}}
    if frappe is not None:
        frappe.response["type"] = "json"  # type: ignore[attr-defined]
        frappe.response["message"] = payload  # type: ignore[attr-defined]
        frappe.response["http_status_code"] = 200  # keep Slack happy
    return payload


def _ephemeral_response(message: str, *, error: bool = False, interaction_type: str = "command") -> Dict[str, Any]:
    text = message
    if error and not text.startswith(":warning:"):
        text = f":warning: {text}"

    payload: Dict[str, Any]
    if interaction_type == "command":
        payload = {"response_type": "ephemeral", "text": text}
    else:
        payload = {"response_action": "clear", "message": text}

    if frappe is not None:
        frappe.response["type"] = "json"  # type: ignore[attr-defined]
        frappe.response["message"] = payload  # type: ignore[attr-defined]
        frappe.response.pop("http_status_code", None)  # type: ignore[attr-defined]

    return payload


def _handle_block_action(payload: Dict[str, Any]) -> Dict[str, Any]:
    actions = payload.get("actions") or []
    if not actions:
        raise SlackPayloadError("Slack action payload is empty.")

    action = actions[0]
    action_id = action.get("action_id")
    if not action_id or not action_id.startswith("pulsecheck_open_modal"):
        raise SlackPayloadError("Unsupported Slack action.")

    metadata = {}
    value = action.get("value")
    if isinstance(value, str) and value:
        metadata = _parse_private_metadata(value)

    trigger_id = payload.get("trigger_id")
    if not trigger_id:
        raise SlackPayloadError("Slack trigger_id is missing from the request.")

    settings = notifications.get_settings()
    if not settings:
        raise SlackPayloadError("PulseCheck Settings are unavailable. Please contact an administrator.")

    token = notifications.get_slack_token(settings)
    if not token:
        raise SlackPayloadError("Slack bot token is missing. Configure it in PulseCheck Settings.")

    employee = _resolve_employee(payload, metadata)
    employee_details = _get_employee_details(employee)
    goals = _fetch_employee_goals(employee)
    initial_goal = metadata.get("goal") or metadata.get("value")

    view = _build_checkin_modal(employee_details, goals, initial_goal=initial_goal)

    try:
        notifications.open_slack_modal(token, trigger_id, view)
    except notifications.SlackDeliveryError as exc:
        raise SlackPayloadError(str(exc)) from exc

    notifications.log_event(
        "Slack Interaction",
        step="modal_opened",
        employee=employee,
        action_id=action_id,
    )

    return _ephemeral_response("Opening the Pulse Check modal…", interaction_type="interaction")


def _enforce_system_manager() -> None:
    if frappe is None:  # pragma: no cover - runtime guard
        return

    only_for = getattr(frappe, "only_for", None)
    if callable(only_for):
        only_for(("System Manager",))


@frappe.whitelist(allow_guest=True, methods=["POST"])
def open_checkin_modal() -> Dict[str, Any]:
    """Slash command/shortcut entry point that opens the weekly check-in modal."""

    if frappe is None:  # pragma: no cover - runtime guard
        raise RuntimeError("Frappe must be installed to handle Slack interactions.")

    frappe.local.flags.ignore_csrf = True  # type: ignore[attr-defined]

    interaction_type = "command"

    try:
        payload = _parse_slack_command_payload()
        interaction_type = payload.get("interaction_type") or "command"

        trigger_id = payload.get("trigger_id")
        if not trigger_id:
            raise SlackPayloadError("Slack trigger_id is missing from the request.")

        settings = notifications.get_settings()
        if not settings:
            raise SlackPayloadError("PulseCheck Settings are unavailable. Please contact an administrator.")

        token = notifications.get_slack_token(settings)
        if not token:
            raise SlackPayloadError("Slack bot token is missing. Configure it in PulseCheck Settings.")

        user_section = payload.get("user") or {}
        employee = _resolve_employee({"user": user_section}, {})
        employee_details = _get_employee_details(employee)
        goals = _fetch_employee_goals(employee)

        command_text = payload.get("text") if isinstance(payload.get("text"), str) else ""
        initial_goal = _match_initial_goal(command_text, goals)

        view = _build_checkin_modal(employee_details, goals, initial_goal=initial_goal)

        try:
            notifications.open_slack_modal(token, trigger_id, view)
        except notifications.SlackDeliveryError as exc:
            raise SlackPayloadError(str(exc)) from exc

        return _ephemeral_response("Opening the Pulse Check modal…", interaction_type=interaction_type)
    except SlackPayloadError as exc:
        return _ephemeral_response(str(exc), error=True, interaction_type=interaction_type)


def _match_initial_goal(command_text: str | None, goals: List[Dict[str, Any]]) -> Optional[str]:
    if not command_text:
        return None

    text = command_text.strip()
    if not text:
        return None

    for goal in goals:
        if text == goal.get("name") or text == goal.get("goal_name"):
            return goal.get("name")
    return None


@frappe.whitelist(methods=["POST"])
def trigger_weekly_prompts(force: Any = True) -> Dict[str, Any]:
    """Manually trigger the weekly prompt job."""

    if frappe is None:  # pragma: no cover - runtime guard
        raise RuntimeError("Frappe must be installed to manage Pulse Check prompts.")

    _enforce_system_manager()

    now = frappe.utils.now_datetime()  # type: ignore[attr-defined]
    sent = prompts.send_weekly_prompts(now=now, force=_coerce_truthy(force))
    return {"sent": bool(sent), "timestamp": now.isoformat()}


@frappe.whitelist(methods=["POST"])
def trigger_weekly_digest(force: Any = True) -> Dict[str, Any]:
    """Manually trigger the weekly digest job."""

    if frappe is None:  # pragma: no cover - runtime guard
        raise RuntimeError("Frappe must be installed to manage Pulse Check digests.")

    _enforce_system_manager()

    now = frappe.utils.now_datetime()  # type: ignore[attr-defined]
    sent = digests.send_weekly_digest(now=now, force=_coerce_truthy(force))
    return {"sent": bool(sent), "timestamp": now.isoformat()}


@frappe.whitelist()
def get_job_status() -> Dict[str, Optional[str]]:
    """Return cached execution timestamps for scheduled jobs."""

    if frappe is None:  # pragma: no cover - runtime guard
        raise RuntimeError("Frappe must be installed to fetch Pulse Check job status.")

    _enforce_system_manager()

    prompts_run = prompts.get_last_prompt_run()
    digest_run = digests.get_last_digest_run()

    return {
        "prompts_last_run": prompts_run.isoformat() if prompts_run else None,
        "digests_last_run": digest_run.isoformat() if digest_run else None,
    }


@frappe.whitelist(allow_guest=True, methods=["POST"])
def handle_slack_interaction() -> Dict[str, Any]:
    """Webhook handler invoked by Slack interactive components."""

    if frappe is None:  # pragma: no cover - runtime guard
        raise RuntimeError("Frappe must be installed to handle Slack interactions.")

    frappe.local.flags.ignore_csrf = True  # type: ignore[attr-defined]

    try:
        payload = _load_payload()
        notifications.log_event(
            "Slack Interaction",
            step="received",
            payload_type=payload.get("type"),
        )
        payload_type = payload.get("type")

        if payload_type == "block_actions":
            return _handle_block_action(payload)

        if payload_type != "view_submission":
            raise SlackPayloadError("Unsupported Slack interaction type.")

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
