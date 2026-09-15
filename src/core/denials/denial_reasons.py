"""
The denial-reason registry — the "what to ask" metadata for the denial
follow-up flow.

One entry per denial reason in the billing team's scope (source: "IVR
Denials Scripting.xlsx", 13 reasons). Each entry carries:
  - trigger_keywords → cheap substring rules used to classify the reason
    LIVE from the IVR claim readout or the rep's speech (first confident
    match sticks; GPT fallback covers phrasings the rules miss)
  - questions       → what the billing office needs answered for THIS
    reason. Injected into the single rep prompt via {denial_context_block}
    — the conversational rules stay shared, only the questions vary.

Adding/refining a reason = editing data here. No prompt or code changes.
Refine trigger_keywords as real call transcripts arrive (each script from
the billing team enriches one entry).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class DenialQuestion:
    text: str
    # Advisory keywords: if a bot say-line contains one of these, the
    # question is considered ASKED (checklist rendering only — the
    # conversation history remains the ground truth GPT reasons over).
    keywords: Tuple[str, ...] = ()


@dataclass(frozen=True)
class DenialReason:
    key: str
    display_name: str
    group_code: str
    carc_codes: Tuple[str, ...]
    trigger_keywords: Tuple[str, ...]
    questions: Tuple[DenialQuestion, ...]


# Needed for EVERY denial, but LOWER priority than the reason-specific
# questions — the claim/ICN number was usually already read out by the IVR
# during the claim readout, so it's typically already captured. Only ask the
# rep for it if you don't already have it. Lead with the reason-specific
# questions (the actual point of the call), not this.
# PASSIVE — no active questions here. The ICN / claim number is read out during
# the claim readout, so it's already captured in the transcript (and now appears
# in the claim summary). Asking the rep for it wastes a turn and confuses reps who
# don't have it in front of them — it derailed a real Baylor call. If a rep
# volunteers it, it lands in the transcript automatically. So do NOT actively ask.
UNIVERSAL_QUESTIONS: Tuple[DenialQuestion, ...] = ()

# Captured PASSIVELY, not actively asked. The IVR reads a call-reference
# number during the transfer, and reps often read one at the end — both land
# in the transcript automatically. Do NOT solicit it digit-by-digit (that
# causes a re-read loop). This entry is informational only; render_denial_context
# keeps it OUT of the active question list.
CLOSING_QUESTIONS: Tuple[DenialQuestion, ...] = ()

# Used ONLY while the reason is unclassified. Deliberately minimal: the
# working questions (corrective action, submission target, deadlines, etc.)
# are asked ONLY after the reason is classified into one of the 13 supported
# entries — each entry carries its own question set. Unknown reason → first
# discover it; out-of-scope reason → wrap up without working questions.
GENERIC_QUESTIONS: Tuple[DenialQuestion, ...] = (
    DenialQuestion(
        "What specifically caused the denial (get the reason in the rep's own words)",
        ("why", "reason", "caused the denial"),
    ),
)


# Special key for reasons OUTSIDE the supported registry — the agent wraps
# up gracefully and the outcome is reported to the billing team verbatim.
OUT_OF_SCOPE_KEY = "out_of_scope"


DENIAL_REASONS: Dict[str, DenialReason] = {
    "invalid_dx": DenialReason(
        key="invalid_dx",
        display_name="Invalid Dx / Diagnosis inconsistent with the procedure",
        group_code="CO",
        carc_codes=("11",),
        trigger_keywords=(
            "diagnosis is inconsistent",
            "inconsistent with the procedure",
            "invalid diagnosis",
            "invalid dx",
            "diagnosis code is invalid",
            "diagnosis codes are not appropriate",
        ),
        questions=(
            DenialQuestion(
                "Which DX/diagnosis code is invalid, or the position of the ICD code on the claim",
                ("which diagnosis", "which dx", "icd", "position"),
            ),
            DenialQuestion(
                "The relevant determination policy or LCD article reference",
                ("policy", "lcd", "article"),
            ),
            DenialQuestion(
                "The time limit for resubmitting the corrected claim from the denial date",
                ("time limit", "deadline", "resubmi"),
            ),
        ),
    ),
    "no_referral": DenialReason(
        key="no_referral",
        display_name="No Referral (referral absent or exceeded)",
        group_code="CO",
        carc_codes=("288",),
        trigger_keywords=(
            "referral absent",
            "referral exceeded",
            "no referral",
            "referral is required",
            "referral was required",
            "pcp referral",
            "without a referral",
        ),
        questions=(
            DenialQuestion(
                "Whether a referral is on file; if YES, obtain the referral number",
                ("referral on file", "referral number"),
            ),
            DenialQuestion(
                "If NO referral exists: the PCP name and contact so the office can request one",
                ("pcp", "primary care"),
            ),
        ),
    ),
    "prior_auth": DenialReason(
        key="prior_auth",
        display_name="Prior Authorization absent or exceeded",
        group_code="CO",
        carc_codes=("197",),
        trigger_keywords=(
            "prior authorization",
            "pre-authorization",
            "pre authorization",
            "preauthorization",
            "precertification",
            "authorization was not obtained",
            "authorization required",
            "authorization was required",
            "no authorization",
        ),
        questions=(
            DenialQuestion(
                "Whether an authorization is on file; if YES, the authorization number and benefits remaining under it",
                ("authorization on file", "authorization number", "auth number", "benefits remaining"),
            ),
            DenialQuestion(
                "If NO authorization: whether retro-authorization can be obtained, and how/where to request it",
                ("retro", "retroactive"),
            ),
        ),
    ),
    "medical_necessity": DenialReason(
        key="medical_necessity",
        display_name="Medical Necessity",
        group_code="CO",
        carc_codes=("50", "151"),
        # NOTE: bare "medical necessity" / "medically necessary" were REMOVED —
        # they appear in every payer's generic disclaimer ("subject to policy
        # guidelines, medical necessity, and member eligibility") and caused
        # false matches. Only ACTUAL medical-necessity denial phrasings here.
        trigger_keywords=(
            "not medically necessary",
            "not deemed a medical necessity",
            "does not support this many",
            "does not support the frequency",
            "does not support this level",
            "does not support the level of service",
            "medical records to establish",
            "records to support the medical necessity",
        ),
        questions=(
            DenialQuestion(
                "The specific basis: invalid primary DX, duplicate/excessive units, or medical records needed",
                ("invalid diagnosis", "excessive units", "duplicate", "medical records"),
            ),
            DenialQuestion(
                "Follow the basis: which DX is invalid / original claim number + same-or-different provider / how to submit the medical records",
                ("which diagnosis", "original claim", "how to submit"),
            ),
        ),
    ),
    "duplicate_claim": DenialReason(
        key="duplicate_claim",
        display_name="Duplicate claim/service",
        group_code="CO",
        carc_codes=("18",),
        trigger_keywords=(
            "duplicate claim",
            "exact duplicate",
            "duplicate of a claim",
            "duplicate service",
            "already been submitted",
            "previously submitted claim",
        ),
        questions=(
            DenialQuestion(
                "The original claim number of the duplicate",
                ("original claim",),
            ),
            DenialQuestion(
                "Whether the original claim was billed by the same provider or a different provider",
                ("same provider", "different provider"),
            ),
        ),
    ),
    "bundled": DenialReason(
        key="bundled",
        display_name="Bundled (included in another adjudicated service)",
        group_code="CO",
        carc_codes=("97",),
        trigger_keywords=(
            "bundled",
            "included in the payment",
            "included in the allowance",
            "already been adjudicated",
            "inclusive of",
        ),
        questions=(
            DenialQuestion(
                "Which CPT is bundled with which CPT on the denied claim",
                ("bundled with", "which cpt"),
            ),
            DenialQuestion(
                "Whether it denied per NCCI edits or payer custom edits",
                ("ncci", "custom edits", "edits"),
            ),
            DenialQuestion(
                "Whether a modifier is allowed for unbundling the specific CPT",
                ("modifier",),
            ),
        ),
    ),
    "timely_filing": DenialReason(
        key="timely_filing",
        display_name="Timely filing limit expired",
        group_code="CO",
        carc_codes=("29",),
        trigger_keywords=(
            "timely filing",
            "time limit for filing has expired",
            "filing limit",
            "filed after the",
            "past the filing deadline",
            "late filing",
        ),
        questions=(
            DenialQuestion(
                "The timely filing limit for submitting claims",
                ("filing limit", "timely filing"),
            ),
            DenialQuestion(
                "Whether an appeal can be filed on a formal letter, or a specific appeal form is required",
                ("formal letter", "appeal form"),
            ),
            DenialQuestion(
                "The time limit for filing the appeal from the date of the denial",
                ("time limit", "appeal", "deadline"),
            ),
        ),
    ),
    "cob": DenialReason(
        key="cob",
        display_name="Coordination of Benefits (other payer primary)",
        group_code="PR",
        carc_codes=("22",),
        trigger_keywords=(
            # NOTE: no bare "cob" — as a substring it would false-match
            # inside ordinary words (jacob, cobra).
            "coordination of benefits",
            "covered by another payer",
            "other insurance",
            "another insurance is primary",
            "primary insurance on file",
            # Real rep phrasings (2026-07): Humana as secondary needs the
            # primary carrier's EOB before it will consider the claim.
            "primary insurance carrier",
            "primary insurance",
            "primary carrier",
            "primary payer",
            "explanation of benefits from",
            "eob from the primary",
            "members primary",
            "member's primary",
            "c o b",  # STT sometimes spells the acronym out
        ),
        questions=(
            DenialQuestion(
                "The name of the primary insurance per their coordination-of-benefits records",
                ("primary insurance", "coordination"),
            ),
        ),
    ),
    "non_covered": DenialReason(
        key="non_covered",
        display_name="Non-covered service under the patient's benefit plan",
        group_code="PR",
        carc_codes=("96", "204"),
        trigger_keywords=(
            "not covered",
            "non-covered",
            "non covered",
            "not a covered benefit",
            "excluded from the plan",
            "benefit plan does not cover",
            # Real Humana rep phrasing for a plan/Medicare exclusion, e.g.
            # "not allowed due to Medicare exclusion of services for flat foot".
            "medicare exclusion",
            "exclusion of services",
            "excluded service",
            "not a covered service",
        ),
        questions=(
            DenialQuestion(
                "Whether the denial is due to the procedure/service billed, or the diagnosis codes on the DOS",
                ("procedure or", "diagnosis codes", "due to the"),
            ),
            DenialQuestion(
                "Whether it's an exclusion under the patient's specific plan or a universal policy exclusion",
                ("specific plan", "universal", "exclusion"),
            ),
            DenialQuestion(
                "If plan-specific: whether prior auth is required / was obtained (get the auth number if so)",
                ("prior auth", "authorization"),
            ),
        ),
    ),
    "missing_info": DenialReason(
        key="missing_info",
        display_name="Claim/service lacks information or has submission/billing errors",
        group_code="CO",
        carc_codes=("16",),
        trigger_keywords=(
            "lacks information",
            "missing information",
            "submission error",
            "billing error",
            "incomplete claim",
            "invalid information",
            "missing claim information",
        ),
        questions=(
            DenialQuestion(
                "What specifically is missing: documentation, billing/coding errors, or invalid member information",
                ("what is missing", "missing", "billing error", "coding"),
            ),
        ),
    ),
    "out_of_network": DenialReason(
        key="out_of_network",
        display_name="Out of network provider",
        group_code="PR",
        carc_codes=("242",),
        trigger_keywords=(
            "out of network",
            "out-of-network",
            "not in network",
            "non-participating",
            "not a network provider",
        ),
        questions=(
            DenialQuestion(
                "Which provider is out of network — the RENDERING provider, the GROUP/BILLING provider, or both",
                ("rendering provider", "billing provider", "group provider"),
            ),
        ),
    ),
    "benefits_exhausted": DenialReason(
        key="benefits_exhausted",
        display_name="Maximum benefit reached / benefits exhausted",
        group_code="PR",
        carc_codes=("119",),
        trigger_keywords=(
            "benefit maximum",
            "maximum benefit",
            "benefits exhausted",
            "maximum has been reached",
            "benefits have been used",
            "benefit limit",
        ),
        questions=(
            DenialQuestion(
                "The maximum benefit limit under the patient's plan for this service for the calendar year",
                ("maximum", "limit", "calendar year"),
            ),
        ),
    ),
    "docs_required": DenialReason(
        key="docs_required",
        display_name="Additional documentation required",
        group_code="CO",
        carc_codes=("226", "252"),
        # Enriched from the real Humana call transcript (2026-07):
        # "the documentation submitted does not meet the code criteria"
        trigger_keywords=(
            "documentation submitted does not meet",
            "does not meet the code criteria",
            "additional documentation",
            "documentation is required",
            "documentation was not provided",
            "attachment",
            "documents that you need to submit",
            "records to support",
            "information requested from the",
        ),
        questions=(
            DenialQuestion(
                "Which type of documentation / which criteria is unmet (per CPT or line item)",
                ("which documentation", "what documentation", "unmet criteria", "documents do"),
            ),
            DenialQuestion(
                "Where to submit the documentation — get the SPECIFIC fax number, portal, or address",
                ("fax", "where do we submit", "portal", "address"),
            ),
            DenialQuestion(
                "The time limit for submitting the documentation",
                ("time limit", "deadline", "how long"),
            ),
        ),
    ),
}


def get_reason(key: Optional[str]) -> Optional[DenialReason]:
    if not key:
        return None
    return DENIAL_REASONS.get(key)
