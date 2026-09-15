"""
RIC tests. Expected values come from three independent sources:
  - LSEG's own get_ric_opra worked examples
  - the assignment handout's AAPL samples
  - RICs we verified empirically against the API
"""

import sys
import datetime as dt
from rics import build_ric, parse_ric, encode_strike, occ_label, suffix_letter

PASS, FAIL = 0, 0
PAST = dt.date(2026, 9, 15)   # "today" for expiry tests


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


print("\nstrike encoding")
check("14.50", encode_strike(14.50), "01450")
check("190", encode_strike(190), "19000")
check("192.50", encode_strike(192.50), "19250")
check("5", encode_strike(5), "00500")
check("10.50", encode_strike(10.50), "01050")
check("700", encode_strike(700), "70000")
check("1500 (4-digit band)", encode_strike(1500), "15000")
check("12500 (letter band A)", encode_strike(12500), "A2500")
check("25000 (letter band B)", encode_strike(25000), "B5000")


print("\nsuffix letter is always the call series")
check("June suffix", suffix_letter(6), "F")
check("September suffix", suffix_letter(9), "I")
check("January suffix", suffix_letter(1), "A")


print("\nRICs verified against the live API")
# From discovery.search on live AAPL options:
#   AAPLI182632000.U = AAPL + I (Sep call) + 18 + 26 + 32000 (strike 320)
check("AAPL 18 Sep 2026 320 call (live, no suffix)",
      build_ric("AAPL.O", dt.date(2026, 9, 18), 320, "C", as_of=dt.date(2026, 9, 1)),
      "AAPLI182632000.U")
check("AAPL 18 Sep 2026 322.5 put (live)",
      build_ric("AAPL.O", dt.date(2026, 9, 18), 322.5, "P", as_of=dt.date(2026, 9, 1)),
      "AAPLU182632250.U")
# Expired, confirmed to return 20 rows each
check("AAPL 11 Sep 2026 335 call",
      build_ric("AAPL.O", dt.date(2026, 9, 11), 335, "C", as_of=PAST),
      "AAPLI112633500.U^I26")
check("AAPL 28 Aug 2026 335 call",
      build_ric("AAPL.O", dt.date(2026, 8, 28), 335, "C", as_of=PAST),
      "AAPLH282633500.U^H26")
check("UUUU 21 Aug 2026 14.5 call",
      build_ric("UUUU.N", dt.date(2026, 8, 21), 14.5, "C", as_of=PAST),
      "UUUUH212601450.U^H26")


print("\nLSEG's own put example (the suffix trap)")
# get_ric_opra('AAPL.O', '2022-01-21', 160, 'P') -> 'AAPLM212216000.U^A22'
check("AAPL 21 Jan 2022 160 put",
      build_ric("AAPL.O", dt.date(2022, 1, 21), 160, "P", as_of=PAST),
      "AAPLM212216000.U^A22")  # LSEG's own worked example
# The matching call shares the suffix but not the body letter.
check("AAPL 21 Jan 2022 160 call",
      build_ric("AAPL.O", dt.date(2022, 1, 21), 160, "C", as_of=PAST),
      "AAPLA212216000.U^A22")


print("\nempirically verified UUUU RICs")
# Confirmed against the live API: ^F26 returns data, ^R26 does not.
check("UUUU 26 Jun 2026 14.00 put",
      build_ric("UUUU.N", dt.date(2026, 6, 26), 14.00, "P", as_of=PAST),
      "UUUUR262601400.U^F26")
check("UUUU 26 Jun 2026 10.50 call",
      build_ric("UUUU.N", dt.date(2026, 6, 26), 10.50, "C", as_of=PAST),
      "UUUUF262601050.U^F26")


print("\nthe day IS zero-padded (the handout says otherwise)")
# AAPLH72633500.U^H26 -> not found; AAPLH072633500.U^H26 -> 20 rows
check("single-digit day 7 Aug",
      build_ric("AAPL.O", dt.date(2026, 8, 7), 335, "C", as_of=PAST),
      "AAPLH072633500.U^H26")
# AAPLI42633500.U^I26 -> not found; AAPLI042633500.U^I26 -> 20 rows
check("single-digit day 4 Sep",
      build_ric("AAPL.O", dt.date(2026, 9, 4), 335, "C", as_of=PAST),
      "AAPLI042633500.U^I26")
check("two-digit day unchanged",
      build_ric("AAPL.O", dt.date(2026, 6, 26), 190, "C", as_of=PAST),
      "AAPLF262619000.U^F26")


print("\nlive contracts get no suffix")
check("expiry in the future",
      build_ric("AAPL.O", dt.date(2026, 12, 18), 190, "C", as_of=PAST),
      "AAPLL182619000.U")
check("expiry in the past",
      build_ric("AAPL.O", dt.date(2026, 6, 5), 190, "C", as_of=PAST),
      "AAPLF052619000.U^F26")


print("\nround-trip parse")
for root, exp, k, t in [
    ("AAPL", dt.date(2026, 6, 5), 190.0, "C"),
    ("AAPL", dt.date(2026, 7, 17), 200.0, "C"),
    ("UUUU", dt.date(2026, 6, 26), 14.0, "P"),
    ("UUUU", dt.date(2026, 8, 21), 14.5, "C"),
    ("MPWR", dt.date(2026, 6, 5), 700.0, "C"),
    ("AAPL", dt.date(2026, 6, 5), 192.5, "P"),
]:
    ric = build_ric(root, exp, k, t, as_of=PAST)
    p = parse_ric(ric)
    ok = (p is not None and p["root"] == root and p["expiry"] == exp
          and abs(p["strike"] - k) < 1e-9
          and p["type"] == ("call" if t == "C" else "put"))
    check(f"round-trip {ric}", ok, True)

check("garbage returns None", parse_ric("NOTARIC"), None)
check("wrong venue returns None", parse_ric("AAPLF52619000.X^F26"), None)


print("\nOCC labels")
check("call label", occ_label("AAPL.O", dt.date(2026, 6, 5), 195.0, "C"),
      "AAPL 5Jun26 195C")
check("fractional strike", occ_label("UUUU.N", dt.date(2026, 8, 21), 14.5, "C"),
      "UUUU 21Aug26 14.5C")


print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
