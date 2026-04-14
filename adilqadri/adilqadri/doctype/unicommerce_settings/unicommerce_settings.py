import frappe
import requests
from frappe.model.document import Document


class UnicommerceSettings(Document):
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
		"""
		Call a known REST endpoint to verify end-to-end auth + entitlement.

		Returns a diagnostic dict the UI can render. If the response is HTML
		(the classic Access Denied page), we explicitly flag that the tenant
		likely needs REST API enablement from Unicommerce support.
		"""
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
				json={"skuCode": "__TEST__"},
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
		}

		if "application/json" in content_type:
			try:
				data = resp.json()
			except ValueError:
				result["ok"] = False
				result["diagnosis"] = "Content-Type said JSON but body was not parseable"
				return result

			result["ok"] = bool(
				resp.status_code < 400 and (data.get("successful") is None or data.get("successful"))
			)
			result["json_keys"] = sorted(list(data.keys())) if isinstance(data, dict) else []
			if not result["ok"]:
				errors = data.get("errors") if isinstance(data, dict) else None
				result["diagnosis"] = (
					f"Uniware API error: {errors}" if errors else f"HTTP {resp.status_code}"
				)
			return result

		if "text/html" in content_type and resp.status_code == 403:
			result["ok"] = False
			result["diagnosis"] = (
				"403 HTML Access Denied. Bearer token was accepted for /oauth/token but this REST "
				"endpoint routed to the web UI (JSESSIONID set). Root cause: REST API access is not "
				"enabled on this Unicommerce tenant. Raise a ticket with Unicommerce support asking "
				"them to enable REST API on tenant "
				f"{self.tenant_url}."
			)
			return result

		result["ok"] = False
		result["diagnosis"] = f"Unexpected HTTP {resp.status_code} with content-type {content_type}"
		return result
