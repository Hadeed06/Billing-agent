import re
import time
import asyncio
import difflib
import logging
from src.config.insurance_config import config_manager
from src.core.prompts.claims_prompts import get_claims_prompt
from src.services.llm.llm_service import _call_gpt_api
from src.core.claims.claims_intent_mapper import map_keyword

logger = logging.getLogger(__name__)

# If the new chunk's similarity to the last chunk sent to GPT is >= this
# threshold, treat it as a near-duplicate and skip the GPT call.
# 0.85 ≈ up to ~37 chars of drift in a 250-char chunk still counts as
# "same" (chosen to absorb STT trailing-char races without swallowing
# meaningful new content).
# Only applied when the insurance config has dedupe_chunks=True.
_NEAR_DUPLICATE_RATIO = 0.85


def get_claims_tail_chars() -> int:
    """Get claims tail chars for current insurance"""
    return config_manager.get_claims_tail_chars()


def get_controller_prompt_template() -> str:
    """Get the controller prompt template for current insurance"""
    config = config_manager.get_config()
    return get_claims_prompt(config.claims_prompt_template)


# Visit-data keys that may carry the specific claim's total charge amount.
# Some BCBS operators (e.g. Florida Blue) disambiguate multi-claim dates by
# asking the caller for the claim's total charge instead of reading each claim.
_CHARGE_KEYS = ("charge_amount", "total_charge", "billed_amount", "charge")


def _charge_for_call(call_id: str) -> str:
    """Return the raw total-charge string for this call from visit_data, or ''."""
    from src.core.claims import claims_agent
    active = getattr(claims_agent, "_active_calls", None)
    call_state = active.get(call_id) if active else None
    visit_data = getattr(call_state, "visit_data", None) or {}
    for k in _CHARGE_KEYS:
        v = visit_data.get(k)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _charge_spoken(raw: str) -> str:
    """Turn a raw charge (e.g. '$1,403.60' or '1403.6') into an ASR-friendly
    dollars-and-cents phrase the IVR can parse: '1403 dollars and 60 cents'."""
    cleaned = raw.replace("$", "").replace(",", "").strip()
    if not cleaned:
        return ""
    if "." in cleaned:
        dollars, cents = cleaned.split(".", 1)
        cents = (cents + "00")[:2]
        dollars = dollars or "0"
        if cents == "00":
            return f"{dollars} dollars"
        return f"{dollars} dollars and {cents} cents"
    return f"{cleaned} dollars"


def _is_near_duplicate_chunk(prev: str, curr: str, threshold: float = _NEAR_DUPLICATE_RATIO) -> bool:
    """True when the current chunk is essentially the previous chunk with
    only a small tail added/removed (STT trailing-char race).

    Note: autojunk=False is required — the default autojunk heuristic
    treats repeated characters (common in IVR transcripts with phrases
    like "press 2", "press 3") as noise and returns misleadingly low
    similarity ratios.
    """
    if not prev or not curr:
        return False
    if prev == curr:
        return True
    if prev in curr or curr in prev:
        return True
    return difflib.SequenceMatcher(None, prev, curr, autojunk=False).ratio() >= threshold


async def handle_final(call_id: str, utterance: str):
    """
    Main calls this for EVERY debounced Final while in claim mode.
    Append -> send tail (last N chars) of transcript to GPT -> act on keyword.

    Imports claims_agent lazily to avoid circular import.
    """
    # Lazy import to break circular dependency
    from src.core.claims import claims_agent

    s = claims_agent._sessions.get(call_id)
    if not s or not s.get("active"):
        return

    utterance = (utterance or "").strip()
    if not utterance:
        return

    lock = claims_agent._locks.setdefault(call_id, asyncio.Lock())
    async with lock:
        # Buffer raw claim transcript
        s["current"].append(utterance)
        s["full_transcript"].append(utterance)

        insurance_name = config_manager.get_insurance_name()

        # Full transcript for conversation history (what was actually said)
        full_text = " ".join(s["full_transcript"]).strip()

        # This is what GPT should see (trimmed for context window)
        if insurance_name.upper() in ("OSCAR", "HEALTH_FIRST"):
            chunk = utterance
        else:
            tail_chars = get_claims_tail_chars()
            chunk = full_text[-tail_chars:].strip()

        last_response = s.get("last_response", "")

        # Near-duplicate short-circuit (insurance-config gated).
        # If this insurer has dedupe enabled and the new chunk is near-
        # identical to the last chunk we ACTUALLY sent to GPT, skip the
        # GPT call and treat it as CONTINUE.
        #
        # IMPORTANT: `last_chunk` and `last_response` are ONLY updated
        # when GPT actually runs. Short-circuits must not overwrite them,
        # otherwise the prompt's duplicate-prevention rule (which receives
        # last_response) would lose track of what GPT last actually said
        # and could legitimately return the same action twice.
        dedupe_enabled = config_manager.get_dedupe_chunks()
        last_chunk = s.get("last_chunk", "")
        if dedupe_enabled and _is_near_duplicate_chunk(last_chunk, chunk):
            intent = "CONTINUE"
            logger.info(f"[{call_id}] 🔁 Near-duplicate chunk; skipping GPT → CONTINUE")
        else:
            intent = await _ask_gpt_keyword(
                call_id,
                chunk,
                last_response,
                conversation_history=full_text,
                charge_amount=_charge_for_call(call_id),
            )
            s["last_chunk"] = chunk
            s["last_response"] = intent

        # Store conversation history here (not inside _ask_gpt_keyword)
        # so we always capture the actual utterance, not the trimmed chunk
        claims_agent._append_conversation_step(call_id, utterance, intent)

        if intent.startswith("DTMF:"):
            digit = intent.split(":", 1)[1]
            # Aetna: pressing 2 hears the claim details — needed only ONCE per
            # claim. The "hear claim details or press 2" menu lingers in the
            # rolling transcript window, so GPT can re-emit DTMF:2 on that stale
            # text. Latch it: press 2 once, then convert any repeat to CONTINUE.
            if digit == "2" and insurance_name.upper() == "AETNA":
                if s.get("aetna_details_pressed"):
                    logger.info(
                        f"[{call_id}] ⏭️ Aetna: already pressed 2 for details this "
                        f"claim; ignoring repeat")
                    s["last_response"] = "CONTINUE"
                    return
                s["aetna_details_pressed"] = True
            if claims_agent._dtmf_cb:
                try:
                    await claims_agent._dtmf_cb(digit, call_id)
                    logger.info(f"[{call_id}] Sent DTMF: {digit}")
                except Exception as e:
                    logger.error(f"[{call_id}] Error sending DTMF: {e}")
            return

        if intent == "CHARGE":
            # IVR is disambiguating a multi-claim date by asking for the
            # specific claim's total charge. Speak the known charge from
            # visit_data (never let GPT echo the digits — it hallucinates them).
            charge = _charge_for_call(call_id)
            spoken = _charge_spoken(charge)
            if spoken and claims_agent._tts_cb:
                try:
                    await claims_agent._tts_cb(spoken, call_id)
                    logger.info(f"[{call_id}] Spoke total charge to select claim: {spoken!r}")
                except Exception as e:
                    logger.error(f"[{call_id}] Error speaking charge: {e}")
            else:
                logger.warning(
                    f"[{call_id}] IVR asked for total charge but none available in "
                    f"visit_data ({_CHARGE_KEYS}); cannot disambiguate claim."
                )
            return

        if intent == "STOP":
            await claims_agent.end_session(call_id, already_locked=True)
            return

        if intent == "NEXT":
            claims_agent._finalize_current(s)
            if claims_agent._tts_cb:
                try:
                    await claims_agent._tts_cb("Next claim", call_id)
                except Exception:
                    pass
            return

        if intent == "DETAILS":
            if claims_agent._tts_cb:
                try:
                    await claims_agent._tts_cb("Details", call_id)
                except Exception:
                    pass
            return

        if intent == "CONFIRM":
            if claims_agent._tts_cb:
                try:
                    await claims_agent._tts_cb("Yes", call_id)
                except Exception:
                    pass
            return

        if intent == "NO":
            if claims_agent._tts_cb:
                try:
                    await claims_agent._tts_cb("No", call_id)
                except Exception:
                    pass
            return

        if intent == "FAX-ID":
            if claims_agent._tts_cb:
                try:
                    await claims_agent._tts_cb("2144465424", call_id)
                except Exception:
                    pass
            return

        return


# ---------- GPT-4o controller (minimal logging) ----------

async def _ask_gpt_keyword(
    call_id: str,
    transcript_chunk: str,
    last_response: str,
    conversation_history: str = "",
    charge_amount: str = "",
) -> str:
    """
    Use GPT to return one control intent.
    Conversation history is stored by the caller (handle_final), not here.

    `conversation_history` is the full claim-mode transcript so far; templates
    that need cross-chunk context (e.g. UHC, which must remember the total
    claim count announced at the start of a multi-claim menu) reference it via
    the {conversation_history} placeholder. Templates that don't use it simply
    ignore the extra key.
    """
    prompt_template = get_controller_prompt_template()

    try:
        system_prompt = prompt_template.format_map({
            "transcript_chunk": transcript_chunk,
            "last_response": last_response or "",
            "conversation_history": conversation_history or "",
            "charge_amount": charge_amount or "",
        })
    except KeyError:
        system_prompt = prompt_template.format(transcript_chunk=transcript_chunk)

    logger.info(f"Transcript chunk sent to GPT: {transcript_chunk}")
    if last_response:
        logger.info(f"Last response: {last_response}")

    try:
        t0 = time.perf_counter()
        raw = await _call_gpt_api(system_prompt)
        ms = (time.perf_counter() - t0) * 1000

        if not raw:
            return "CONTINUE"

        intent = map_keyword(raw.upper())
        logger.info(f"[{call_id}] <- GPT: {raw!r} -> {intent} ({ms:.0f}ms)")
        return intent

    except Exception as e:
        logger.error(f"[{call_id}] GPT controller error: {e}")
        return "CONTINUE"
