# routes/webhooks.py
from __future__ import annotations

import asyncio
import logging
from typing import Dict, Callable, Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.config.insurance_config import set_active_insurance_for_call
from src.models.data_models import CallState
from src.utils.logging_config import set_call_id, set_visit_context

logger = logging.getLogger(__name__)

def make_webhooks_router(
    active_calls: Dict[str, CallState],
    initiated_events: Dict[str, asyncio.Event],
    *,
    ensure_call_cleanup: Callable[..., Any],
) -> APIRouter:
    """
    Factory router that closes over your live state and cleanup function.
    No behavior changes vs your original endpoint.
    """
    router = APIRouter()

    @router.post("/webhooks/calls")
    async def handle_call_webhooks(request: Request):
        """Handle Telnyx call control webhooks"""
        try:
            body = await request.json()
            data = body.get("data", {})
            event_type = data.get("event_type")
            payload = data.get("payload", {})
            call_control_id = payload.get("call_control_id")

            # Tag every log line in this webhook handling with the short call ID.
            set_call_id(call_control_id or "")

            logger.info(f"📞 Call Event: {event_type}")

            if call_control_id not in active_calls:
                logger.info(f"Webhook for unknown call (likely already cleaned): {call_control_id}")
                return JSONResponse({"status": "ok"})

            call_state = active_calls[call_control_id]

            # Restore visit_id/customer_id log context for this webhook's task.
            set_visit_context(call_state.visit_id, call_state.customer_id)

            # Restore the insurance ContextVar for this webhook's task so any
            # downstream config_manager.get_*() calls (e.g. during cleanup) see
            # the right insurer. The call was created with insurance_name set.
            if call_state.insurance_name:
                set_active_insurance_for_call(call_state)

            if event_type == "call.initiated":
                logger.info(f"[{call_control_id}]📞 Call initiated")
                call_state.status = "initiated"
                if call_control_id in initiated_events:
                    initiated_events[call_control_id].set()

            elif event_type == "call.ringing":
                logger.info(f"[{call_control_id}]🔔 Call ringing")
                call_state.status = "ringing"

            elif event_type == "call.answered":
                logger.info(f"[{call_control_id}]✅ Call answered - Media streaming should start automatically")
                call_state.status = "answered"

            elif event_type == "call.hangup":
                logger.info(
                f"🔚 Call ended | call_control_id={call_control_id} "
                f"call_session_id={payload.get('call_session_id')} "
                f"hangup_cause={payload.get('hangup_cause')} "
                f"hangup_source={payload.get('hangup_source')}"
                )

                call_state.status = "hangup"
                # Centralized, idempotent cleanup; Telnyx already ended the call → no outbound hangup
                await ensure_call_cleanup(call_control_id, reason="webhook: call.hangup", send_hangup=False)

            elif event_type in ("call.streaming.started", "streaming.started"):
                logger.info("🎵 Streaming started successfully")

            elif event_type in ("call.streaming.stopped", "streaming.stopped"):
                logger.info(
                    f"🎵 Streaming stopped | call_control_id={call_control_id} "
                    f"call_session_id={payload.get('call_session_id')} "
                    f"payload={payload}"
                )

            else:
                logger.info(f"📌 Unhandled event: {event_type}")

            return JSONResponse({"status": "ok"})

        except Exception as e:
            logger.error(f"❌ Error handling webhook: {str(e)}")
            return JSONResponse({"error": str(e)}, status_code=500)

    return router
