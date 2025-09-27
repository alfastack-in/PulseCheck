import frappe


def execute():
    if not frappe.db.exists("DocType", "Pulse Check Setting"):
        return

    frappe.rename_doc(
        "DocType",
        "Pulse Check Setting",
        "PulseCheck Settings",
        force=True,
        merge=False,
    )
