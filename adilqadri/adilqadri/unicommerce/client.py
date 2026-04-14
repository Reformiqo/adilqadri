"""
Thin REST client for Unicommerce (Uniware) APIs.

Usage:
    from adilqadri.adilqadri.unicommerce.client import UniwareClient
    client = UniwareClient()  # reads Uniware Connector Settings
    resp = client.post("/services/rest/v1/oms/saleOrder/search", json={"searchOptions": {}})

All calls go through a single session; facility code is sent in the 'Facility' header when provided.
"""

import frappe
import requests

from adilqadri.adilqadri.unicommerce.auth import get_access_token

REQUEST_TIMEOUT = 60


class UniwareAPIError(Exception):
	def __init__(self, message: str, status_code: int = 0, payload=None):
		super().__init__(message)
		self.status_code = status_code
		self.payload = payload


class UniwareClient:
	def __init__(self, facility_code: str | None = None):
		self.settings = frappe.get_single("Uniware Connector Settings")
		self.base_url = self.settings.tenant_url.rstrip("/")
		self.facility_code = facility_code or self._default_facility_code()

	def _default_facility_code(self) -> str | None:
		for row in self.settings.get("facilities") or []:
			if row.is_default:
				return row.facility_code
		rows = self.settings.get("facilities") or []
		return rows[0].facility_code if rows else None

	def _headers(self, extra: dict | None = None) -> dict:
		headers = {
			"Authorization": f"Bearer {get_access_token()}",
			"Content-Type": "application/json",
			"Accept": "application/json",
		}
		if self.facility_code:
			headers["Facility"] = self.facility_code
		if extra:
			headers.update(extra)
		return headers

	def request(self, method: str, path: str, *, params=None, json=None, headers=None) -> dict:
		url = path if path.startswith("http") else f"{self.base_url}{path}"
		try:
			resp = requests.request(
				method=method.upper(),
				url=url,
				params=params,
				json=json,
				headers=self._headers(headers),
				timeout=REQUEST_TIMEOUT,
			)
		except requests.RequestException as e:
			raise UniwareAPIError(f"HTTP error calling {url}: {e}") from e

		if resp.status_code >= 400:
			raise UniwareAPIError(
				f"{method} {path} → HTTP {resp.status_code}: {resp.text[:500]}",
				status_code=resp.status_code,
				payload=_safe_json(resp),
			)

		data = _safe_json(resp)
		if isinstance(data, dict) and data.get("successful") is False:
			raise UniwareAPIError(
				f"Uniware reported failure: {data.get('errors') or data.get('message')}",
				status_code=resp.status_code,
				payload=data,
			)
		return data

	def get(self, path: str, **kwargs) -> dict:
		return self.request("GET", path, **kwargs)

	def post(self, path: str, **kwargs) -> dict:
		return self.request("POST", path, **kwargs)


def _safe_json(resp):
	try:
		return resp.json()
	except ValueError:
		return {"raw": resp.text}
