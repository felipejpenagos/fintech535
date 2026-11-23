"""One-off check: are the LSEG secrets present, and does a headless login work?
Prints only yes/no and row counts, never data (LSEG data must not be shown publicly). Rerun 2.
import os
import sys

names = ["LSEG_APP_KEY", "LSEG_USERNAME", "LSEG_PASSWORD"]
for n in names:
    print(f"{n}: {'set' if os.environ.get(n) else 'MISSING'}")
if not all(os.environ.get(n) for n in names):
    sys.exit("secrets missing")

import lseg.data as ld
try:
    s = ld.session.platform.Definition(
        app_key=os.environ["LSEG_APP_KEY"],
        grant=ld.session.platform.GrantPassword(username=os.environ["LSEG_USERNAME"],
                                                password=os.environ["LSEG_PASSWORD"]),
        signon_control=True,
    ).get_session()
    s.open()
    ld.session.set_default(s)
    print("login: OK" if str(s.open_state).endswith("Opened") else f"login: state {s.open_state}")
except Exception as e:
    sys.exit(f"login: FAILED ({type(e).__name__})")

for label, fn in [
    ("history GOOGL.O", lambda: ld.get_history("GOOGL.O", count=5)),
    ("snapshot GOOGL.O", lambda: ld.get_data("GOOGL.O", ["TR.PriceClose"])),
    ("option chain search", lambda: ld.discovery.search(view=ld.discovery.Views.SEARCH_ALL,
                                                        query="GOOGL options", top=5)),
]:
    try:
        df = fn()
        print(f"{label}: {0 if df is None else len(df)} rows")
    except Exception as e:
        print(f"{label}: FAILED ({type(e).__name__})")
s.close()
