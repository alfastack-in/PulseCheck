"""Server-side logic for the Weekly Checkin DocType."""

import frappe
from frappe import _
from frappe.model.document import Document


class WeeklyCheckin(Document):
    """Weekly progress check-in document."""

    def validate(self):
        """Run document level validations prior to saving."""
        self._validate_progress_range()

    def before_submit(self):
        """Stamp the posting date just before submission."""
        if not self.posting_date:
            self.posting_date = frappe.utils.today()

    def _validate_progress_range(self):
        """Ensure progress related values stay within a 0-100 range."""
        for fieldname in ("progress_reported", "progress"):
            value = getattr(self, fieldname, None)
            if value in (None, ""):
                continue

            try:
                numeric_value = float(value)
            except (TypeError, ValueError):
                label = fieldname.replace("_", " ").title()
                frappe.throw(_("{0} must be a number between 0 and 100.").format(label))

            if not 0 <= numeric_value <= 100:
                label = fieldname.replace("_", " ").title()
                frappe.throw(_("{0} must be between 0 and 100.").format(label))
