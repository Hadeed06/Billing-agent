"""
Live denial-reason classification — two-tier, mirroring failure_classifier:

  1. `match_reason_rules()` — free substring rules over the 13-reason
     registry, run synchronously on payer utterances (claim readout AND
     rep speech). Microseconds, zero API cost. First confident match
     sticks; a later different confident match (rep contradicts IVR)
     overwrites with a log line.
  2. `classify_reason_gpt()` — GPT fallback, fired as a BACKGROUND task
     (never in the speech loop), at most twice per call: once at the
     pivot if rules haven't matched, once more in rep phase when a
     "reason cue" is heard. Writes the result onto CallState; the next
     turn's re-rendered prompt picks it up.

Out-of-scope: GPT may answer that the stated reason matches none of the
13 — the call then wraps up gracefully and the outcome is reported to the
billing team with the verbatim reason (Path C).
"""
import logging
from typing import Optional, Tuple

from src.core.denials.denial_reasons import (
    DENIAL_REASONS,
    OUT_OF_SCOPE_KEY,
    DenialReason,
)
from src.services.llm.llm_service import _call_gpt_api

logger = logging.getLogger(__name__)


# Generic IVR/rep legal disclaimers that contain denial-reason keywords
# ("medical necessity", "eligibility", etc.) but are NOT the actual reason.
# These are stripped before rule matching so boilerplate can't trigger a
# false classification (e.g. WellMed IPA denial mislabeled medical_necessity
# because the IVR read "...subject to policy guidelines, medical necessity,
# and member eligibility..."). Real 2026 Humana boilerplate included.
_BOILERPLATE_PHRASES = (
    "subject to policy guidelines medical necessity and member eligibility",
    "subject to policy guidelines, medical necessity, and member eligibility",
    "policy guidelines medical necessity and member eligibility",
    "medical necessity and member eligibility",
    "this is only an estimate of benefits",
    "only an estimate of benefits",
    "all payments are subject to",
    "subject to changes in terms conditions and members eligibility",
    "subject to change in terms conditions and member eligibility",
    "at the time services are performed",
    "at time of service",
)


def _normalize(text: str) -> str:
    return " ".join(text.replace("’", "'").lower().split())


def _strip_boilerplate(t: str) -> str:
    """Remove generic legal-disclaimer phrases so their embedded reason
    keywords can't cause a false match. Operates on already-normalized text."""
    for phrase in _BOILERPLATE_PHRASES:
        t = t.replace(phrase, " ")
    return " ".join(t.split())


def match_reason_rules(text: str) -> Optional[DenialReason]:
    """Registry entry whose trigger phrase matches MOST SPECIFICALLY.

    Boilerplate legalese is stripped first, so a reason keyword buried in a
    generic disclaimer ("subject to ... medical necessity ...") never
    triggers a false classification.

    When a rambling rep utterance hits triggers for MORE THAN ONE reason
    (e.g. "the documentation submitted does not meet the code criteria ...
    this is a billing error" matches both docs_required AND missing_info),
    we pick the reason whose matched keyword is the LONGEST — the most
    specific phrase wins over a stray generic word. This prevents a short,
    weak keyword ("billing error") from hijacking the real, specific CARC
    phrasing ("documentation submitted does not meet"). Ties keep registry
    order (first match wins), so single-hit behaviour is unchanged.
    """
    if not text:
        return None
    t = _strip_boilerplate(_normalize(text))
    best: Optional[DenialReason] = None
    best_len = 0
    for reason in DENIAL_REASONS.values():
        for kw in reason.trigger_keywords:
            if kw in t and len(kw) > best_len:
                best = reason
                best_len = len(kw)
    return best


# Phrases in rep speech that signal the denial reason is being (or is about
# to be) stated — used to trigger the second GPT fallback attempt.
_REASON_CUES = (
    "denied because",
    "denial reason",
    "reason for the denial",
    "reason is",
    "was denied for",
    "got denied",
    "denied due to",
    "the claim is denied",
    # Broader phrasings reps use to explain WHY without saying "denied because"
    # (e.g. COB: "an EOB from the primary is needed before this can be considered").
    "is needed before",
    "is required before",
    "before this claim can be considered",
    "cannot be considered",
    "cannot process",
    "we need",
    "is required",
    "primary insurance",
    "primary carrier",
    # Exclusion / non-covered phrasings a rep uses without saying "denied because"
    "not allowed due to",
    "exclusion",
    "not covered",
    "is excluded",
)


def has_reason_cue(text: str) -> bool:
    if not text:
        return False
    t = _normalize(text)
    return any(cue in t for cue in _REASON_CUES)


def _build_key_list() -> str:
    lines = []
    for r in DENIAL_REASONS.values():
        lines.append(f"- {r.key}: {r.display_name} ({r.group_code} {'/'.join(r.carc_codes)})")
    return "\n".join(lines)


_CLASSIFY_PROMPT = """A claim-denial conversation transcript is below. Classify the DENIAL REASON \
into exactly one of these categories:

{key_list}

Rules:
- Answer with ONLY the category key (e.g. prior_auth) if one clearly matches.
- If the transcript states a denial reason that matches NONE of the categories, answer:
  out_of_scope: <the reason in a few words, as stated>
- If the denial reason has not actually been stated yet, answer: unknown

Transcript:
\"\"\"{transcript}\"\"\"
"""


async def classify_reason_gpt(transcript_tail: str) -> Tuple[Optional[str], Optional[str]]:
    """GPT fallback classification over a transcript tail.

    Returns (key, verbatim):
      - (registry key, None)          → classified into the 13
      - (OUT_OF_SCOPE_KEY, "<text>")  → real reason, outside our scope
      - (None, None)                  → unknown / error (keep generic questions)
    Never raises.
    """
    text = (transcript_tail or "").strip()
    if not text:
        return None, None
    try:
        raw = await _call_gpt_api(
            _CLASSIFY_PROMPT.format(key_list=_build_key_list(), transcript=text[-3000:]),
            max_tokens=60,
        )
        answer = (raw or "").strip()
        low = answer.lower()

        if low.startswith("out_of_scope"):
            verbatim = answer.split(":", 1)[1].strip() if ":" in answer else ""
            logger.info(f"🧭 GPT reason classify → OUT OF SCOPE: {verbatim!r}")
            return OUT_OF_SCOPE_KEY, verbatim or "unrecognized denial reason"

        if low == "unknown":
            logger.info("🧭 GPT reason classify → unknown (reason not stated yet)")
            return None, None

        key = low.strip(".,'\" ")
        if key in DENIAL_REASONS:
            logger.info(f"🧭 GPT reason classify → {key}")
            return key, None

        logger.info(f"🧭 GPT reason classify returned unusable answer: {answer!r}")
        return None, None
    except Exception as e:
        logger.error(f"GPT reason classification failed (keeping generic questions): {e}")
        return None, None
