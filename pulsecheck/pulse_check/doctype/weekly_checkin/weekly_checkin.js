frappe.ui.form.on("Weekly Checkin", {
    refresh(frm) {
        if (frm.is_new() && !frm.doc.posting_date) {
            frm.set_value("posting_date", frappe.datetime.get_today());
        }
    },
});
