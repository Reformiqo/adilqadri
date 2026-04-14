"""
Unicommerce (Uniware) OAuth2 token handling.

Verified against live tenant 2026-04-14:
  GET {tenant}/oauth/token
    ?grant_type=password
    &client_id=my-trusted-client
    &username=<email>
    &password=<password>
  Header: Content-Type: application/json

Response:
  { access_token, token_type, refresh_token, expires_in, scope }
  expires_in is ~43200 (12h); refresh token ~30 days.

Refresh grant:
  GET {tenant}/oauth/token?grant_type=refresh_token&client_id=my-trusted-client&refresh_token=<rt>
"""

from datetime import timedelta

import frappe
import requests
from frappe.utils import get_datetime, now_datetime

TOKEN_PATH = "/oauth/token"
REFRESH_SAFETY_SECONDS = 120
REQUEST_TIMEOUT = 30


class UnicommerceAuthError(Exception):
	pass


def _token_url(settings) -> str:
	return f"{settings.tenant_url.rstrip('/')}{TOKEN_PATH}"


def _do_token_request(url: str, params: dict, headers: dict) -> dict:
	try:
		resp = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
	except requests.RequestException as e:
		raise UnicommerceAuthError(f"Token request failed: {e}") from e

	if resp.status_code != 200:
		raise UnicommerceAuthError(
			f"Token request returned HTTP {resp.status_code}: {resp.text[:500]}"
		)

	try:
		data = resp.json()
	except ValueError as e:
		raise UnicommerceAuthError(f"Token response was not JSON: {resp.text[:500]}") from e

	if "access_token" not in data:
		raise UnicommerceAuthError(f"Token response missing access_token: {data}")

	return data


def fetch_new_token(settings) -> dict:
	"""Password grant — initial token fetch."""
	password = settings.get_password("password")
	if not password:
		raise UnicommerceAuthError("API password is not set in Uniware Connector Settings")

	data = _do_token_request(
		url=_token_url(settings),
		params={
			"grant_type": "password",
			"client_id": settings.client_id or "my-trusted-client",
			"username": settings.username,
			"password": password,
		},
		headers={"Content-Type": "application/json"},
	)
	_persist_token(settings, data)
	return data


def refresh_token(settings) -> dict:
	"""Refresh grant — cheaper than re-authenticating."""
	rt = settings.get_password("refresh_token")
	if not rt:
		raise UnicommerceAuthError("No refresh_token stored; call fetch_new_token instead")

	data = _do_token_request(
		url=_token_url(settings),
		params={
			"grant_type": "refresh_token",
			"client_id": settings.client_id or "my-trusted-client",
			"refresh_token": rt,
		},
		headers={"Content-Type": "application/json"},
	)
	_persist_token(settings, data)
	return data


def _persist_token(settings, data: dict) -> None:
	expires_in = int(data.get("expires_in") or 0)
	now = now_datetime()
	settings.access_token = data.get("access_token")
	settings.token_expires_at = now + timedelta(seconds=expires_in)
	if data.get("refresh_token"):
		settings.refresh_token = data["refresh_token"]
		settings.refresh_expires_at = now + timedelta(days=30)
	settings.save(ignore_permissions=True)
	frappe.db.commit()


def get_access_token() -> str:
	"""
	Return a valid access token, refreshing or re-fetching as needed.
	This is the main entrypoint used by the REST client.
	"""
	settings = frappe.get_single("Uniware Connector Settings")
	if not settings.enabled:
		raise UnicommerceAuthError("Unicommerce integration is disabled")

	cached = settings.get_password("access_token", raise_exception=False)
	expires_at = get_datetime(settings.token_expires_at) if settings.token_expires_at else None
	if cached and expires_at and (expires_at - now_datetime()).total_seconds() > REFRESH_SAFETY_SECONDS:
		return cached

	refresh_expires_at = (
		get_datetime(settings.refresh_expires_at) if settings.refresh_expires_at else None
	)
	rt = settings.get_password("refresh_token", raise_exception=False)
	if rt and refresh_expires_at and refresh_expires_at > now_datetime():
		try:
			return refresh_token(settings)["access_token"]
		except UnicommerceAuthError:
			pass

	return fetch_new_token(settings)["access_token"]
