# routes/orchestrate.py
from __future__ import annotations

import asyncio
import logging
from typing import Dict, Callable
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from src.auth.jwt_auth import verify_token
from src.config.insurance_config import (
    config_manager,
    lookup_by_payer_id,
    bcbs_number_for_payer_id,
    set_active_insurance,
)
from src.services.agent_profile.client import fetch_next_agent_ivr, to_e164
from src.services.billing_log.log_client import create_billing_log_row
from src.services.clinical.client import ClinicalApiError, fetch_visit_data
from src.services.clinical.required_fields import missing_fields_for
from src.services.practice_ehr_auth.client import AuthApiError, fetch_api_key_for_customer
from src.utils.logging_config import set_call_id, set_visit_context
from src.models.data_models import CallState
from src.core.denials.denial_reasons import DENIAL_REASONS
import src.services.telnyx.client as telnyx_client


logger = logging.getLogger(__name__)


def _respond(
    succeeded: bool,
    message: str,
    http_status: int = 200,
    ref_no: int | None = None,
) -> JSONResponse:
    """Standard frontend response shape — mirrors the inbound agent:
      {"succeeded": bool, "message": str, "refNo": int | null}

    `refNo` is the Billing-Agent/Log row id reserved for this call. Present on
    every response made AFTER the initial Log INSERT (success or timeout).
    Omitted from pre-Log validation errors (no row was created yet).
    """
    body: dict = {"succeeded": succeeded, "message": message}
    if ref_no is not None:
        body["refNo"] = ref_no
    return JSONResponse(body, status_code=http_status)


# Every field the denial follow-up must have COMPLETE before it dials. A live rep
# can ask for ANY of these mid-call — a null would force an awkward "I don't have
# that" and can kill the call — so we require the whole set up front rather than
# risk a dead call. (caller-facing label, visit_data key.) These map 1:1 to the
# Clinical view columns the Clinical API supplies; `npi` is the PRACTICE NPI and
# `rendering_npi` is the rendering provider's NPI (kept separate on purpose — see
# the Clinical normalizer). `claim_submit_date` is intentionally NOT required — if
# the DB doesn't carry it, the rep phase defers gracefully.
DENIAL_REQUIRED_FIELDS = (
    ("member name",             "member_name"),
    ("member ID",              "member_id"),
    ("date of birth",          "dob"),
    ("date of service",        "dos"),
    ("payer ID",               "payer_id"),
    ("practice NPI",           "npi"),
    ("rendering provider NPI", "rendering_npi"),
    ("tax ID",                 "tax_id"),
    ("charge amount",          "charge_amount"),
    ("rendering provider name","provider_name"),
    ("practice name",          "practice_name"),
    ("practice address",       "practice_address"),
    ("plan name",              "plan_short_name"),
)


def _missing_denial_fields(visit_data: dict) -> list:
    """Labels of any DENIAL_REQUIRED_FIELDS entry that is absent or blank."""
    return [
        label for label, key in DENIAL_REQUIRED_FIELDS
        if not str(visit_data.get(key) or "").strip()
    ]


# Billing-Agent/Log tags so denial rows are distinguishable from claim-status.
DENIAL_LOG_REQUEST_TYPE = "DENIAL_FOLLOW_UP"
DENIAL_LOG_ENTERED_BY = "AI-Denial-Follow-Up-Agent"


def make_orchestrate_router(
    active_calls: Dict[str, CallState],
    initiated_events: Dict[str, asyncio.Event],
    *,
    TELNYX_BASE_URL: str,
    HEADERS: dict,
    TEL_FROM: str,
    CALL_CONTROL_APP_ID: str,
    WEBHOOK_BASE_URL: str,
    STREAM_BASE_URL: str,
    auto_hangup_fn: Callable[[str, int], asyncio.Task] | Callable[[str, int], None],
) -> APIRouter:
    router = APIRouter()

    async def _start_call(
        *,
        visit_id: str,
        customer_id: str,
        payer_id,
        insurance,
        visit_data: dict,
        auth_token: str,
        api_key,
        denial_reason: str | None = None,
        denial_upfront: bool = False,
        claim_status_seq_num=None,
        create_log: bool = True,
        log_request_type: str = "CLAIM_STATUS",
        log_entered_by: str = "AI-Billing-Agent",
        wait_for_initiated_ms: int = 10000,
    ) -> JSONResponse:
        """Shared tail for /Call and /Denial-Follow-Up: dial via Telnyx, register
        the CallState, arm the auto-hangup watchdog, wait briefly for
        'call.initiated', and return the frontend response.

        The two endpoints differ ONLY in how they obtain `visit_data` + `insurance`
        (Clinical API vs. raw request body). `set_active_insurance(insurance)` MUST
        already have been called by the caller (CallState.__post_init__ reads it).
        `create_log=False` skips the Billing-Agent/Log INSERT (used while testing
        the denial endpoint with IDs that don't resolve). `denial_reason`, when
        given, is stored as the KNOWN reason so the rep flow can skip classification.
        """
        TEL_TO = insurance.phone_number
        # BCBS: all state plans share one prompt but have different provider IVR
        # numbers — pick the number from the payer_id.
        if insurance.name == "BCBS":
            bcbs_num = bcbs_number_for_payer_id(payer_id)
            if bcbs_num:
                TEL_TO = bcbs_num
                logger.info(f"BCBS: dialing {TEL_TO} for payer_id {payer_id!r}")
            else:
                logger.warning(
                    f"BCBS payer_id {payer_id!r} has no number mapped — using default {TEL_TO}"
                )

        # ── Per-customer agent (Support API) ─────────────────────────────
        # The assigned agent supplies the Telnyx FROM number and the call's
        # persona/voice. No profile → do NOT dial (no hardcoded fallback).
        agent = await fetch_next_agent_ivr(customer_id, auth_token)
        if not agent:
            return _respond(
                False, "No agent profile available for this client", http_status=400
            )
        tel_from = to_e164(agent.get("number"))
        if not tel_from:
            logger.error(
                f"Agent {agent.get('id')!r} for customer {customer_id} has no valid "
                f"FROM number ({agent.get('number')!r}) — cannot place call"
            )
            return _respond(
                False, "No agent profile available for this client", http_status=400
            )

        # ── Telnyx call payload ──────────────────────────────────────────
        call_payload = {
            "to": TEL_TO,
            "from": tel_from,
            "connection_id": CALL_CONTROL_APP_ID,
            "webhook_url": f"{WEBHOOK_BASE_URL}/webhooks/calls",
            "webhook_url_method": "POST",
            "stream_url": f"{STREAM_BASE_URL}/stream",
            "stream_track": "both_tracks",
            "stream_bidirectional_mode": "rtp",
            "stream_bidirectional_codec": "PCMU",
            "send_silence_when_idle": True,
        }

        # ── start the call with Telnyx ───────────────────────────────────
        response = await telnyx_client.create_call_raw(call_payload, TELNYX_BASE_URL, HEADERS)

        try:
            body = response.json()
        except Exception:
            logger.exception("❌ Failed to parse JSON from Telnyx")
            return _respond(False, "Could not start call (invalid Telnyx response)", http_status=500)

        if not (200 <= response.status_code < 300):
            logger.error(f"❌ Telnyx error {response.status_code}: {body!r}")
            return _respond(
                False,
                f"Could not start call (Telnyx returned {response.status_code})",
                http_status=500,
            )

        # ── extract data ─────────────────────────────────────────────────
        data = body.get("data", {})
        call_control_id = data.get("call_control_id")
        call_session_id = data.get("call_session_id")
        is_alive        = data.get("is_alive")

        if not call_control_id:
            logger.error(f"❌ Missing call_control_id in response: {body!r}")
            return _respond(False, "Telnyx response missing call_control_id", http_status=500)

        # Tag every subsequent log line with the short call ID + visit context.
        set_call_id(call_control_id)
        set_visit_context(visit_id, customer_id)

        # ── create initial Billing-Agent/Log row to reserve a RefNo ──────
        ref_no = None
        if create_log:
            ref_no = await create_billing_log_row(
                visit_seq_num=visit_id,
                customer_id=customer_id,
                payer_name=insurance.name,
                auth_token=auth_token,
                request_type=log_request_type,
                entered_by=log_entered_by,
            )
            if ref_no is None:
                logger.warning(
                    f"Initial Billing-Agent/Log INSERT failed for call_control_id={call_control_id} "
                    f"— call continues without a RefNo (will retry as single POST at call end)"
                )

        # ── store call state ─────────────────────────────────────────────
        call_state = CallState(
            call_control_id=call_control_id,
            visit_id=visit_id,
            customer_id=customer_id,
            call_session_id=call_session_id,
            auth_token=auth_token,
            api_key=api_key,
            insurance_name=insurance.name,
            visit_data=visit_data,
            ref_no=ref_no,
            agent_id=agent.get("id"),
        )
        # The full agent profile drives the persona name + per-agent TTS voice.
        call_state.agent_profile = agent
        # Denial follow-up: the reason is KNOWN up front (sent in the request), so
        # seed it instead of classifying it live. denial_upfront also makes the
        # call skip the claim read-out and go straight to a representative.
        if denial_reason:
            call_state.denial_reason_key = denial_reason
        call_state.denial_upfront = denial_upfront
        call_state.claim_status_seq_num = claim_status_seq_num
        active_calls[call_control_id] = call_state

        # Keep a handle to the watchdog so the denial pivot can cancel the
        # claim-status timer and re-arm a longer one at pivot time.
        try:
            auto_hangup_seconds = config_manager.get_auto_hangup_seconds()
            watchdog = asyncio.create_task(auto_hangup_fn(call_control_id, delay_seconds=auto_hangup_seconds))  # type: ignore[arg-type]
            active_calls[call_control_id].auto_hangup_task = watchdog
        except Exception:
            active_calls.pop(call_control_id, None)
            raise

        # ── race-proof wait for 'call.initiated' ────────────────────────
        status = "queued"
        if (wait_for_initiated_ms or 0) > 0:
            ev = initiated_events.setdefault(call_control_id, asyncio.Event())
            cs = active_calls.get(call_control_id)
            if cs and getattr(cs, "status", None) == "initiated":
                ev.set()
            try:
                await asyncio.wait_for(ev.wait(), timeout=(wait_for_initiated_ms / 1000.0))
                status = "initiated"
            except asyncio.TimeoutError:
                status = "queued"
            finally:
                initiated_events.pop(call_control_id, None)

        plan_short_name = visit_data.get("plan_short_name")
        plan_description = visit_data.get("plan_description")
        logger.info(
            f"✅ Call queued | status={status} visit_id={visit_id} customer_id={customer_id} "
            f"insurance={insurance.name} payer_id={payer_id!r} plan={plan_short_name!r} "
            f"description={plan_description!r} "
            f"call_control_id={call_control_id} call_session_id={call_session_id} "
            f"is_alive={is_alive}"
        )

        if status == "initiated":
            return _respond(True, "Call answered", ref_no=ref_no)
        else:
            return _respond(False, "Call not answered (reason: timeout)", ref_no=ref_no)

    @router.post("/v1/Billing-Agent/Call")
    async def create_billing_agent_call(
        request: Request,
        user: dict = Depends(verify_token),
        wait_for_initiated_ms: int = 10000,
    ):
        """
        POST /v1/Billing-Agent/Call — start an outbound IVR call.

        Receive visit_id + customer_id, look up the insurance from the
        Clinical API's plan name, start the Telnyx call, optionally wait
        briefly for 'call.initiated', then return status.
        """
        try:
            # ── parse body ───────────────────────────────────────────────────
            try:
                incoming = await request.json()
            except Exception:
                logger.exception("❌ Invalid JSON body")
                return _respond(False, "Invalid JSON body", http_status=400)

            visit_id    = incoming.get("visit_id")
            customer_id = incoming.get("customer_id")
            # Claim id (ClaimStatusSeqNum) — required; echoed back on the
            # Ivr/ClaimStatus write-back after the call ends.
            claim_id = (incoming.get("claim_id") or incoming.get("claimId")
                        or incoming.get("claim_status_seq_num")
                        or incoming.get("ClaimStatusSeqNum"))

            # Frontend sends visit_id + customer_id + claim_id. The insurance is
            # derived later from the plan name returned by Clinical.
            missing_body = [
                name for name, val in (
                    ("visit_id", visit_id),
                    ("customer_id", customer_id),
                    ("claim_id", claim_id),
                )
                if not val
            ]
            if missing_body:
                return _respond(
                    False,
                    f"Missing or empty required field(s): {', '.join(missing_body)}",
                    http_status=400,
                )

            # Capture the raw Bearer token from the Authorization header —
            # reused for Auth / Clinical / PracticeEHR upload / Billing-Agent Log.
            auth_header = request.headers.get("authorization", "")
            auth_token = auth_header.split(" ", 1)[1] if auth_header.lower().startswith("bearer ") else ""

            # 1) Resolve the per-customer API key (needs customer_id + token).
            try:
                api_key = await fetch_api_key_for_customer(customer_id, auth_token)
            except AuthApiError as e:
                return _respond(False, str(e), http_status=400)

            # 2) Fetch visit data from Clinical API (needs visit_id + token + key).
            #    The response carries the plan name we use to pick the insurance.
            try:
                visit_data = await fetch_visit_data(visit_id, auth_token, api_key)
            except ClinicalApiError as e:
                return _respond(False, str(e), http_status=400)

            # 3) Derive the insurance from the Clinical API's payerId.
            #    payer_id is the stable routing key — plan names and
            #    descriptions can change in the billing DB over time and
            #    aren't safe for routing decisions.
            payer_id = visit_data.get("payer_id")
            if not payer_id:
                return _respond(
                    False,
                    "Payer ID is missing for this visit.",
                    http_status=400,
                )
            insurance = lookup_by_payer_id(payer_id)
            if insurance is None:
                return _respond(
                    False,
                    "Billing Agent does not support this payer yet.",
                    http_status=400,
                )

            # 4) Bind this insurance to the current async task's ContextVar.
            #    Must happen before CallState() (its __post_init__ reads config).
            set_active_insurance(insurance)

            # 5) Validate that this insurance has all the fields it needs.
            missing = missing_fields_for(insurance.name, visit_data)
            if missing:
                return _respond(
                    False,
                    f"Missing required clinical data for {insurance.name}: {', '.join(missing)}",
                    http_status=400,
                )

            # 6) Dial + register + wait (shared with the denial endpoint).
            return await _start_call(
                visit_id=visit_id,
                customer_id=customer_id,
                payer_id=payer_id,
                insurance=insurance,
                visit_data=visit_data,
                auth_token=auth_token,
                api_key=api_key,
                claim_status_seq_num=claim_id,
                create_log=True,
                wait_for_initiated_ms=wait_for_initiated_ms,
            )

        except Exception:
            logger.exception("❌ Unexpected error orchestrating call")
            return _respond(False, "Internal error starting call", http_status=500)

    @router.post("/v1/Billing-Agent/Denial-Follow-Up")
    async def create_denial_follow_up_call(
        request: Request,
        user: dict = Depends(verify_token),
        wait_for_initiated_ms: int = 10000,
    ):
        """
        POST /v1/Billing-Agent/Denial-Follow-Up — start an outbound denial
        follow-up call.

        The frontend sends only `visit_id`, `customer_id`, and the KNOWN
        `denial_reason`. Like /Call, patient/claim data (and the payer) are
        fetched from the Auth + Clinical APIs — NOT from the request body.

        Required in body: visit_id, customer_id, denial_reason.
        The Clinical record must carry every field the rep may be asked for
        (name, DOB, DOS, both NPIs, tax id, plan, charge, practice name/address);
        the request is rejected if any is missing. `denial_reason` must be one of
        the 13 supported categories, else the call is refused.

        denial_upfront=True → the call verifies, SKIPS the claim read-out, and
        goes straight to a representative with the known reason. The Billing-Agent/
        Log row is tagged requestType=DENIAL_FOLLOW_UP.
        """
        try:
            try:
                incoming = await request.json()
            except Exception:
                logger.exception("❌ Invalid JSON body")
                return _respond(False, "Invalid JSON body", http_status=400)

            visit_id      = incoming.get("visit_id")
            customer_id   = incoming.get("customer_id")
            denial_reason = incoming.get("denial_reason")
            # ClaimStatusSeqNum — the claim id we echo back on the FollowupAgent/
            # ClaimStatus write-back after the call ends.
            claim_id = (incoming.get("claim_id") or incoming.get("claimId")
                        or incoming.get("claim_status_seq_num")
                        or incoming.get("ClaimStatusSeqNum"))

            # Frontend sends visit_id + customer_id + the KNOWN denial_reason +
            # claim_id. The patient/claim data (and payer) come from the Clinical
            # API — same as /Call — NOT from the request body.
            missing_body = [
                name for name, val in (
                    ("visit_id", visit_id),
                    ("customer_id", customer_id),
                    ("denial_reason", denial_reason),
                    ("claim_id", claim_id),
                )
                if not val
            ]
            if missing_body:
                return _respond(
                    False,
                    f"Missing or empty required field(s): {', '.join(missing_body)}",
                    http_status=400,
                )

            # Guard (BEFORE any Clinical/Auth call): the denial reason MUST be one
            # of the 13 supported categories. If the frontend sends anything outside
            # the registry (e.g. an "invalid_modifier" we have no question set for),
            # don't fetch data or place the call — return a clear "not supported".
            if denial_reason not in DENIAL_REASONS:
                return _respond(
                    False,
                    f"Denial reason '{denial_reason}' is not supported. It must be one of the 13 "
                    f"categories: {', '.join(DENIAL_REASONS.keys())}.",
                    http_status=400,
                )

            auth_header = request.headers.get("authorization", "")
            auth_token = auth_header.split(" ", 1)[1] if auth_header.lower().startswith("bearer ") else ""

            # 1) Resolve the per-customer API key (Auth API).
            try:
                api_key = await fetch_api_key_for_customer(customer_id, auth_token)
            except AuthApiError as e:
                return _respond(False, str(e), http_status=400)

            # 2) Fetch visit data from the Clinical API (needs visit_id + token + key).
            try:
                visit_data = await fetch_visit_data(visit_id, auth_token, api_key)
            except ClinicalApiError as e:
                return _respond(False, str(e), http_status=400)

            # 3) Derive the payer from the Clinical API's payerId (stable routing key).
            payer_id = visit_data.get("payer_id")
            insurance = lookup_by_payer_id(payer_id)
            if insurance is None:
                return _respond(
                    False,
                    "Billing Agent does not support this payer yet.",
                    http_status=400,
                )

            # Bind insurance before CallState() (its __post_init__ reads config).
            set_active_insurance(insurance)

            # Gate: the denial agent is enabled per payer via supports_denial_inquiry.
            # For the initial prod rollout only HUMANA is on; Cigna/Oscar/Baylor are
            # disabled in insurance_config until they're QA'd. Reject others here.
            try:
                if not config_manager.get_supports_denial_inquiry():
                    return _respond(
                        False,
                        f"Denial follow-up is not enabled for {insurance.name} yet.",
                        http_status=400,
                    )
            except RuntimeError:
                return _respond(False, "Denial follow-up is not enabled for this payer yet.", http_status=400)

            # Validate the Clinical record is COMPLETE. Every field must be present
            # and non-null: the rep may ask for any of them (NPIs, tax id, practice
            # name/address, charge, plan), and a blank would derail the call. Reject
            # up front rather than risk a dead call mid-way. (claim_submit_date is
            # intentionally NOT required — the rep phase defers when it's missing.)
            missing = _missing_denial_fields(visit_data)
            if missing:
                return _respond(
                    False,
                    "Cannot start the denial follow-up — the Clinical record is missing "
                    f"required field(s): {', '.join(missing)}.",
                    http_status=400,
                )

            # Dial + register + wait (shared with /Call). denial_upfront=True → skip
            # the claim read-out and go straight to a rep with the KNOWN reason.
            # Log the row tagged as the follow-up agent.
            return await _start_call(
                visit_id=visit_id,
                customer_id=customer_id,
                payer_id=payer_id,
                insurance=insurance,
                visit_data=visit_data,
                auth_token=auth_token,
                api_key=api_key,
                denial_reason=denial_reason,
                denial_upfront=True,
                claim_status_seq_num=claim_id,
                create_log=True,
                log_request_type=DENIAL_LOG_REQUEST_TYPE,
                log_entered_by=DENIAL_LOG_ENTERED_BY,
                wait_for_initiated_ms=wait_for_initiated_ms,
            )

        except Exception:
            logger.exception("❌ Unexpected error orchestrating denial follow-up call")
            return _respond(False, "Internal error starting call", http_status=500)

    return router
