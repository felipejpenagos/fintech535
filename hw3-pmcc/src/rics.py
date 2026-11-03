"""
OPRA option RIC construction.

Rules are taken from LSEG's own `get_ric_opra` reference implementation, not
from the assignment handout -- the handout gets the put suffix wrong.

    {ROOT}{MONTH}{DAY}{YY}{STRIKE}.U{^{EXPMONTH}{YY} if expired}

MONTH      body letter: A-L for calls, M-X for puts
EXPMONTH   caret-suffix letter: ALWAYS A-L, even for puts
DAY        expiry day, zero-padded to two digits (05, not 5)
YY         two-digit expiry year
STRIKE     encoding depends on magnitude (see encode_strike)
.U         OPRA

Two places the handout is wrong
-------------------------------
1. It says the day is not zero-padded ("5 not 05"). Verified against the API on
   AAPL: AAPLH72633500.U^H26 is not found, AAPLH072633500.U^H26 returns 20 rows.
   Same for 2026-09-04. The day is always two digits.
2. The put caret suffix, below.

The suffix trap
---------------
LSEG's own ident table has three keys per month:

    '6': {'exp': 'F', 'C': 'F', 'P': 'R'}

'C'/'P' are body letters; 'exp' is the suffix letter and is always the call
series. Their worked example proves it:

    get_ric_opra('AAPL.O', '2022-01-21', 160, 'P') -> 'AAPLM212216000.U^A22'
                                                          ^body M    ^suffix A

Using ^M22 there returns "universe is not found" -- verified empirically on
UUUU June puts: ^R26 fails, ^F26 returns data.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Optional

CALL_LETTERS = "ABCDEFGHIJKL"   # Jan..Dec calls, and the caret suffix series
PUT_LETTERS = "MNOPQRSTUVWX"    # Jan..Dec puts (body only)


def month_letter(month: int, opt_type: str) -> str:
    if not 1 <= month <= 12:
        raise ValueError(f"bad month {month}")
    t = opt_type.upper()[0]
    if t == "C":
        return CALL_LETTERS[month - 1]
    if t == "P":
        return PUT_LETTERS[month - 1]
    raise ValueError(f"bad option type {opt_type!r}")


def suffix_letter(month: int) -> str:
    """Caret-suffix letter. Always the call series, regardless of put/call."""
    return CALL_LETTERS[month - 1]


def encode_strike(strike: float) -> str:
    """
    OPRA strike encoding, per LSEG's reference implementation.

        < 1000     strike x 100, zero-padded to 5 digits   (14.50 -> 01450)
        1000-9999  integer part + '0'                      (1500  -> 15000)
        10000+     letter prefix + last 4 digits           (12500 -> A2500)

    The 5-digit rule in the assignment only holds below $1,000. It breaks
    silently on index options and high-priced names.
    """
    if strike <= 0:
        raise ValueError(f"bad strike {strike}")

    int_part = int(strike)

    if int_part < 1000:
        return f"{int(round(strike * 100)):05d}"
    if int_part < 10000:
        return f"{int_part}0"

    band = (int_part // 10000)
    prefix = "ABCD"[band - 1] if 1 <= band <= 4 else None
    if prefix is None:
        raise ValueError(f"strike {strike} out of supported range")
    return prefix + str(int_part)[-4:]


def build_ric(
    root: str,
    expiry: dt.date,
    strike: float,
    opt_type: str,
    as_of: Optional[dt.date] = None,
) -> str:
    """
    Build an OPRA RIC. Appends the expired-contract suffix when `expiry` is in
    the past relative to `as_of` (default: today).

    Strikes >= 1000 lowercase the body letter (LSEG convention); the suffix
    letter stays uppercase.
    """
    as_of = as_of or dt.date.today()
    root = root.upper().split(".")[0]

    body = month_letter(expiry.month, opt_type)
    if strike > 999.999:
        body = body.lower()

    ric = f"{root}{body}{expiry.day:02d}{expiry.year % 100:02d}{encode_strike(strike)}.U"

    if expiry < as_of:
        ric += f"^{suffix_letter(expiry.month)}{expiry.year % 100:02d}"

    return ric


# Day is two digits and the strike field is always exactly five characters,
# so the layout after the month letter is fixed at 2 + 2 + 5.
_PARSE = re.compile(
    r"^(?P<root>[A-Z]+)(?P<body>[A-Xa-x])(?P<day>\d{2})(?P<yy>\d{2})"
    r"(?P<strike>[A-D]\d{4}|\d{5})\.U(?:\^(?P<sfx>[A-L])(?P<sfxyy>\d{2}))?$"
)


def parse_ric(ric: str) -> Optional[dict]:
    """Inverse of build_ric. Returns None if the string doesn't match."""
    m = _PARSE.match(ric)
    if not m:
        return None

    body = m.group("body")
    upper = body.upper()
    if upper in CALL_LETTERS:
        opt_type, month = "call", CALL_LETTERS.index(upper) + 1
    elif upper in PUT_LETTERS:
        opt_type, month = "put", PUT_LETTERS.index(upper) + 1
    else:
        return None

    raw = m.group("strike")
    if raw[0] in "ABCD":
        strike = float((("ABCD".index(raw[0]) + 1) * 10000) + int(raw[1:]))
    elif len(raw) == 5 and body.islower():
        strike = float(int(raw[:-1]))       # 1000-9999 band
    else:
        strike = int(raw) / 100.0

    try:
        expiry = dt.date(2000 + int(m.group("yy")), month, int(m.group("day")))
    except ValueError:
        return None

    return {
        "ric": ric,
        "root": m.group("root"),
        "expiry": expiry,
        "type": opt_type,
        "strike": strike,
        "expired": m.group("sfx") is not None,
    }


def occ_label(root: str, expiry: dt.date, strike: float, opt_type: str) -> str:
    """Human-readable subtitle for the blotter, e.g. 'AAPL 5Jun26 195C'."""
    t = "C" if opt_type.upper().startswith("C") else "P"
    s = f"{strike:g}"
    return f"{root.upper().split('.')[0]} {expiry.strftime('%-d%b%y')} {s}{t}"
