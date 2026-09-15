import re

CLAIM_NOT_FOUND_TRIGGERS = [
    # Short, high-recall substrings — variations on "no claims found" that
    # survive STT rewording. Keep them lowercased and use ONLY straight
    # ASCII apostrophes; _normalize_for_match() folds Unicode variants below.
    "couldn't find any claims",
    "could not find any claims",
    "didn't find any claims",
    "did not find any claims",
    "not find any claims",
    "no claims found",
    "no matching claims",
    "no claim was found",
    "there are no claims on that date",
    "no claims on that date",
    "no claims for that date",
    "no claims for this date of service",
    "not seeing any claims",
    "we don't have any claims on file",
    "we do not have any claims on file",
]


def _normalize_for_match(text: str) -> str:
    """Fold curly apostrophes → straight and collapse whitespace so triggers
    match across STT and prompt rewordings. Cheap: one lower() + one replace()
    + a split/join."""
    return " ".join(text.lower().replace("’", "'").split())


def is_claim_not_found(text: str) -> bool:
    n = _normalize_for_match(text)
    return any(phrase in n for phrase in CLAIM_NOT_FOUND_TRIGGERS)
#### Claims helper functions 
# --- Claim-capture triggers (keep tight & cheap) ---
CLAIM_START_TRIGGERS = [
    "i found your claim",
    "i found a claim",
    "i found one claim",
    "i've found one claim",
    "i found two claims",
    "i found three claims",
    "i found four claims",
    "i found five claims",
    "here's the first one",
    "here is the first one",
    "the first one was for service",
    # NOTE: "the first claim" was removed because it caused false positives.
    # Example: CIGNA IVR said "if this is the first claim you filed with this
    # tax ID..." while REJECTING the tax ID — the substring match fired
    # claim_mode incorrectly, causing the call to be logged as a "successful
    # unknown" claim rather than a failed verification.
    "please wait for the silence while we locate your claim",
    "we found the requested claim",
    "there is one claim for this date of service",
]

# Robust count-based claim announcement. Matches things like:
#   "i found one claim", "i've found three claims", "we found 2 claims",
#   "there are 3 claims for this member", "there is one claim", "there's a claim"
# — i.e. a (found | there are | there is | there's) lead-in, then a WORD or
# DIGIT count, then claim(s). This catches phrasings the fixed substrings miss,
# across single/multi-claim and word/digit STT renderings.
# Guards against known false positives:
#   - excludes "0" ("found 0 claims"), and no-claim wording is handled by
#     is_claim_not_found() which main.py checks FIRST.
#   - "if this is the first claim you filed with this tax ID" has no
#     found/there-are + number, so it does NOT match.
CLAIM_COUNT_RE = re.compile(
    r"\b(?:found|there\s+(?:are|is)|there's)\s+"
    r"(?:an?|one|two|three|four|five|six|seven|eight|nine|ten|[1-9]\d*)\s+"
    r"claims?\b"
)

# Robust claim-READOUT detector. Some IVRs (e.g. Florida Blue / BCBS) start
# reading a specific claim WITHOUT a "found N claims" lead-in, e.g.
#   "I see that claim number Q100001338019912 in the amount of $1403.60 has not been paid"
#   "your claim has been denied", "claim number 12345 was paid"
# These match neither CLAIM_COUNT_RE nor the fixed triggers, so real-time entry
# used to depend entirely on the navigation GPT returning claim_mode. This
# catches the readout directly: the word "claim" within a short distance of an
# amount/status cue. The bounded gap (.{0,40}) keeps it from matching a "claim"
# in one sentence against a cue in an unrelated later one, and none of these
# cues appear in navigation menus ("press 2 for claim status", "enter the claim
# number"), so it does not false-positive during navigation.
CLAIM_READOUT_RE = re.compile(
    r"\bclaim\b.{0,40}?\b(?:"
    r"in the amount of|"
    r"has (?:not )?been (?:paid|denied|processed|finalized)|"
    r"was (?:paid|denied|rejected|processed|finalized)|"
    r"total charge|non[- ]?covered amount|billed amount|paid amount"
    r")\b"
)


def is_claim_start(text: str) -> bool:
    n = _normalize_for_match(text)
    if CLAIM_COUNT_RE.search(n):
        return True
    if CLAIM_READOUT_RE.search(n):
        return True
    return any(t in n for t in CLAIM_START_TRIGGERS)

