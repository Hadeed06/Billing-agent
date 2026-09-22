"""
Client for the Clinical API: fetches visit data for a given visit_id.

GET {CLINICAL_API_BASE_URL}/v1/Clinical/Billing-Agent/Visit/{visit_id}
Headers:
  Authorization: Bearer <frontend_token>
  APIKey:        <per-customer api key, resolved via PracticeEHR Auth API>

Returns a normalized dict shaped like the prompt placeholders:
  {tax_id, npi, member_id, dob, member_name, dos, plan_short_name, plan_description}

Raises ClinicalApiError with a human-readable message on any failure.
"""
import logging
import os

import httpx

from src.services.http_client import get_http_client, request_timeout

logger = logging.getLogger(__name__)


class ClinicalApiError(Exception):
    """Raised when the Clinical API returned something we can't use."""
    pass


def _strip_time(value):
    """Turn '02/21/2023 00:00:00' → '02/21/2023'. Leaves other formats alone."""
    if not value or not isinstance(value, str):
        return value
    return value.split(" ", 1)[0].strip() if " " in value else value.strip()


def _normalize(api_data: dict) -> dict:
    """Map Clinical API field names to prompt placeholder keys."""
    return {
        "tax_id":            api_data.get("taxIdNum"),
        "npi":               api_data.get("npi"),
        "member_id":         api_data.get("memberId"),
        "dob":               _strip_time(api_data.get("dob")),
        "member_name":       api_data.get("memberName"),
        "dos":               _strip_time(api_data.get("dos")),
        # payer_id is the authoritative routing key — insurance is resolved
        # from this, NOT from the plan name (names/descriptions change over
        # time in the billing DB; payer IDs are stable).
        "payer_id":          api_data.get("payerId"),
        # plan_short_name / plan_description are kept for logging and future
        # display purposes but must NOT be used for routing decisions.
        "plan_short_name":   api_data.get("planShortName"),
        "plan_description":  api_data.get("planDescription"),
        # INS_CHARGE_AMT from the WEB_VN_VISIT_BILLING_AGENT view — the claim's
        # total charge. Used by Anthem/Elevance IVRs to disambiguate multi-claim
        # dates (claims controller speaks it). The API camelCases the column
        # (INS_CHARGE_AMT -> insChargeAmt); accept a few spellings defensively so
        # a naming variance can't silently drop it.
        "charge_amount":     (api_data.get("insChargeAmt")
                              or api_data.get("insChargeAmount")
                              or api_data.get("INS_CHARGE_AMT")
                              or api_data.get("chargeAmount")),
        # Denial follow-up fields. `npi` above is the practice NPI; `rendering_npi`
        # here is the provider's individual NPI — keep them distinct.
        "rendering_npi":     api_data.get("renderingProviderNpi"),
        "provider_name":     api_data.get("renderingProviderName"),
        "practice_name":     api_data.get("practiceName"),
        "practice_address":  api_data.get("practiceAddress"),
        # Rendering provider's OWN office address + zip (added to the view for the
        # denial rep, who asks for it to check in/out-of-network status). When
        # present these are preferred over the practice address; the denial
        # context falls back to practice_address only if the office one is blank.
        "provider_address":  api_data.get("renderingProviderOfficeAddress"),
        "provider_zip":      api_data.get("renderingProviderZipCode"),
        "claim_submit_date": _strip_time(api_data.get("claimSubmitDate")),
    }


async def fetch_visit_data(visit_id: str, auth_token: str, api_key: str) -> dict:
    """Fetch + normalize visit data for a visit_id.

    `api_key` is the per-customer key resolved earlier from
    /v1/Clients/AuthClients. Raises ClinicalApiError on any failure so the
    caller can use the message directly in the frontend response.
    """
    if not visit_id:
        raise ClinicalApiError("visit_id is required")
    if not auth_token:
        raise ClinicalApiError("Missing auth token")
    if not api_key:
        raise ClinicalApiError("Missing API key for Clinical call")

    base_url = os.getenv("CLINICAL_API_BASE_URL", "").rstrip("/")
    if not base_url or base_url == "REPLACE_ME":
        raise ClinicalApiError("Clinical API is not configured (CLINICAL_API_BASE_URL)")

    url = f"{base_url}/v1/Clinical/Billing-Agent/Visit/{visit_id}"
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "apikey": api_key,
    }

    logger.info(f"🔎 Fetching Clinical visit data for visit_id={visit_id}")

    try:
        client = get_http_client()
        resp = await client.get(url, headers=headers, timeout=request_timeout(read=30))
    except Exception as e:
        logger.exception(f"Clinical API request exception: {e}")
        raise ClinicalApiError("Clinical API unreachable")

    if resp.status_code != 200:
        logger.error(f"Clinical API {resp.status_code}: {resp.text[:500]}")
        raise ClinicalApiError(f"Clinical API returned {resp.status_code}")

    try:
        body = resp.json()
    except Exception as e:
        logger.error(f"Clinical API returned non-JSON: {e}")
        raise ClinicalApiError("Clinical API returned invalid JSON")

    if not body.get("succeeded"):
        msg = body.get("message") or "Clinical API reported failure"
        raise ClinicalApiError(msg)

    api_data = body.get("data")
    if not api_data:
        raise ClinicalApiError(f"No visit data found for visit_id={visit_id}")

    normalized = _normalize(api_data)
    # NOTE: do NOT log PHI (member name, member ID, DOB, DOS) — only the visit
    # identifier, payer_id (used for routing), and the plan name/description
    # for observability.
    logger.info(
        f"✅ Clinical visit data received for visit_id={visit_id} "
        f"payer_id={normalized.get('payer_id')!r} "
        f"plan={normalized.get('plan_short_name')!r} "
        f"description={normalized.get('plan_description')!r}"
    )
    return normalized
