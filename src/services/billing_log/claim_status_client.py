"""
PATCH to Ivr/ClaimStatus — the final per-call status update, sent AFTER
Billing-Agent/Log returns the row id (which becomes RefNo here).

PATCH {IVR_CLAIM_STATUS_BASE_URL}/api/v1/Ivr/ClaimStatus
Headers:
  x-customer-id : <customer_id from the frontend>
  Authorization : Bearer <frontend JWT (the same token used to call us)>
  Content-Type  : application/json
Body:
  {
    "VisitSeqNum": <visit_id, long>,
    "RefNo":       <Billing-Agent/Log data id, long>,
    "Summary":     "<claim summary>",
    "Status":      "<claim status>"
  }
"""
import logging
import os

import httpx

from src.services.http_client import get_http_client, request_timeout

logger = logging.getLogger(__name__)

# Map our internal claim status → the exact values the IVR/ClaimStatus endpoint
# accepts (case-sensitive). "no claim" maps to "NOT ON FILE".
_STATUS_MAP = {
    "paid": "PAID",
    "inprocess": "IN PROCESS",
    "denied": "DENIED",
    "unknown": "UNKNOWN",
    "no claim": "NOT ON FILE",
    # Failure-mode statuses added 2026-07 — sent when the call couldn't
    # reach a real claim outcome, so the frontend still gets an update.
    "patient not found": "PATIENT NOT FOUND",
    "call failed": "CALL FAILED",
}


def _to_ivr_status(internal_status: str) -> str:
    """Translate our internal status to the endpoint's allowed enum.
    Falls back to UNKNOWN for anything unexpected."""
    return _STATUS_MAP.get((internal_status or "").strip().lower(), "UNKNOWN")


async def post_ivr_claim_status(
    *,
    visit_seq_num,
    ref_no,
    summary: str,
    status: str,
    customer_id,
    bearer_token: str,
    claim_status_seq_num=None,
    dcn_icn: str = "",
    ai_denial_reason: str = "",
) -> bool:
    """
    Returns True on a successful (HTTP 200 + Success=true) response, else False.
    Never raises — all failures are logged and swallowed.

    `bearer_token` is the frontend JWT (the same token the frontend used to
    call /v1/Billing-Agent/Call).
    """
    base_url = os.getenv("IVR_CLAIM_STATUS_BASE_URL", "").rstrip("/")
    if not base_url or base_url == "REPLACE_ME":
        logger.error("IVR_CLAIM_STATUS_BASE_URL is not configured — skipping IVR/ClaimStatus")
        return False
    if not bearer_token:
        logger.error("No bearer token for IVR/ClaimStatus — skipping")
        return False
    if not customer_id:
        logger.error("No customer_id for IVR/ClaimStatus — skipping")
        return False

    # NOTE (2026-07): endpoint switched from POST to PATCH, path capitalization
    # changed from IVR to Ivr, and RefNo is now expected as a long (int) not a
    # string. x-customer-id header is still required despite what the spec
    # doc says — verified via Postman that removing it produces an error.
    url = f"{base_url}/api/v1/Ivr/ClaimStatus"
    headers = {
        "x-customer-id": str(customer_id),
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json",
    }
    ivr_status = _to_ivr_status(status)  # map internal → endpoint enum
    payload = {
        "VisitSeqNum": _as_int(visit_seq_num),
        "RefNo": _as_int(ref_no),
        "Summary": summary,
        "Status": ivr_status,
        # Discrete fields extracted from the IVR claim readout. Sent as-is; empty
        # string when the IVR didn't state them (either, both, or neither).
        # DcnIcn = the claim's document control / claim / ICN / reference number.
        # AiDenialReason = the mapped denial CATEGORY (one of the 13 enum keys)
        #   when we could map it, else the RAW denial reason as spoken by the IVR.
        #   "" when the claim wasn't denied / no reason given.
        "DcnIcn": dcn_icn or "",
        "AiDenialReason": ai_denial_reason or "",
    }
    # Claim id (ClaimStatusSeqNum) — included only when the frontend sent one, so
    # existing claim-status calls without it send the exact same payload as before.
    if claim_status_seq_num is not None:
        payload["ClaimStatusSeqNum"] = _as_int(claim_status_seq_num)

    logger.info(
        f"📮 PATCH IVR/ClaimStatus VisitSeqNum={payload['VisitSeqNum']} "
        f"RefNo={payload['RefNo']!r} Status={ivr_status!r} (from {status!r}) "
        f"DcnIcn={payload['DcnIcn']!r} AiDenialReason={payload['AiDenialReason']!r} "
        f"ClaimStatusSeqNum={payload.get('ClaimStatusSeqNum')!r}"
    )

    try:
        client = get_http_client()
        resp = await client.patch(url, headers=headers, json=payload, timeout=request_timeout(read=60))
    except Exception as e:
        logger.exception(f"❌ IVR/ClaimStatus request exception: {e}")
        return False

    # HTTP-level failure (server crash, gateway timeout, etc.) — response
    # body is likely not the standard {Success,Message,ErrorCode} shape.
    if resp.status_code != 200:
        logger.error(
            f"❌ IVR/ClaimStatus HTTP {resp.status_code}: {resp.text[:500]}"
        )
        return False

    try:
        body = resp.json()
    except Exception as e:
        logger.error(f"❌ IVR/ClaimStatus returned non-JSON: {e}")
        return False

    # Business-level failure — server returned 200 but Success=false.
    # Their contract: {"Success": bool, "Message": str, "ErrorCode": str?}
    if not body.get("Success"):
        msg = body.get("Message") or "(no message)"
        err_code = body.get("ErrorCode") or "(no code)"
        logger.error(
            f"❌ IVR/ClaimStatus Success=false | Message={msg!r} ErrorCode={err_code!r}"
        )
        return False

    logger.info(f"✅ IVR/ClaimStatus OK: {body.get('Message')!r}")
    return True


async def post_followup_claim_status(
    *,
    visit_seq_num,
    ref_no,
    summary: str,
    status: str,
    customer_id,
    claim_status_seq_num,
    bearer_token: str,
) -> bool:
    """PATCH the Followup Agent claim status — the denial follow-up's write-back.

    Same shape as post_ivr_claim_status but a DIFFERENT endpoint path
    (/FollowupAgent/ClaimStatus vs /Ivr/ClaimStatus) and one extra field,
    ClaimStatusSeqNum (the claim id passed into the follow-up endpoint). The
    claim-status agent still uses post_ivr_claim_status; only the denial
    follow-up uses this. Never raises.
    """
    base_url = (
        os.getenv("FOLLOWUP_CLAIM_STATUS_BASE_URL")
        or os.getenv("IVR_CLAIM_STATUS_BASE_URL", "")
    ).rstrip("/")
    if not base_url or base_url == "REPLACE_ME":
        logger.error("No base URL configured — skipping FollowupAgent/ClaimStatus")
        return False
    if not bearer_token:
        logger.error("No bearer token for FollowupAgent/ClaimStatus — skipping")
        return False
    if not customer_id:
        logger.error("No customer_id for FollowupAgent/ClaimStatus — skipping")
        return False

    url = f"{base_url}/api/v1/FollowupAgent/ClaimStatus"
    headers = {
        "x-customer-id": str(customer_id),
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "VisitSeqNum": _as_int(visit_seq_num),
        "RefNo": _as_int(ref_no),
        "Summary": summary,
        "Status": _to_ivr_status(status),
        "ClaimStatusSeqNum": _as_int(claim_status_seq_num),
    }

    logger.info(
        f"📮 PATCH FollowupAgent/ClaimStatus VisitSeqNum={payload['VisitSeqNum']} "
        f"RefNo={payload['RefNo']!r} Status={payload['Status']!r} "
        f"ClaimStatusSeqNum={payload['ClaimStatusSeqNum']!r}"
    )

    try:
        client = get_http_client()
        resp = await client.patch(url, headers=headers, json=payload, timeout=request_timeout(read=60))
    except Exception as e:
        logger.exception(f"❌ FollowupAgent/ClaimStatus request exception: {e}")
        return False

    if resp.status_code != 200:
        logger.error(f"❌ FollowupAgent/ClaimStatus HTTP {resp.status_code}: {resp.text[:500]}")
        return False

    try:
        body = resp.json()
    except Exception as e:
        logger.error(f"❌ FollowupAgent/ClaimStatus returned non-JSON: {e}")
        return False

    if not body.get("Success"):
        msg = body.get("Message") or "(no message)"
        err_code = body.get("ErrorCode") or "(no code)"
        logger.error(
            f"❌ FollowupAgent/ClaimStatus Success=false | Message={msg!r} ErrorCode={err_code!r}"
        )
        return False

    logger.info(f"✅ FollowupAgent/ClaimStatus OK: {body.get('Message')!r}")
    return True


def _as_int(value):
    """Convert to int if possible; otherwise return the original."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return value
