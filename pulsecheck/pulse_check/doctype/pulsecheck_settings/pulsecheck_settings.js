// Copyright (c) 2025, Prashant Agrawal and contributors
// For license information, please see license.txt

frappe.ui.form.on("PulseCheck Settings", {
    refresh(frm) {
        if (frm.is_new()) {
            return;
        }

        add_actions(frm);
        update_job_status(frm);
    },
});

function add_actions(frm) {
    if (frm.__pulsecheck_actions_added) {
        return;
    }

    const actions = {
        "Send Weekly Prompts Now": "pulsecheck.pulse_check.api.trigger_weekly_prompts",
        "Send Weekly Digest Now": "pulsecheck.pulse_check.api.trigger_weekly_digest",
    };

    Object.entries(actions).forEach(([label, method]) => {
        frm.add_custom_button(
            label,
            () => run_pulsecheck_job(method, frm),
            __("Pulse Check"),
        );
    });

    frm.__pulsecheck_actions_added = true;
}

function run_pulsecheck_job(method, frm) {
    frappe.call({
        method,
        args: { force: 1 },
        freeze: true,
        freeze_message: __("Running Pulse Check job..."),
        callback: (r) => {
            if (r && r.message && r.message.sent) {
                frappe.msgprint({
                    message: __("Pulse Check job completed and Slack messages were sent."),
                    indicator: "green",
                });
            } else {
                frappe.msgprint({
                    message: __("Pulse Check job completed but no Slack messages were sent."),
                    indicator: "orange",
                });
            }
            update_job_status(frm);
        },
        error: () => {
            frappe.msgprint({
                message: __("Pulse Check job failed. Check the error log for details."),
                indicator: "red",
            });
        },
    });
}

function update_job_status(frm) {
    frappe.call({
        method: "pulsecheck.pulse_check.api.get_job_status",
        callback: (r) => {
            const data = (r && r.message) || {};
            const prompts = data.prompts_last_run ? format_datetime(data.prompts_last_run) : __("Never");
            const digests = data.digests_last_run ? format_datetime(data.digests_last_run) : __("Never");

            frm.dashboard.set_headline(
                __("Prompts last sent: {0} · Digests last sent: {1}", [prompts, digests])
            );
        },
    });
}

function format_datetime(value) {
    try {
        const dt = frappe.datetime.convert_to_user_tz(value);
        return frappe.datetime.str_to_user(dt);
    } catch (error) {
        return value;
    }
}
