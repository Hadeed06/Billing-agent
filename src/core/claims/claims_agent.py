import asyncio
import logging
from typing import Dict, List
from dotenv import load_dotenv
from src.config.insurance_config import config_manager

load_dotenv()
logger = logging.getLogger(__name__)


# ── per-call state ──────────────────────────────────────────────────────────
_sessions: Dict[str, Dict] = {}       # call_id -> {"active": bool, "current": List[str], "claims": List[str]}
_locks: Dict[str, asyncio.Lock] = {}  # call_id -> asyncio.Lock

# ── injected callbacks from main ────────────────────────────────────────────
_hangup_cb = None        # async def (call_id: str) -> None
_tts_cb = None           # async def (text: str, call_id: str) -> None
_dtmf_cb = None          # async def (dtmf: str, call_id: str) -> None
_active_calls = None


def register_active_calls(active_calls):
    global _active_calls
    _active_calls = active_calls


def register_hangup(cb):
    """Main should call this once: claims_agent.register_hangup(hangup_call)"""
    global _hangup_cb
    _hangup_cb = cb


def register_tts(cb):
    """Main must call once: claims_agent.register_tts(speak_with_azure)"""
    global _tts_cb
    _tts_cb = cb


def register_dtmf(cb):
    """Main should call once: claims_agent.register_dtmf(send_dtmf)"""
    global _dtmf_cb
    _dtmf_cb = cb


# ── session lifecycle ───────────────────────────────────────────────────────
def is_active(call_id: str) -> bool:
    s = _sessions.get(call_id)
    return bool(s and s.get("active"))


async def start_session(call_id: str):
    _sessions[call_id] = {
        "active": True,
        "current": [],
        "claims": [],
        "last_response": "",
        "last_chunk": "",  # last transcript chunk ACTUALLY sent to GPT (used for dedupe)
        "full_transcript": [],
    }
    _locks[call_id] = _locks.get(call_id) or asyncio.Lock()


async def end_session(call_id: str, *, already_locked: bool = False):
    """End the claims session, persist captured claims onto CallState, and hang up."""
    s = _sessions.get(call_id)
    if not s or not s.get("active"):
        return

    async def _finish():
        _finalize_current(s)

        # Build the data we persist on call_state for the post-call upload.
        # Verbose terminal dumps (full transcript / claims / conversation
        # history) were removed — they're available in the uploaded JSON
        # transcript and the recording.
        full_claims_text = "\n\n--- CLAIM BREAK ---\n\n".join(s.get("claims", []))
        raw_full_transcript = " ".join(s.get("full_transcript", []))

        try:
            call_state = _active_calls.get(call_id) if _active_calls else None
        except Exception:
            call_state = None

        if call_state:
            call_state.finalized_claims = s.get("claims", [])
            call_state.full_claims_transcript = full_claims_text
            call_state.raw_full_transcript = raw_full_transcript

        logger.info(f"📦 Claims session ended: {call_id} (claims captured: {len(s.get('claims', []))})")

        s["active"] = False

        if _hangup_cb:
            try:
                await _hangup_cb(call_id)
            except Exception:
                pass

    if already_locked:
        await _finish()
    else:
        async with _locks.setdefault(call_id, asyncio.Lock()):
            await _finish()

    # Release per-call state so these module-level dicts don't grow one entry
    # per call forever (memory leak). Safe here: end_session is the single
    # idempotent teardown (the guard at the top makes a second call a no-op),
    # and any late claims event tolerates a missing session (is_active → False).
    # The _locks pop runs AFTER the `async with` above has released the lock.
    _sessions.pop(call_id, None)
    _locks.pop(call_id, None)


def get_claims(call_id: str) -> List[str]:
    s = _sessions.get(call_id) or {}
    return list(s.get("claims", []))


def _finalize_current(s: Dict):
    txt = " ".join(s["current"]).strip()
    if txt:
        s["claims"].append(txt)
    s["current"].clear()  # next claim starts clean


def _append_conversation_step(call_id: str, transcript: str, gpt_result: str):
    if not _active_calls:
        return

    call_state = _active_calls.get(call_id)
    if not call_state:
        return

    transcript = (transcript or "").strip()
    gpt_result = (gpt_result or "").strip()

    # don't add empty rows
    if not transcript and not gpt_result:
        return

    call_state.add_history({
        "transcript": transcript,
        "gpt_result": gpt_result
    })


# ── re-export handle_final so existing imports don't break ──────────────────
from src.core.claims.claims_controller import handle_final  # noqa: E402, F401
