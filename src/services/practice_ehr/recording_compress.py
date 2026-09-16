"""
Shrink a Telnyx call recording before uploading it to PracticeEHR.

Telnyx returns uncompressed stereo 8 kHz/16-bit PCM (~1.9 MB/min), which 413s
the doc-upload endpoint on long calls. A phone call gains nothing from stereo,
so we downmix to mono — lossless and enough for most calls. Calls too long for
even mono PCM fall back to mono G.711 u-law (the codec the audio already used on
the wire, so no real quality loss) to guarantee they fit.

Uses stdlib `audioop` (C-level, a few ms) — no ffmpeg, no extra dependency.
Note: `audioop` ships with CPython <=3.12; revisit if the runtime moves to 3.13+.
Never raises: any failure returns the original bytes so the upload still runs.
"""
import audioop
import io
import logging
import struct
import wave

logger = logging.getLogger(__name__)

MIN_SIZE_TO_COMPRESS = 10 * 1024 * 1024   # skip short recordings entirely
MAX_MONO_PCM_SIZE = 25 * 1024 * 1024      # above this, use u-law to stay under the endpoint limit

WAVE_FORMAT_MULAW = 0x0007


def compress_recording(wav_bytes: bytes) -> bytes:
    """Return a smaller mono WAV, or the original bytes if no shrink is needed/possible."""
    if len(wav_bytes) <= MIN_SIZE_TO_COMPRESS:
        return wav_bytes

    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            sample_rate = wav.getframerate()
            frames = wav.readframes(wav.getnframes())
    except Exception as exc:
        logger.warning(f"⚠️ Recording compress: unreadable WAV ({exc}) — uploading original")
        return wav_bytes

    if sample_width != 2:
        return wav_bytes

    try:
        mono = frames if channels == 1 else audioop.tomono(frames, sample_width, 0.5, 0.5)
        mono_wav = _mono_pcm_wav(mono, sample_rate, sample_width)

        if len(mono_wav) <= MAX_MONO_PCM_SIZE:
            result, label = mono_wav, "stereo→mono PCM"
        else:
            ulaw = audioop.lin2ulaw(mono, sample_width)
            result, label = _mono_ulaw_wav(ulaw, sample_rate), "stereo→mono u-law"
    except Exception as exc:
        logger.warning(f"⚠️ Recording compress failed ({exc}) — uploading original")
        return wav_bytes

    logger.info(
        f"🗜 Recording compressed {len(wav_bytes) / 1e6:.1f}MB → {len(result) / 1e6:.1f}MB "
        f"({label}, {len(wav_bytes) / max(len(result), 1):.1f}x)"
    )
    return result


def _mono_pcm_wav(frames: bytes, sample_rate: int, sample_width: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(sample_width)
        wav.setframerate(sample_rate)
        wav.writeframes(frames)
    return buffer.getvalue()


def _mono_ulaw_wav(ulaw_bytes: bytes, sample_rate: int) -> bytes:
    """Build a mono u-law WAV by hand (stdlib `wave` only writes PCM)."""
    sample_count = len(ulaw_bytes)
    fmt_chunk = struct.pack(
        "<HHIIHHH",
        WAVE_FORMAT_MULAW,  # format tag
        1,                  # channels
        sample_rate,
        sample_rate,        # byte rate (block align is 1)
        1,                  # block align
        8,                  # bits per sample
        0,                  # cbSize
    )
    fact_chunk = struct.pack("<I", sample_count)
    data = ulaw_bytes + (b"\x00" if sample_count % 2 else b"")
    riff_size = 4 + (8 + len(fmt_chunk)) + (8 + len(fact_chunk)) + (8 + len(data))

    out = io.BytesIO()
    out.write(b"RIFF" + struct.pack("<I", riff_size) + b"WAVE")
    out.write(b"fmt " + struct.pack("<I", len(fmt_chunk)) + fmt_chunk)
    out.write(b"fact" + struct.pack("<I", len(fact_chunk)) + fact_chunk)
    out.write(b"data" + struct.pack("<I", sample_count) + data)
    return out.getvalue()
