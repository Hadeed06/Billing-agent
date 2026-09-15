# services/llm_service.py
from __future__ import annotations
import os
import httpx
import logging
import re
from openai import AsyncAzureOpenAI
logger = logging.getLogger(__name__)
from dotenv import load_dotenv

load_dotenv()

# Initialize client once (do NOT create per request)
azure_client = AsyncAzureOpenAI(
    api_key=os.getenv("PTU_API_KEY"),
    azure_endpoint=os.getenv("PTU_AZURE_ENDPOINT"),
    api_version=os.getenv("PTU_API_VERSION"),
)

async def _call_gpt_api(prompt: str, max_tokens: int = 50) -> str:
    """
    Azure GPT call replacing LLaMA.
    Returns plain text response.

    max_tokens defaults to 50 — enough for the short IVR-control replies
    (one-word intents, "say:"/"value:" commands). Callers that need a longer
    response (e.g. the claim classifier returning JSON + a description) should
    pass a larger value.
    """

    try:
        response = await azure_client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1"),  # Azure deployment name
            messages=[
                {
                    "role": "system",
                    "content": "You are an outbound calling agent for insurance IVR handling claim status calls."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=0,
            max_tokens=max_tokens,
        )


        answer = response.choices[0].message.content or "(no response)"
        # Exact per-call token usage from the provider (more accurate than
        # tiktoken — includes message/role overhead). Auto-tagged with the
        # call_id by the logger, so you can sum per call in App Insights.
        usage = getattr(response, "usage", None)
        if usage is not None:
            logger.info(
                f"📊 GPT tokens: prompt={usage.prompt_tokens} "
                f"completion={usage.completion_tokens} total={usage.total_tokens}"
            )
        logger.info(f"🤖 GPT response: {answer!r}")
        return answer.strip()

    except Exception as e:
        logger.error(f"❌ GPT API exception: {e}")
        raise

# ---- text command parsing helper----
PAUSE_RE = re.compile(r"\s+")

def _normalize(s: str) -> str:
    return s.strip().strip("`").strip()

def _after(s: str, low: str, keyword: str) -> str | None:
    i = low.find(keyword)
    if i == -1:
        return None
    j = i + len(keyword)
    while j < len(s) and s[j] in " :=-\t":
        j += 1
    if j >= len(s):
        return ""
    if s[j] in ("'", '"'):
        q = s[j]
        k = s.find(q, j + 1)
        return s[j + 1:k].strip() if k != -1 else s[j + 1:].strip()
    return s[j:].strip()


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance. Used only on short ID strings so O(n*m) is fine."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            curr.append(min(
                curr[-1] + 1,               # insert
                prev[j] + 1,                # delete
                prev[j - 1] + (ca != cb),   # substitute
            ))
        prev = curr
    return prev[-1]


def _correct_hallucinated_member_id(gpt_echo: str, visit_data: dict) -> str | None:
    """Detect GPT character-substitution on member_id (1↔I, 0↔O, 5↔S, etc.).

    Returns the authoritative member_id only when GPT's echo is same length
    and edit distance ≤ 2 from the real member_id. Otherwise returns None so
    the caller uses GPT's value unchanged.

    Only touches member_id — tax_id, npi, dob, dos, member_name are numeric
    or structured and don't have this failure mode. All-digit member_ids are
    also skipped: they aren't prone to letter/digit confusion.
    """
    real = visit_data.get("member_id")
    if not real or not isinstance(real, str):
        return None
    if not any(c.isalpha() for c in real):
        return None

    norm_gpt = gpt_echo.strip().upper().replace(" ", "").replace("-", "")
    norm_real = real.strip().upper().replace(" ", "").replace("-", "")

    if norm_gpt == norm_real:
        return None
    if len(norm_gpt) != len(norm_real):
        return None
    if _edit_distance(norm_gpt, norm_real) > 2:
        return None

    return real


# ---- response handler (dependencies injected) ----
async def _process_llama_response(
    response: str,
    call_control_id: str,
    *,
    speak_with_azure,                 # callable(text, call_id)
    send_dtmf,                        # callable(digits, call_id)
    ensure_call_cleanup,              # callable(call_id, reason=..., send_hangup=...)
    active_calls: dict,
) -> None:
    """
    Implements exactly the same behavior you had in main.py:
      - say/value/confirm -> TTS
      - dtmf -> send DTMF
      - end/endcall/hangup -> cleanup + hangup
      - 'fallback' and unknown -> ignore
    All dependencies are injected from main via partial.
    """
    if not response:
        logger.info("❗ Llama returned empty response")
        return

    s = _normalize(response)
    if not s:
        return

    # If it's just a quoted string, treat it as: say <text>
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = f"say {s[1:-1].strip()}"

    low = s.lower()

    # Ignore explicit "fallback" (do nothing)
    if low == "fallback" or low.startswith("fallback "):
        logger.info("Llama returned fallback; ignoring")
        return

    # claim_mode / rep_mode are handled in main.py BEFORE this function is
    # called (safety-nets for is_claim_start / the denial-IVR rep detection).
    # Guard here as defense in depth so they never fall through as
    # "unrecognized" — and can never be spoken — if the flow ever reroutes.
    compact_signal = low.replace(" ", "").replace("_", "").rstrip(".,!?:;'\"")
    if compact_signal in ("claimmode", "repmode"):
        logger.info(f"Llama returned mode signal {s!r}; already handled upstream, ignoring")
        return

    # 1) DTMF (explicit only)
    if low.startswith("dtmf"):
        tail = _after(s, low, "dtmf") or ""
        digits = "".join(ch for ch in tail if ch.isdigit() or ch in "*#")
        if digits:
            logger.info(f"→ Sending DTMF: {digits}")
            await send_dtmf(digits, call_control_id)
        else:
            logger.info("DTMF payload empty after sanitizing; ignoring")
        return

    # 2) SAY / VALUE / CONFIRM → speak
    for kw in ("say", "value", "confirm"):
        val = _after(s, low, kw)
        if val:
            # GPT sometimes substitutes look-alike characters when echoing
            # alphanumeric member_ids (U1 → UI, O0 → OO, 5 → S). Match against
            # the authoritative member_id from visit_data and correct if it
            # looks like a hallucination (same length, edit distance ≤ 2).
            if kw == "value":
                call_state = active_calls.get(call_control_id)
                visit_data = getattr(call_state, "visit_data", None) if call_state else None
                if visit_data:
                    corrected = _correct_hallucinated_member_id(val, visit_data)
                    if corrected is not None and corrected != val:
                        logger.warning(
                            f"⚠️ GPT hallucinated member_id: sent {val!r}, "
                            f"corrected to {corrected!r}"
                        )
                        val = corrected
            logger.info(f"→ Speak ({kw}): {val!r}")
            await speak_with_azure(val, call_control_id)
            return

    # 3) End / hangup
    compact = low.replace(" ", "")
    if compact in ("endcall", "end", "hangup"):
        logger.info("→ Hanging up per instruction")
        # NOTE: do NOT set cs.status = "hangup" here. ensure_call_cleanup only
        # sends the Telnyx hangup when status is not already "hangup"/"ended";
        # pre-setting it would make cleanup skip the hangup and the call would
        # linger on Telnyx (and the recording would never finalize). Let
        # cleanup send the hangup and let the webhook set the status.
        await ensure_call_cleanup(call_control_id, reason="llm: end/hangup command", send_hangup=True)
        return

    # 4) Explicit no-response → ignore
    if "no response" in low or compact == "noresponse":
        logger.info("→ No response; continue listening")
        return

    # 5) Anything else → ignore (no TTS)
    logger.info(f"Ignoring unrecognized Llama reply: {s!r}")
