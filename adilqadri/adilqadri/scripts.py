"""
One-shot helpers for configuring and live-testing the Uniware integration
from a bench execute call. Not imported anywhere else.

Usage:
    bench --site <site> execute adilqadri.adilqadri.scripts.setup_and_test
"""

import frappe


DEFAULT_TENANT = "https://adilqadri.unicommerce.com"
DEFAULT_CLIENT_ID = "my-trusted-client"
DEFAULT_USERNAME = "finance.adilqadri@gmail.com"

SEED_CHANNELS = [
	("UNIWARE-HR", "Uniware Adilqadri-HR", "Unicommerce"),
	("UNIWARE-LABEL", "Uniware Adilqadri Label", "Unicommerce"),
	("UNIWARE-SCAN", "Uniware Adilqadri Scanning", "Unicommerce"),
	("UNIWARE-SRMUM", "Uniware Shiprocket Mumbai", "Unicommerce"),
	("UNIWARE-MYNTRA", "Uniware Myntra HF", "Unicommerce"),
	("AMAZON-IN", "Amazon India", "Amazon"),
	("FLIPKART", "Flipkart", "Flipkart"),
	("WEBSITE", "Adilqadri Website", "Website"),
]

SEED_FACILITIES = [
	("Adilqadri-HR", "Adilqadri HR", 1),
	("Adilqadri Label", "Adilqadri Label", 0),
	("Adilqadri Scanning", "Adilqadri Scanning", 0),
	("Adilqadri Shiprocket Mumbai", "Adilqadri Shiprocket Mumbai", 0),
	("Adilqadri Myntra HF", "Adilqadri Myntra HF", 0),
]


def seed_sales_channels():
	"""Idempotent: insert seed sales channels if missing."""
	created = 0
	for code, name, platform in SEED_CHANNELS:
		if frappe.db.exists("Sales Channel", code):
			continue
		doc = frappe.new_doc("Sales Channel")
		doc.channel_code = code
		doc.channel_name = name
		doc.platform = platform
		doc.is_active = 1
		doc.insert(ignore_permissions=True)
		created += 1
	frappe.db.commit()
	total = frappe.db.count("Sales Channel")
	print(f"Sales Channel: created={created}, total={total}")
	return {"created": created, "total": total}


def seed_connector_settings(password: str | None = None):
	"""
	Configure the Uniware Connector Settings single doctype with
	sensible defaults for the adilqadri tenant. Password should be
	passed explicitly — we never hard-code it here.
	"""
	s = frappe.get_single("Uniware Connector Settings")
	s.enabled = 1
	s.tenant_url = DEFAULT_TENANT
	s.client_id = DEFAULT_CLIENT_ID
	s.username = DEFAULT_USERNAME
	if password and not s.get_password("password", raise_exception=False):
		s.password = password

	existing_codes = {row.facility_code for row in (s.get("facilities") or [])}
	for code, name, is_default in SEED_FACILITIES:
		if code not in existing_codes:
			s.append(
				"facilities",
				{"facility_code": code, "facility_name": name, "is_default": is_default},
			)

	s.flags.ignore_permissions = True
	s.save()
	frappe.db.commit()
	print(
		f"Uniware Connector Settings: enabled={s.enabled}, "
		f"tenant={s.tenant_url}, facilities={len(s.facilities)}, "
		f"password_set={bool(s.get_password('password', raise_exception=False))}"
	)
	return s


def live_test():
	"""Call test_oauth and test_rest_call, print results."""
	s = frappe.get_single("Uniware Connector Settings")
	print("\n--- test_oauth ---")
	try:
		oauth = s.test_oauth()
	except Exception as e:
		oauth = {"ok": False, "error": f"{type(e).__name__}: {e}"}
	for k, v in (oauth or {}).items():
		print(f"  {k}: {v}")

	print("\n--- test_rest_call ---")
	try:
		rest = s.test_rest_call()
	except Exception as e:
		rest = {"ok": False, "error": f"{type(e).__name__}: {e}"}
	for k, v in (rest or {}).items():
		if k == "preview":
			v = (v or "")[:200]
		print(f"  {k}: {v}")
	return {"oauth": oauth, "rest": rest}


def verify_schema():
	"""Print doctype presence and Item custom field state."""
	print("\n--- doctypes ---")
	for dt in [
		"Uniware Connector Settings",
		"Uniware Connector Facility",
		"Sales Channel",
		"Channel Item Code",
	]:
		exists = frappe.db.exists("DocType", dt)
		mod = frappe.db.get_value("DocType", dt, "module") if exists else None
		print(f"  {dt}: exists={bool(exists)}, module={mod}")

	print("\n--- old colliding doctypes (should be owned by ecommerce_integrations) ---")
	for dt in ["Unicommerce Settings", "Unicommerce Facility"]:
		exists = frappe.db.exists("DocType", dt)
		mod = frappe.db.get_value("DocType", dt, "module") if exists else None
		print(f"  {dt}: exists={bool(exists)}, module={mod}")

	print("\n--- Item custom field ---")
	meta = frappe.get_meta("Item")
	f = meta.get_field("channel_item_codes")
	if f:
		print(f"  Item.channel_item_codes: {f.fieldtype} → options={f.options}")
	else:
		print("  Item.channel_item_codes: MISSING")


def install_custom_fields():
	"""
	Manually load the Custom Field fixture. `bench migrate` only auto-imports
	fixtures on first install of an app; subsequent migrations don't re-import,
	so this helper is needed when adding new custom fields mid-lifecycle.
	"""
	import json
	import os

	from frappe.custom.doctype.custom_field.custom_field import create_custom_field

	fixture_path = os.path.join(
		frappe.get_app_path("adilqadri"), "adilqadri", "fixtures", "custom_field.json"
	)
	with open(fixture_path) as f:
		entries = json.load(f)

	for entry in entries:
		entry = dict(entry)
		dt = entry.pop("dt")
		entry.pop("doctype", None)
		fieldname = entry["fieldname"]
		existing = frappe.db.get_value(
			"Custom Field", {"dt": dt, "fieldname": fieldname}, "name"
		)
		if existing:
			doc = frappe.get_doc("Custom Field", existing)
			for k, v in entry.items():
				setattr(doc, k, v)
			doc.save(ignore_permissions=True)
			print(f"updated: {dt}.{fieldname}")
		else:
			create_custom_field(dt, entry)
			print(f"created: {dt}.{fieldname}")

	frappe.db.commit()
	frappe.clear_cache(doctype="Item")
	print("Item cache cleared.")


def setup_and_test():
	"""Run the full sequence. The password MUST be configured by the caller."""
	verify_schema()
	seed_sales_channels()
	seed_connector_settings()
	live_test()
	return "done"
