# Pulse Check

Pulse Check keeps weekly goal tracking inside Slack. Team members submit short, structured updates through a modal, while Frappe stores them as **Weekly Checkin** documents. Automated reminders and manager digests ensure the right people see progress without leaving chat.

---

## Table of Contents

1. [Overview](#overview)
2. [Capabilities](#capabilities)
3. [Prerequisites](#prerequisites)
4. [Installation](#installation)
5. [PulseCheck Settings](#pulsecheck-settings)
6. [Slack Setup](#slack-setup)
7. [Using Pulse Check](#using-pulse-check)
8. [Background Jobs](#background-jobs)
9. [Monitoring & Logging](#monitoring--logging)
10. [Troubleshooting](#troubleshooting)
11. [Development Workflow](#development-workflow)
12. [Continuous Integration](#continuous-integration)
13. [License](#license)

---

## Overview

The app ships with two primary flows:

- **Slack modal submissions** — Users launch a modal (via slash command or shortcut) and record goal progress, confidence, blockers, and next-week plans. Submissions become submit-able **Weekly Checkin** documents.
- **Weekly automations** — Scheduled jobs nudge employees to submit updates and compile approved check-ins into digest summaries for managers.

Everything runs on the Frappe scheduler, so once installed the workflow continues without further manual effort.

---

## Capabilities

- **Slack-native check-ins:** `pulsecheck.pulse_check.api.open_checkin_modal` opens a modal tailored to the employee’s active goals.
- **Duplicate protection:** `handle_slack_interaction` rejects multiple submissions for the same goal within the same reporting week.
- **Automated prompts:** `prompts.send_weekly_prompts` DM’s active employees whose `Employee` records have a linked Frappe user (`user_id`).
- **Manager digests:** `digests.send_weekly_digest` groups approved check-ins by manager (`reports_to`/`leave_approver`) and posts summaries in their Slack DMs.
- **Audit logs:** Every significant step calls `notifications.log_event`, which writes to Frappe’s error log for later review.
- **Goal progress updates:** When a submission includes progress, the matching `Goal.progress` value is updated automatically.

_Add screenshots by replacing the placeholders below when you capture real UI states:_

- `![Slack modal](docs/images/slack-modal-placeholder.png)`
- `![Weekly Checkin list](docs/images/weekly-checkin-list.png)`
- `![Digest example](docs/images/digest-placeholder.png)`

---

## Prerequisites

Pulse Check depends on the following components:

- **Frappe Framework v15** (matching your bench).
- **ERPNext v15** and **HRMS v15** — provide Employee and Goal doctypes used by the app.
- **Slack App** with the ability to issue slash commands, open modals, and send direct messages.
- **Bench CLI** access to install and manage apps/sites.

Ensure outbound HTTPS traffic to Slack is allowed from the server hosting your bench.

---

## Installation

Below assumes a development bench; adjust for production deployments.

1. **Fetch the required repositories**
   ```bash
   bench get-app erpnext --branch version-15
   bench get-app hrms --branch version-15
   bench get-app pulsecheck https://github.com/alfastack-in/PulseCheck.git --branch develop
   ```

2. **Create a site (skip if you already have one)**
   ```bash
   bench new-site your-site.local \
     --db-root-password <mysql-root-password> \
     --admin-password <frappe-admin-password>
   ```

3. **Install the apps**
   ```bash
   bench --site your-site.local install-app erpnext hrms pulsecheck
   bench --site your-site.local migrate
   ```

4. **Build front-end assets (optional)**
   ```bash
   bench build --app pulsecheck
   ```

5. **Enable the scheduler**
   ```bash
   bench --site your-site.local set-config scheduler_enabled 1
   bench restart
   ```

---

## PulseCheck Settings

Navigate to **PulseCheck Settings** after installation. The doctype contains:

| Field | Purpose |
| --- | --- |
| Enable Weekly Prompts | Master toggle for both DM prompts and manager digests. |
| Slack Bot Token | Stored securely using Frappe’s password storage; required for all Slack API calls. |
| Notification Day | Weekday that determines when prompts/digests should run. |
| Notification Time | 24-hour time (HH:MM) evaluated against the scheduler window. |
| Last Weekly Prompt Run (read-only) | Timestamp updated after a successful prompt run. |
| Last Weekly Digest Run (read-only) | Timestamp updated after a successful digest run. |

There are **no** built-in buttons on the form. Use the commands listed in [Background Jobs](#background-jobs) to trigger runs manually.

### Mapping Employees to Slack

Prompts consider any active Employee records returned by `notifications.get_employee_directory(require_slack=True)`, which effectively means the employee must have:

- A linked `user_id` (Frappe User) **and/or**
- A company/personal email that matches a Slack member email.

If your organisation prefers storing direct Slack IDs, add a custom field named `slack_user_id` to the Employee doctype; the API checks this field first when processing modal submissions.

---

## Slack Setup

Configure a Slack app with the following pieces:

1. **Interactivity & Shortcuts**
   - Request URL → `https://<your-domain>/api/method/pulsecheck.pulse_check.api.handle_slack_interaction`

2. **Slash Command** (example `/pulsecheck`)
   - Request URL → `https://<your-domain>/api/method/pulsecheck.pulse_check.api.open_checkin_modal`
   - Description → “Submit this week’s update” (or similar)

3. **OAuth Scopes** required for the bot token
   - `commands`
   - `chat:write`
   - `im:write`
   - `users:read`
   - `users:read.email`

4. **Install the Slack app** to your workspace and copy the bot token into **PulseCheck Settings**.

> Signing secret validation is not yet implemented in the app. Restrict your Slack command to trusted workspaces.

---

## Using Pulse Check

### Submit a check-in

1. Run the slash command or shortcut in Slack.
2. Choose your goal (pre-populated from HRMS) and fill in progress, confidence, blockers, and plans.
3. Submit to create a **Weekly Checkin** document; progress values update the corresponding Goal if provided.
4. Slack responds with a confirmation message inside the modal.

### Review submissions

- Open **Weekly Checkin** in ERPNext to filter by employee, goal, or date range.
- Documents are submit-able and include notes for blockers, confidence, and next-week plans.

### Manager digests

- When the digest job runs, managers identified via `reports_to` or `leave_approver` receive direct messages highlighting the team’s check-ins for the completed week.

---

## Background Jobs

Both scheduled jobs respect the “Enable Weekly Prompts” toggle and skip execution when tokens, recipients, or data are missing.

| Purpose | Scheduler Function | Manual Command |
| --- | --- | --- |
| Send weekly DM prompts | `prompts.enqueue_weekly_prompts` | `bench --site your-site.local execute pulsecheck.pulse_check.prompts.enqueue_weekly_prompts` |
| Deliver manager digests | `digests.enqueue_weekly_digest` | `bench --site your-site.local execute pulsecheck.pulse_check.digests.enqueue_weekly_digest` |

Use the commands to test new configurations or to re-send messages on demand.

---

## Monitoring & Logging

- `notifications.log_event` writes structured JSON into Frappe’s Error Log. Search for entries titled “PulseCheck …” when diagnosing issues.
- Read-only timestamps on **PulseCheck Settings** confirm the last successful run for prompts and digests.
- Duplicate submissions detected by `handle_slack_interaction` surface a warning modal and are recorded in the logs.

---

## Troubleshooting

| Symptom | Likely Cause | Suggested Fix |
| --- | --- | --- |
| Slack modal shows “We had trouble connecting” | Backend raised `SlackPayloadError` or returned non-JSON | Inspect **Error Log** entries with title “PulseCheck Slack Interaction”. |
| Slash command does nothing | Command URL incorrect or modal triggered outside Slack | Verify the slash command points to `/api/method/pulsecheck.pulse_check.api.open_checkin_modal`. |
| Employees do not receive prompts | Employee lacks `user_id`/matching email or bot token missing | Ensure Employee records link to Frappe Users and the bot token is set. |
| Digest delivers empty results | No submitted check-ins for the completed week | Confirm `Weekly Checkin` documents are submitted (docstatus = 1). |
| Scheduler never runs jobs | Scheduler disabled or workers stopped | `bench enable-scheduler --site <site>` then `bench restart`. |

---

## Development Workflow

Run the provided pre-commit hooks before opening a pull request:

```bash
cd apps/pulsecheck
pre-commit install
pre-commit run --all-files
```

Hooks include Ruff (lint + format), ESLint, Prettier, Pyupgrade, and the standard formatting/consistency checks.

### Tests

```bash
bench --site your-site.local set-config allow_tests true
bench --site your-site.local run-tests --app pulsecheck
```

The test suite focuses on Slack interaction flows and scheduled job behaviour using fake frappe fixtures.

---

## Continuous Integration

GitHub Actions workflows (see `.github/workflows/`):

- **CI (Server):** Spins up a bench, installs ERPNext + HRMS + Pulse Check, and runs the automated tests.
- **Linters:** Executes the repository’s pre-commit hooks plus Semgrep rules and `pip-audit`.
- **Vulnerability Check:** Runs `pip-audit` separately against dependency manifests.

---

## License

Released under the [MIT license](license.txt).

Need help or have feedback? Reach out at [prashant@alfastack.in](mailto:prashant@alfastack.in).

