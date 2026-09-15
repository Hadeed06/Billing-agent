"""
POST to v1/Billing-Agent/Log — records the outcome of a completed call
(stored file paths + classified status) to the PracticeEHR billing DB.

Auth: Bearer token only (no API key).
"""
import logging
import os
from typing import Optional

import httpx

from src.services.http_client import get_http_client, request_timeout

logger = logging.getLogger(__name__)


async def post_billing_log(
    *,
    request_type: str,
    transcript_path: str,
    recording_path: str,
    request_status: str,
    claim_status: str,
    description: str,
    visit_seq_num,
    customer_id,
    entered_by: str,
    payer_name: str,
    auth_token: str,
) -> Optional[int]:
    """
    Returns the `data` field from the response on success, or None on failure.
    Never raises — all failures are logged and swallowed.
    """
    base_url = os.getenv("BILLING_AGENT_LOG_BASE_URL", "").rstrip("/")

    if not base_url or base_url == "REPLACE_ME":
        logger.error("BILLING_AGENT_LOG_BASE_URL is not configured — skipping Log call")
        return None
    if not auth_token:
        logger.error("No auth token available for Billing-Agent Log call")
        return None

    url = f"{base_url}/v1/Billing-Agent/Log"
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "requestType": request_type,
        "transcriptPath": transcript_path,
        "recordingPath": recording_path,
        "claimStatus": claim_status,
        "requestStatus": request_status,
        "description": description,
        "visitSeqNum": _as_int(visit_seq_num),
        "customerId": _as_int(customer_id),
        "enteredBy": entered_by,
        "payerName": payer_name,
    }

    logger.info(
        f"📮 POST Billing-Agent/Log requestStatus={request_status!r} claimStatus={claim_status!r} "
        f"payerName={payer_name!r} visit={payload['visitSeqNum']} customer={payload['customerId']}"
    )

    try:
        client = get_http_client()
        resp = await client.post(url, headers=headers, json=payload, timeout=request_timeout(read=60))
    except Exception as e:
        logger.exception(f"❌ Billing-Agent/Log request exception: {e}")
        return None

    if resp.status_code != 200:
        logger.error(f"❌ Billing-Agent/Log failed ({resp.status_code}): {resp.text[:500]}")
        return None

    try:
        body = resp.json()
    except Exception as e:
        logger.error(f"❌ Billing-Agent/Log returned non-JSON: {e}")
        return None

    if not body.get("succeeded"):
        logger.error(f"❌ Billing-Agent/Log returned succeeded=false: {body}")
        return None

    data = body.get("data")
    logger.info(f"✅ Billing-Agent/Log OK: data={data}")
    return data


async def create_billing_log_row(
    *,
    visit_seq_num,
    customer_id,
    payer_name: str,
    auth_token: str,
    request_type: str = "CLAIM_STATUS",
    entered_by: str = "AI-Billing-Agent",
) -> Optional[int]:
    """Initial INSERT at call start. Returns the new row id (RefNo) or None.

    Sends only the identifiers — claimStatus / description / transcriptPath /
    recordingPath are left blank and filled in via PUT once the call ends.
    Never raises; failures are logged so the call flow can continue without
    a RefNo (post_call_upload will fall back to a single POST in that case).
    """
    base_url = os.getenv("BILLING_AGENT_LOG_BASE_URL", "").rstrip("/")
    if not base_url or base_url == "REPLACE_ME":
        logger.error("BILLING_AGENT_LOG_BASE_URL is not configured — skipping initial Log INSERT")
        return None
    if not auth_token:
        logger.error("No auth token for initial Log INSERT")
        return None

    url = f"{base_url}/v1/Billing-Agent/Log"
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "requestType":   request_type,
        "requestStatus": "in_progress",
        "visitSeqNum":   _as_int(visit_seq_num),
        "customerId":    _as_int(customer_id),
        "payerName":     payer_name,
        "enteredBy":     entered_by,
    }

    logger.info(
        f"📮 POST Billing-Agent/Log (INSERT) visit={payload['visitSeqNum']} "
        f"customer={payload['customerId']} payer={payer_name!r}"
    )

    try:
        client = get_http_client()
        resp = await client.post(url, headers=headers, json=payload, timeout=request_timeout(read=30))
    except Exception as e:
        logger.exception(f"❌ Billing-Agent/Log INSERT request exception: {e}")
        return None

    if resp.status_code != 200:
        logger.error(f"❌ Billing-Agent/Log INSERT failed ({resp.status_code}): {resp.text[:500]}")
        return None

    try:
        body = resp.json()
    except Exception as e:
        logger.error(f"❌ Billing-Agent/Log INSERT returned non-JSON: {e}")
        return None

    if not body.get("succeeded"):
        logger.error(f"❌ Billing-Agent/Log INSERT succeeded=false: {body}")
        return None

    data = body.get("data")
    try:
        ref_no = int(data) if data is not None else None
    except (ValueError, TypeError):
        logger.error(f"❌ Billing-Agent/Log INSERT returned non-int data: {data!r}")
        return None

    logger.info(f"✅ Billing-Agent/Log INSERT OK: RefNo={ref_no}")
    return ref_no


async def update_billing_log_row(
    *,
    ref_no: int,
    request_status: str,
    claim_status: str,
    description: str,
    transcript_path: str,
    recording_path: str,
    auth_token: str,
) -> bool:
    """Final PUT at call end. Returns True on success.

    Only sends the 5 updatable fields (the immutable identifiers stay as they
    were at INSERT). Never raises — failures are logged so cleanup can finish.
    """
    base_url = os.getenv("BILLING_AGENT_LOG_BASE_URL", "").rstrip("/")
    if not base_url or base_url == "REPLACE_ME":
        logger.error("BILLING_AGENT_LOG_BASE_URL is not configured — skipping Log UPDATE")
        return False
    if not auth_token:
        logger.error("No auth token for Log UPDATE")
        return False
    if not ref_no:
        logger.error("No RefNo for Log UPDATE")
        return False

    url = f"{base_url}/v1/Billing-Agent/Log/{ref_no}"
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "requestStatus":  request_status,
        "claimStatus":    claim_status,
        "description":    description,
        "transcriptPath": transcript_path,
        "recordingPath":  recording_path,
    }

    logger.info(
        f"📮 PUT Billing-Agent/Log/{ref_no} requestStatus={request_status!r} "
        f"claimStatus={claim_status!r}"
    )

    try:
        client = get_http_client()
        resp = await client.put(url, headers=headers, json=payload, timeout=request_timeout(read=60))
    except Exception as e:
        logger.exception(f"❌ Billing-Agent/Log UPDATE request exception: {e}")
        return False

    if resp.status_code != 200:
        logger.error(f"❌ Billing-Agent/Log UPDATE failed ({resp.status_code}): {resp.text[:500]}")
        return False

    try:
        body = resp.json()
    except Exception as e:
        logger.error(f"❌ Billing-Agent/Log UPDATE returned non-JSON: {e}")
        return False

    if not body.get("succeeded"):
        logger.error(f"❌ Billing-Agent/Log UPDATE succeeded=false: {body}")
        return False

    # data: true → row was updated, data: false → SEQ_NUM not found
    if body.get("data") is False:
        logger.error(f"❌ Billing-Agent/Log UPDATE: RefNo {ref_no} not found in DB")
        return False

    logger.info(f"✅ Billing-Agent/Log UPDATE OK for RefNo={ref_no}")
    return True


def _as_int(value):
    """Convert to int if possible; otherwise return the original (API can validate)."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return value