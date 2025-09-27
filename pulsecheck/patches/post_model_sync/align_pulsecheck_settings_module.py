import frappe


def execute():
    docname = "PulseCheck Settings"
    module_name = "Pulse Check"

    if frappe.db.exists("DocType", docname):
        frappe.db.set_value("DocType", docname, "module", module_name)

    if frappe.db.exists("Module Def", module_name):
        return

    if frappe.db.exists("Module Def", docname):
        frappe.db.set_value("Module Def", docname, "module_name", module_name)
