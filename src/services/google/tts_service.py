"""Google Cloud Text-to-Speech engine (Chirp3-HD voices).

Drop-in alternative to the Azure TTS engine. Selected at runtime via the
`TTS_PROVIDER` env var (see main.py). The public entry point
`speak_with_google` has the SAME call contract as `speak_with_azure`
(`text, call_control_id, active_calls`) and produces the SAME wire format —
raw 8 kHz mono mulaw (PCMU) — so NOTHING downstream changes: the Telnyx
answer payload, the inbound STT path, and stream.py are all untouched, and
the outbound chunk/pacing loop is identical to the Azure path.

Why Chirp3-HD: it is Google's most natural, human-sounding voice family.
Caveat handled here: Chirp3-HD voices do NOT support SSML, so the digit-by-digit
spelling of IDs/ICNs (which the Azure path does via <say-as>) is reproduced in
PLAIN TEXT (comma-separated characters) to preserve today's spelling behaviour.
"""

import asyncio
import base64
import json
import os
import re
import time
import logging
from typing import Optional

from google.cloud import texttospeech_v1 as texttospeech
from google.api_core.client_options import ClientOptions

from src.utils.transcript import append_agent
# Reuse the pure token classifiers from the Azure engine so both engines agree
# on what counts as a "date" or a "spell-it-out code" — no behaviour drift.
# Also borrow the Azure engine itself for spelling out IDs (see speak_with_google).
from src.services.azure.tts_service import (
    _is_date_token,
    _is_code_token,
    speak_with_azure as _azure_speak,
)

logger = logging.getLogger(__name__)

# Most natural conversational Chirp3-HD voice; override without a code change.
GOOGLE_TTS_VOICE = os.getenv("GOOGLE_TTS_VOICE", "en-US-Chirp3-HD-Achird")
GOOGLE_TTS_LANGUAGE = os.getenv("GOOGLE_TTS_LANGUAGE", "en-US")

CHUNK_SIZE = 800          # bytes of 8 kHz mulaw per WebSocket frame
# Stream frames AHEAD of playback with a tiny inter-frame sleep so Telnyx keeps a
# buffer cushion — event-loop jitter (STT callbacks, background GPT) then can't
# starve the stream and make the voice choppy. (Real-time pacing, 0.1s, left zero
# headroom.) The half-duplex echo gate is held for the TRUE audio duration
# separately in speak_with_google, so sending fast does NOT re-open the mic early.
SEND_PACING = 0.015       # seconds between frames while sending ahead
BYTES_PER_SECOND = 8000   # 8 kHz mono 8-bit mulaw → 8000 bytes = 1 second of audio
TTS_REENABLE_ASR_DELAY = 0.2

# Low-latency mode: stream audio out of Google's streaming-synthesize API AS it
# is generated, so the bot starts speaking ~1s+ sooner instead of waiting for the
# whole sentence to synthesize first.
#
# CODE-CONFIGURABLE: default is ON (True) so it's active on QA without needing env
# access — flip _STREAMING_DEFAULT to "false" here to turn it off in code. The env
# var GOOGLE_TTS_STREAMING overrides it when set. ANY streaming error falls back
# per-utterance to the proven blocking synth, so a call can never break — worst
# case is audio quality, which you evaluate on QA. ⚠️ Re-confirm this before a
# wider prod rollout.
_STREAMING_DEFAULT = "true"   # ← toggle here (no env needed)
GOOGLE_TTS_STREAMING = os.getenv("GOOGLE_TTS_STREAMING", _STREAMING_DEFAULT).lower() in ("true", "1", "yes")

# Shared async client (connection reuse — mirrors the Azure http-client singleton).
_tts_client: Optional[texttospeech.TextToSpeechAsyncClient] = None


def get_tts_client() -> texttospeech.TextToSpeechAsyncClient:
    """Get or lazily create the shared Google TTS async client.

    Auth resolves in this order:
      1. GOOGLE_APPLICATION_CREDENTIALS_JSON — the service-account key's JSON
         *content* pasted into an env var. Preferred on hosts (Azure App
         Service) where committing a key file isn't possible; keeps the secret
         out of git entirely.
      2. GOOGLE_APPLICATION_CREDENTIALS — a file PATH to the key (local dev).
    """
    global _tts_client
    if _tts_client is None:
        client_options = ClientOptions(api_endpoint="texttospeech.googleapis.com")
        creds_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
        if creds_json:
            from google.oauth2 import service_account
            credentials = service_account.Credentials.from_service_account_info(
                json.loads(creds_json)
            )
            _tts_client = texttospeech.TextToSpeechAsyncClient(
                credentials=credentials, client_options=client_options
            )
        else:
            # Falls back to GOOGLE_APPLICATION_CREDENTIALS (file path) or ADC.
            _tts_client = texttospeech.TextToSpeechAsyncClient(
                client_options=client_options
            )
    return _tts_client


async def cleanup_tts_client():
    """Release the shared client on app shutdown."""
    global _tts_client
    _tts_client = None


def _shape_text_for_chirp(text: str) -> str:
    """Reproduce the Azure SSML spelling rules in plain text (Chirp has no SSML).

    - Dates (06/05/2024) are spoken as-is; Chirp reads them naturally.
    - Codes / long IDs (ICN, member id) are spelled character-by-character as a
      comma-separated list ("1, 5, 2, 8, ...") so Chirp reads each digit/letter
      individually instead of as one giant number.
    - Everything else is passed through unchanged.
    """
    clean = " ".join(text.split())

    if _is_date_token(clean):
        return clean

    if _is_code_token(clean):
        return ", ".join(ch for ch in clean if not ch.isspace())

    # Prose: a long digit run embedded in a sentence (e.g. "the provider NPI is
    # 1083263875") would be read as a cardinal ("one billion..."). Spell any run
    # of 7+ digits one digit at a time. Threshold 7 leaves years (4) and typical
    # money amounts alone.
    return re.sub(r"\d{7,}", lambda m: ", ".join(m.group()), clean)


def _extract_pcm_mulaw(audio: bytes) -> bytes:
    """Return raw mulaw payload, stripping a RIFF/WAV header if Google added one.

    Google may wrap MULAW output in a WAV container. Telnyx wants raw mulaw, so
    we locate the `data` sub-chunk and return its bytes; if there's no RIFF
    header the content is already raw and returned as-is.
    """
    if audio[:4] == b"RIFF":
        idx = audio.find(b"data")
        if idx != -1:
            # 4 bytes 'data' + 4 bytes little-endian size, then the samples.
            return audio[idx + 8:]
    return audio


def _normalize_google_voice(voice_id: str) -> str:
    """Google wants BCP-47 casing on the language tag ('en-US-Chirp3-HD-Zephyr').
    The Support API sends it lowercased ('en-us-…'), so upper the region subtag."""
    parts = (voice_id or "").split("-")
    if len(parts) >= 2:
        parts[0], parts[1] = parts[0].lower(), parts[1].upper()
        return "-".join(parts)
    return voice_id or GOOGLE_TTS_VOICE


def _resolve_voice(call_state) -> str:
    """Per-call Google voice from the assigned agent's `voiceId`; falls back to
    the global default when there's no profile/voiceId (safe, non-breaking)."""
    try:
        vid = (getattr(call_state, "agent_profile", None) or {}).get("voiceId")
        if vid:
            return _normalize_google_voice(vid)
    except Exception:
        pass
    return GOOGLE_TTS_VOICE


async def _synthesize_mulaw(text: str, voice: str = GOOGLE_TTS_VOICE) -> bytes:
    """Synthesize `text` to 8 kHz mono mulaw bytes via Google TTS."""
    shaped = _shape_text_for_chirp(text)
    if not shaped:
        return b""

    client = get_tts_client()
    response = await client.synthesize_speech(
        input=texttospeech.SynthesisInput(text=shaped),
        voice=texttospeech.VoiceSelectionParams(
            language_code=GOOGLE_TTS_LANGUAGE,
            name=voice,
        ),
        audio_config=texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MULAW,
            sample_rate_hertz=8000,
        ),
    )
    return _extract_pcm_mulaw(response.audio_content)


def _should_spell_via_azure(text: str) -> bool:
    """True for a standalone ID/code that must be spelled character-by-character
    (route to Azure's crisp SSML). False for money amounts — which read
    naturally, so Google keeps them — and for prose/dates.
    """
    clean = " ".join(text.split())
    if not _is_code_token(clean):
        return False
    # A decimal money amount (e.g. "297.65", "$1,299.00") reads naturally — an ID
    # never has a decimal point, so exclude these from spelling.
    if "." in clean and clean.replace("$", "").replace(",", "").replace(".", "").isdigit():
        return False
    return True


async def _stream_synthesize_to_ws(text: str, ws, voice: str = GOOGLE_TTS_VOICE) -> None:
    """Low-latency path: stream audio out of Google's streaming-synthesize API AS
    it is generated and forward each chunk to Telnyx immediately — so playback
    starts before the whole sentence is synthesized. Requests MULAW/8 kHz
    directly (no conversion). Same pacing + echo/half-duplex hold as the blocking
    path: we send ahead of real time, then sleep the remainder so is_tts_active
    stays True until playback actually finishes."""
    shaped = _shape_text_for_chirp(text)
    if not shaped:
        return
    client = get_tts_client()
    config = texttospeech.StreamingSynthesizeConfig(
        voice=texttospeech.VoiceSelectionParams(
            language_code=GOOGLE_TTS_LANGUAGE, name=voice
        ),
        streaming_audio_config=texttospeech.StreamingAudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MULAW,
            sample_rate_hertz=8000,
        ),
    )

    async def _requests():
        yield texttospeech.StreamingSynthesizeRequest(streaming_config=config)
        yield texttospeech.StreamingSynthesizeRequest(
            input=texttospeech.StreamingSynthesisInput(text=shaped)
        )

    send_start = time.monotonic()
    total_bytes = 0
    stream = await client.streaming_synthesize(requests=_requests())
    async for response in stream:
        audio = _extract_pcm_mulaw(response.audio_content)
        for offset in range(0, len(audio), CHUNK_SIZE):
            frame = audio[offset:offset + CHUNK_SIZE]
            if not frame:
                continue
            payload = base64.b64encode(frame).decode("ascii")
            msg = {"event": "media", "media": {"track": "outbound", "payload": payload}}
            await ws.send_text(json.dumps(msg))
            total_bytes += len(frame)
            await asyncio.sleep(SEND_PACING)

    # We streamed faster than real time; hold the gate until playback finishes so
    # the mic doesn't reopen early and transcribe our own voice (echo).
    audio_seconds = total_bytes / BYTES_PER_SECOND
    elapsed = time.monotonic() - send_start
    if elapsed < audio_seconds:
        await asyncio.sleep(audio_seconds - elapsed)
    await asyncio.sleep(TTS_REENABLE_ASR_DELAY)


async def speak_with_google(text: str, call_control_id: str, active_calls: dict):
    """Generate Google TTS audio and stream it back over the same WebSocket.

    Same contract and side effects as `speak_with_azure`: appends to history,
    serializes on `tts_lock`, toggles `is_tts_active`, and streams mulaw in the
    identical chunk/pacing pattern.
    """
    # "Azure spells, Google talks." Chirp3-HD has no SSML say-as and mangles
    # spelled-out IDs (letters like "BSW..." come out garbled, so IVRs reject
    # them). For a standalone code/ID token, hand the whole utterance to the
    # Azure engine — its <say-as> spells crisply. Everything else (prose,
    # amounts, dates) stays on Google's natural voice. Delegated BEFORE any
    # history/lock work so Azure does its own — no double append.
    if _should_spell_via_azure(text):
        return await _azure_speak(
            text,
            call_control_id,
            active_calls,
            AZURE_SPEECH_KEY=os.getenv("AZURE_SPEECH_KEY"),
            AZURE_SPEECH_REGION=os.getenv("AZURE_SPEECH_REGION"),
        )

    call_state = active_calls.get(call_control_id)
    ws = getattr(call_state, "websocket", None)
    if not ws:
        logger.error("No WebSocket found for TTS")
        return

    # Per-agent voice from the Support API profile (falls back to the default).
    voice = _resolve_voice(call_state)

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
            logger.info(f" Generating Google TTS ({voice}): {text!r}")

            # Low-latency streaming path (opt-in). On ANY failure fall through to
            # the proven blocking synth below, so this can never break a call.
            if GOOGLE_TTS_STREAMING:
                try:
                    await _stream_synthesize_to_ws(text, ws, voice)
                    logger.info("Google TTS (streaming) complete")
                    return
                except Exception as e:
                    logger.warning(f"Google streaming TTS failed ({e}); falling back to blocking synth")

            audio_bytes = await _synthesize_mulaw(text, voice)
            if not audio_bytes:
                logger.warning("Google TTS returned no audio")
                return

            # Send the frames ahead of playback (small pacing) so Telnyx buffers
            # a cushion → smooth, jitter-proof audio.
            audio_seconds = len(audio_bytes) / BYTES_PER_SECOND
            send_start = time.monotonic()
            for offset in range(0, len(audio_bytes), CHUNK_SIZE):
                chunk = audio_bytes[offset:offset + CHUNK_SIZE]
                payload = base64.b64encode(chunk).decode("ascii")
                msg = {"event": "media", "media": {"track": "outbound", "payload": payload}}
                await ws.send_text(json.dumps(msg))
                await asyncio.sleep(SEND_PACING)

            # We sent faster than real time, but Telnyx is still PLAYING. Hold the
            # half-duplex gate (is_tts_active stays True in the finally below) until
            # the audio has actually finished playing, so the mic doesn't re-open
            # early and transcribe the bot's own voice (echo).
            elapsed = time.monotonic() - send_start
            if elapsed < audio_seconds:
                await asyncio.sleep(audio_seconds - elapsed)

            await asyncio.sleep(TTS_REENABLE_ASR_DELAY)
            logger.info("Google TTS streaming complete")

        except Exception as e:
            logger.error(f"Google TTS error: {e}")
        finally:
            setattr(call_state, "is_tts_active", False)
