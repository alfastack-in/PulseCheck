# Copyright (c) 2025, Prashant Agrawal and Contributors
# See license.txt

import pytest

frappe = pytest.importorskip("frappe")
from frappe.tests.utils import FrappeTestCase


class TestWeeklyCheckin(FrappeTestCase):
    def test_posting_date_auto_populated(self):
        weekly_checkin = frappe.new_doc("Weekly Checkin")
        weekly_checkin.insert()

        self.assertEqual(weekly_checkin.posting_date, frappe.utils.today())
