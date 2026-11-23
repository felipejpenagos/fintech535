"""
sanji_check.py -- check orders against the Sanji spec before they are written.

The spec (jakevestal.github.io/server-logic-533/order.html) rejects any order with an
unknown field, unknown token, missing required field, or quantity below 1, and does not
guess. This module applies the same rules locally, so a bad order fails here, tonight,
instead of silently on Luffy.
"""
import datetime as dt
import re

FIELDS = {"id", "for", "order_type", "action", "qty", "underlying", "contract",
          "expiry", "strike", "ref", "lmt_prc"}
EXPIRY = re.compile(r"^(\d+DTE[+-]?|W\+|M\+|\d{4}-\d{2}-\d{2})$")
STRIKE = re.compile(r"^(\d+(\.\d+)?|atm|(otm|itm):\d+|pct:[+-]?\d+(\.\d+)?|delta:\d+(\.\d+)?|premium:\d+(\.\d+)?)$")
LIMIT = re.compile(r"^(\d+(\.\d+)?|(mid|bid|ask)([+-]\d+(\.\d+)?)?)$")


def check(order):
    """List of problems with one order (empty list = valid)."""
    e = []
    if not isinstance(order, dict):
        return ["not an object"]
    extra = set(order) - FIELDS
    if extra:
        e.append(f"unknown fields {sorted(extra)}")
    for f in ("id", "for", "order_type", "action", "qty", "underlying", "contract"):
        if f not in order or order[f] in ("", None):
            e.append(f"missing {f}")
    if not isinstance(order.get("id", ""), str):
        e.append("id must be a string")
    try:
        dt.date.fromisoformat(str(order.get("for", "")))
    except ValueError:
        e.append("for must be YYYY-MM-DD")
    if order.get("order_type") not in ("mkt", "lmt"):
        e.append("order_type must be mkt or lmt")
    if order.get("action") not in ("BUY", "SEL", "SHT", "BTC"):
        e.append("action must be BUY, SEL, SHT or BTC")
    q = order.get("qty")
    if not isinstance(q, int) or isinstance(q, bool) or q < 1:
        e.append("qty must be an integer >= 1")
    u = order.get("underlying")
    if not isinstance(u, int) or isinstance(u, bool):
        e.append("underlying must be an integer conid")
    c = order.get("contract")
    if c not in ("put", "call", "underlying"):
        e.append("contract must be put, call or underlying")
    if c in ("put", "call"):
        if not EXPIRY.match(str(order.get("expiry", ""))):
            e.append("bad or missing expiry token")
        s = str(order.get("strike", ""))
        if s.startswith("vega:"):
            e.append("vega is reserved and rejected")
        elif not STRIKE.match(s):
            e.append("bad or missing strike token")
    elif c == "underlying" and ("expiry" in order or "strike" in order):
        e.append("underlying orders must omit expiry and strike")
    if order.get("ref", "open") not in ("open", "prev_close"):
        e.append("ref must be open or prev_close")
    if order.get("order_type") == "lmt" and not LIMIT.match(str(order.get("lmt_prc", ""))):
        e.append("lmt order needs a valid lmt_prc")
    if order.get("order_type") == "mkt" and "lmt_prc" in order:
        e.append("mkt order must not have lmt_prc")
    return e


def check_book(orders):
    """Problems across the whole file: each order, plus duplicate ids."""
    if not isinstance(orders, list):
        return ["open_orders.json must be a JSON array"]
    e = [f"{o.get('id', '?') if isinstance(o, dict) else '?'}: {p}" for o in orders for p in check(o)]
    ids = [o.get("id") for o in orders if isinstance(o, dict)]
    e += [f"duplicate id {i}" for i in sorted({i for i in ids if ids.count(i) > 1})]
    return e
