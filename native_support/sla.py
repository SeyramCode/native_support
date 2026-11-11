"""Item driven SLA selection for Issues.

ERPNext applies SLAs from a wildcard ``validate`` hook that runs *after* every Issue specific hook.
``validate_issue`` (an Issue hook) therefore decides first and leaves the decision on ``doc.flags``;
the wrapped ``get_active_service_level_agreement_for`` hands that decision to ERPNext, so ERPNext's
own SLA pipeline still runs exactly once per save.
"""

import re
from html import unescape

import frappe
from frappe import _
from erpnext.support.doctype.service_level_agreement import service_level_agreement as erpnext_sla
from frappe.utils import getdate

from native_support.utils import is_enabled

SERIAL_PENDING = "Serial Pending"
NOT_COVERED = "Not Covered"
EXPIRED = "Expired"
COVERED = "Covered"

ENTITY_RANK = {"Customer": 0, "Customer Group": 1, "Territory": 2}

MAX_ITEMS = 50
# several values typed in one cell
VALUE_SEPARATORS = re.compile(r"[,;\n]+")

SLA_FIELDS = [
	"name",
	"enabled",
	"start_date",
	"end_date",
	"entity_type",
	"entity",
	"default_priority",
	"apply_sla_for_resolution",
	"ns_sla_tier",
]


def validate_issue(doc, method=None):
	install_sla_lookup_override()
	doc.flags.native_support_sla = None

	if not is_gated(doc):
		return

	# input errors are the agent's to fix; everything after this never blocks a ticket
	clean_items(doc)

	try:
		sla = resolve_sla(doc)
		set_serial_stats(doc)
		doc.flags.native_support_sla = frappe._dict(sla=sla)
	except Exception:
		# ERPNext's standard SLA selection applies instead
		frappe.log_error(
			title="Native Support: SLA resolution failed", reference_doctype="Issue", reference_name=doc.name
		)


def is_gated(doc) -> bool:
	"""Only tickets created while Native Support is enabled; older tickets keep standard ERPNext behaviour."""
	flags = frappe.local.flags
	if flags.in_patch or flags.in_migrate or flags.in_install or flags.in_setup_wizard:
		return False

	return is_enabled() and bool(doc.is_new() or doc.get("ns_cover_status"))


def clean_items(doc):
	"""One value per Affected Items row: split cells holding several values, drop surrounding spaces."""
	rows = []
	for row in doc.get("ns_items"):
		serials = split_values(row.serial_no)
		parts = split_values(row.part_number)
		if not (serials or parts):
			frappe.throw(_("Affected Items row {0}: enter a Serial No or a Part Number.").format(row.idx))

		row.serial_no = serials[0] if serials else None
		row.part_number = parts[0] if parts else None
		rows.append(row)
		rows += [frappe._dict(serial_no=value) for value in serials[1:]]
		rows += [frappe._dict(part_number=value) for value in parts[1:]]

	if len(rows) > MAX_ITEMS:
		frappe.throw(_("A ticket can hold at most {0} Affected Items.").format(MAX_ITEMS))

	if len(rows) != len(doc.get("ns_items")):
		doc.set("ns_items", [])
		for row in rows:
			doc.append("ns_items", {"serial_no": row.serial_no, "part_number": row.part_number})


def split_values(text):
	return [value.strip() for value in VALUE_SEPARATORS.split(text or "") if value.strip()]


def resolve_sla(doc):
	"""Give every Affected Item its own cover; the most urgent covered item sets the ticket's SLA.

	Returns the SLA ERPNext may apply, or None (no SLA until an item is known to be covered)."""
	add_items_from_ticket(doc)

	if current := get_current_coverage(doc):
		# already covered: keep the SLA (and its timers); items added since only get their own cover
		for row in doc.ns_items:
			if not row.governing:
				resolve_item(doc, row)
		set_priority(doc, current, sla_changed=False)
		return as_sla_result(current)

	covered = []
	for row in doc.ns_items:
		coverage = resolve_item(doc, row)
		if coverage and coverage.status == COVERED:
			covered.append((row, coverage))

	if not covered:
		statuses = {row.cover_status for row in doc.ns_items}
		doc.ns_cover_status = EXPIRED if EXPIRED in statuses else NOT_COVERED if statuses else SERIAL_PENDING
		doc.ns_serial_no = doc.ns_part_number = doc.ns_sla_tier = None
		return None

	row, coverage = get_governing_item(doc, covered)
	row.governing = 1
	doc.ns_serial_no, doc.ns_part_number = row.serial_no, row.part_number
	doc.ns_sla_tier = coverage.ns_sla_tier
	doc.ns_cover_status = COVERED

	if not doc.customer and coverage.entity_type == "Customer":
		doc.customer = coverage.entity

	set_priority(doc, coverage, sla_changed=doc.service_level_agreement != coverage.name)
	return as_sla_result(coverage)


def set_serial_stats(doc):
	"""Serials Covered on the ticket, e.g. "2 of 3"; empty when no serial was received."""
	serials = [row for row in doc.get("ns_items") if row.serial_no]
	covered = sum(row.cover_status == COVERED for row in serials)
	doc.ns_serials_covered = _("{0} of {1}").format(covered, len(serials)) if serials else None


def add_items_from_ticket(doc):
	"""On a new ticket, or one with no items yet, add a row for each registered serial in its text.

	Tickets from before Affected Items existed get a row from their single serial / part number."""
	if doc.get("ns_items") and not doc.is_new():
		return

	if not doc.get("ns_items") and (doc.ns_serial_no or doc.ns_part_number):
		doc.append("ns_items", {"serial_no": doc.ns_serial_no, "part_number": doc.ns_part_number})

	add_serials(doc, find_serials(doc.subject, doc.description))


def add_serials(doc, serials):
	"""Append rows for serials not on the ticket yet; returns how many were added."""
	known = {row.serial_no.upper() for row in doc.get("ns_items") if row.serial_no}
	added = 0
	for serial in serials:
		if serial.upper() not in known and len(doc.get("ns_items")) < MAX_ITEMS:
			doc.append("ns_items", {"serial_no": serial})
			known.add(serial.upper())
			added += 1
	return added


def resolve_item(doc, row):
	"""Find and record the SLA covering one Affected Item; returns that SLA (with ``status``) or None."""
	coverage = None
	if row.serial_no:
		coverage = get_serial_coverage(row.serial_no, doc.customer)
		if coverage:
			row.part_number, row.description = coverage.part_number, coverage.description
	elif row.part_number and doc.customer:
		coverage = get_part_coverage(row.part_number, doc.customer, doc.service_level_agreement)
		if coverage and coverage.get("parts"):
			row.description = coverage.parts[0].description

	row.service_level_agreement = coverage.name if coverage else None
	row.sla_tier = coverage.ns_sla_tier if coverage else None
	row.cover_status = coverage.status if coverage else NOT_COVERED
	row.governing = 0
	return coverage


def get_governing_item(doc, covered):
	"""The SLA the agent applied, if it covers an item; else the fastest response, then the first row."""
	if applied := get_applied_sla(doc):
		for row, coverage in covered:
			if coverage.name == applied:
				return row, coverage

	return min(covered, key=lambda pair: get_response_seconds(pair[1]))


def get_applied_sla(doc):
	"""SLA the agent set in this save (the Active SLAs card's Apply button), not the one kept from before."""
	before = doc.get_doc_before_save()
	if doc.service_level_agreement and (
		not before or before.service_level_agreement != doc.service_level_agreement
	):
		return doc.service_level_agreement


def get_response_seconds(agreement):
	response = frappe.db.get_value(
		"Service Level Priority",
		{"parent": agreement.name, "parenttype": "Service Level Agreement", "priority": agreement.default_priority},
		"response_time",
	)
	return response if response is not None else float("inf")


def get_current_coverage(doc):
	"""A covered ticket stays on the SLA it was logged under, even if that SLA later expires or its items
	change, so its response / resolution history is kept. Only an agent's change to the customer, the SLA
	or the Affected Items makes it look again; rows added from an emailed reply do not."""
	before = doc.get_doc_before_save()
	if not before or before.get("ns_cover_status") != COVERED or not doc.service_level_agreement:
		return None

	if doc.customer != before.customer or doc.service_level_agreement != before.service_level_agreement:
		return None

	if not doc.flags.native_support_items_from_reply and get_item_keys(doc) != get_item_keys(before):
		return None

	return frappe.db.get_value("Service Level Agreement", doc.service_level_agreement, SLA_FIELDS, as_dict=True)


def get_item_keys(doc):
	return sorted(((row.serial_no or "").upper(), (row.part_number or "").upper()) for row in doc.get("ns_items"))


def set_priority(doc, agreement, sla_changed):
	"""Take the SLA's default priority when the ticket moves to it, or when its priority is not on the SLA."""
	priorities = frappe.get_all(
		"Service Level Priority",
		filters={"parent": agreement.name, "parenttype": "Service Level Agreement"},
		pluck="priority",
	)
	if sla_changed or doc.priority not in priorities:
		doc.priority = agreement.default_priority or priorities[0]


def find_serials(*texts):
	"""Registered SLA Item serials found in the texts (whole word, any case), in order of appearance."""
	text = " ".join(html_to_text(t) for t in texts if t)
	if not text.strip():
		return []

	found = {}
	for serial in frappe.get_all(
		"SLA Item",
		filters={"parenttype": "Service Level Agreement", "parentfield": "ns_sla_items", "serial_no": ["is", "set"]},
		pluck="serial_no",
		distinct=True,
	):
		# not part of a longer serial: "ABC-1" must not match inside "ABC-12" or "XABC-1"
		match = re.search(rf"(?<![\w-]){re.escape(serial.strip())}(?![\w-])", text, re.IGNORECASE)
		if match and serial.upper() not in found:
			found[serial.upper()] = (match.start(), serial)

	return [serial for _position, serial in sorted(found.values())]


def html_to_text(html):
	# tags become spaces so words in separate paragraphs / lines don't run together
	return unescape(re.sub(r"<[^>]+>", " ", html))


def get_serial_coverage(serial_no, customer=None):
	"""Best SLA holding this serial for the customer, with its cover status, or None if the serial is unknown."""
	items = frappe.get_all(
		"SLA Item",
		filters={"parenttype": "Service Level Agreement", "parentfield": "ns_sla_items", "serial_no": serial_no},
		fields=["parent", "part_number", "description"],
	)
	if not items:
		return None

	item = {d.parent: d for d in items}
	agreements = frappe.get_all(
		"Service Level Agreement",
		filters={"name": ["in", list(item)], "document_type": "Issue"},
		fields=SLA_FIELDS,
	)
	if customer:
		scope = get_customer_scope(customer)
		agreements = [a for a in agreements if not a.entity_type or (a.entity_type, a.entity) in scope]

	if not agreements:
		return None

	today = getdate()
	for agreement in agreements:
		agreement.part_number = item[agreement.name].part_number
		agreement.description = item[agreement.name].description
		if agreement.end_date and getdate(agreement.end_date) < today:
			agreement.status = EXPIRED
		elif agreement.enabled and (not agreement.start_date or getdate(agreement.start_date) <= today):
			agreement.status = COVERED
		else:
			agreement.status = NOT_COVERED

	agreements.sort(key=lambda a: (a.status != COVERED, ENTITY_RANK.get(a.entity_type, 3)))
	return agreements[0]


def get_part_coverage(part_number, customer, selected_sla=None):
	"""Active SLA of the customer covering this part; the one the agent picked if it is among the matches."""
	matches = [a for a in get_active_customer_slas(customer, part_number) if a.parts]
	if not matches:
		return get_expired_part_coverage(part_number, customer)

	coverage = next((a for a in matches if a.name == selected_sla), matches[0])
	coverage.status = COVERED
	return coverage


def get_expired_part_coverage(part_number, customer):
	"""The customer's most recently ended SLA covering this part, reported as Expired."""
	scope = get_customer_scope(customer)
	expired = frappe.db.sql(
		"""
		select sla.name, sla.entity_type, sla.entity, sla.ns_sla_tier
		from `tabService Level Agreement` sla
		join `tabSLA Item` item on item.parent = sla.name
			and item.parenttype = 'Service Level Agreement' and item.parentfield = 'ns_sla_items'
		where sla.document_type = 'Issue' and sla.end_date < %(today)s and item.part_number like %(part)s
		order by sla.end_date desc
		""",
		{"today": getdate(), "part": like_pattern(part_number)},
		as_dict=True,
	)
	coverage = next((a for a in expired if (a.entity_type, a.entity) in scope), None)
	if coverage:
		coverage.status = EXPIRED
	return coverage


def like_pattern(text):
	"""``%text%`` with the user's own % and _ matched literally."""
	text = text.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
	return f"%{text}%"


def get_active_customer_slas(customer, part_number=None):
	"""Enabled, in-date Issue SLAs held by the customer (directly or via its group / territory).

	With ``part_number``, each SLA carries the SLA Item rows whose part number contains it."""
	scope = get_customer_scope(customer)
	today = getdate()
	agreements = [
		a
		for a in frappe.get_all(
			"Service Level Agreement",
			filters={
				"document_type": "Issue",
				"enabled": 1,
				"default_service_level_agreement": 0,
				"entity_type": ["is", "set"],
			},
			fields=SLA_FIELDS,
		)
		if (a.entity_type, a.entity) in scope
		and (not a.start_date or getdate(a.start_date) <= today)
		and (not a.end_date or getdate(a.end_date) >= today)
	]

	parts = {}
	if part_number and agreements:
		for row in frappe.get_all(
			"SLA Item",
			filters={
				"parenttype": "Service Level Agreement",
				"parentfield": "ns_sla_items",
				"parent": ["in", [a.name for a in agreements]],
				"part_number": ["like", like_pattern(part_number)],
			},
			fields=["parent", "part_number", "description", "qty", "serial_no"],
			order_by="idx asc",
		):
			parts.setdefault(row.parent, []).append(row)

	for agreement in agreements:
		agreement.parts = parts.get(agreement.name, [])

	agreements.sort(key=lambda a: (ENTITY_RANK.get(a.entity_type, 3), getdate(a.end_date or "9999-12-31")))
	return agreements


def get_customer_scope(customer):
	return {
		("Customer", customer),
		*(("Customer Group", d) for d in erpnext_sla.get_customer_group(customer)),
		*(("Territory", d) for d in erpnext_sla.get_customer_territory(customer)),
	}


def as_sla_result(agreement):
	"""Shape ERPNext's ``process_sla`` expects."""
	return frappe._dict(
		name=agreement.name,
		default_priority=agreement.default_priority,
		apply_sla_for_resolution=agreement.apply_sla_for_resolution,
		condition=None,
	)


def on_communication_insert(doc, method=None):
	"""Add the registered serials in a customer's emailed reply to the ticket's Affected Items."""
	if doc.reference_doctype != "Issue" or not doc.reference_name or doc.sent_or_received != "Received":
		return

	if not is_enabled() or not frappe.db.get_value("Issue", doc.reference_name, "ns_cover_status"):
		return

	frappe.db.savepoint("native_support_serial")
	try:
		serials = find_serials(doc.subject, doc.content)
		ticket = frappe.get_doc("Issue", doc.reference_name) if serials else None
		if ticket and add_serials(ticket, serials):
			# a covered ticket keeps its SLA; the new rows only get their own cover
			ticket.flags.native_support_items_from_reply = True
			# like email intake: agent-filled mandatory fields must not stop the serial being recorded
			ticket.flags.ignore_mandatory = True
			ticket.save(ignore_permissions=True)
	except Exception:
		# never block incoming email
		frappe.db.rollback(save_point="native_support_serial")
		frappe.log_error(
			title="Native Support: serial from reply failed",
			reference_doctype="Issue",
			reference_name=doc.reference_name,
		)


def install_sla_lookup_override():
	"""Wrap ERPNext's SLA lookup once per process so it honours the decision made in ``validate_issue``."""
	current = erpnext_sla.get_active_service_level_agreement_for
	if getattr(current, "native_support", False):
		return

	def get_active_service_level_agreement_for(doc):
		decision = doc.flags.get("native_support_sla") if hasattr(doc, "flags") else None
		if decision is None:
			return keep_item_slas_to_their_tickets(doc, current(doc))

		if not frappe.db.get_single_value("Support Settings", "track_service_level_agreement"):
			return None

		return decision.sla

	get_active_service_level_agreement_for.native_support = True
	erpnext_sla.get_active_service_level_agreement_for = get_active_service_level_agreement_for


def keep_item_slas_to_their_tickets(doc, sla):
	"""For tickets Native Support does not decide (older tickets, or the app disabled), ERPNext may pick
	any SLA of the customer. Never let it move a ticket onto an item based SLA it is not already on;
	fall back the way ERPNext ranks them: current SLA, the customer's general SLAs, the default SLA."""
	if (
		not sla
		or doc.get("doctype") != "Issue"
		or sla.name == doc.get("service_level_agreement")
		or not is_item_sla(sla.name)
	):
		return sla

	candidates = []
	if doc.get("service_level_agreement"):
		candidates.append(doc.service_level_agreement)
	if doc.get("customer"):
		candidates += [a.name for a in get_active_customer_slas(doc.customer) if not is_item_sla(a.name)]
	for filters in (
		{"default_service_level_agreement": 0, "entity_type": ["is", "not set"]},
		{"default_service_level_agreement": 1},
	):
		candidates += [
			name
			for name in frappe.get_all(
				"Service Level Agreement", filters={**filters, "enabled": 1, "document_type": "Issue"}, pluck="name"
			)
			if not is_item_sla(name)
		]

	context = erpnext_sla.get_context(doc)
	for name in candidates:
		agreement = frappe.db.get_value(
			"Service Level Agreement", {"name": name, "enabled": 1}, [*SLA_FIELDS, "condition"], as_dict=True
		)
		if not agreement:
			continue
		if doc.get("priority") and not frappe.db.exists(
			"Service Level Priority", {"parent": name, "priority": doc.priority}
		):
			continue
		if agreement.condition and not frappe.safe_eval(agreement.condition, None, context):
			continue
		return as_sla_result(agreement)


def is_item_sla(name):
	return bool(frappe.db.exists("SLA Item", {"parenttype": "Service Level Agreement", "parent": name}))
