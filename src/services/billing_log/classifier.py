"""
GPT-based classifier that collapses one or more claim transcripts into a
final status AND a short human-readable description of the LAST claim.

When multiple claims are present, the prompt instructs GPT to emphasize
the LAST claim — earlier claims are context only.
"""
import json
import logging

from src.services.llm.llm_service import _call_gpt_api

logger = logging.getLogger(__name__)

_ALLOWED = {"paid", "denied", "inprocess", "unknown"}

_PROMPT = """You are a medical insurance claim summarizer for a billing team.

You will be given one or more claim transcripts captured from an IVR call.
Return a JSON object with exactly FOUR keys:
  "status"          — one of: paid, denied, inprocess, unknown
  "description"     — a clear, plain-English summary of the claim that a biller can
                      understand at a glance WITHOUT reading the full transcript.
  "claim_reference" — the claim number / claim control number / ICN / DCN /
                      reference number of the claim, reproduced EXACTLY as stated
                      (digits exact — never paraphrase, round, truncate, reformat,
                      or add spaces). Use "" (empty string) if the transcript
                      states no such number. NEVER invent one.
  "denial_reason"   — the exact reason the claim was denied or not paid, as stated
                      in the transcript (e.g. "service is not payable under provider
                      agreement", "not medically necessary", "member not eligible on
                      the date of service"). Quote/paraphrase closely what the IVR
                      said. Use "" if the claim was NOT denied, or if no reason was
                      given. NEVER invent a reason.

Writing the description:
- Describe the claim directly. Do NOT call it "the last claim" or refer to its
  position/order — just summarize the claim itself.
- ALWAYS include the claim number / claim control number (ICN) when the
  transcript contains one (e.g. "the claim 820260490326129 has only Medicare
  information...", "claim number ...", "ICN ...", "DCN ..."). The billing team
  needs it to locate the claim, so it is the single most important identifier to
  carry into the summary. Reproduce the digits EXACTLY as stated — never
  paraphrase, round, truncate, or reformat them. If no claim number is stated,
  simply leave it out (do not invent one).
- Write 2-5 short sentences in natural language; explain the numbers, don't just
  list them.
- Cover ONLY what the transcript states. Depending on the insurer, that may
  include: the claim number / ICN, date(s) of service, billed amount, the
  outcome and the reason if given, how much the plan paid, the patient's
  responsibility, deductible, copay, not-covered amounts + reasons, and
  check / remittance numbers.
- A claim can have several "claim line details" (line items) — these ARE part
  of the claim. Include the relevant line-level details; if there are many,
  summarize them concisely rather than dropping them.

CRITICAL — never invent or assume anything:
- Different insurers format claims differently, so some fields will be absent.
- Only state values that actually appear in the transcript. If a value is not
  mentioned, leave it out — never guess a number, date, amount, or reason.

Status meaning — decide ONLY from the LAST claim, and pick EXACTLY ONE by going
through these IN ORDER. Use the FIRST one that applies and stop:

1. paid       → an actual payment was made to the provider. Evidence: "the plan
                paid $X", "the provider was paid $X", "we paid $X", or a check /
                EFT / remittance amount greater than $0. Any real payment (even
                partial) counts here and takes priority over everything below.
2. inprocess  → NO payment yet AND the PAYER is still working the claim with NO
                action required from the provider: pending, in review, still
                adjudicating, or the payer ITSELF forwarded / auto-reprocessed it
                (e.g. routed internally to another department). The provider just
                waits — nobody on our side has to do anything.
3. denied     → NO payment AND a negative outcome. This covers BOTH:
                (a) a flat denial / rejection, or the billed amount fully not
                    covered with nothing further; AND
                (b) a denial that can only be RECONSIDERED once the PROVIDER acts
                    — i.e. must resubmit, file an appeal, or submit documentation
                    (e.g. the primary insurance's EOB, medical records). Even if
                    the IVR says it "may be reconsidered", if WE have to submit /
                    resubmit / appeal / send documents, it is DENIED — you only
                    resubmit or appeal a DENIED claim, never an in-process one.
4. unknown    → none of the above clearly applies, or the outcome isn't stated.

These four are mutually exclusive — exactly one applies. Tie-breakers:
- "processed" only means adjudication happened; it is NOT a payment. Never treat
  "processed" as paid on its own.
- "no payment was made" rules out paid → it is inprocess or denied.
- Decide inprocess vs denied by WHO must act: if the claim can only move forward
  once the PROVIDER resubmits / appeals / submits documentation → DENIED. If the
  PAYER is reprocessing it on its own with no provider action needed → inprocess.
- The word "denied" plus a request for documentation / EOB / records to reconsider
  → denied (provider action required), NOT inprocess.
- ⚠️ IVR MENU OPTIONS ARE NOT CLAIM STATUS. Many payer IVRs end EACH claim with a
  navigation menu such as "say repeat that, next claim, file an appeal, stop, or
  line level information" — those are choices offered for EVERY claim (paid or
  not). Do NOT treat "file an appeal" / "appeal" / "next claim" / "stop" / "line
  level information" / "repeat that" as evidence of denial when they appear as a
  menu list. Treat an appeal/resubmit/documentation instruction as DENIED ONLY
  when the IVR states it ABOUT THIS CLAIM (e.g. "this claim was denied, you'll need
  to file an appeal"), never when it is merely one of the listed menu options. If a
  claim's actual outcome (paid amount / explicit denial) was not read, use unknown.
- If you cannot place it in 1–3 with confidence, use unknown.

Rules:
- If multiple claims are present, base ALL FOUR fields on the LAST claim. Earlier
  claims are context only.
- Do NOT invent anything. Only summarize what the transcript actually says.
  claim_reference and denial_reason follow the same rule — leave them "" when the
  transcript does not state them.
- If the status is unclear, use "unknown".
- Return ONLY the JSON object. No markdown, no extra text.

Claim transcripts (in chronological order; the LAST one is the answer):
---
{claims_text}
---"""


def _empty() -> dict:
    return {"status": "unknown", "description": "", "claim_reference": "", "denial_reason": ""}


async def classify_claim(finalized_claims: list[str]) -> dict:
    """
    Return {"status": <paid|denied|inprocess|unknown>, "description": <str>,
            "claim_reference": <str>, "denial_reason": <str>}.

    claim_reference / denial_reason are extracted verbatim from the (last) claim
    and are "" when the transcript doesn't state them. Never raises — on any
    error, returns the all-unknown/empty shape.
    """
    if not finalized_claims:
        return _empty()

    claims_text = "\n\n--- CLAIM BREAK ---\n\n".join(finalized_claims)
    prompt = _PROMPT.format(claims_text=claims_text)

    try:
        # Room for a JSON object + a multi-sentence plain-English summary.
        raw = await _call_gpt_api(prompt, max_tokens=400)
    except Exception as e:
        logger.exception(f"Claim classification GPT call failed: {e}")
        return _empty()

    if not raw:
        return _empty()

    result = _parse(raw)
    logger.info(
        f"📊 Claim classified: status={result['status']} "
        f"claim_reference={result['claim_reference']!r} "
        f"denial_reason={result['denial_reason']!r} "
        f"description={result['description']!r}"
    )
    return result


def _parse(raw: str) -> dict:
    """Parse GPT output into {status, description, claim_reference, denial_reason}.
    Tolerant of stray text / markdown fences around the JSON."""
    text = raw.strip()

    # Strip ```json ... ``` fences if present
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    # Try to isolate the JSON object
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]

    try:
        obj = json.loads(text)
        status = str(obj.get("status", "")).strip().lower().strip(".,!? \"'")
        if status not in _ALLOWED:
            status = "unknown"
        return {
            "status":          status,
            "description":     str(obj.get("description", "")).strip(),
            # Extracted verbatim; "" when absent. Keep exactly as GPT returned
            # (only trim whitespace) so claim numbers aren't reformatted.
            "claim_reference": str(obj.get("claim_reference", "")).strip(),
            "denial_reason":   str(obj.get("denial_reason", "")).strip(),
        }
    except Exception:
        # Fallback: maybe GPT returned just a bare status word
        word = raw.strip().lower().strip(".,!? \"'")
        out = _empty()
        if word in _ALLOWED:
            out["status"] = word
        else:
            logger.warning(f"Could not parse claim classification {raw!r}; defaulting to unknown")
        return out