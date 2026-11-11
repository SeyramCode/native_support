# Copyright (c) 2026, Richmond Seyram and contributors
# For license information, please see license.txt

import frappe
from erpnext.support.doctype.service_level_agreement.test_service_level_agreement import (
	create_customer,
	create_service_level_agreement,
)
from frappe.tests.utils import FrappeTestCase

FAST, SLOW = "NS-FAST-0001", "NS-SLOW-0001"


class TestItemSLA(FrappeTestCase):
	def setUp(self):
		frappe.db.set_single_value("Support Settings", "track_service_level_agreement", 1)
		frappe.db.set_single_value("Native Support Settings", "enabled", 1)
		self.customer = create_customer()
		# response time in seconds: the faster SLA governs a ticket with both devices
		self.fast = make_item_sla("__NS Fast", self.customer, 1800, "Premium", FAST, "FG-60F")
		self.slow = make_item_sla("__NS Slow", self.customer, 14400, "Standard", SLOW, "C9200-24P")

	def tearDown(self):
		frappe.db.rollback()

	def test_every_serial_in_the_email_becomes_an_item(self):
		issue = make_issue(self.customer, f"Both {SLOW} and {FAST} are down")

		self.assertEqual([row.serial_no for row in issue.ns_items], [SLOW, FAST])
		self.assertEqual({row.cover_status for row in issue.ns_items}, {"Covered"})

	def test_fastest_cover_governs(self):
		issue = make_issue(self.customer, f"Both {SLOW} and {FAST} are down")

		self.assertEqual(issue.service_level_agreement, self.fast)
		self.assertEqual(issue.ns_sla_tier, "Premium")
		self.assertEqual(issue.ns_serial_no, FAST)
		self.assertEqual([row.serial_no for row in issue.ns_items if row.governing], [FAST])
		self.assertEqual(issue.ns_serials_covered, "2 of 2")

	def test_serials_covered_counts_only_serials(self):
		issue = make_issue(
			self.customer,
			"Mixed",
			items=[{"serial_no": FAST}, {"serial_no": "NS-UNKNOWN-1"}, {"part_number": "C9200"}],
		)

		self.assertEqual(issue.ns_serials_covered, "1 of 2")
		self.assertEqual(issue.ns_cover_status, "Covered")

	def test_several_values_in_one_cell_become_rows(self):
		issue = make_issue(self.customer, "Devices down", items=[{"serial_no": f"{SLOW}, {FAST}"}])

		self.assertEqual([row.serial_no for row in issue.ns_items], [SLOW, FAST])

	def test_part_number_item(self):
		issue = make_issue(self.customer, "Switch down", items=[{"part_number": "C9200"}])

		self.assertEqual(issue.service_level_agreement, self.slow)
		self.assertEqual(issue.ns_items[0].service_level_agreement, self.slow)
		self.assertFalse(issue.ns_serials_covered)

	def test_no_covered_item_no_sla(self):
		issue = make_issue(self.customer, "Unknown device", items=[{"serial_no": "NS-UNKNOWN-1"}])

		self.assertFalse(issue.service_level_agreement)
		self.assertEqual(issue.ns_cover_status, "Not Covered")

	def test_no_item_no_sla(self):
		issue = make_issue(self.customer, "Something is broken")

		self.assertFalse(issue.service_level_agreement)
		self.assertEqual(issue.ns_cover_status, "Serial Pending")

	def test_empty_row_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			make_issue(self.customer, "Empty row", items=[{"serial_no": " "}])

	def test_reply_adds_item_but_keeps_the_sla(self):
		issue = make_issue(self.customer, f"{SLOW} is down")
		self.assertEqual(issue.service_level_agreement, self.slow)

		receive_reply(issue, f"Also {FAST} now")
		issue.reload()

		self.assertEqual([row.serial_no for row in issue.ns_items], [SLOW, FAST])
		self.assertEqual(issue.service_level_agreement, self.slow)

	def test_applied_sla_governs(self):
		issue = make_issue(self.customer, f"Both {SLOW} and {FAST} are down")
		issue.service_level_agreement = self.slow
		issue.save()

		self.assertEqual(issue.service_level_agreement, self.slow)
		self.assertEqual(issue.ns_serial_no, SLOW)

	def test_disabled_never_picks_an_item_sla(self):
		frappe.db.set_single_value("Native Support Settings", "enabled", 0)
		issue = make_issue(self.customer, f"{FAST} is down")

		self.assertNotIn(issue.service_level_agreement, (self.fast, self.slow))
		self.assertFalse(issue.ns_items)


def make_item_sla(service_level, customer, response_time, tier, serial_no, part_number):
	sla = create_service_level_agreement(
		default_service_level_agreement=0,
		holiday_list="__Test Holiday List",
		response_time=response_time,
		resolution_time=response_time * 2,
		entity_type="Customer",
		entity=customer,
		service_level=service_level,
	)
	sla.ns_sla_tier = tier
	sla.append(
		"ns_sla_items", {"part_number": part_number, "description": service_level, "qty": 1, "serial_no": serial_no}
	)
	sla.save(ignore_permissions=True)
	return sla.name


def make_issue(customer, description, items=None):
	return frappe.get_doc(
		{
			"doctype": "Issue",
			"subject": description,
			"description": description,
			"customer": customer,
			"raised_by": "test@example.com",
			"ns_items": items or [],
		}
	).insert(ignore_permissions=True, ignore_mandatory=True)


def receive_reply(issue, content):
	frappe.get_doc(
		{
			"doctype": "Communication",
			"communication_type": "Communication",
			"communication_medium": "Email",
			"sent_or_received": "Received",
			"subject": f"Re: {issue.subject}",
			"content": content,
			"reference_doctype": "Issue",
			"reference_name": issue.name,
			"sender": "test@example.com",
		}
	).insert(ignore_permissions=True)
