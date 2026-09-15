"""Denial follow-up call-flow orchestration.

Extracted from main.py so the denial flow has its own home and can be reasoned
about independently of the claim-status path. Holds the in-call phase transitions
(claim_status → denial_ivr → denial_rep), the per-turn speech handling, the
reason-classification maintenance, and the hold-silence watchdog.

Depends on the denial DOMAIN modules directly (detection, reason_classifier,
context, prompts) and on the shared call registry. The handful of main-level
call-I/O primitives it can't import without a cycle — speak, model-response
execution, turn recording, cleanup, DTMF — are injected once via configure().
"""
import asyncio
import logging
import os
import time

from src.core.call_registry import active_calls
from src.config.insurance_config import config_manager
from src.services.llm.llm_service import _call_gpt_api
from src.services.call_lifecycle import auto_hangup
from src.core.denials.reason_classifier import (
    match_reason_rules,
    has_reason_cue,
    classify_reason_gpt,
)
from src.core.denials.context import build_denial_format_kwargs, render_denial_context
from src.core.prompts.manager import get_denial_prompt_template

logger = logging.getLogger(__name__)


# ── Injected call-I/O primitives (set once by main via configure) ────────────
# These live in main.py because they close over Telnyx/TTS wiring; injecting them
# keeps this module import-cycle-free. See the call_io follow-up to promote them.
_speak = None                 # async speak_with_azure(text, call_control_id)
_process_response = None      # async process_llama_response(response, call_control_id)
_append_step = None           # append_conversation_step(call_state, transcript, gpt_result)
_ensure_call_cleanup = None   # cleanup callable passed to auto_hangup
_send_dtmf = None             # async send_dtmf(digits, call_control_id)


def configure(*, speak, process_response, append_step, ensure_call_cleanup, send_dtmf):
    """Wire the main-level call-I/O primitives. Called once at startup."""
    global _speak, _process_response, _append_step, _ensure_call_cleanup, _send_dtmf
    _speak = speak
    _process_response = process_response
    _append_step = append_step
    _ensure_call_cleanup = ensure_call_cleanup
    _send_dtmf = send_dtmf


# How many recent turns of the denial conversation to inject into the rep prompt.
# A real denial call has ~15-25 exchanges; 12 covers the active context without
# blowing the token budget.
_DENIAL_HISTORY_MAX_TURNS = 12

# Hold-silence watchdog knobs. If the rep line is quiet this long during the rep
# phase, nudge with a "hello". Capped so a truly dead line isn't nagged forever —
# auto_hangup ends it.
_HOLD_SILENCE_S = float(os.getenv("DENIAL_HOLD_SILENCE_S", "180"))
_HOLD_CHECK_INTERVAL_S = 20.0
_MAX_HOLD_NUDGES = int(os.getenv("DENIAL_MAX_HOLD_NUDGES", "3"))

# GPT is the PRIMARY reason classifier (keyword rules are only a free fast-path).
# The background fallback fires at most this many times per call.
_MAX_REASON_GPT_ATTEMPTS = 5
# Rep utterances shorter than this (and without an obvious reason cue) are too
# thin to classify from — wait for a substantive explanation.
_MIN_SUBSTANTIVE_LEN = 30


def _is_gpt_rep_mode_signal(response: str) -> bool:
    """Detect the 'rep_mode' safety-net signal from the denial-IVR prompt — a live
    human picked up without any transfer phrase being heard. Strict equality on
    the compact form."""
    if not response:
        return False
    compact = response.strip().lower().replace(" ", "").replace("_", "").rstrip(".,!?:;'\"")
    return compact == "repmode"


async def _hold_watchdog(call_control_id: str):
    """While talking to a live rep, if the line goes silent for a long stretch
    (rep put us on hold and wandered off), proactively check we're still
    connected — like a person would — instead of sitting mute forever."""
    try:
        while True:
            await asyncio.sleep(_HOLD_CHECK_INTERVAL_S)
            cs = active_calls.get(call_control_id)
            if not cs or getattr(cs, "phase", None) != "denial_rep":
                return  # call ended or left the rep phase
            if getattr(cs, "is_tts_active", False):
                continue  # bot is currently speaking
            last = getattr(cs, "last_activity_ts", None)
            if last is None:
                cs.last_activity_ts = time.time()
                continue
            if (time.time() - last) < _HOLD_SILENCE_S:
                continue
            if getattr(cs, "hold_nudge_count", 0) >= _MAX_HOLD_NUDGES:
                continue  # stop nagging a dead line; auto_hangup will end it
            cs.hold_nudge_count = getattr(cs, "hold_nudge_count", 0) + 1
            cs.last_activity_ts = time.time()  # reset so we wait again before the next nudge
            logger.info(
                f"🔔 Hold-silence nudge #{cs.hold_nudge_count} "
                f"(quiet ≥ {_HOLD_SILENCE_S:.0f}s)"
            )
            try:
                await _speak("Hello, are you still there?", call_control_id)
            except Exception as e:
                logger.warning(f"Hold nudge TTS failed: {e}")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"Hold watchdog error: {e}")


def _mark_rep_activity(call_state):
    """Rep spoke (or we're entering rep phase) — reset the hold-silence clock and
    the nudge count so a responsive rep is never nudged."""
    call_state.last_activity_ts = time.time()
    call_state.hold_nudge_count = 0


def _enter_denial_rep_phase(call_state):
    """Flip to the live-representative phase: looser speech timings (humans pause
    more than IVR menus) + rep prompt from the next turn on."""
    call_state.phase = "denial_rep"
    rep_debounce = config_manager.get_denial_rep_debounce_seconds()
    call_state.debounce_seconds = rep_debounce
    call_state.need_debounce_reset = True
    rep_seg = config_manager.get_denial_rep_segmentation_silence_ms()
    call_state.segmentation_silence_ms = rep_seg
    if getattr(call_state, "azure_stt_session", None):
        call_state.azure_stt_session.update_segmentation_timeout(rep_seg)

    # Start the hold-silence watchdog for this call (once).
    _mark_rep_activity(call_state)
    prior = getattr(call_state, "hold_watchdog_task", None)
    if prior is None or prior.done():
        call_state.hold_watchdog_task = asyncio.create_task(
            _hold_watchdog(call_state.call_control_id)
        )

    logger.info(
        f"🧑‍💼 Denial REP phase entered (debounce={rep_debounce}s, segmentation={rep_seg}ms)"
    )


def _schedule_reason_classification(call_state, transcript_tail: str):
    """Fire the GPT reason-classification as a BACKGROUND task — never in the
    speech loop. The task writes denial_reason_key/verbatim onto CallState; the
    next turn's re-rendered prompt picks it up. Cancelled in cleanup step 0 via
    CallState.denial_reason_task."""
    attempts = getattr(call_state, "denial_gpt_attempts", 0)
    if attempts >= _MAX_REASON_GPT_ATTEMPTS:
        return
    prior = getattr(call_state, "denial_reason_task", None)
    if prior is not None and not prior.done():
        return  # one in flight at a time
    call_state.denial_gpt_attempts = attempts + 1

    async def _run():
        try:
            key, verbatim = await classify_reason_gpt(transcript_tail)
            if not key:
                return
            current = getattr(call_state, "denial_reason_key", None)
            provisional = getattr(call_state, "denial_reason_provisional", False)
            # GPT is AUTHORITATIVE: set the reason when it's unknown, OR override a
            # provisional keyword guess. A GPT-confirmed reason is left alone so we
            # don't thrash turn to turn.
            if current is None or provisional:
                if current is not None and current != key:
                    logger.info(f"🧭 GPT overrode provisional keyword guess: {current} → {key}")
                else:
                    logger.info(f"🧭 Denial reason set by GPT: {key}")
                call_state.denial_reason_key = key
                if verbatim:
                    call_state.denial_reason_verbatim = verbatim
                call_state.denial_reason_provisional = False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Reason-classification task failed (ignored): {e}")

    call_state.denial_reason_task = asyncio.create_task(_run())


async def _ensure_reason_classified_sync(call_state, text: str) -> None:
    """Lock the denial reason SYNCHRONOUSLY the moment the rep clearly states one,
    so THIS turn's prompt already carries the reason-specific questions. The
    background classifier can lag or be cancelled at cleanup; when a reason cue is
    present and we don't yet have a GPT-confirmed reason, await one classify
    (~1s, once per denial). GPT stays authoritative; a keyword guess is
    provisional and gets overridden here."""
    if call_state.phase != "denial_rep" or not has_reason_cue(text):
        return
    key = getattr(call_state, "denial_reason_key", None)
    provisional = getattr(call_state, "denial_reason_provisional", False)
    if key is not None and not provisional:
        return  # already GPT-confirmed — don't re-classify every turn
    tail = _format_denial_history(call_state)
    new_key, verbatim = await classify_reason_gpt(f"{tail}\nRep: {text}")
    if new_key:
        call_state.denial_reason_key = new_key
        if verbatim:
            call_state.denial_reason_verbatim = verbatim
        call_state.denial_reason_provisional = False
        logger.info(f"🧭 Denial reason locked synchronously: {new_key}")


def _update_denial_reason(call_state, payer_text: str):
    """Per-utterance reason maintenance — GPT is the AUTHORITATIVE classifier.

    Reps phrase the same denial many ways, so keyword rules alone misfire. GPT
    runs on every substantive rep utterance while the reason is unknown or only a
    keyword guess (provisional), and OVERRIDES the guess. Keyword rules only
    supply an instant provisional guess when nothing is set yet; they NEVER
    overwrite a reason once set — GPT owns corrections."""
    current = getattr(call_state, "denial_reason_key", None)
    provisional = getattr(call_state, "denial_reason_provisional", False)
    substantive = len(payer_text.strip()) >= _MIN_SUBSTANTIVE_LEN or has_reason_cue(payer_text)

    if call_state.phase == "denial_rep" and substantive and (current is None or provisional):
        tail = _format_denial_history(call_state)
        _schedule_reason_classification(call_state, f"{tail}\nRep: {payer_text}")

    if current is None:
        hit = match_reason_rules(payer_text)
        if hit is not None:
            call_state.denial_reason_key = hit.key
            call_state.denial_reason_verbatim = payer_text.strip()[:300]
            call_state.denial_reason_provisional = True
            logger.info(f"🧭 Provisional reason from keywords (GPT will confirm): {hit.key}")


def _format_denial_history(call_state) -> str:
    """Render the denial-phase conversation history for the rep prompt. Only
    entries appended AFTER the pivot (denial_history_start) are shown — earlier
    claim-status turns would be noise. Bot fallback/endcall lines are skipped
    (no audio was produced for them)."""
    start = getattr(call_state, "denial_history_start", 0)
    entries = (call_state.conversation_history or [])[start:][-_DENIAL_HISTORY_MAX_TURNS:]
    lines = []
    for e in entries:
        heard = (e.get("transcript") or "").strip()
        replied = (e.get("gpt_result") or "").strip()
        if heard:
            lines.append(f"Rep: {heard}")
        if replied:
            low = replied.lower()
            compact = low.replace(" ", "")
            if low.startswith("fallback") or compact in ("endcall", "end", "hangup", "repmode"):
                continue
            if low.startswith("say:"):
                replied = replied[4:].strip()
            lines.append(f"You: {replied}")
    if not lines:
        return "(no prior conversation yet — this is the first turn)"
    return "\n".join(lines)


def _enter_denial_ivr_phase(call_state) -> None:
    """Flip a call into the denial-IVR (reach-a-representative) phase: set the
    phase/flags, revert speech timings to the IVR baseline, and re-arm the
    auto-hangup watchdog with the longer denial budget. Shared by the
    detect-then-pivot path and the reason-known-up-front direct path. Does NOT
    speak or classify — callers decide those."""
    call_id = call_state.call_control_id
    call_state.denial_pivoted = True
    call_state.claim_mode = False
    call_state.phase = "denial_ivr"
    call_state.denial_history_start = len(call_state.conversation_history)

    # Revert speech timings from claim-mode values back to the IVR baseline.
    call_state.debounce_seconds = config_manager.get_debounce_seconds()
    call_state.need_debounce_reset = True
    normal_seg = config_manager.get_segmentation_silence_ms()
    call_state.segmentation_silence_ms = normal_seg
    if getattr(call_state, "azure_stt_session", None):
        call_state.azure_stt_session.update_segmentation_timeout(normal_seg)

    # Re-arm the auto-hangup watchdog with the denial budget. Create the NEW timer
    # first, then cancel the old one, so a failure never leaves the call with NO
    # watchdog (which could strand the CallState in active_calls forever).
    try:
        denial_secs = config_manager.get_denial_auto_hangup_seconds()
        new_task = asyncio.create_task(
            auto_hangup(call_id, active_calls, _ensure_call_cleanup, denial_secs)
        )
        old_task = getattr(call_state, "auto_hangup_task", None)
        call_state.auto_hangup_task = new_task
        if old_task is not None and not old_task.done():
            old_task.cancel()
        logger.info(f"⏲️ Auto-hangup re-armed for denial flow: {denial_secs}s")
    except Exception as e:
        logger.warning(f"Auto-hangup re-arm failed (original timer still active): {e}")


async def _enter_denial_ivr_direct(call_state, call_control_id: str) -> None:
    """Denial follow-up with the reason KNOWN up front: the moment the IVR starts
    the claim read-out, skip reading/classifying the claim and go straight for a
    representative. No detection/classification runs — the reason is already
    seeded on the call state."""
    _enter_denial_ivr_phase(call_state)
    logger.info(
        "🎯 DENIAL (reason known up front): claim read-out started — skipping it "
        "and going straight to a representative"
    )


async def _handle_denial_speech(text: str, call_control_id: str):
    """Handle one debounced utterance while the call is in a denial phase. Serialized
    per call: a per-call lock plus a sequence guard drops a turn superseded by a
    newer utterance while it waited for the lock, so we respond ONCE, to the most
    recent utterance."""
    call_state = active_calls.get(call_control_id)
    if not call_state:
        return
    if not hasattr(call_state, "denial_turn_lock"):
        call_state.denial_turn_lock = asyncio.Lock()
    call_state.denial_turn_seq = getattr(call_state, "denial_turn_seq", 0) + 1
    my_seq = call_state.denial_turn_seq

    async with call_state.denial_turn_lock:
        if my_seq != getattr(call_state, "denial_turn_seq", my_seq):
            logger.info(f"⏭️ Superseded denial turn (seq {my_seq}) — skipping duplicate reply")
            return
        await _handle_denial_speech_locked(text, call_state, call_control_id)


async def _handle_denial_speech_locked(text: str, call_state, call_control_id: str):
    if call_state.phase == "denial_rep":
        _mark_rep_activity(call_state)

    # We deliberately do NOT flip to the rep phase on a transfer ANNOUNCEMENT
    # ("transferring you now", "estimated wait time"). Those are the automated
    # system + hold — no human yet — and flipping early made the rep template ask
    # denial questions into the hold/reference-number read-out. We STAY in
    # denial_ivr and only switch when a real human greets us (rep_mode below).

    # Lock the reason the moment the rep states it, so THIS turn's prompt already
    # carries the reason-specific questions. One-time ~1s cost on the reason turn.
    await _ensure_reason_classified_sync(call_state, text)
    _update_denial_reason(call_state, text)

    fmt = build_denial_format_kwargs(call_state)

    def _build_prompt() -> str:
        if call_state.phase == "denial_rep":
            template = get_denial_prompt_template("representative")
            return template.format(
                transcript=text,
                conversation_history=_format_denial_history(call_state),
                denial_context_block=render_denial_context(call_state),
                **fmt,
            )
        template = get_denial_prompt_template("ivr")
        return template.format(transcript=text, **fmt)

    # Rep-phase replies are full sentences — the default max_tokens=50 (sized for
    # one-word IVR commands) can truncate them. 80 covers the longest replies.
    _DENIAL_MAX_TOKENS = 80

    t0 = time.perf_counter()
    response = await _call_gpt_api(_build_prompt(), max_tokens=_DENIAL_MAX_TOKENS)
    gpt_ms = (time.perf_counter() - t0) * 1000
    logger.info(f"GPT latency (denial/{call_state.phase}): {gpt_ms:.0f} ms")
    logger.info(f"GPT response (denial/{call_state.phase}): {response!r}")

    # rep_mode safety net: a live human picked up but no transfer phrase was heard.
    # Flip phase and re-run THIS chunk under the rep template so the greeting gets a
    # proper conversational response.
    if call_state.phase == "denial_ivr" and _is_gpt_rep_mode_signal(response):
        logger.info("→ GPT signaled rep_mode (human picked up — no transfer phrase heard)")
        _enter_denial_rep_phase(call_state)
        response = await _call_gpt_api(_build_prompt(), max_tokens=_DENIAL_MAX_TOKENS)
        logger.info(f"GPT response (denial/rep re-run): {response!r}")

    _append_step(call_state, text, response)
    await _process_response(response, call_control_id)
