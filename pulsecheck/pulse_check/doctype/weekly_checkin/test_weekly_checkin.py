# Copyright (c) 2025, Prashant Agrawal and Contributors
# See license.txt

import pytest

frappe = pytest.importorskip("frappe")
from frappe.tests.utils import FrappeTestCase


class TestWeeklyCheckin(FrappeTestCase):
    def test_posting_date_auto_populated(self):
        weekly_checkin = frappe.new_doc("Weekly Checkin")
        weekly_checkin.flags.ignore_mandatory = True
        weekly_checkin.flags.ignore_links = True

        weekly_checkin.insert()
        self.assertFalse(weekly_checkin.posting_date)

        weekly_checkin.submit()

        self.assertEqual(weekly_checkin.posting_date, frappe.utils.today())
