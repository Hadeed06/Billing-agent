"""
Resolves the per-customer API key used by the Clinical API and the
Billing-Agent/Log endpoint.

POST {PRACTICE_EHR_AUTH_BASE_URL}/v1/Clients/AuthClients
Headers: Authorization: Bearer <frontend_token>, Content-Type: application/json
Body:    {"Filter": " A.SEQ_NUM IN (<customer_id>) "}

Success response shape:
  {
    "succeeded": true,
    "data": [ { ..., "apiKey": "...", ... } ],
    ...
  }

Returns just the apiKey string. Raises AuthApiError on any failure.
"""
import logging
import os

import httpx

from src.services.http_client import get_http_client, request_timeout

logger = logging.getLogger(__name__)


class AuthApiError(Exception):
    """Raised when the Auth API didn't give us a usable API key."""
    pass


async def fetch_api_key_for_customer(customer_id: str, auth_token: str) -> str:
    if not customer_id:
        raise AuthApiError("customer_id is required")
    if not auth_token:
        raise AuthApiError("Missing auth token")

    base_url = os.getenv("PRACTICE_EHR_AUTH_BASE_URL", "").rstrip("/")
    if not base_url or base_url == "REPLACE_ME":
        raise AuthApiError("Auth API is not configured (PRACTICE_EHR_AUTH_BASE_URL)")

    url = f"{base_url}/v1/Clients/AuthClients"
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json",
    }
    body = {"Filter": f" A.SEQ_NUM IN ({customer_id}) "}

    logger.info(f"🔑 Fetching per-customer API key for customer_id={customer_id}")

    try:
        client = get_http_client()
        resp = await client.post(url, json=body, headers=headers, timeout=request_timeout(read=15))
    except Exception as e:
        logger.exception(f"Auth API request exception: {e}")
        raise AuthApiError("Auth API unreachable")

    if resp.status_code != 200:
        logger.error(f"Auth API {resp.status_code}: {resp.text[:500]}")
        raise AuthApiError(f"Auth API returned {resp.status_code}")

    try:
        body_json = resp.json()
    except Exception as e:
        logger.error(f"Auth API returned non-JSON: {e}")
        raise AuthApiError("Auth API returned invalid JSON")

    if not body_json.get("succeeded"):
        msg = body_json.get("message") or "Auth API reported failure"
        raise AuthApiError(msg)

    data = body_json.get("data")
    if not isinstance(data, list) or not data:
        raise AuthApiError(f"No AuthClients row found for customer_id={customer_id}")

    api_key = (data[0] or {}).get("apiKey")
    if not api_key:
        raise AuthApiError(f"AuthClients row for customer_id={customer_id} has no apiKey")

    logger.info(f"✅ Resolved API key for customer_id={customer_id} (length={len(api_key)})")
    return api_key
