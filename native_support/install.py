import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.custom.doctype.property_setter.property_setter import make_property_setter

from native_support.utils import SETTINGS_DOCTYPE

CUSTOM_FIELDS = {
	"Service Level Agreement": [
		{
			"fieldname": "ns_sla_tier",
			"fieldtype": "Link",
			"label": "SLA Tier",
			"options": "SLA Tier",
			"insert_after": "service_level",
			"in_list_view": 1,
			"in_standard_filter": 1,
		},
		{
			"fieldname": "ns_sla_items_section",
			"fieldtype": "Section Break",
			"label": "SLA Items",
			"insert_after": "support_and_resolution",
		},
		{
			"fieldname": "ns_sla_items",
			"fieldtype": "Table",
			"label": "SLA Items",
			"options": "SLA Item",
			"insert_after": "ns_sla_items_section",
		},
	],
	"Issue": [
		{
			"fieldname": "ns_sla_section",
			"fieldtype": "Section Break",
			"label": "SLA Coverage",
			"insert_after": "description",
			# only tickets created while Native Support is enabled
			"depends_on": "eval:doc.__islocal || doc.ns_cover_status",
		},
		{
			"fieldname": "ns_part_number",
			"fieldtype": "Data",
			"label": "Part Number",
			"insert_after": "ns_sla_section",
			"description": "Search the customer's active SLAs by part number.",
			"no_copy": 1,
		},
		{
			"fieldname": "ns_serial_no",
			"fieldtype": "Data",
			"label": "Serial No",
			"insert_after": "ns_part_number",
			"description": "Detected automatically from the ticket and emailed replies when registered on an SLA.",
			"search_index": 1,
		},
		{
			"fieldname": "ns_sla_cb",
			"fieldtype": "Column Break",
			"insert_after": "ns_serial_no",
		},
		{
			"fieldname": "ns_cover_status",
			"fieldtype": "Select",
			"label": "Cover Status",
			"options": "\nSerial Pending\nCovered\nExpired\nNot Covered",
			"insert_after": "ns_sla_cb",
			"read_only": 1,
			"no_copy": 1,
			"in_standard_filter": 1,
		},
		{
			"fieldname": "ns_sla_tier",
			"fieldtype": "Link",
			"label": "SLA Tier",
			"options": "SLA Tier",
			"insert_after": "ns_cover_status",
			"depends_on": "eval:doc.ns_sla_tier",
			"read_only": 1,
			"no_copy": 1,
		},
	],
}

# (doctype, fieldname, property, value, property_type); Custom Field has no column for these
PROPERTY_SETTERS = [
	# Download / Upload buttons on the SLA Items grid
	("Service Level Agreement", "ns_sla_items", "allow_bulk_edit", "1", "Check"),
]

SLA_TIERS = {
	"Default": "Baseline cover for registered devices.",
	"Standard": "Business hours cover with standard response and resolution targets.",
	"Premium": "Extended cover with the fastest response and resolution targets.",
}


def after_install():
	make_custom_fields()

	for tier_name, description in SLA_TIERS.items():
		if not frappe.db.exists("SLA Tier", tier_name):
			frappe.get_doc({"doctype": "SLA Tier", "tier_name": tier_name, "description": description}).insert(
				ignore_permissions=True
			)

	# ships switched off: nothing changes for new tickets until SLA Items are configured and this is checked
	frappe.db.set_single_value(SETTINGS_DOCTYPE, "enabled", 0)


def after_migrate():
	make_custom_fields()


def make_custom_fields():
	for fields in CUSTOM_FIELDS.values():
		for df in fields:
			df["module"] = "Native Support"
	create_custom_fields(CUSTOM_FIELDS, update=True)

	for doctype, fieldname, prop, value, property_type in PROPERTY_SETTERS:
		if frappe.db.get_value(
			"Property Setter", {"doc_type": doctype, "field_name": fieldname, "property": prop}, "value"
		) != value:
			make_property_setter(doctype, fieldname, prop, value, property_type, validate_fields_for_doctype=False)


def before_uninstall():
	for doctype, fieldname, prop, _value, _type in PROPERTY_SETTERS:
		frappe.db.delete("Property Setter", {"doc_type": doctype, "field_name": fieldname, "property": prop})

	for doctype, fields in CUSTOM_FIELDS.items():
		for df in fields:
			frappe.db.delete("Custom Field", {"dt": doctype, "fieldname": df["fieldname"]})
		frappe.clear_cache(doctype=doctype)
