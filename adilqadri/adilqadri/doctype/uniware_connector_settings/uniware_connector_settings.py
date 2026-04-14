import frappe
import requests
from frappe.model.document import Document


class UniwareConnectorSettings(Document):
	def validate(self):
		if self.tenant_url:
			self.tenant_url = self.tenant_url.rstrip("/")

	@frappe.whitelist()
	def test_oauth(self):
		"""Fetch a fresh OAuth2 token and persist it. Returns a summary dict."""
		from adilqadri.adilqadri.unicommerce.auth import UnicommerceAuthError, fetch_new_token

		try:
			data = fetch_new_token(self)
		except UnicommerceAuthError as e:
			return {"ok": False, "error": str(e)}
		except Exception as e:
			return {"ok": False, "error": f"{type(e).__name__}: {e}"}

		return {
			"ok": True,
			"token_type": data.get("token_type"),
			"expires_in": data.get("expires_in"),
			"scope": data.get("scope"),
		}

	@frappe.whitelist()
	def test_rest_call(self):
		"""Call a real REST endpoint to verify end-to-end auth + entitlement."""
		from adilqadri.adilqadri.unicommerce.auth import UnicommerceAuthError, get_access_token

		endpoint = "/services/rest/v1/catalog/itemType/get"
		url = f"{self.tenant_url.rstrip('/')}{endpoint}"

		try:
			token = get_access_token()
		except UnicommerceAuthError as e:
			return {"ok": False, "endpoint": endpoint, "status_code": 0, "diagnosis": f"Auth failed: {e}"}

		try:
			resp = requests.post(
				url,
				json={"skuCode": "__ADILQADRI_PROBE__"},
				headers={
					"Authorization": f"Bearer {token}",
					"Content-Type": "application/json",
				},
				timeout=30,
			)
		except requests.RequestException as e:
			return {"ok": False, "endpoint": endpoint, "status_code": 0, "diagnosis": f"Network error: {e}"}

		content_type = resp.headers.get("content-type", "")
		preview = (resp.text or "")[:500]
		result = {
			"endpoint": endpoint,
			"status_code": resp.status_code,
			"content_type": content_type,
			"preview": preview,
			"unirequestid": resp.headers.get("unirequestid"),
		}

		if "application/json" in content_type:
			try:
				data = resp.json()
			except ValueError:
				result["ok"] = False
				result["diagnosis"] = "Content-Type said JSON but body was not parseable"
				return result

			result["json_keys"] = sorted(list(data.keys())) if isinstance(data, dict) else []
			if resp.status_code == 200 and isinstance(data, dict):
				errors = data.get("errors") or []
				if data.get("successful"):
					result["ok"] = True
					result["diagnosis"] = "REST API reachable — successful response."
				elif errors and errors[0].get("message") in {"INVALID_ITEM_TYPE"}:
					result["ok"] = True
					result["diagnosis"] = (
						"REST API reachable — probe SKU not found (expected). "
						"Auth and permissions are working."
					)
				else:
					result["ok"] = False
					result["diagnosis"] = f"Uniware API error: {errors}"
			else:
				result["ok"] = False
				result["diagnosis"] = f"HTTP {resp.status_code} — body: {preview[:200]}"
			return result

		if "text/html" in content_type and resp.status_code == 403:
			result["ok"] = False
			result["diagnosis"] = (
				"403 HTML Access Denied. The bearer token was accepted at /oauth/token but this "
				"REST endpoint was blocked at Unicommerce's application-layer whitelist. "
				"Root cause: this server's outbound IP is NOT in Unicommerce's IP whitelist "
				"for this tenant. Contact your Unicommerce Account Manager with your bench's "
				"outbound IP (run: curl https://api.ipify.org) and ask them to whitelist it."
			)
			return result

		result["ok"] = False
		result["diagnosis"] = f"Unexpected HTTP {resp.status_code} with content-type {content_type}"
		return result
