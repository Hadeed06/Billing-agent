"""
Map a raw IVR denial reason to one of the 13 canonical denial categories
(from the billing team's "IVR Denials Scripting"). The category (an enum key we
agree on with the EPM/billing team) tags the denial so the future denial-follow-up
agent knows which script (Q1-Q4) to run.

NEVER forces a match: if the raw reason does not CLEARLY correspond to one of the
13 categories, returns None (NO_MATCH). The caller then sends the RAW reason
unchanged — we do not shoehorn a weak/uncertain match into a category.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from src.services.llm.llm_service import _call_gpt_api

logger = logging.getLogger(__name__)

# Canonical categories: KEY -> (Group-CARC, plain meaning). KEY is the enum value
# we share with EPM; CARC is the standard Claim Adjustment Reason Code.
DENIAL_CATEGORIES: dict[str, tuple[str, str]] = {
    "INVALID_DIAGNOSIS":   ("CO-11",      "The diagnosis is inconsistent with the procedure"),
    "NO_REFERRAL":         ("CO-288",     "Referral absent or exceeded"),
    "PRIOR_AUTHORIZATION": ("CO-197",     "Precertification/authorization/notification absent or exceeded"),
    "MEDICAL_NECESSITY":   ("CO-50/151",  "Not deemed medically necessary, or info does not support the level/frequency of service"),
    "DUPLICATE_CLAIM":     ("CO-18",      "Exact duplicate claim or service"),
    "BUNDLED":             ("CO-97",      "Benefit is included in the payment/allowance for another already-adjudicated service"),
    "TIMELY_FILING":       ("CO-29",      "The time limit for filing has expired"),
    "COB":                 ("PR-22",      "Care may be covered by another payer per coordination of benefits"),
    "NON_COVERED":         ("PR-96/204",  "Service/equipment/drug is not covered under the patient's benefit plan"),
    "MISSING_INFORMATION": ("CO-16",      "Claim/service lacks information or has a submission/billing error"),
    "OUT_OF_NETWORK":      ("PR-242",     "Services not provided by network / primary care providers"),
    "MAX_BENEFIT":         ("PR-119",     "Benefit maximum for this time period or occurrence has been reached"),
    "ADDITIONAL_DOCS":     ("CO-226/252", "Requested documentation/attachment was not provided (or was insufficient) to adjudicate"),
}

# CARC number -> category key, for a deterministic code match when the IVR states
# an explicit reason code (split the multi-CARC entries like "50/151").
_CARC_TO_KEY: dict[str, str] = {}
for _key, (_codes, _) in DENIAL_CATEGORIES.items():
    for _num in re.findall(r"\d+", _codes):
        _CARC_TO_KEY[_num] = _key

# Only treat a number as a CARC when it's clearly labelled as a reason code —
# "CO 29", "PR-22", "CARC 97", "reason code 16", "denial code 50". A bare number
# (a dollar amount, date, claim #) must NOT be read as a CARC.
_CARC_RE = re.compile(
    r"\b(?:co|pr|oa|pi|carc|(?:reason|denial|adjustment)\s+code)\b[\s:#-]*?(\d{1,3})\b",
    re.IGNORECASE,
)

_PROMPT = """You classify a health-insurance claim DENIAL REASON into ONE standard category, or NONE.

Categories (KEY — meaning):
{catalog}

Return the KEY of the category whose meaning CLEARLY matches the denial reason below.
- Match on MEANING (the wording will differ from the meanings above).
- Match ONLY when the reason IS that category — NOT when it is merely related or adjacent.
- If the denial reason does NOT clearly correspond to any category, return exactly: NO_MATCH
- NEVER force a weak or uncertain match. When unsure, return NO_MATCH.

The following are NOT in the list — return NO_MATCH for them (do NOT map to a nearby category):
- patient eligibility, coverage terminated / not active on the date of service
- deductible, copay, coinsurance, or any patient-responsibility amount
- provider credentialing / enrollment status (NOT the same as out-of-network)
- the claim was billed/submitted to the WRONG payer or insurance company (NOT the same as COB)
- fee-schedule / contracted-rate / allowed-amount reductions
- the patient is deceased
- the claim is still pending / in review (that is not a denial)
Note: use COB ONLY when ANOTHER payer is PRIMARY per coordination of benefits.

- Reply with ONLY the KEY (e.g. TIMELY_FILING) or NO_MATCH. No other text.

Denial reason: "{reason}"
Answer:"""


def _carc_match(raw: str) -> Optional[str]:
    m = _CARC_RE.search(raw or "")
    if not m:
        return None
    return _CARC_TO_KEY.get(m.group(1))


async def map_denial_reason(raw_reason: str) -> Optional[str]:
    """Return the canonical category KEY for a raw IVR denial reason, or None
    (NO_MATCH) when it doesn't clearly fit any of the 13. Never raises."""
    if not raw_reason or not raw_reason.strip():
        return None

    # 1) Deterministic: an explicitly-stated CARC code wins.
    key = _carc_match(raw_reason)
    if key:
        logger.info(f"🏷️ Denial mapped by CARC code → {key}")
        return key

    # 2) Semantic match via GPT (strict, no forcing).
    catalog = "\n".join(f"{k} — {meaning}" for k, (_c, meaning) in DENIAL_CATEGORIES.items())
    prompt = _PROMPT.format(catalog=catalog, reason=raw_reason.strip())
    try:
        raw = (await _call_gpt_api(prompt, max_tokens=20)) or ""
    except Exception as e:
        logger.warning(f"map_denial_reason GPT error (ignored): {e}")
        return None

    token = raw.strip().upper().strip(".,!?:;'\"` ")
    if token in DENIAL_CATEGORIES:
        logger.info(f"🏷️ Denial mapped semantically → {token}")
        return token
    logger.info(f"🏷️ Denial reason did not match any category (NO_MATCH): {raw_reason!r}")
    return None
