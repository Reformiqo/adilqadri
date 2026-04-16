"""
One-shot helpers for configuring, inspecting, and live-testing the Uniware
integration from a bench execute call. Not imported by runtime code.

Usage:
    bench --site <site> execute adilqadri.adilqadri.scripts.setup_and_test
    bench --site <site> execute adilqadri.adilqadri.scripts.probe_order
"""

import json

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
	"""Manually load Custom Field fixture. Needed when adding fields after install."""
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
	frappe.clear_cache(doctype="Sales Order")
	frappe.clear_cache(doctype="Sales Order Item")
	print("Caches cleared.")


def probe_order():
	"""
	Fetch ONE real sale order via the search endpoint and dump its full
	structure so we know exactly what fields to map. Run this BEFORE writing
	order_pull.py so we build against real data, not guesswork.
	"""
	from adilqadri.adilqadri.unicommerce.auth import get_access_token
	import requests

	settings = frappe.get_single("Uniware Connector Settings")
	tenant = settings.tenant_url.rstrip("/")
	token = get_access_token()
	headers = {
		"Authorization": f"Bearer {token}",
		"Content-Type": "application/json",
	}

	# Step 1: search for recent orders, tiny page
	print("=== STEP 1: saleOrder/search (last 10 mins, first 2 results) ===")
	r = requests.post(
		f"{tenant}/services/rest/v1/oms/saleOrder/search",
		json={
			"updatedSinceInMinutes": 10,
			"searchOptions": {"displayStart": 0, "displayLength": 2, "getCount": True},
		},
		headers=headers,
		timeout=30,
	)
	print(f"HTTP {r.status_code}")
	search_data = r.json()
	total = search_data.get("totalRecords")
	elements = search_data.get("elements") or []
	print(f"totalRecords in last 10 min: {total}")
	print(f"returned: {len(elements)} order summary rows")
	if elements:
		print("first summary keys:", sorted(elements[0].keys()))
		print("first summary sample:")
		print(json.dumps(elements[0], indent=2, default=str)[:1500])

	if not elements:
		print("No recent orders — widening to 60 minutes")
		r = requests.post(
			f"{tenant}/services/rest/v1/oms/saleOrder/search",
			json={
				"updatedSinceInMinutes": 60,
				"searchOptions": {"displayStart": 0, "displayLength": 2, "getCount": True},
			},
			headers=headers,
			timeout=30,
		)
		search_data = r.json()
		elements = search_data.get("elements") or []
		print(f"returned: {len(elements)} order summary rows")

	if not elements:
		print("Still no orders — aborting probe.")
		return

	sample_code = elements[0].get("code")
	print(f"\n=== STEP 2: saleOrder/get with code={sample_code} ===")
	r = requests.post(
		f"{tenant}/services/rest/v1/oms/saleorder/get",
		json={"code": sample_code},
		headers=headers,
		timeout=30,
	)
	print(f"HTTP {r.status_code}")
	detail = r.json()
	if detail.get("successful"):
		dto = detail.get("saleOrderDTO") or detail.get("saleOrder") or {}
		print("saleOrderDTO top-level keys:", sorted(dto.keys()))
		# Redact PII but keep structure
		print("\n--- full DTO (first 4000 chars, PII-scrubbed) ---")
		scrubbed = _scrub_pii(dto)
		print(json.dumps(scrubbed, indent=2, default=str)[:4000])
	else:
		print("ERROR:", detail.get("errors"))


def _scrub_pii(obj):
	"""Recursively mask obvious PII fields so they don't leak in logs."""
	if isinstance(obj, dict):
		return {
			k: (
				"***"
				if k.lower()
				in {"name", "phone", "mobile", "email", "pincode", "addressline1", "addressline2", "city", "gstin"}
				and isinstance(v, str)
				else _scrub_pii(v)
			)
			for k, v in obj.items()
		}
	if isinstance(obj, list):
		return [_scrub_pii(v) for v in obj]
	return obj


def create_demo_mapping():
	"""
	Create ONE Channel Item Code mapping using the first ERPNext item and
	a known-recent Uniware channel+SKU, then re-run the dry-run so we can
	see the resolver return "created" instead of errors.

	This proves the end-to-end flow works. Real mappings must be entered
	by the data team or imported from a CSV.
	"""
	# Pick the first real ERPNext item (not a service)
	item_code = frappe.db.get_value(
		"Item",
		{"disabled": 0, "is_sales_item": 1},
		"name",
		order_by="creation asc",
	)
	if not item_code:
		print("No ERPNext item found to use for the demo mapping.")
		return

	# Pick the first unresolved Uniware order and use its first line
	from adilqadri.adilqadri.unicommerce.auth import get_access_token
	import requests

	settings = frappe.get_single("Uniware Connector Settings")
	tenant = settings.tenant_url.rstrip("/")
	headers = {
		"Authorization": f"Bearer {get_access_token()}",
		"Content-Type": "application/json",
	}
	search = requests.post(
		f"{tenant}/services/rest/v1/oms/saleOrder/search",
		json={"updatedSinceInMinutes": 60, "searchOptions": {"displayStart": 0, "displayLength": 1}},
		headers=headers,
		timeout=30,
	).json()
	elements = search.get("elements") or []
	if not elements:
		print("No recent Uniware orders to borrow a SKU from.")
		return

	order_code = elements[0]["code"]
	detail = requests.post(
		f"{tenant}/services/rest/v1/oms/saleorder/get",
		json={"code": order_code},
		headers=headers,
		timeout=30,
	).json()
	dto = detail.get("saleOrderDTO") or {}
	line = (dto.get("saleOrderItems") or [None])[0]
	if not line:
		print("First order has no line items.")
		return

	channel = dto.get("channel")
	channel_product_code = line.get("sellerSkuCode") or line.get("channelProductId")
	if not channel or not channel_product_code:
		print(f"Missing data: channel={channel}, code={channel_product_code}")
		return

	# Ensure the Sales Channel exists
	if not frappe.db.exists("Sales Channel", channel):
		ch = frappe.new_doc("Sales Channel")
		ch.channel_code = channel
		ch.channel_name = channel.replace("_", " ").title()
		ch.platform = "Other"
		ch.is_active = 1
		ch.insert(ignore_permissions=True)
		print(f"Auto-created Sales Channel: {channel}")

	# Add a Channel Item Code row to the ERPNext item
	item = frappe.get_doc("Item", item_code)
	existing = [r for r in (item.get("channel_item_codes") or []) if r.channel == channel and r.channel_product_code == channel_product_code]
	if existing:
		print(f"Mapping already exists: {item_code} → ({channel}, {channel_product_code})")
	else:
		item.append(
			"channel_item_codes",
			{
				"channel": channel,
				"channel_product_code": channel_product_code,
				"is_active": 1,
				"remarks": "Demo mapping created by scripts.create_demo_mapping",
			},
		)
		item.flags.ignore_permissions = True
		item.flags.ignore_mandatory = True
		item.save(ignore_permissions=True)
		frappe.db.commit()
		print(f"Created mapping: {item_code} → ({channel}, {channel_product_code})")
	return {"item_code": item_code, "channel": channel, "channel_product_code": channel_product_code}


def prime_mappings_from_recent_orders(count: int = 5):
	"""
	Fetch `count` most recent Uniware orders and create Channel Item Code
	mappings for each of their line items, borrowing unused ERPNext items
	(one ERPNext item per distinct (channel, channel_product_code) key).

	This lets us prove the end-to-end pull works with real order data
	without requiring the user to hand-enter mappings.
	"""
	from adilqadri.adilqadri.unicommerce.auth import get_access_token
	import requests

	count = int(count)
	settings = frappe.get_single("Uniware Connector Settings")
	tenant = settings.tenant_url.rstrip("/")
	headers = {
		"Authorization": f"Bearer {get_access_token()}",
		"Content-Type": "application/json",
	}

	search = requests.post(
		f"{tenant}/services/rest/v1/oms/saleOrder/search",
		json={
			"updatedSinceInMinutes": 60,
			"searchOptions": {"displayStart": 0, "displayLength": count},
		},
		headers=headers,
		timeout=30,
	).json()
	elements = search.get("elements") or []
	print(f"Fetched {len(elements)} recent orders for mapping priming")

	# Collect distinct (channel, product_code) pairs
	pairs: list[tuple[str, str, str]] = []  # (channel, product_code, line_name)
	seen = set()
	for summ in elements:
		order_code = summ["code"]
		detail = requests.post(
			f"{tenant}/services/rest/v1/oms/saleorder/get",
			json={"code": order_code},
			headers=headers,
			timeout=30,
		).json()
		dto = detail.get("saleOrderDTO") or {}
		channel = dto.get("channel")
		for li in dto.get("saleOrderItems") or []:
			product_code = li.get("sellerSkuCode") or li.get("channelProductId")
			if not channel or not product_code:
				continue
			key = (channel, product_code)
			if key in seen:
				continue
			seen.add(key)
			pairs.append((channel, product_code, li.get("itemName") or ""))

	print(f"Distinct (channel, product_code) pairs: {len(pairs)}")

	# Get ERPNext items to borrow — exclude templates (has_variants=1) because
	# Sales Order lines reject them; require concrete items/variants only.
	erp_items = frappe.get_all(
		"Item",
		filters={"disabled": 0, "is_sales_item": 1, "has_variants": 0},
		fields=["name", "item_name"],
		order_by="creation asc",
		limit=max(len(pairs) * 2, 20),
	)
	if not erp_items:
		print("No ERPNext items to borrow.")
		return

	created = 0
	skipped = 0
	for idx, (channel, product_code, item_name) in enumerate(pairs):
		erp_item_code = erp_items[idx % len(erp_items)]["name"]

		# Ensure Sales Channel exists
		if not frappe.db.exists("Sales Channel", channel):
			ch = frappe.new_doc("Sales Channel")
			ch.channel_code = channel
			ch.channel_name = channel.replace("_", " ").title()
			ch.platform = "Other"
			ch.is_active = 1
			ch.insert(ignore_permissions=True)

		# Skip if mapping already exists on any item
		existing = frappe.db.get_value(
			"Channel Item Code",
			{
				"parenttype": "Item",
				"parentfield": "channel_item_codes",
				"channel": channel,
				"channel_product_code": product_code,
			},
			"parent",
		)
		if existing:
			skipped += 1
			print(f"  skip: ({channel}, {product_code}) already mapped to {existing}")
			continue

		item = frappe.get_doc("Item", erp_item_code)
		item.append(
			"channel_item_codes",
			{
				"channel": channel,
				"channel_product_code": product_code,
				"is_active": 1,
				"remarks": f"Auto-primed from Uniware: {item_name}"[:140],
			},
		)
		item.flags.ignore_permissions = True
		item.flags.ignore_mandatory = True
		item.save(ignore_permissions=True)
		created += 1
		print(f"  map: ({channel}, {product_code}) → {erp_item_code}")

	frappe.db.commit()
	print(f"\nDone. created={created}, skipped={skipped}, total={len(pairs)}")
	return {"created": created, "skipped": skipped, "total": len(pairs)}


def cleanup_test_invoices_and_items():
	"""Delete all test Sales Invoices (with uniware_order_code) and auto-created
	Items (numeric 617571* codes created by auto_create_items)."""
	# Delete Sales Invoices with Uniware codes
	invoices = frappe.get_all(
		"Sales Invoice",
		filters={"uniware_order_code": ["is", "set"]},
		pluck="name",
	)
	for name in invoices:
		frappe.delete_doc("Sales Invoice", name, force=True, ignore_permissions=True)
	print(f"Deleted {len(invoices)} Sales Invoices")

	# Delete auto-created Items (numeric codes starting with 617571)
	items = frappe.get_all(
		"Item",
		filters={"name": ["like", "617571%"]},
		pluck="name",
	)
	for name in items:
		try:
			frappe.delete_doc("Item", name, force=True, ignore_permissions=True)
		except Exception as e:
			print(f"  skip {name}: {e}")
	print(f"Deleted {len(items)} auto-created Items")

	frappe.db.commit()
	print("Done.")


def cleanup_demo_data():
	"""Delete all demo/prime/auto-mapped Channel Item Code rows that were
	created during testing. These are random item-to-channel pairings and
	must NOT be used for real sync — they can cause wrong data pushes."""
	deleted = frappe.db.sql(
		"""
		DELETE FROM `tabChannel Item Code`
		WHERE remarks LIKE %s
		   OR remarks LIKE %s
		   OR remarks LIKE %s
		""",
		("%Auto-primed%", "%Demo mapping%", "%Auto-mapped%"),
	)
	frappe.db.commit()
	remaining = frappe.db.count("Channel Item Code")
	print(f"Cleanup done. Remaining Channel Item Code rows: {remaining}")
	return {"remaining": remaining}


def probe_gofrugal():
	"""Probe GoFrugal RayMedi HQ auth methods and find working API access."""
	import requests

	BASE = "https://aqhq.gofrugal.com/RayMedi_HQ"
	USER = "Accounts"
	PASS = "Adil@7861"

	# Try 1: Form-based login + session cookies
	print("=== Try 1: POST login form ===")
	s = requests.Session()
	r = s.post(
		BASE + "/login.do",
		data={"userName": USER, "password": PASS},
		allow_redirects=False,
		timeout=30,
	)
	loc = r.headers.get("location", "-")
	print(f"  HTTP {r.status_code}  Location: {loc}")
	cookies = dict(s.cookies)
	print(f"  Cookies: {list(cookies.keys())}")
	if r.status_code in (200, 302):
		r2 = s.get(BASE + "/api/v1/items", timeout=30)
		ct = r2.headers.get("content-type", "?")
		print(f"  API v1/items with session: HTTP {r2.status_code} CT: {ct}")
		if r2.status_code != 401:
			print(f"  Body preview: {r2.text[:500]}")
		else:
			print("  Still 401")

	# Try 2: JSON login
	print("\n=== Try 2: POST JSON /api/v1/login ===")
	r = requests.post(
		BASE + "/api/v1/login",
		json={"userName": USER, "password": PASS},
		headers={"Content-Type": "application/json"},
		timeout=30,
	)
	print(f"  HTTP {r.status_code}")
	print(f"  Body: {r.text[:400]}")

	# Try 3: /api/v1/auth
	print("\n=== Try 3: POST /api/v1/auth ===")
	r = requests.post(
		BASE + "/api/v1/auth",
		json={"userName": USER, "password": PASS},
		timeout=30,
	)
	print(f"  HTTP {r.status_code}")
	print(f"  Body: {r.text[:400]}")

	# Try 4: /api/v2/login
	print("\n=== Try 4: POST /api/v2/login ===")
	r = requests.post(
		BASE + "/api/v2/login",
		json={"userName": USER, "password": PASS},
		timeout=30,
	)
	print(f"  HTTP {r.status_code}")
	print(f"  Body: {r.text[:400]}")

	# Try 5: session-based v2 after form login
	print("\n=== Try 5: Form login then v2/items with session ===")
	s2 = requests.Session()
	s2.post(BASE + "/login.do", data={"userName": USER, "password": PASS}, timeout=30)
	r = s2.get(BASE + "/api/v2/items", timeout=30)
	ct = r.headers.get("content-type", "?")
	print(f"  HTTP {r.status_code} CT: {ct}")
	if "json" in ct:
		print(f"  Body: {r.text[:500]}")
	elif r.status_code != 401:
		print(f"  Body preview: {r.text[:300]}")
	else:
		print("  Still 401")

	# Try 5b: Re-check login — maybe field names are different
	print("\n=== Try 5b: Login with different field names ===")
	for fields in [
		{"userName": USER, "password": PASS},
		{"username": USER, "password": PASS},
		{"user": USER, "pwd": PASS},
		{"loginId": USER, "loginPwd": PASS},
	]:
		s_test = requests.Session()
		r = s_test.post(BASE + "/login.do", data=fields, timeout=15)
		# Check if login succeeded by trying an API call
		r2 = s_test.get(BASE + "/api/v1/sales", timeout=15)
		success = r2.status_code != 401
		print(f"  fields={list(fields.keys())} -> login={r.status_code}, api={r2.status_code} {'SUCCESS!' if success else ''}")
		if success:
			print(f"  API body: {r2.text[:500]}")
			break

	# Try 5c: Fetch the API key pages (they returned 200)
	print("\n=== Try 5c: API key pages content ===")
	s_login = requests.Session()
	s_login.post(BASE + "/login.do", data={"userName": USER, "password": PASS}, timeout=15)
	for path in ["/generateApiKey.do", "/settings/apiKey.do"]:
		r = s_login.get(BASE + path, timeout=15)
		body = r.text
		# Look for anything that looks like a key/token
		import re
		tokens = re.findall(r'[a-f0-9]{20,}|[A-Za-z0-9_\-]{20,}', body)
		print(f"\n  {path}: HTTP {r.status_code}, body length={len(body)}")
		if tokens:
			print(f"  Potential tokens found: {tokens[:5]}")
		# Also look for key/token in input fields
		inputs = re.findall(r'<input[^>]*value=["\']([^"\']{10,})["\']', body)
		if inputs:
			print(f"  Input field values: {inputs[:5]}")
		# Print first 600 chars for manual inspection
		print(f"  Body preview: {body[:600]}")

	# Try 6: token generation endpoints
	print("\n=== Try 6: Token generation endpoints ===")
	for path in ["/api/v1/token", "/api/v2/token", "/generateApiKey.do",
	             "/api/v1/generateToken", "/api/v2/generateToken",
	             "/api/generateAccessToken", "/settings/apiKey.do"]:
		r = s2.get(BASE + path, timeout=15)
		print(f"  {path} -> HTTP {r.status_code}")
		if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
			print(f"    Body: {r.text[:300]}")

	# Try 7: Check what the login.do response body contains
	print("\n=== Try 7: Login response body ===")
	s3 = requests.Session()
	r = s3.post(BASE + "/login.do",
	            data={"userName": USER, "password": PASS}, timeout=30)
	body = r.text[:1000]
	if "token" in body.lower() or "key" in body.lower() or "api" in body.lower():
		print(f"  Found interesting keywords in login response!")
		print(f"  Body: {body}")
	else:
		print(f"  No token/key/api keywords. Body length: {len(r.text)} chars")
		print(f"  First 300 chars: {body[:300]}")

	# Try 8: sales related endpoints with session
	print("\n=== Try 8: Sales endpoints ===")
	for path in ["/api/v1/salesBills", "/api/v1/sales", "/api/v2/sales",
	             "/api/v1/transactions", "/api/v2/transactions",
	             "/api/v1/reports/sales", "/api/v2/reports/sales",
	             "/api/v1/billDetails", "/api/v2/billDetails"]:
		r = s3.get(BASE + path, timeout=15)
		print(f"  {path} -> {r.status_code}")


def setup_and_test():
	verify_schema()
	seed_sales_channels()
	seed_connector_settings()
	live_test()
	return "done"
