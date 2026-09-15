# routes/stream.py
from __future__ import annotations
import asyncio
import base64
import json
import logging
import time
import uuid
from typing import Dict, Callable, Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from src.models.data_models import CallState
from src.services.azure.stt_service import stt_manager, convert_mulaw_to_pcm, AzureRealtimeSttService
from src.config.insurance_config import config_manager, set_active_insurance_for_call
from src.utils.logging_config import set_call_id, set_visit_context

# Default initial debounce — per-call values come from call_state once the
# 'start' event is received and we know which call this WebSocket belongs to.
DEFAULT_DEBOUNCE_SECONDS = 0.5

  # your dataclass

logger = logging.getLogger(__name__)

def make_stream_router(
    *,
    active_calls: Dict[str, CallState],
    stt_manager,                          # pass the module/object from main
    convert_mulaw_to_pcm: Callable[[bytes], bytes],
    claims_agent,                         # pass the module
    ensure_call_cleanup: Callable[..., Any],
    handle_user_speech: Callable[[str, str], Any],
) -> APIRouter:
    """
    Factory that returns a router exposing /stream.
    It closes over your live state and helpers from main.py.
    """
    router = APIRouter()

    @router.websocket("/stream")
    async def media_stream_endpoint(websocket: WebSocket):
        logger.info("🔗 New WebSocket connection")
        await websocket.accept()

        ###  temp vars for this connection testing   ###

        # DIAG counters (once per connection)
        media_frames = 0           # how many 'media' events we received
        fed_frames = 0             # how many frames we actually fed to Azure STT
        skipped_tts_gate = 0       # frames skipped because is_tts_active=True
        skipped_track_mismatch = 0 # frames we ignored due to track filter
        last_media_at = None       # timestamp of last media frame
        start_media_format = None  # format from Telnyx 'start' event


        call_control_id = None
        call_state      = None
        websocket_id    = str(uuid.uuid4())

        # ───────── Debounce state (per-connection) ─────────
        from types import SimpleNamespace
        state = SimpleNamespace(
            pending_finals=[],                                  # accumulate final STT chunks here
            debounce_task=None,                                 # the timer task we cancel/restart
            debounce_time=DEFAULT_DEBOUNCE_SECONDS,             # overwritten from call_state on 'start'
            last_partial_len=0,                                 # for throttling partial-log spam
        )

        async def _process_after_quiet():
            #logger.info(f"🔔 _process_after_quiet STARTED - pending_finals count: {len(state.pending_finals)}")
            try:
                await asyncio.sleep(state.debounce_time)
                logger.info(f"✅ Debounce timer COMPLETED ({state.debounce_time}s)")
            except asyncio.CancelledError:
                logger.info(f"❌ Debounce timer CANCELLED")
                return

            # Detach from debounce_task so _reschedule_debounce won't cancel us
            # while we're actively processing (e.g. waiting for GPT response).
            # Without this, a new STT event arriving during handle_user_speech
            # would cancel this task and lose the first claims transcript.
            state.debounce_task = None

            if not state.pending_finals:
                logger.info(f"⚠️ No pending finals after debounce")
                return

            text = " ".join(state.pending_finals).strip()
            #logger.info(f"📦 Concatenated {len(state.pending_finals)} finals into: {text[:100]}...")
            state.pending_finals.clear()
            if not text:
                return

            if call_state and hasattr(call_state, "last_media_ts"):
                ms = (time.perf_counter() - call_state.last_media_ts) * 1000
                #logger.info(f"⏱️ Debounced STT latency: {ms:.0f} ms")


            logger.info(f"🚀 Calling handle_user_speech with concatenated text")
            await handle_user_speech(text, call_control_id)

        def _reschedule_debounce():
            #logger.info(f"🔄 _reschedule_debounce called - current pending_finals: {len(state.pending_finals)}")
            if state.debounce_task and not state.debounce_task.done():
                logger.info(f"⏹️ Cancelling existing debounce task")
                state.debounce_task.cancel()
            #logger.info(f"▶️ Creating NEW debounce task with {state.debounce_time}s timer")
            state.debounce_task = asyncio.create_task(_process_after_quiet())
            # Expose the task to CallState so ensure_call_cleanup can cancel
            # it when the call ends. Prevents late STT finals from firing
            # handle_user_speech for a call that's already been cleaned up.
            if call_state is not None:
                call_state.debounce_task = state.debounce_task


        async def _flush_pending_now():
            if state.debounce_task and not state.debounce_task.done():
                state.debounce_task.cancel()

            if state.pending_finals:
                text = " ".join(state.pending_finals).strip()
                state.pending_finals.clear()
                if text:
                    if call_state and hasattr(call_state, "last_media_ts"):
                        ms = (time.perf_counter() - call_state.last_media_ts) * 1000
                        logger.info(f" Debounced STT latency (flush): {ms:.0f} ms")
                    
                    await handle_user_speech(text, call_control_id)

        # ───────── Azure STT callbacks ─────────
        async def on_partial(text: str):
            # Throttle partial logging: only log when the transcript grows by a
            # meaningful chunk (~4 words) or a new/shorter utterance begins. This
            # kills the per-token spam and the frozen-partial repeats that flooded
            # the logs. Finals are always logged in full by _handle_recognized.
            n = len(text)
            if n < state.last_partial_len or n >= state.last_partial_len + 25:
                logger.info(f"[STT][partial] {text[:80]!r}")
                state.last_partial_len = n

            if call_state:
                desired = getattr(call_state, "debounce_seconds", DEFAULT_DEBOUNCE_SECONDS)
                if state.debounce_time != desired:
                    state.debounce_time = desired
                if getattr(call_state, "need_debounce_reset", False):
                    _reschedule_debounce()
                    call_state.need_debounce_reset = False

            #_reschedule_debounce()  for testing 

        async def on_final(text: str):
            #logger.info(f"📥 on_final called with: '{text[:50]}...'")
            #logger.info(f"[STT][final] {text[:60]!r}")

            state.last_partial_len = 0  # reset partial-log throttle for the next utterance

            text = text.strip()
            if not text:
                logger.info(f"⚠️ Empty text after strip, returning")
                return

            if call_state:
                desired = getattr(call_state, "debounce_seconds", DEFAULT_DEBOUNCE_SECONDS)
                if state.debounce_time != desired:
                    logger.info(f"⚙️ Debounce time changed from {state.debounce_time} to {desired}")
                    state.debounce_time = desired
                if getattr(call_state, "need_debounce_reset", False):
                    logger.info(f"🔃 need_debounce_reset flag detected")
                    _reschedule_debounce()
                    call_state.need_debounce_reset = False

            if call_state and hasattr(call_state, "last_media_ts"):
                ms = (time.perf_counter() - call_state.last_media_ts) * 1000
                logger.info(f"⏱️ STT final piece latency: {ms:.0f} ms")

            state.pending_finals.append(text)
            #logger.info(f"➕ Added to pending_finals. Total count now: {len(state.pending_finals)}")
            _reschedule_debounce()
            #logger.info(f"✅ on_final completed - NOT calling handle_user_speech directly")


        async def on_error(err: str):
            logger.error(f"[STT][error] {err}")

            logger.error(f"[{websocket_id}] STT error: {err}")

        # ───────── WebSocket receive loop ─────────
        try:
            while True:
                frame = await websocket.receive_text()
                msg   = json.loads(frame)
                ev    = msg.get("event")

                if ev == "start":
                    call_control_id = msg["start"]["call_control_id"]
                    # Tag every log line from this WebSocket session with the call's short ID.
                    set_call_id(call_control_id)

                    # Restore the insurance ContextVar for this WebSocket's task.
                    # All downstream config_manager.get_*() calls (stream.py, main.py's
                    # handle_user_speech, claims_controller) depend on this.
                    cs_lookup = active_calls.get(call_control_id)
                    if cs_lookup and cs_lookup.insurance_name:
                        # OPERATOR-aware for BCBS: picks the payer_id's operator
                        # prompt pair so handle_user_speech + claims controller
                        # use the right per-operator template.
                        set_active_insurance_for_call(cs_lookup)
                    # Restore visit_id/customer_id log context so every log
                    # line from this WebSocket carries the visit tag.
                    if cs_lookup:
                        set_visit_context(cs_lookup.visit_id, cs_lookup.customer_id)

                    logger.info(f" Call started: {call_control_id}")

                    # log Telnyx-reported media format (if provided)
                    start_media_format = msg["start"].get("media_format")
                    #logger.info(f"[START] media_format={start_media_format}")

                    call_state = active_calls.get(call_control_id)
                    if not call_state:
                        logger.warning(" Unknown call ID")
                        continue

                    call_state.websocket = websocket
                    call_state.websocket_id = websocket_id  # useful for STT cleanup

                    # ✅ NEW: get insurer-specific baseline segmentation timeout
                    initial_seg_ms = config_manager.get_segmentation_silence_ms()
                    call_state.segmentation_silence_ms = initial_seg_ms

                    session = stt_manager.create_session(websocket_id)
                    call_state.azure_stt_session = session
                    session.initialize(
                        on_partial_result=on_partial,
                        on_final_result=on_final,
                        on_error=on_error,
                        segmentation_silence_ms=initial_seg_ms,     # ✅ pass baseline here
                    )
                    session.start_continuous_recognition()
                    session.start_async_event_handler(asyncio.get_running_loop())

                    logger.info(
                        f"[{call_control_id}] 🎙️ STT initialized "
                        f"(segmentation_silence_ms={initial_seg_ms})"
                    )

                elif ev == "media":
                    media = msg["media"]
                    track = media.get("track")                  # 'inbound'/'outbound'/None
                    payload_b64 = media.get("payload", "")

                    media_frames += 1
                    last_media_at = time.perf_counter()

                    # log first few frames and then every 100th to avoid spam
                    # if media_frames <= 3 or media_frames % 100 == 0:
                    #     logger.info(f"[MEDIA] #{media_frames} track={track} size_b64={len(payload_b64)}")

                    if media.get("track") == "inbound" and call_state:
                        # offload decode so the WS loop stays responsive (diagnostic-safe)
                        raw = base64.b64decode(payload_b64)
                        pcm = await asyncio.to_thread(convert_mulaw_to_pcm, raw)
                        call_state.last_media_ts = time.perf_counter()

                        if getattr(call_state, "is_tts_active", False):
                            skipped_tts_gate += 1
                            if skipped_tts_gate <= 3:
                                logger.warning("[MEDIA] skipped due to is_tts_active=True")
                        else:
                            call_state.azure_stt_session.feed_audio(pcm)
                            fed_frames += 1
                            if fed_frames <= 3 or fed_frames % 500 == 0:
                                logger.info(f"[MEDIA] fed_frames={fed_frames}")
                    else:
                        # either track != inbound or no call_state
                        skipped_track_mismatch += 1
                        if skipped_track_mismatch <= 3:
                            logger.warning(f"[MEDIA] skipped due to track filter (track={track})")

                elif ev == "error":
                    err = msg.get("payload", {})
                    logger.error(f"❌ Telnyx WS error: code={err.get('code')} title={err.get('title')} detail={err.get('detail')}")
    
                elif ev == "stop":
                    logger.info(" Stream stopped")
                    await _flush_pending_now()
                    try:
                        await claims_agent.end_session(call_control_id)
                    except Exception:
                        pass
                    if call_state:
                        call_state.claim_mode = False
                    break

        except WebSocketDisconnect:
            if call_state:
                call_state.status = "websocket_close"
            idle_ms = None
            if last_media_at:
                idle_ms = (time.perf_counter() - last_media_at) * 1000
            logger.info(
                f"[WS CLOSED] media_frames={media_frames} fed_frames={fed_frames} "
                f"skipped_tts_gate={skipped_tts_gate} skipped_track_mismatch={skipped_track_mismatch} "
                f"idle_ms_since_last_media={int(idle_ms) if idle_ms else 'n/a'} "
                f"start_media_format={start_media_format}"
            )

            logger.info(f" WebSocket disconnected")

        except Exception as e:
            logger.error(f" Stream error: {e}")

        finally:
            try:
                await _flush_pending_now()
            except Exception:
                pass

            if call_control_id and call_control_id in active_calls:
                try:
                    await ensure_call_cleanup(
                        call_control_id,
                        reason="websocket: finally/disconnect",
                        send_hangup=True
                    )
                except Exception as e:
                    logger.error(f"[{websocket_id}] ensure_call_cleanup error: {e}")

    return router
