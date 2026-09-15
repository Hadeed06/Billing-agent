"""Date helpers shared across services."""


def as_mdy_date(s: str):
    """If `s` is a date in MMDDYYYY / MM/DD/YYYY / MM-DD-YYYY form, return it
    normalized to 'MM/DD/YYYY' (e.g. for Azure's <say-as interpret-as="date">).
    Returns None otherwise. Validated (real month/day/year) so member IDs and
    NPIs — which are 9–13 digits, not a valid 8-digit MMDDYYYY — never match.
    DOB/DOS are exactly 8 digits (MMDDYYYY), so this targets them precisely.
    """
    digits = s.replace("/", "").replace("-", "").strip()
    if len(digits) != 8 or not digits.isdigit():
        return None
    mm, dd, yyyy = digits[0:2], digits[2:4], digits[4:8]
    m, d, y = int(mm), int(dd), int(yyyy)
    if not (1 <= m <= 12 and 1 <= d <= 31 and 1900 <= y <= 2100):
        return None
    return f"{mm}/{dd}/{yyyy}"


_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def as_spoken_date(s: str):
    """If `s` is a date in MMDDYYYY / MM/DD/YYYY form, return it as a naturally
    spoken date like 'November 9, 1965'. Returns None otherwise.

    Used so a human REP hears a real date instead of the raw 8-digit number
    (TTS reads '11091965' as 'eleven million…', which confused a live rep).
    Reuses as_mdy_date's validation, so member IDs / NPIs never match.
    """
    mdy = as_mdy_date(s)
    if not mdy:
        return None
    mm, dd, yyyy = mdy.split("/")
    return f"{_MONTH_NAMES[int(mm) - 1]} {int(dd)}, {yyyy}"
