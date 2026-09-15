import asyncio
import time
import os
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from typing import Dict
from dataclasses import dataclass, field
from datetime import datetime
import logging
from src.services.azure.stt_service import stt_manager, convert_mulaw_to_pcm, AzureRealtimeSttService
from pydantic import BaseModel
import re
from functools import partial
#from prompt import PROMPT_TEMPLATE
import src.core.claims.claims_agent as claims_agent
from src.config.insurance_config import config_manager
from src.core.prompts.manager import get_main_prompt_template
from src.core.denials import call_flow
from src.core.denials.call_flow import (
    _handle_denial_speech,
    _enter_denial_ivr_direct,
)
import src.services.telnyx.client as telnyx_client
from src.api.v1.orchestrate import make_orchestrate_router
from src.api.v1.webhooks import make_webhooks_router
from src.api.v1.stream import make_stream_router
#from services.azure_tts_service import speak_with_azure
from src.services.azure.tts_service import speak_with_azure as _speak_with_azure
from src.services.llm.llm_service import _call_gpt_api, _process_llama_response
from src.core.claims.claims_helpers import is_claim_not_found, is_claim_start
from src.services.call_lifecycle import hangup_call, auto_hangup
from src.services.call_lifecycle import hangup_call as _hangup_call

from src.services.call_cleanup import ensure_call_cleanup as _ensure_call_cleanup
from src.services.agent_profile.client import persona_name_parts
from src.utils.logging_config import setup_logging, set_call_id
from src.utils.transcript import append_ivr, append_agent_dtmf




# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# Configuration
TELNYX_API_KEY = os.getenv("TELNYX_API_KEY")
TELNYX_BASE_URL = "https://api.telnyx.com/v2"
#TEL_TO = os.getenv("TEL_TO")  # Number to call
TEL_FROM = os.getenv("TEL_FROM")  # Your Telnyx number
CALL_CONTROL_APP_ID = os.getenv("CALL_CONTROL_APP_ID")
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL")  # Your server URL
STREAM_BASE_URL = WEBHOOK_BASE_URL.replace("https://", "wss://")
AZURE_SPEECH_KEY    = os.getenv("AZURE_SPEECH_KEY")
AZURE_SPEECH_REGION = os.getenv("AZURE_SPEECH_REGION")


# NOTE: insurance-specific values (TEL_TO, debounce timings, etc.) are read
# per call from the active insurance config (ContextVar). Do not materialize
# them at module load — no insurer is active until a request arrives.


HEADERS = {
    "Authorization": f"Bearer {TELNYX_API_KEY}",
    "Content-Type": "application/json"
}

app = FastAPI()
setup_logging()

# Ship logs + traces to Azure Application Insights when the connection string
# is set (Azure App Service injects it once App Insights is enabled in the
# portal). On local dev the env var is absent and this is a no-op.
if os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"):
    from azure.monitor.opentelemetry import configure_azure_monitor
    configure_azure_monitor(logger_name=None)  # captures the root logger → all our logs


logger = logging.getLogger(__name__)


# Convert every HTTPException (including JWT auth failures raised by
# src/auth/jwt_auth.py) to the frontend-standard {succeeded, message} shape.
# Keeps response format uniform across success and error paths.
@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"succeeded": False, "message": detail},
    )


initiated_events: Dict[str, asyncio.Event] = {}


# Global state management. active_calls lives in call_registry so every layer
# shares the same dict without importing the entrypoint.
from src.core.call_registry import active_calls

bound_hangup = partial(
    _hangup_call,
    telnyx_client=telnyx_client,
    TELNYX_BASE_URL=TELNYX_BASE_URL,
    HEADERS=HEADERS,
)


# Phase-based TTS routing. The denial REP conversation uses Google's natural
# Chirp3-HD voice; EVERYTHING ELSE — claim-status, and the denial IVR /
# verification / navigation — uses Azure, whose <say-as> spells IDs/digits
# crisply and keeps ONE consistent voice through the whole automated portion.
# So a denial call speaks in a single Azure voice until a live rep actually picks
# up, then switches ONCE to Google — no more mid-utterance voice flip-flop.
# Claim-status never enters the denial_rep phase, so it stays 100% Azure —
# byte-identical to today. (This supersedes the old global TTS_PROVIDER switch.)
#
# Voices are env-configurable for A/B testing without a code change:
#   AZURE_TTS_VOICE   (default en-US-AvaMultilingualNeural)
#   GOOGLE_TTS_VOICE  (default en-US-Chirp3-HD-Achird)
# The bound callable keeps the name `speak_with_azure` so every caller downstream
# (LLM service, claims agent, hold watchdog, pivot) is untouched.
_azure_impl = partial(
    _speak_with_azure,
    active_calls=active_calls,
    AZURE_SPEECH_KEY=AZURE_SPEECH_KEY,
    AZURE_SPEECH_REGION=AZURE_SPEECH_REGION,
)
try:
    from src.services.google.tts_service import speak_with_google as _google_speak
    _google_impl = partial(_google_speak, active_calls=active_calls)
    logger.info("TTS: Azure (IVR/claim-status) + Google Chirp3-HD (denial rep phase)")
except Exception as e:
    _google_impl = None
    logger.warning(f"Google TTS unavailable ({e}); using Azure for ALL phases")


async def speak_with_azure(text, call_control_id):
    """Phase-aware TTS dispatcher (kept under the old name so every caller is
    untouched). denial_rep → Google (natural human voice for the live rep);
    everything else → Azure (crisp ID spelling + one consistent voice through the
    automated IVR/verification, and all of claim-status)."""
    if _google_impl is not None:
        cs = active_calls.get(call_control_id)
        if cs is not None and getattr(cs, "phase", None) == "denial_rep":
            return await _google_impl(text, call_control_id)
    return await _azure_impl(text, call_control_id)



ensure_call_cleanup = partial(
    _ensure_call_cleanup,
    active_calls=active_calls,
    claims_agent=claims_agent,
    stt_manager=stt_manager,
    hangup_call=bound_hangup,   # ← use the bound version here
)

def append_conversation_step(call_state, transcript: str, gpt_result: str):
    if not call_state:
        return
    transcript = (transcript or "").strip()
    gpt_result = (gpt_result or "").strip()
    if not transcript and not gpt_result:
        return

    call_state.add_history({
        "transcript": transcript,
        "gpt_result": gpt_result
    })


_CLAIM_HISTORY_MAX_TURNS = 3

def _format_claim_history(call_state) -> str:
    """Last few IVR↔bot turns for the claim-status prompt, so GPT has context
    for back-references like "can you repeat that?" (otherwise it has no idea
    what it just said). Kept SHORT — the IVR flow is fast and menu-driven, so a
    long history would bloat the prompt and risk re-driving old phases. Only the
    {transcript, gpt_result} entries are shown (assistant-content entries just
    duplicate the gpt_result)."""
    if not call_state:
        return "(no prior turns yet)"
    turns = []
    for e in (call_state.conversation_history or []):
        t = (e.get("transcript") or "").strip()
        g = (e.get("gpt_result") or "").strip()
        if t or g:
            turns.append((t, g))
    turns = turns[-_CLAIM_HISTORY_MAX_TURNS:]
    if not turns:
        return "(no prior turns yet)"
    lines = []
    for t, g in turns:
        if t:
            lines.append(f'IVR said: "{t}"')
        if g:
            lines.append(f'You responded: {g}')
    return "\n".join(lines)


def _is_gpt_claim_mode_signal(response: str) -> bool:
    """Detect the 'claim_mode' safety-net signal from GPT — used as a fallback
    when is_claim_start() missed the IVR phrasing. Strict-equality on the
    compact form so no normal say/value/confirm response can accidentally
    match (no substring, no prefix)."""
    if not response:
        return False
    compact = response.strip().lower().replace(" ", "").replace("_", "").rstrip(".,!?:;'\"")
    return compact == "claimmode"


async def _enter_claim_mode_and_forward(call_state, call_control_id: str, first_chunk: str):
    """Flip claim_mode, bump debounce + STT segmentation for claims flow,
    start the claims session, and forward the first chunk. Shared by the
    real-time is_claim_start path and the post-GPT claim_mode fallback so
    both behave identically."""
    if call_state.claim_mode:
        return
    call_state.claim_mode = True
    logger.info("Debounce time changed for claims flow")
    call_state.debounce_seconds = config_manager.get_claim_debounce_seconds()
    call_state.need_debounce_reset = True
    claim_seg_timeout = config_manager.get_claim_segmentation_silence_ms()
    call_state.segmentation_silence_ms = claim_seg_timeout
    if hasattr(call_state, 'azure_stt_session') and call_state.azure_stt_session:
        call_state.azure_stt_session.update_segmentation_timeout(claim_seg_timeout)
        logger.info(f"✅ Segmentation timeout changed to {claim_seg_timeout}ms for claims")

    await claims_agent.start_session(call_control_id)
    await claims_agent.handle_final(call_control_id, first_chunk)




# Pre-compute member-ID digit variants so prompts don't rely on GPT to do
# string math (which it gets wrong, e.g. "last 8 digits" of VMBH53428089 -> 28089).
# The BCBS prompt references these ready-made values. Templates that don't use
# them simply ignore the extra keys.
def _with_member_id_variants(visit_data: dict) -> dict:
    mid = (visit_data or {}).get("member_id")
    if not mid:
        return visit_data or {}
    s = str(mid)
    digits = "".join(c for c in s if c.isdigit())
    m = re.search(r"[A-Za-z](\d+)$", s)          # digits AFTER the last letter
    trailing = m.group(1) if m else digits
    out = dict(visit_data)
    out.setdefault("member_id_digits", digits)               # all digits
    out.setdefault("member_id_trailing", trailing)           # digits after last letter
    out.setdefault("member_id_last8", digits[-8:])           # last 8 digits
    out.setdefault("member_id_last9", digits[-9:])           # last 9 digits
    return out


# ─── 1. handle_user_speech: decorate transcript into a full prompt ────────────

async def handle_user_speech(transcript: str, call_control_id: str):

    #logger.info(f"🎯 handle_user_speech CALLED with transcript length: {len(transcript)}")
    #logger.info(f"📝 Whole transcript: {transcript}")

    text = transcript.strip()
    if not transcript or len(transcript) < 3:
        # Too short to send to GPT (e.g. the IVR spelling a claim/check number one
        # digit at a time). Skip the GPT call, but STILL append it to the transcript
        # so those digits aren't lost from the final record/description.
        logger.warning(f"Transcript too short for GPT — saving to transcript, skipping GPT")
        _cs = active_calls.get(call_control_id)
        if _cs:
            append_ivr(_cs, text)
        return

    call_state = active_calls.get(call_control_id)

    # Bail if the call is already gone (late STT final arrived after cleanup).
    # Without this guard, visit_data becomes {} and the prompt template's
    # first {placeholder} (usually {tax_id}) triggers a KeyError that surfaces
    # as "Task exception was never retrieved" in App Insights.
    if not call_state:
        logger.warning(
            f"handle_user_speech: call_state gone for {call_control_id} — "
            f"skipping (late STT after cleanup)"
        )
        return

    append_ivr(call_state, text)

    # ── denial follow-up routing (post-pivot phases only) ───────────────────
    # Once the call pivoted into the denial flow, every utterance goes to the
    # denial handler — the claim-status ladder below is bypassed entirely.
    # Calls that never pivot (phase == "claim_status") are unaffected.
    if getattr(call_state, "phase", "claim_status") in ("denial_ivr", "denial_rep"):
        await _handle_denial_speech(text, call_control_id)
        return

    # ── claim routing (the only logic in main) ──────────────────────────────
    if call_state:
        if is_claim_not_found(text):
            logger.info("❌ No claims found for this patient. Ending call.")
            await ensure_call_cleanup(call_control_id, reason="claims: not found", send_hangup=True)
            return


        # ENTER claim mode (real-time keyword match)
        if not call_state.claim_mode and is_claim_start(text):
            # Denial follow-up (reason known up front): don't read/classify the
            # claim — go straight to a representative. Flip into the denial-IVR
            # phase and let the reach-rep prompt handle THIS chunk.
            if getattr(call_state, "denial_upfront", False) and not getattr(call_state, "denial_pivoted", False):
                await _enter_denial_ivr_direct(call_state, call_control_id)
                await _handle_denial_speech(text, call_control_id)
                return
            await _enter_claim_mode_and_forward(call_state, call_control_id, text)
            return

        # STAY/EXIT claim mode
        if call_state.claim_mode:
            # while in claim mode, every debounced chunk goes to claims.py
            await claims_agent.handle_final(call_control_id, text)

            # if the claims session ended, drop out and revert debounce
            if hasattr(claims_agent, "is_active") and not claims_agent.is_active(call_control_id):
                call_state.claim_mode = False
                call_state.debounce_seconds = config_manager.get_debounce_seconds()  # revert to baseline
                call_state.need_debounce_reset = True

                # NEW: Revert segmentation timeout
                normal_seg_timeout = config_manager.get_segmentation_silence_ms()
                call_state.segmentation_silence_ms = normal_seg_timeout
                if hasattr(call_state, 'azure_stt_session') and call_state.azure_stt_session:
                    call_state.azure_stt_session.update_segmentation_timeout(normal_seg_timeout)
                    logger.info(f"✅ Segmentation timeout reverted to {normal_seg_timeout}ms")

            return
 


    prompt_template = get_main_prompt_template()  # Gets correct template for current insurance

    # Visit data was fetched from the Clinical API in /v1/Billing-Agent/Call
    # and stored on CallState. Required fields were validated there, so by this
    # point visit_data has everything the prompt template needs.
    visit_data = (call_state.visit_data or {}) if call_state else {}
    # Caller (agent) identity — the bot is the CALLER from the provider's office,
    # NOT the patient. Used only by prompts that ask "who am I talking to" (e.g.
    # Cigna: "say and spell your first and last name"). Env-configurable; prompts
    # that don't reference these placeholders simply ignore the extra keys.
    # Base spread includes the BCBS member-ID digit variants (main) so those
    # prompts keep working; denial persona keys are layered on top.
    _persona_first, _persona_last = persona_name_parts(
        getattr(call_state, "agent_profile", None) if call_state else None
    )
    fmt = {
        **_with_member_id_variants(visit_data),
        "transcript": transcript,
        "agent_persona_name": _persona_first,
        "agent_persona_last_name": _persona_last,
        # Digits-only callback number for IVRs that ask us to SPEAK a callback
        # phone number "in case we get disconnected" (Cigna does this). Stripped
        # of formatting so the TTS reads it digit-by-digit for clean recognition.
        # Only the Cigna claim prompt references it; other templates ignore it.
        "callback_number": re.sub(r"\D", "", os.getenv("DENIAL_CALLBACK_NUMBER", "469-581-2936")),
        # Practice/facility name for IVRs that ask "say and spell the company you
        # work for" (Cigna) — this is the clinic we bill for, the answer to that
        # question. Explicit key (after the base spread) so the placeholder always
        # resolves and an empty value renders as the sentinel the prompt is told
        # never to read aloud — never a KeyError.
        "practice_name": (visit_data.get("practice_name") or "__UNKNOWN__"),
        # Short recent-turn history so the prompt has context for "repeat that"
        # and other back-references. Templates that don't use {conversation_history}
        # simply ignore this key.
        "conversation_history": _format_claim_history(call_state),
    }
    prompt = prompt_template.format(**fmt)

    # Log the exact transcript we hand to GPT for IVR/navigation handling.
    # STT emits many partials before a final; this shows the concatenated
    # final text that actually drove the GPT decision below (mirrors the
    # claims controller's "Transcript chunk sent to GPT" line).
    logger.info(f"Transcript sent to GPT (IVR handling): {text!r}")

    t0 = time.perf_counter()
    response = await _call_gpt_api(prompt)
    if call_state:
        append_conversation_step(call_state, text, response)
    gpt_ms = (time.perf_counter() - t0) * 1000
    logger.info(f"GPT latency: {gpt_ms:.0f} ms")
    logger.info(f"GPT response: {response!r}")

    # GPT-driven claim_mode fallback: if the real-time is_claim_start missed
    # the IVR phrasing, GPT may recognize it semantically and return the
    # exact word "claim_mode". Strict-equality match — cannot collide with
    # say/value/confirm/dtmf/endcall/fallback formats.
    if _is_gpt_claim_mode_signal(response) and not call_state.claim_mode:
        logger.info("→ GPT signaled claim_mode (safety net — real-time detector missed)")
        # Denial follow-up (reason known up front): skip the read-out here too —
        # go straight to a representative instead of entering claim mode.
        if getattr(call_state, "denial_upfront", False) and not getattr(call_state, "denial_pivoted", False):
            await _enter_denial_ivr_direct(call_state, call_control_id)
            await _handle_denial_speech(text, call_control_id)
            return
        await _enter_claim_mode_and_forward(call_state, call_control_id, text)
        return

    await process_llama_response(response, call_control_id)



_DTMF_DEDUPE_WINDOW_S = 5.0

async def send_dtmf(digits: str, call_control_id: str):
    """Send DTMF tones to the call (digits already sanitized by caller)."""
    try:
        cleaned = "".join(ch for ch in digits if ch.isdigit() or ch in "*#")
        cs = active_calls.get(call_control_id)
        # Suppress an IDENTICAL press repeated within a short window. The IVR often
        # speaks one menu in two STT chunks ("...which service..." then "...for
        # Medicaid press 1"), so GPT answers the SAME menu twice (dtmf:1, dtmf:1);
        # the second press lands on the NEXT menu and confuses the IVR. Different
        # digits, or the same digit after the window (a genuine next menu), pass.
        # OPT-IN per insurer (dedupe_dtmf) — Molina only, so working payers are
        # completely unaffected.
        try:
            _dedupe_dtmf = config_manager.get_config().dedupe_dtmf
        except Exception:
            _dedupe_dtmf = False
        if _dedupe_dtmf and cs is not None:
            if (getattr(cs, "last_dtmf_digits", None) == cleaned
                    and (time.time() - getattr(cs, "last_dtmf_ts", 0.0)) < _DTMF_DEDUPE_WINDOW_S):
                logger.warning(f"⏭️ Skipping duplicate DTMF {cleaned!r} (repeat within {_DTMF_DEDUPE_WINDOW_S}s)")
                return
            cs.last_dtmf_digits = cleaned
            cs.last_dtmf_ts = time.time()
        await telnyx_client.send_dtmf(call_control_id, cleaned, TELNYX_BASE_URL, HEADERS)
        append_agent_dtmf(active_calls.get(call_control_id), cleaned)
        logger.info(f"✅ DTMF sent: {cleaned}")
    except Exception as e:
        logger.error(f"❌ Error sending DTMF: {str(e)}")

# keep same signature: process_llama_response(response, call_control_id)
process_llama_response = partial(
    _process_llama_response,
    speak_with_azure=speak_with_azure,       # already partial-bound above
    send_dtmf=send_dtmf,                     # requires send_dtmf to be defined first
    ensure_call_cleanup=ensure_call_cleanup,
    active_calls=active_calls,
)

# Inject the main-level call-I/O primitives into the denial call-flow module
# (now that all five are defined). Everything else it needs it imports directly.
call_flow.configure(
    speak=speak_with_azure,
    process_response=process_llama_response,
    append_step=append_conversation_step,
    ensure_call_cleanup=ensure_call_cleanup,
    send_dtmf=send_dtmf,
)


claims_agent.register_hangup(bound_hangup)  # ← same 1-arg signature
claims_agent.register_active_calls(active_calls)


# Mount the orchestrate router (uses the SAME shared state/funcs from main.py)
app.include_router(
    make_orchestrate_router(
        active_calls,
        initiated_events,
        TELNYX_BASE_URL=TELNYX_BASE_URL,
        HEADERS=HEADERS,
        TEL_FROM=TEL_FROM,
        CALL_CONTROL_APP_ID=CALL_CONTROL_APP_ID,
        WEBHOOK_BASE_URL=WEBHOOK_BASE_URL,
        STREAM_BASE_URL=STREAM_BASE_URL,
        # Wrap auto_hangup so dependencies are passed automatically.
        # Note: delay_seconds is supplied by orchestrate.py from the insurance
        # config — the default here is only a safety net.
        auto_hangup_fn=lambda call_id, delay_seconds: auto_hangup(
            call_id,
            active_calls,
            ensure_call_cleanup,
            delay_seconds
        ),
    )
)


# DEV-ONLY test endpoint (inline visit data, no PracticeEHR writes).
# Route returns 404 unless ENABLE_TEST_CALL_ENDPOINT=true — safe to ship.
from src.api.v1.orchestrate_test import make_orchestrate_test_router
app.include_router(
    make_orchestrate_test_router(
        active_calls,
        initiated_events,
        TELNYX_BASE_URL=TELNYX_BASE_URL,
        HEADERS=HEADERS,
        TEL_FROM=TEL_FROM,
        CALL_CONTROL_APP_ID=CALL_CONTROL_APP_ID,
        WEBHOOK_BASE_URL=WEBHOOK_BASE_URL,
        STREAM_BASE_URL=STREAM_BASE_URL,
        auto_hangup_fn=lambda call_id, delay_seconds: auto_hangup(
            call_id,
            active_calls,
            ensure_call_cleanup,
            delay_seconds
        ),
    )
)


# NEW: mount webhooks router (pass the SAME live state + cleanup fn)
app.include_router(
    make_webhooks_router(
        active_calls,
        initiated_events,
        ensure_call_cleanup=ensure_call_cleanup,
    )
)

# after you define: ensure_call_cleanup, handle_user_speech, etc.

app.include_router(
    make_stream_router(
        active_calls=active_calls,
        stt_manager=stt_manager,
        convert_mulaw_to_pcm=convert_mulaw_to_pcm,
        claims_agent=claims_agent,
        ensure_call_cleanup=ensure_call_cleanup,
        handle_user_speech=handle_user_speech,
    )
)

claims_agent.register_tts(speak_with_azure)
claims_agent.register_dtmf(send_dtmf)


@app.on_event("shutdown")
async def on_shutdown():
    logger.info("🔌 Shutdown event: hanging up all active calls…")
    for call_id in list(active_calls.keys()):
        try:
            set_call_id(call_id)
            await ensure_call_cleanup(call_id, reason="shutdown", send_hangup=True)
        except Exception as e:
            logger.error(f"❌ Cleanup error for {call_id}: {e}")
    stt_manager.cleanup_all()
    # Let any in-flight post-call uploads finish (bounded) before tearing down
    # the shared HTTP client, so they aren't cut off or forced to rebuild a
    # fresh, never-closed client on the way out.
    from src.services.call_cleanup import drain_pending_uploads
    from src.services.http_client import aclose_http_client
    await drain_pending_uploads(timeout=float(os.getenv("SHUTDOWN_UPLOAD_DRAIN_S", "8")))
    await aclose_http_client()
    logger.info("✅ All calls hung up and STT sessions cleaned up. Goodbye!")


@app.get("/")
async def health_check():
    return {"status": "ok", "app_version": os.getenv("APP_VERSION", "unknown")}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, 
                host="0.0.0.0",
                port=5000,
                reload=False,
                )


#  TO run hit the start_call endpoint

#curl -X POST http://localhost:5000/start_call -H "Content-Type: application/json" -d "{}"
#curl -X POST "http://localhost:5000/v1/Billing-Agent/Call?wait_for_initiated_ms=10000" -H "Content-Type: application/json" -H "Authorization: Bearer <JWT>" -d "{\"visit_id\":\"...\",\"customer_id\":\"...\"}"