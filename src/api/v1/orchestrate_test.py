# routes/orchestrate_test.py
"""
POST /v1/Billing-Agent/Call/Test — DEV-ONLY call starter with inline visit data.

Exists for testing flows the Clinical API has no seeded data for (e.g. the
denial follow-up pivot against a real denied claim). Differences from the
production /v1/Billing-Agent/Call:

  - visit_data comes INLINE in the request body (no Auth API, no Clinical API)
  - routed by insurance name (no payer_id lookup)
  - same JWT auth as production (Bearer token via verify_token) — send an
    Authorization: Bearer <token> header, exactly like /v1/Billing-Agent/Call
  - no Billing-Agent/Log row (ref_no=None)
  - CallState.is_test=True → the post-call pipeline runs the real outcome
    classification but LOGS the would-be writes instead of touching any
    PracticeEHR endpoint

Everything else — Telnyx dial, webhooks, WebSocket audio, STT/GPT/TTS, the
claim flow, and the denial pivot — is the REAL production path.

Example body:
{
  "insurance": "HUMANA",
  "visit_id": "TEST-102940740",
  "visit_data": {
    "tax_id": "830510206", "npi": "1952408932", "member_id": "49693275",
    "member_name": "Thomas Gooden", "dob": "08/16/1954", "dos": "04/27/2026",
    "billed_amount": "4437.72", "provider_name": "KATRANJI HAND CENTER"
  }
}
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, Callable

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from src.auth.jwt_auth import verify_token
from src.config.insurance_config import (
    INSURANCE_CONFIGS,
    config_manager,
    set_active_insurance,
    bcbs_config_for_payer_id,
)
from src.services.clinical.required_fields import missing_fields_for
from src.utils.logging_config import set_call_id, set_visit_context
from src.models.data_models import CallState
import src.services.telnyx.client as telnyx_client

logger = logging.getLogger(__name__)


def _respond(succeeded: bool, message: str, http_status: int = 200) -> JSONResponse:
    return JSONResponse({"succeeded": succeeded, "message": message}, status_code=http_status)


def make_orchestrate_test_router(
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

    @router.post("/v1/Billing-Agent/Call/Test")
    async def create_test_call(
        request: Request,
        user: dict = Depends(verify_token),   # same JWT auth as /v1/Billing-Agent/Call
        wait_for_initiated_ms: int = 10000,
    ):
        try:
            try:
                incoming = await request.json()
            except Exception:
                return _respond(False, "Invalid JSON body", http_status=400)

            insurance_name = str(incoming.get("insurance") or "").strip().upper()
            insurance = INSURANCE_CONFIGS.get(insurance_name)
            if insurance is None:
                return _respond(
                    False,
                    f"Unknown insurance {insurance_name!r}. Options: {', '.join(INSURANCE_CONFIGS)}",
                    http_status=400,
                )

            # BCBS chooses its operator prompt + dial number by payer_id. Let the
            # test caller pass a payer_id so a specific BCBS operator (e.g. an
            # Elevance state) is exercised; without one, BCBS uses its default
            # prompt + fallback number. Ignored for non-BCBS insurers.
            if insurance.name == "BCBS":
                _pid = incoming.get("payer_id") or incoming.get("payerId")
                if _pid:
                    insurance = bcbs_config_for_payer_id(insurance, _pid)
                    logger.info(f"🧪 BCBS test: payer_id={_pid!r} -> prompt={insurance.prompt_template} num={insurance.phone_number}")

            visit_data = incoming.get("visit_data") or {}
            if not isinstance(visit_data, dict):
                return _respond(False, "visit_data must be an object", http_status=400)

            # Propagate the BCBS payer_id INTO visit_data so the in-call insurance
            # restore (set_active_insurance_for_call, called at each webhook/stream
            # entry) re-selects the SAME operator prompt + number. In prod the
            # Clinical API returns payer_id inside visit_data; the test endpoint
            # takes it at the top level, so without this every in-call GPT turn
            # reverts to the default BCBS prompt (wrong template).
            if insurance.name == "BCBS" and _pid and not visit_data.get("payer_id"):
                visit_data["payer_id"] = str(_pid)

            visit_id = str(incoming.get("visit_id") or "TEST-CALL")

            # Bind insurance to this task's ContextVar BEFORE CallState()
            # (its __post_init__ reads config) — same rule as orchestrate.
            set_active_insurance(insurance)

            missing = missing_fields_for(insurance.name, visit_data)
            if missing:
                return _respond(
                    False,
                    f"Missing required visit_data field(s) for {insurance.name}: {', '.join(missing)}",
                    http_status=400,
                )

            call_payload = {
                "to": insurance.phone_number,
                "from": TEL_FROM,
                "connection_id": CALL_CONTROL_APP_ID,
                "webhook_url": f"{WEBHOOK_BASE_URL}/webhooks/calls",
                "webhook_url_method": "POST",
                "stream_url": f"{STREAM_BASE_URL}/stream",
                "stream_track": "both_tracks",
                "stream_bidirectional_mode": "rtp",
                "stream_bidirectional_codec": "PCMU",
                "send_silence_when_idle": True,
            }

            response = await telnyx_client.create_call_raw(call_payload, TELNYX_BASE_URL, HEADERS)
            try:
                body = response.json()
            except Exception:
                return _respond(False, "Could not start call (invalid Telnyx response)", http_status=500)
            if not (200 <= response.status_code < 300):
                logger.error(f"❌ Telnyx error {response.status_code}: {body!r}")
                return _respond(
                    False,
                    f"Could not start call (Telnyx returned {response.status_code})",
                    http_status=500,
                )

            data = body.get("data", {})
            call_control_id = data.get("call_control_id")
            call_session_id = data.get("call_session_id")
            if not call_control_id:
                return _respond(False, "Telnyx response missing call_control_id", http_status=500)

            set_call_id(call_control_id)
            set_visit_context(visit_id, None)

            active_calls[call_control_id] = CallState(
                call_control_id=call_control_id,
                visit_id=visit_id,
                customer_id=None,
                call_session_id=call_session_id,
                auth_token=None,
                api_key=None,
                insurance_name=insurance.name,
                visit_data=visit_data,
                ref_no=None,
                is_test=True,
            )

            auto_hangup_seconds = config_manager.get_auto_hangup_seconds()
            watchdog = asyncio.create_task(
                auto_hangup_fn(call_control_id, delay_seconds=auto_hangup_seconds)  # type: ignore[arg-type]
            )
            active_calls[call_control_id].auto_hangup_task = watchdog

            # Race-proof wait for 'call.initiated' — same as orchestrate.
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

            logger.info(
                f"🧪 TEST call queued | status={status} insurance={insurance.name} "
                f"visit_id={visit_id} "
                f"call_control_id={call_control_id}"
            )

            if status == "initiated":
                return _respond(True, "Test call answered")
            return _respond(False, "Test call not answered (reason: timeout)")

        except Exception:
            logger.exception("❌ Unexpected error starting test call")
            return _respond(False, "Internal error starting test call", http_status=500)

    return router
