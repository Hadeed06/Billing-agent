"""
Denial-call summarizer — turns the live-rep conversation into a
biller-actionable description for a claim reached via the denial pivot.

`classifier.classify_claim` summarizes the IVR claim READOUT (status + numbers).
This module summarizes what the REP actually told us during the follow-up:
the denial reason(s) in plain terms, the CORRECTIVE ACTION needed to get the
claim reprocessed, and any specific values the rep gave (fax / portal / address,
deadline, primary carrier, auth or claim numbers) — per denied line when several
are discussed.

Design mirrors classifier.py / failure_classifier.py:
  - one GPT pass, prompt-driven;
  - extracts ONLY what the transcript states, never invents;
  - never raises — returns "" on any failure so the caller can fall back to the
    plain reason label.
"""
import logging

from src.services.llm.llm_service import _call_gpt_api

logger = logging.getLogger(__name__)

# Keep the token cost bounded. The live-rep conversation is at the END of the
# call, so the tail carries the actionable content; earlier IVR menu navigation
# is noise for this summary.
_TRANSCRIPT_TAIL_CHARS = 4000
_MAX_TOKENS = 500


_PROMPT = """You are writing the billing-team record for a DENIED insurance claim that our \
agent worked with a live representative.

You are given:
- The denial reason our system classified: {reason_label}
- A summary of the claim as the IVR read it out (claim number / amounts): {claim_readout}
- The LIST OF QUESTIONS the agent set out to get answered.
- The transcript of the conversation with the representative.

Produce a record in THIS EXACT structure and nothing else:

For EACH question in the list, on its own two lines:
Q: <the question>
A: <the answer the rep actually gave — quote specific values (fax number, portal, \
mailing/appeal address, deadline, primary carrier name, authorization or claim/ICN \
number). If the transcript does NOT contain an answer to that question, write exactly "Not obtained".>

Then, after all the Q/A pairs, a final block:
Summary: <2-4 sentences: why the claim denied in plain terms, and the CORRECTIVE \
ACTION the office must take to get it reprocessed. Include the claim/ICN number, \
date of service, and billed amount if the readout states them.>

Rules:
- Use ONLY facts present in the transcript or readout. NEVER invent a value. \
Reproduce any claim/ICN number exactly.
- Keep each A to a single line. If the rep could not provide something, the answer is "Not obtained".
- Output ONLY the Q/A lines and the Summary block — no preamble, no markdown headers, no bullets.

Questions:
{questions_block}

Representative conversation transcript:
---
{transcript}
---
"""


async def summarize_denial(
    *,
    rep_transcript: str,
    claim_readout_summary: str,
    reason_label: str,
    questions: list,
    call_tag: str,
) -> str:
    """Return a Q&A-structured, biller-actionable record of the denial follow-up:
    one Q/A pair per question the agent set out to answer, then an overall
    Summary with the corrective action.

    Never raises. Returns "" if there's nothing to summarize or GPT fails, so
    the caller falls back to the plain reason label.
    """
    transcript = (rep_transcript or "").strip()
    if not transcript:
        return ""

    questions_block = "\n".join(f"- {q}" for q in (questions or [])) or "- Why was the claim denied?"
    prompt = _PROMPT.format(
        reason_label=reason_label or "not identified",
        claim_readout=(claim_readout_summary or "").strip() or "(none captured)",
        questions_block=questions_block,
        transcript=transcript[-_TRANSCRIPT_TAIL_CHARS:],
    )

    try:
        raw = await _call_gpt_api(prompt, max_tokens=_MAX_TOKENS)
    except Exception as e:
        logger.warning(f"denial summarizer GPT call failed (ignored): {e}")
        return ""

    summary = (raw or "").strip()
    if summary:
        logger.info(f"📝 Denial summary built ({len(summary)} chars) call_id={call_tag}")
    return summary
