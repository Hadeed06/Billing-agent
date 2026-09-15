import asyncio
import base64
import json
import os
import re
import time
import httpx
import logging

from src.utils.transcript import append_agent
from src.utils.dates import as_mdy_date
from src.services.http_client import get_http_client, request_timeout

logger = logging.getLogger(__name__)

# --- Tiny-pause spelling (no global slowdown) ---
PAUSE_MS_EACH       = 120
NUMERIC_HEAVY_RATIO = 0.60

DATE_SLASH_RE = re.compile(r"^\s*\d{1,2}/\d{1,2}/\d{2,4}\s*$")

def _is_date_token(s: str) -> bool:
    return bool(DATE_SLASH_RE.match(s))

def _is_code_token(s: str) -> bool:
    if not s or any(ch.isspace() for ch in s):
        return False
    if "/" in s:
        return False
    has_letters = any(ch.isalpha() for ch in s)
    has_digits  = any(ch.isdigit() for ch in s)
    if not has_digits:
        return False
    digit_ratio = sum(ch.isdigit() for ch in s) / len(s)
    return has_letters or digit_ratio >= NUMERIC_HEAVY_RATIO


# TTS voice — env-configurable so you can A/B or revert without a code change.
# Ava (multilingual) is a newer, more natural conversational voice than the
# older Jenny. To go back: set AZURE_TTS_VOICE=en-US-JennyNeural.
voice_name = os.getenv("AZURE_TTS_VOICE", "en-US-AvaMultilingualNeural")
# Slight speed-up on normal speech makes the cadence conversational instead of
# the flat "announcer" default. Applied ONLY to prose — dates and spelled-out
# codes/IDs stay at default speed for clarity. Set AZURE_TTS_RATE=+0% to disable.
SPEECH_RATE = os.getenv("AZURE_TTS_RATE", "+6%")

def _build_ssml_for(text: str) -> str:
    clean = " ".join(text.split())

    # 1) Dates (DOB / DOS) — MMDDYYYY or MM/DD/YYYY → speak as a natural date
    #    e.g. "12142010" → "December fourteenth, two thousand ten" (NOT digit-by-
    #    digit). Uses Azure <say-as interpret-as="date"> so the month name,
    #    ordinal day, and year grouping come out correctly. format="mdy" matches
    #    our MMDDYYYY input and what the UHC assistant expects ("...June 19th 1967").
    _mdy = as_mdy_date(clean)
    if _mdy:
        return f"""
<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">
  <voice xml:lang="en-US" xml:gender="Female" name="{voice_name}">
    <say-as interpret-as="date" format="mdy">{_mdy}</say-as>
  </voice>
</speak>
""".strip()

    # 2) Pure-numeric IDs (member ID, NPI, reference #, etc.) → ONE smooth
    #    digits block. interpret-as="digits" reads each digit separately
    #    ("1-1-2-6-5-9-...") but as a SINGLE utterance — no per-digit <break>
    #    or prosody reset, which removes the choppy/laggy cadence. It never
    #    reads them as a cardinal ("one hundred twelve million…") — that only
    #    happens with plain text or interpret-as="number"/"cardinal".
    if _is_code_token(clean) and clean.isdigit():
        return f"""
<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">
  <voice xml:lang="en-US" xml:gender="Female" name="{voice_name}">
    <say-as interpret-as="digits">{clean}</say-as>
  </voice>
</speak>
""".strip()

    # 3) Alphanumeric codes (letters + digits) → spell each character so the
    #    letters vs digits stay unambiguous. Per-char breaks are kept here
    #    because mixed IDs genuinely need the crisp separation.
    if _is_code_token(clean):
        tokens = []
        for ch in clean:
            if ch.isalpha():
                tokens.append(f'<lang xml:lang="en-US"><say-as interpret-as="characters">{ch}</say-as></lang>')
            elif ch.isdigit():
                tokens.append(f'<lang xml:lang="en-US"><say-as interpret-as="digits">{ch}</say-as></lang>')
            else:
                tokens.append(f'<break time="{PAUSE_MS_EACH}ms"/>')
        inner = f' <break time="{PAUSE_MS_EACH}ms"/> '.join(tokens)
        return f"""
<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">
  <voice xml:lang="en-US" xml:gender="Female" name="{voice_name}">
    {inner}
  </voice>
</speak>
""".strip()

    # 4) Everything else → normal speech (light rate bump for a conversational,
    #    less "announcer" cadence)
    return f"""
<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">
  <voice xml:lang="en-US" xml:gender="Female" name="{voice_name}">
    <prosody rate="{SPEECH_RATE}">{clean}</prosody>
  </voice>
</speak>
""".strip()


TTS_REENABLE_ASR_DELAY = 0.2

async def speak_with_azure(
    text: str,
    call_control_id: str,
    active_calls: dict,
    AZURE_SPEECH_KEY: str,
    AZURE_SPEECH_REGION: str
):
    """Generate Azure TTS audio and stream it back via the same WebSocket."""
    call_state = active_calls.get(call_control_id)
    ws = getattr(call_state, "websocket", None)
    if not ws:
        logger.error("No WebSocket found for TTS")
        return
    
    try:
        if call_state is not None:
            call_state.add_history({"role": "assistant", "content": text})
        append_agent(call_state, text)
    except Exception as e:
        logger.error(f"Error appending to conversation history: {e}")

    if not hasattr(call_state, "tts_lock"):
        call_state.tts_lock = asyncio.Lock()

    async with call_state.tts_lock:
        setattr(call_state, "is_tts_active", True)
        try:
            logger.info(f" Generating TTS: {text!r}")
            ssml = _build_ssml_for(text)

            url = f"https://{AZURE_SPEECH_REGION}.tts.speech.microsoft.com/cognitiveservices/v1"
            headers = {
                "Ocp-Apim-Subscription-Key": AZURE_SPEECH_KEY,
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": "raw-8khz-8bit-mono-mulaw",
            }

            client = get_http_client()
            resp = await client.post(url, content=ssml, headers=headers, timeout=request_timeout(read=15))
            resp.raise_for_status()
            audio_bytes = resp.content

            chunk_size = 800
            for offset in range(0, len(audio_bytes), chunk_size):
                chunk = audio_bytes[offset:offset + chunk_size]
                payload = base64.b64encode(chunk).decode("ascii")
                msg = {"event": "media", "media": {"track": "outbound", "payload": payload}}
                await ws.send_text(json.dumps(msg))
                await asyncio.sleep(0.1)

            await asyncio.sleep(TTS_REENABLE_ASR_DELAY)
            logger.info("TTS streaming complete")

        except Exception as e:
            logger.error(f"TTS error: {e}")
        finally:
            setattr(call_state, "is_tts_active", False)
