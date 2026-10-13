"""Item driven SLA selection for Issues.

ERPNext applies SLAs from a wildcard ``validate`` hook that runs *after* every Issue specific hook.
``validate_issue`` (an Issue hook) therefore decides first and leaves the decision on ``doc.flags``;
the wrapped ``get_active_service_level_agreement_for`` hands that decision to ERPNext, so ERPNext's
own SLA pipeline still runs exactly once per save.
"""

import re
from html import unescape

import frappe
from erpnext.support.doctype.service_level_agreement import service_level_agreement as erpnext_sla
from frappe.utils import getdate
from rapidfuzz import fuzz, process
from rapidfuzz.utils import default_process

from native_support.utils import is_enabled

SERIAL_PENDING = "Serial Pending"
NOT_COVERED = "Not Covered"
EXPIRED = "Expired"
COVERED = "Covered"

ENTITY_RANK = {"Customer": 0, "Customer Group": 1, "Territory": 2}

# close-match suggestions for a typed serial / part number (same cutoff ERPNext uses for party matching)
SUGGESTION_CUTOFF = 80
SUGGESTION_LIMIT = 5
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

	try:
		doc.flags.native_support_sla = frappe._dict(sla=resolve_sla(doc))
	except Exception:
		# never block a ticket; ERPNext's standard SLA selection applies instead
		frappe.log_error(
			title="Native Support: SLA resolution failed", reference_doctype="Issue", reference_name=doc.name
		)


def is_gated(doc) -> bool:
	"""Only tickets created while Native Support is enabled; older tickets keep standard ERPNext behaviour."""
	flags = frappe.local.flags
	if flags.in_patch or flags.in_migrate or flags.in_install or flags.in_setup_wizard:
		return False

	return is_enabled() and bool(doc.is_new() or doc.get("ns_cover_status"))


def resolve_sla(doc):
	"""Record part number, tier and cover status on the ticket and return the SLA ERPNext may apply."""
	doc.ns_serial_no = (doc.ns_serial_no or "").strip() or None
	doc.ns_part_number = (doc.ns_part_number or "").strip() or None

	if current := get_current_coverage(doc):
		set_priority(doc, current, sla_changed=False)
		return as_sla_result(current)

	if not doc.ns_serial_no:
		doc.ns_serial_no = find_serial(doc.subject, doc.description)

	coverage = get_serial_coverage(doc.ns_serial_no, doc.customer) if doc.ns_serial_no else None
	if coverage:
		doc.ns_part_number = coverage.part_number
	elif doc.ns_part_number and doc.customer:
		# keep the agent's search text as typed: it may match several SLAs to choose from
		coverage = get_part_coverage(doc.ns_part_number, doc.customer, doc.service_level_agreement)

	if coverage:
		doc.ns_cover_status = coverage.status
	else:
		doc.ns_cover_status = NOT_COVERED if (doc.ns_serial_no or doc.ns_part_number) else SERIAL_PENDING
	doc.ns_sla_tier = coverage.ns_sla_tier if coverage else None

	if not coverage or coverage.status != COVERED:
		return None

	if not doc.customer and coverage.entity_type == "Customer":
		doc.customer = coverage.entity

	set_priority(doc, coverage, sla_changed=doc.service_level_agreement != coverage.name)
	return as_sla_result(coverage)


def get_current_coverage(doc):
	"""A covered ticket stays on the SLA it was logged under, even if that SLA later expires or its items
	change, so its response / resolution history is kept. Only a change by the agent to the customer,
	serial, part number or SLA makes it look again."""
	before = doc.get_doc_before_save()
	if not before or before.get("ns_cover_status") != COVERED or not doc.service_level_agreement:
		return None

	if any(
		doc.get(f) != before.get(f) for f in ("customer", "ns_serial_no", "ns_part_number", "service_level_agreement")
	):
		return None

	return frappe.db.get_value("Service Level Agreement", doc.service_level_agreement, SLA_FIELDS, as_dict=True)


def set_priority(doc, agreement, sla_changed):
	"""Take the SLA's default priority when the ticket moves to it, or when its priority is not on the SLA."""
	priorities = frappe.get_all(
		"Service Level Priority",
		filters={"parent": agreement.name, "parenttype": "Service Level Agreement"},
		pluck="priority",
	)
	if sla_changed or doc.priority not in priorities:
		doc.priority = agreement.default_priority or priorities[0]


def find_serial(*texts):
	"""Return the registered SLA Item serial that appears first in the given texts (whole word, any case)."""
	text = " ".join(html_to_text(t) for t in texts if t)
	if not text.strip():
		return None

	found = None
	for serial in frappe.get_all(
		"SLA Item",
		filters={"parenttype": "Service Level Agreement", "parentfield": "ns_sla_items", "serial_no": ["is", "set"]},
		pluck="serial_no",
		distinct=True,
	):
		# not part of a longer serial: "ABC-1" must not match inside "ABC-12" or "XABC-1"
		match = re.search(rf"(?<![\w-]){re.escape(serial.strip())}(?![\w-])", text, re.IGNORECASE)
		if match and (not found or match.start() < found[0]):
			found = (match.start(), serial)

	return found[1] if found else None


def html_to_text(html):
	# tags become spaces so words in separate paragraphs / lines don't run together
	return unescape(re.sub(r"<[^>]+>", " ", html))


def get_serial_coverage(serial_no, customer=None):
	"""Best SLA holding this serial for the customer, with its cover status, or None if the serial is unknown."""
	items = frappe.get_all(
		"SLA Item",
		filters={"parenttype": "Service Level Agreement", "parentfield": "ns_sla_items", "serial_no": serial_no},
		fields=["parent", "part_number"],
	)
	if not items:
		return None

	part_number = {d.parent: d.part_number for d in items}
	agreements = frappe.get_all(
		"Service Level Agreement",
		filters={"name": ["in", list(part_number)], "document_type": "Issue"},
		fields=SLA_FIELDS,
	)
	if customer:
		scope = get_customer_scope(customer)
		agreements = [a for a in agreements if not a.entity_type or (a.entity_type, a.entity) in scope]

	if not agreements:
		return None

	today = getdate()
	for agreement in agreements:
		agreement.part_number = part_number[agreement.name]
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


def get_active_customer_slas(customer, part_number=None, serial_no=None):
	"""Enabled, in-date Issue SLAs held by the customer (directly or via its group / territory).

	With ``serial_no`` (exact) or else ``part_number`` (contains), each SLA carries its matching SLA Item rows."""
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
	item_filter = (
		{"serial_no": serial_no.strip()}
		if serial_no
		else {"part_number": ["like", like_pattern(part_number)]}
		if part_number
		else None
	)
	if item_filter and agreements:
		for row in frappe.get_all(
			"SLA Item",
			filters={
				"parenttype": "Service Level Agreement",
				"parentfield": "ns_sla_items",
				"parent": ["in", [a.name for a in agreements]],
				**item_filter,
			},
			fields=["parent", "part_number", "description", "qty", "serial_no"],
			order_by="idx asc",
		):
			parts.setdefault(row.parent, []).append(row)

	for agreement in agreements:
		agreement.parts = parts.get(agreement.name, [])

	agreements.sort(key=lambda a: (ENTITY_RANK.get(a.entity_type, 3), getdate(a.end_date or "9999-12-31")))
	return agreements


@frappe.whitelist()
def get_customer_slas(customer, part_number=None, serial_no=None):
	"""Data for the Issue form's Active SLAs card; None when Native Support is disabled.

	With a serial or part number only the SLAs covering it are returned. When nothing covers it,
	close values from the customer's active SLAs are offered for the agent to pick; they are never applied
	automatically."""
	frappe.has_permission("Issue", "read", throw=True)
	if not is_enabled() or not customer:
		return None

	agreements = get_active_customer_slas(customer, part_number, serial_no)
	if not (serial_no or part_number):
		return {"slas": agreements, "suggestions": []}

	matches = [a for a in agreements if a.parts]
	suggestions = [] if matches else get_suggestions(agreements, serial_no, part_number)
	return {"slas": matches, "suggestions": suggestions}


def get_suggestions(agreements, serial_no=None, part_number=None):
	"""Serials (or part numbers) on these SLAs that closely resemble what was typed, best first."""
	field, typed = ("serial_no", serial_no) if serial_no else ("part_number", part_number)
	tier = {a.name: a.ns_sla_tier for a in agreements}
	rows = frappe.get_all(
		"SLA Item",
		filters={
			"parenttype": "Service Level Agreement",
			"parentfield": "ns_sla_items",
			"parent": ["in", list(tier) or [""]],
			field: ["is", "set"],
		},
		fields=["parent", "part_number", "description", "serial_no"],
	)

	results = process.extract(
		typed,
		{i: row[field] for i, row in enumerate(rows)},
		scorer=fuzz.ratio,
		processor=default_process,
		score_cutoff=SUGGESTION_CUTOFF,
		limit=SUGGESTION_LIMIT,
	)
	return [
		{
			"field": field,
			"value": value,
			"score": round(score),
			"sla": rows[i].parent,
			"tier": tier[rows[i].parent],
			"part_number": rows[i].part_number,
			"description": rows[i].description,
		}
		for value, score, i in results
	]


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
	"""Pick up a serial from a customer's emailed reply to a ticket that is still waiting for one."""
	if doc.reference_doctype != "Issue" or not doc.reference_name or doc.sent_or_received != "Received":
		return

	if not is_enabled():
		return

	issue = frappe.db.get_value("Issue", doc.reference_name, ["ns_cover_status", "ns_serial_no"], as_dict=True)
	if not issue or issue.ns_cover_status != SERIAL_PENDING or issue.ns_serial_no:
		return

	frappe.db.savepoint("native_support_serial")
	try:
		serial_no = find_serial(doc.subject, doc.content)
		if serial_no:
			ticket = frappe.get_doc("Issue", doc.reference_name)
			ticket.ns_serial_no = serial_no
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
