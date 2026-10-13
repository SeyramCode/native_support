import frappe
from frappe.utils import now_datetime

SETTINGS_DOCTYPE = "Native Support Settings"


def is_enabled() -> bool:
	"""Single switch every Native Support feature checks before changing standard behaviour."""
	try:
		return bool(frappe.db.get_single_value(SETTINGS_DOCTYPE, "enabled"))
	except frappe.DoesNotExistError:
		# settings doctype not synced yet (install / uninstall)
		return False


def pull_emails():
	"""Check incoming mail every 3 minutes instead of Frappe's 10 (scheduled in hooks.py).

	Runs Frappe's own pull. Minutes ending in 0 are left to Frappe's own schedule (0/10), so the two
	never run together and race past the "already queued" check into a duplicate pull."""
	if is_enabled() and now_datetime().minute % 10:
		from frappe.email.doctype.email_account.email_account import pull

		pull()
