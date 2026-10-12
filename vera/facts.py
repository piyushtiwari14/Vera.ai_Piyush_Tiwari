"""Derived facts over raw category / merchant / customer JSON.

Each composer reads facts through these wrappers instead of poking at raw dicts,
so a missing field degrades to "don't mention it" rather than a crash or a
made-up number.
"""

from __future__ import annotations

import re
from typing import Any

from .fmt import clean_name, first, g, month_in_range, month_of, parent_name

# Per-category behaviour that the category JSON does not carry explicitly.
CATEGORY_PROFILE = {
    "dentists": {"noun": "clinic", "people": "patients", "visit": "appointment"},
    "salons": {"noun": "salon", "people": "clients", "visit": "appointment"},
    "restaurants": {"noun": "restaurant", "people": "diners", "visit": "order"},
    "gyms": {"noun": "studio", "people": "members", "visit": "session"},
    "pharmacies": {"noun": "store", "people": "customers", "visit": "order"},
}
DEFAULT_PROFILE = {"noun": "business", "people": "customers", "visit": "visit"}


class Category:
    def __init__(self, raw: dict | None, slug: str | None = None):
        self.raw = raw or {}
        self.slug = self.raw.get("slug") or slug or ""
        self.profile = CATEGORY_PROFILE.get(self.slug, DEFAULT_PROFILE)

    @property
    def peer(self) -> dict:
        return self.raw.get("peer_stats") or {}

    @property
    def taboo(self) -> list[str]:
        return list(g(self.raw, "voice", "vocab_taboo", default=[]) or [])

    @property
    def code_mix(self) -> str:
        return g(self.raw, "voice", "code_mix", default="") or ""

    def digest_item(self, item_id: str | None) -> dict | None:
        if not item_id:
            return None
        return first(self.raw.get("digest") or [], lambda d: d.get("id") == item_id)

    def digest_by_kind(self, *kinds: str) -> list[dict]:
        return [d for d in self.raw.get("digest") or [] if d.get("kind") in kinds]

    def digest_matching(self, *words: str) -> dict | None:
        words_l = [w.lower() for w in words if w]
        for d in self.raw.get("digest") or []:
            blob = " ".join(str(d.get(k, "")) for k in ("title", "summary", "actionable", "id")).lower()
            if any(w in blob for w in words_l):
                return d
        return None

    def catalog_offer(self, *words: str, audience: str | None = None) -> dict | None:
        words_l = [w.lower() for w in words if w]
        for o in self.raw.get("offer_catalog") or []:
            if audience and o.get("audience") not in (audience, "all"):
                continue
            if not words_l or any(w in o.get("title", "").lower() for w in words_l):
                return o
        return None

    def seasonal_now(self, now: Any) -> list[dict]:
        m = month_of(now)
        return [b for b in self.raw.get("seasonal_beats") or [] if month_in_range(m, b.get("month_range", ""))]

    def top_trend(self, *words: str) -> dict | None:
        trends = sorted(self.raw.get("trend_signals") or [], key=lambda t: -(t.get("delta_yoy") or 0))
        if words:
            words_l = [w.lower() for w in words]
            hit = first(trends, lambda t: any(w in t.get("query", "").lower() for w in words_l))
            if hit:
                return hit
        return trends[0] if trends else None


class Merchant:
    def __init__(self, raw: dict | None):
        self.raw = raw or {}

    # identity ------------------------------------------------------------
    @property
    def id(self) -> str:
        return self.raw.get("merchant_id", "")

    @property
    def name(self) -> str:
        return g(self.raw, "identity", "name", default="your business")

    @property
    def first_name(self) -> str:
        return g(self.raw, "identity", "owner_first_name", default="") or ""

    @property
    def locality(self) -> str:
        return g(self.raw, "identity", "locality", default="") or g(self.raw, "identity", "city", default="") or ""

    @property
    def city(self) -> str:
        return g(self.raw, "identity", "city", default="") or ""

    @property
    def verified(self) -> bool | None:
        return g(self.raw, "identity", "verified")

    @property
    def speaks_hindi(self) -> bool:
        return "hi" in (g(self.raw, "identity", "languages", default=[]) or [])

    def salutation(self, category: Category) -> str:
        fn = self.first_name
        if category.slug == "dentists":
            return f"Dr. {fn}" if fn else "Doc"
        return fn or f"{self.name} team"

    # performance ---------------------------------------------------------
    @property
    def perf(self) -> dict:
        return self.raw.get("performance") or {}

    def delta(self, metric: str) -> float | None:
        return g(self.perf, "delta_7d", f"{metric}_pct")

    @property
    def sub(self) -> dict:
        return self.raw.get("subscription") or {}

    @property
    def agg(self) -> dict:
        return self.raw.get("customer_aggregate") or {}

    # offers --------------------------------------------------------------
    @property
    def active_offers(self) -> list[dict]:
        return [o for o in self.raw.get("offers") or [] if (o.get("status") or "active") == "active"]

    @property
    def best_offer(self) -> dict | None:
        offers = self.active_offers
        return offers[0] if offers else None

    def offer_matching(self, *words: str) -> dict | None:
        words_l = [w.lower() for w in words if w]
        return first(self.active_offers, lambda o: any(w in o.get("title", "").lower() for w in words_l))

    # signals -------------------------------------------------------------
    @property
    def signals(self) -> dict[str, str]:
        out = {}
        for s in self.raw.get("signals") or []:
            k, _, v = str(s).partition(":")
            out[k] = v
        return out

    def has(self, *names: str) -> bool:
        sig = self.signals
        return any(n in sig for n in names)

    def signal_days(self, name: str) -> int | None:
        v = self.signals.get(name, "")
        m = re.match(r"(\d+)", v)
        return int(m.group(1)) if m else None

    # conversation --------------------------------------------------------
    @property
    def history(self) -> list[dict]:
        return self.raw.get("conversation_history") or []

    @property
    def last_merchant_msg(self) -> dict | None:
        return first(reversed(self.history), lambda h: h.get("from") == "merchant")

    @property
    def last_vera_msg(self) -> dict | None:
        return first(reversed(self.history), lambda h: h.get("from") == "vera")

    @property
    def ignored_last(self) -> bool:
        last = self.last_vera_msg
        return bool(last and last.get("engagement") == "merchant_no_reply")

    def review_theme(self, theme: str | None = None, sentiment: str | None = None) -> dict | None:
        themes = self.raw.get("review_themes") or []
        return first(themes, lambda t: (not theme or t.get("theme") == theme)
                     and (not sentiment or t.get("sentiment") == sentiment))

    @property
    def best_praise(self) -> dict | None:
        pos = [t for t in self.raw.get("review_themes") or [] if t.get("sentiment") == "pos"]
        return max(pos, key=lambda t: t.get("occurrences_30d") or 0) if pos else None


class Customer:
    def __init__(self, raw: dict | None):
        self.raw = raw or {}

    @property
    def exists(self) -> bool:
        return bool(self.raw)

    @property
    def id(self) -> str:
        return self.raw.get("customer_id", "")

    @property
    def raw_name(self) -> str:
        return g(self.raw, "identity", "name", default="") or ""

    @property
    def name(self) -> str:
        return clean_name(self.raw_name)

    @property
    def guardian(self) -> str:
        return parent_name(self.raw_name)

    @property
    def is_minor(self) -> bool:
        return str(g(self.raw, "identity", "age_band", default="")).startswith("child")

    @property
    def addressee(self) -> str:
        """Who actually reads the message (parent for kids)."""
        if self.guardian:
            return self.guardian
        return self.name

    @property
    def lang(self) -> str:
        return (g(self.raw, "identity", "language_pref", default="") or "").lower()

    @property
    def hinglish(self) -> bool:
        return self.lang.startswith("hi")

    @property
    def senior(self) -> bool:
        return bool(g(self.raw, "identity", "senior_citizen")) or str(
            g(self.raw, "identity", "age_band", default="")).startswith(("60", "65", "70", "75"))

    @property
    def rel(self) -> dict:
        return self.raw.get("relationship") or {}

    @property
    def prefs(self) -> dict:
        return self.raw.get("preferences") or {}

    @property
    def consent_scope(self) -> list[str]:
        return list(g(self.raw, "consent", "scope", default=[]) or [])

    @property
    def consented(self) -> bool:
        if not g(self.raw, "consent", "opted_in_at"):
            return False
        if self.prefs.get("reminder_opt_in") is False:
            return False
        if self.raw.get("opted_out") or g(self.raw, "consent", "opted_out_at"):
            return False
        return bool(self.consent_scope)

    def allows(self, *scopes: str) -> bool:
        """True when consent exists and covers at least one of the given scopes.
        An empty `scopes` tuple means 'any consent at all'."""
        if not self.consented:
            return False
        if not scopes:
            return True
        cs = set(self.consent_scope)
        return any(s in cs for s in scopes)
