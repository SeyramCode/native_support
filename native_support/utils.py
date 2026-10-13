import frappe

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

	Runs Frappe's own pull, which skips accounts that already have a pull queued."""
	if is_enabled():
		from frappe.email.doctype.email_account.email_account import pull

		pull()
