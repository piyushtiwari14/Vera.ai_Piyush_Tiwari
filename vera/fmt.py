"""Small, pure formatting + lookup helpers. Everything here is deterministic."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any, Iterable

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ---------------------------------------------------------------- numbers

def inr(value: Any) -> str:
    """4999 -> '₹4,999', 125000 -> '₹1,25,000' (Indian grouping)."""
    try:
        n = int(round(float(str(value).replace(",", "").replace("₹", ""))))
    except (TypeError, ValueError):
        return str(value)
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return ("-" if n < 0 else "") + "₹" + s


def num(value: Any) -> str:
    """Integer with Indian grouping, no currency sign."""
    if value is None:
        return ""
    return inr(value).replace("₹", "")


def pct(value: Any, signed: bool = False) -> str:
    """0.18 -> '18%'; with signed -> '+18%' / '-18%'. Accepts already-% ints like 40."""
    if value is None:
        return ""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(v) <= 1.5:
        v *= 100
    r = round(v, 1)
    txt = f"{abs(r):.1f}".rstrip("0").rstrip(".")
    if signed:
        return ("+" if r >= 0 else "-") + txt + "%"
    return txt + "%"


def ratio_x(a: float, b: float) -> str:
    if not b:
        return ""
    return f"{a / b:.1f}x".replace(".0x", "x")


# ---------------------------------------------------------------- dates

def parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    v = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        try:
            dt = datetime.fromisoformat(v[:10])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def nice_date(value: Any) -> str:
    """'2026-11-12' -> '12 Nov'."""
    dt = parse_dt(value)
    if not dt:
        return str(value) if value else ""
    return f"{dt.day} {MONTHS[dt.month - 1]}"


def days_between(start: Any, end: Any) -> int | None:
    a, b = parse_dt(start), parse_dt(end)
    if not a or not b:
        return None
    return (b.date() - a.date()).days


def month_of(now: Any) -> int | None:
    dt = parse_dt(now)
    return dt.month if dt else None


def month_in_range(month: int, rng: str) -> bool:
    """'Oct-Dec', 'Jan', 'Nov-Feb' (wraps), 'Feb 14'."""
    if not month or not rng:
        return False
    tokens = re.findall(r"[A-Za-z]{3}", rng)
    idx = [MONTHS.index(t.title()) + 1 for t in tokens if t.title() in MONTHS]
    if not idx:
        return False
    if len(idx) == 1:
        return month == idx[0]
    a, b = idx[0], idx[1]
    return a <= month <= b if a <= b else (month >= a or month <= b)


# ---------------------------------------------------------------- text

def humanize(slug: Any) -> str:
    """'6_month_cleaning' -> '6-month cleaning', 'kids_yoga_summer_camp' -> 'kids yoga summer camp'."""
    if slug is None:
        return ""
    s = str(slug)
    s = re.sub(r"(\d)_(month|day|week|year)", r"\1-\2", s)
    return s.replace("_", " ").strip()


def join_and(items: Iterable[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def clean_name(name: Any) -> str:
    """'Aanya (parent: Sneha)' -> 'Aanya'; '(walk-in, no profile)' -> ''."""
    if not name:
        return ""
    s = str(name).strip()
    if s.startswith("("):
        return ""
    return re.sub(r"\s*\(.*?\)\s*", "", s).strip()


def parent_name(name: Any) -> str:
    m = re.search(r"parent:\s*([A-Za-z]+)", str(name or ""))
    return m.group(1) if m else ""


def squash(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" +([,.?!])", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------- dict helpers

def g(d: Any, *path: str, default: Any = None) -> Any:
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
        if cur is None:
            return default
    return cur


def first(items: Iterable[Any], pred=lambda _x: True, default: Any = None) -> Any:
    for it in items or []:
        if pred(it):
            return it
    return default
