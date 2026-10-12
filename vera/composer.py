"""compose(category, merchant, trigger, customer=None, now=None) -> dict

Deterministic message engine. No LLM, no randomness, no wall-clock reads:
same input -> byte-identical output.

Every handler follows the same recipe:
  1. pick the single strongest fact for *this* trigger (the "why now"),
  2. back it with one merchant-specific number (merchant fit),
  3. end with exactly one low-effort CTA (yes/no, or pick 1/2),
  4. pre-compute `on_yes`, the concrete deliverable Vera sends if the
     recipient agrees, so the reply turn delivers instead of asking again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable

from .facts import Category, Customer, Merchant
from .fmt import (days_between, g, humanize, inr, join_and, nice_date, num,
                  parse_dt, pct, squash)

CTA_YES_NO = "binary_yes_no"
CTA_SLOT = "multi_choice_slot"
CTA_CHOICE = "multi_choice"

SEND_AS_VERA = "vera"
SEND_AS_MERCHANT = "merchant_on_behalf"


@dataclass
class Draft:
    body: str
    cta: str = CTA_YES_NO
    why: str = ""
    on_yes: str = ""
    facts: list[str] = field(default_factory=list)
    to_customer: bool = False


@dataclass
class Ctx:
    cat: Category
    m: Merchant
    t: dict
    c: Customer
    now: str | None

    @property
    def p(self) -> dict:
        p = self.t.get("payload")
        return p if isinstance(p, dict) else {}

    @property
    def kind(self) -> str:
        return self.t.get("kind") or ""

    @property
    def sal(self) -> str:
        return self.m.salutation(self.cat)

    @property
    def people(self) -> str:
        return self.cat.profile["people"]

    @property
    def noun(self) -> str:
        return self.cat.profile["noun"]

    def days_to(self, iso: Any) -> int | None:
        if not self.now:
            return None
        return days_between(self.now, iso)

    @property
    def signer(self) -> str:
        """How the merchant signs customer-facing messages."""
        if self.cat.slug == "dentists" and self.m.first_name:
            return f"Dr. {self.m.first_name}'s clinic ({self.m.name})" if "Dr." not in self.m.name else self.m.name
        return self.m.name


# =====================================================================
# shared building blocks
# =====================================================================

def _starter_offer(x: Ctx) -> dict | None:
    """The category's flagship new-customer offer (catalog order = canonical order)."""
    cands = [o for o in x.cat.raw.get("offer_catalog") or []
             if o.get("audience") in ("new_user", "all") and o.get("type") not in ("membership", "free_addon")]
    return cands[0] if cands else None


def _premium_offer(x: Ctx) -> dict | None:
    """Highest-ticket live offer: festivals and seasons are when people trade up."""
    def price(o):
        mt = re.search(r"₹\s?([\d,]+)", o.get("title", ""))
        return int(mt.group(1).replace(",", "")) if mt else 0
    offers = x.m.active_offers
    return max(offers, key=price) if offers else None


def best_lever(x: Ctx) -> tuple[str, str, str]:
    """(short diagnosis, CTA question, on_yes deliverable) for the single most
    fixable gap in this merchant's listing."""
    m = x.m
    if not m.active_offers or m.has("no_active_offers"):
        o = _starter_offer(x)
        if o:
            return ("there's no live offer on your listing",
                    f"Want me to put '{o['title']}' live today?",
                    f"Done: '{o['title']}' is drafted for {m.name}. It goes live on your magicpin + Google listing "
                    f"as soon as you reply OK. Want to change the price first? Just send the new number.")
    if m.verified is False or m.has("unverified_gbp"):
        return ("your Google profile is still unverified",
                "Want me to start verification now? It's one phone call on your side.",
                "Started: Google will call the listing number to verify. Pick up and read the 5-digit code; "
                "I'll confirm here once it's through.")
    stale = m.signal_days("stale_posts")
    if stale or m.has("no_recent_post"):
        when = f"last post was {stale} days ago" if stale else "there's no recent post"
        return (f"your {when}",
                "Want me to draft 3 posts for you to approve?",
                "Here are 3 drafts. Reply 1, 2 or 3 to publish, or EDIT to change one.")
    if m.has("ctr_below_peer_median"):
        return ("your click-through is below similar listings",
                "Want me to rewrite your listing headline + top photo order?",
                "Draft ready: new headline + photo order. Reply OK to apply.")
    o = m.best_offer
    if o:
        return (f"'{o['title']}' is your strongest hook",
                f"Want me to push '{o['title']}' to nearby searchers this week?",
                f"Scheduled: '{o['title']}' boost for this week. I'll report clicks and calls on Monday.")
    return ("", "Want a 2-minute listing check-up?", "Running the check-up now; results in your next message.")


def peer_line(x: Ctx, metric: str) -> str:
    peer = x.cat.peer.get(f"avg_{metric}_30d")
    mine = x.m.perf.get(metric)
    if peer is None or mine is None:
        return ""
    if mine < peer:
        return f"Similar {x.noun}s average {num(peer)} {metric}/month; you're at {num(mine)}."
    return ""


_ABBREV = re.compile(r"(?:\b(?:Dr|Mr|Mrs|Ms|St|vs|No|p)|\b[A-Z])\.$")


def _sentences(text: str) -> list[str]:
    out, buf = [], ""
    for piece in re.split(r"(?<=[.!?])\s+", (text or "").strip()):
        buf = f"{buf} {piece}".strip() if buf else piece
        if not _ABBREV.search(buf):
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


def _first_sentence(text: str) -> str:
    s = _sentences(text)
    return s[0] if s else ""


def _lc(text: str) -> str:
    """Lower-case the first letter so a clause can follow a colon."""
    text = (text or "").strip().rstrip(".")
    return text[:1].lower() + text[1:] if text[:2] != text[:2].upper() else text


def _slot_labels(x: Ctx) -> list[str]:
    slots = x.p.get("available_slots") or x.p.get("next_session_options") or []
    return [s.get("label") or nice_date(s.get("iso")) for s in slots if isinstance(s, dict)][:2]


# =====================================================================
# merchant-facing handlers (send_as = vera)
# =====================================================================

def _cohort(x: Ctx, segment: str) -> tuple[int | None, str]:
    """Map a digest item's segment (e.g. 'high_risk_adults') to a count in the
    merchant's customer_aggregate (e.g. 'high_risk_adult_count')."""
    toks = [w.rstrip("s") for w in re.split(r"[_\W]+", segment.lower()) if w and w not in ("with", "and", "the")]
    if not toks:
        return None, ""
    best, best_hits = None, 0
    for key, val in x.m.agg.items():
        if not isinstance(val, (int, float)) or isinstance(val, bool) or val < 1 or key.endswith("_pct"):
            continue
        hits = sum(1 for tk in toks if tk in key.lower())
        if hits > best_hits:
            best, best_hits = (key, val), hits
    if best and best_hits >= max(1, len(toks) - 1):
        return int(best[1]), humanize(segment).replace("high risk", "high-risk")
    return None, ""


def h_research(x: Ctx) -> Draft:
    inline = x.p.get("item") or x.p.get("digest_item")
    item = (inline if isinstance(inline, dict) else None) \
        or x.cat.digest_item(x.p.get("top_item_id") or x.p.get("digest_item_id")) \
        or (x.cat.digest_by_kind("research", "trend") or [None])[0]
    if not item:
        return h_generic(x)
    cohort, seg_h = _cohort(x, item.get("patient_segment") or item.get("segment") or "")
    trial = f" ({num(item['trial_n'])}-patient trial)" if item.get("trial_n") else ""
    head = f"{x.sal}, worth a look: {item.get('source', 'new research')}{trial}."
    finding = _first_sentence(item.get("summary", "")) or item.get("title", "")
    if cohort:
        blob = (item.get("title", "") + item.get("summary", "")).lower()
        if "recall" in blob and ("caries" in blob or "decay" in blob):
            pitch = ("New research shows that for patients with a history of cavities, a check-up every 3 months "
                     "instead of 6 cuts repeat decay sharply. We'd like to see you a little sooner this time.")
        else:
            pitch = f"Sharing something relevant for you: {item.get('title', '').rstrip('.')}. We'd be happy to explain what it means for you."
        body = (f"{head} {finding} You have {num(cohort)} {seg_h} on file, so this applies directly. "
                f"Want me to draft a WhatsApp for them? You approve before anything goes out.")
        on_yes = (f"Draft for your {num(cohort)} {seg_h}:\n\n"
                  f"\"Hi {{name}}, {x.signer} here. {pitch} Reply YES and we'll share 2 slots.\"\n\n"
                  f"Reply SEND to send it to all {num(cohort)}, or EDIT to tweak the wording.")
        why = f"research digest matched to merchant cohort ({num(cohort)} {seg_h})"
    else:
        body = (f"{head} {finding} Takeaway for {x.m.name}: {item.get('actionable', '').rstrip('.')}. "
                f"Want a 3-line summary you can share with your team?")
        on_yes = (f"Summary: {item.get('title')}.\n1) {finding}\n2) {item.get('actionable', '')}\n"
                  f"3) Source: {item.get('source')}.")
        why = "research digest; no matching cohort, so offered a shareable summary"
    return Draft(body, why=why, on_yes=on_yes, facts=[item.get("id", "")])


def h_regulation(x: Ctx) -> Draft:
    item = x.cat.digest_item(x.p.get("top_item_id") or x.p.get("digest_item_id")) \
        or (x.cat.digest_by_kind("compliance") or [None])[0]
    deadline = x.p.get("deadline_iso") or x.t.get("expires_at")
    left = x.days_to(deadline)
    when = f"by {nice_date(deadline)}" + (f" ({left} days left)" if left is not None and left >= 0 else "")
    if not item:
        return Draft(f"{x.sal}, a compliance change applies to your {x.noun} {when}. "
                     f"Want the checklist?", why="regulation trigger without digest detail")
    summary = " ".join(_sentences(item.get("summary", ""))[:2])
    body = (f"{x.sal}, compliance heads-up from {item.get('source')}: {summary} "
            f"Action needed {when}: {_lc(item.get('actionable', ''))}. "
            f"Want a 4-point audit checklist for your {x.noun}?")
    on_yes = (f"Audit checklist ({item.get('title')}):\n"
              f"1) {item.get('actionable', 'Review current setup').rstrip('.')}.\n"
              f"2) Note make/model + date of each unit or register in your SOP file.\n"
              f"3) Fix or replace anything that fails before {nice_date(deadline)}.\n"
              f"4) Keep the circular ({item.get('source')}) printed with your SOPs for inspection.\n"
              f"Want a reminder 7 days before the deadline? Reply YES.")
    return Draft(body, why=f"regulatory deadline {nice_date(deadline)} with concrete action",
                 on_yes=on_yes, facts=[item.get("id", "")])


def h_cde(x: Ctx) -> Draft:
    item = x.cat.digest_item(x.p.get("digest_item_id") or x.p.get("top_item_id")) \
        or (x.cat.digest_by_kind("cde") or [None])[0]
    if not item:
        return h_generic(x)
    dt = parse_dt(item.get("date"))
    when = f"{nice_date(item.get('date'))}, {dt.strftime('%-I%p').lower()}" if dt and dt.hour else nice_date(item.get("date"))
    credits = x.p.get("credits") or item.get("credits")
    title = item.get("title", "")
    src = item.get("source", "").split(" calendar")[0]
    lead = src.split(" chapter")[0]
    if lead and title.startswith(lead + ":"):
        title = title[len(lead) + 1:].strip()
    body = (f"{x.sal}, {src or 'upcoming'}: "
            f"\"{title}\" on {when}"
            + (f", {credits} CDE credits" if credits else "") + ". "
            f"{_first_sentence(item.get('summary', ''))} {item.get('actionable', '').rstrip('.')}. "
            f"Want me to save the date and send you the joining link?")
    return Draft(body, why="CDE opportunity with date + credits",
                 on_yes=f"Saved: {item.get('title')} on {when}. I'll send the link 1 hour before.",
                 facts=[item.get("id", "")])


def h_perf_dip(x: Ctx) -> Draft:
    metric = x.p.get("metric", "views")
    delta = x.p.get("delta_pct") if x.p.get("delta_pct") is not None else x.m.delta(metric)
    base = x.p.get("vs_baseline")
    window = x.p.get("window", "7d").replace("7d", "this week")
    cur = f" (about {num(base * (1 + delta))} vs your usual {num(base)})" if base and delta is not None else ""
    diag, ask, on_yes = best_lever(x)
    peer = peer_line(x, metric)
    renew = x.m.sub.get("days_remaining")
    renew_txt = f" With renewal due in {renew} days, this is the week to fix it." if renew is not None and renew <= 15 else ""
    reasons = []
    if not x.m.active_offers:
        reasons.append("no live offer")
    if x.m.verified is False:
        reasons.append("an unverified Google profile")
    if x.m.signal_days("stale_posts"):
        reasons.append(f"no post in {x.m.signal_days('stale_posts')} days")
    reason_txt = (f" The fixable causes: {join_and(reasons)}." if len(reasons) > 1
                  else (f" Biggest fixable gap: {diag}." if diag else ""))
    body = (f"{x.sal}, {metric} at {x.m.name} dropped {pct(delta)} {window}{cur}. "
            f"{peer}{reason_txt}{renew_txt} {ask}")
    return Draft(body, why=f"{metric} {pct(delta, True)} → fix highest-leverage gap ({diag or 'promotion'})",
                 on_yes=on_yes)


def h_perf_spike(x: Ctx) -> Draft:
    metric = x.p.get("metric", "views")
    delta = x.p.get("delta_pct") if x.p.get("delta_pct") is not None else x.m.delta(metric)
    base = x.p.get("vs_baseline")
    driver = humanize(x.p.get("likely_driver")) if x.p.get("likely_driver") else ""
    cur = f" (~{num(base * (1 + delta))} vs your usual {num(base)})" if base and delta is not None else ""
    offer = x.m.best_offer
    hook = f" + your '{offer['title']}' offer" if offer else ""
    body = (f"{x.sal}, {metric} are up {pct(delta)} this week{cur}"
            + (f", and it lines up with your {driver}" if driver else "") + ". "
            f"Momentum fades in about a week. Want me to post a follow-up this week{(' on the same theme' if driver else '')}{hook}?")
    return Draft(body, why=f"{metric} spike {pct(delta, True)}; double down on driver '{driver or 'n/a'}'",
                 on_yes=f"Follow-up post drafted{(' around your ' + driver) if driver else ''}{hook}. Reply OK to publish today.")


def h_renewal(x: Ctx) -> Draft:
    days = x.p.get("days_remaining", x.m.sub.get("days_remaining"))
    plan = x.p.get("plan") or x.m.sub.get("plan") or "magicpin"
    amount = x.p.get("renewal_amount")
    perf = x.m.perf
    got = []
    if perf.get("views"):
        got.append(f"{num(perf['views'])} views")
    if perf.get("calls"):
        got.append(f"{num(perf['calls'])} calls")
    if perf.get("leads"):
        got.append(f"{num(perf['leads'])} leads")
    value = f" In the last {perf.get('window_days', 30)} days it brought {join_and(got)}." if got else ""
    dip = x.m.delta("calls")
    amt = f" ({inr(amount)})" if amount else ""
    ends = f"ends in {days} days" if days is not None else "is up for renewal"
    if dip is not None and dip <= -0.2:
        diag, _, _ = best_lever(x)
        body = (f"{x.sal}, your {plan} plan {ends}{amt}.{value} "
                f"Calls are down {pct(dip)} this week though, so I'd pair renewal with a fix: {diag}. "
                f"Want the renewal link with that recovery plan attached?")
        why = f"renewal in {days}d + calls {pct(dip, True)}: tie renewal to a fix instead of a bare ask"
    else:
        body = (f"{x.sal}, your {plan} plan {ends}{amt}.{value} "
                f"Renewing now keeps your listing boost running without a gap. Want the renewal link?")
        why = f"renewal in {days}d; value recap from own metrics"
    on_yes = (f"Here's your renewal link for the {plan} plan{amt}: [renewal link]. "
              f"Once it's through I'll start the recovery plan the same day.")
    return Draft(body, why=why, on_yes=on_yes)


def h_winback(x: Ctx) -> Draft:
    days = x.p.get("days_since_expiry", x.m.sub.get("days_since_expiry"))
    dip = x.p.get("perf_dip_pct", x.m.delta("calls"))
    added = x.p.get("lapsed_customers_added_since_expiry")
    lapsed = x.m.agg.get("lapsed_90d_plus") or x.m.agg.get("lapsed_180d_plus")
    offer = x.m.best_offer or x.cat.catalog_offer("spa", "cleaning", "trial", "consult", audience="new_user") or _starter_offer(x)
    parts = [f"{x.sal}, it's been {days} days since {x.m.name}'s plan lapsed." if days is not None
             else f"{x.sal}, {x.m.name}'s plan has lapsed."]
    if dip is not None:
        parts.append(f"Since then calls are down {pct(dip)}"
                     + (f" and {added} more {x.people} have stopped coming" if added else "")
                     + (f" ({num(lapsed)} lapsed in total)." if lapsed else "."))
    offer_txt = f" with '{offer['title']}'" if offer else ""
    parts.append(f"If you restart, the first thing I'd do is a comeback message to those {num(lapsed) if lapsed else ''} {x.people}{offer_txt}. "
                 f"Want me to reactivate and set that up?")
    return Draft(squash(" ".join(parts)), why=f"expired {days}d; quantify loss with lapsed {x.people}",
                 on_yes=f"Reactivation link: [link]. The comeback message{offer_txt} is drafted and ready for your OK.")


def h_dormant(x: Ctx) -> Draft:
    days = x.p.get("days_since_last_merchant_message")
    # Re-open with one free, useful, data-backed tip instead of re-pitching.
    tip = x.cat.digest_matching("walk-in", "verified", "reminder", "7am", "sugar-free") \
        or (x.cat.digest_by_kind("trend") or [None])[0]
    if tip:
        stat = _first_sentence(tip.get("summary", ""))
        body = (f"{x.sal}, no pitch today, just one quick win from this week: {_lc(stat)} "
                f"({tip.get('source')}). Want me to apply it to {x.m.name}'s listing? Takes me 2 minutes.")
        on_yes = f"Done: applied to {x.m.name}: {tip.get('actionable', '').rstrip('.')}. I'll share the before/after calls in 7 days."
    else:
        diag, ask, on_yes = best_lever(x)
        body = f"{x.sal}, quick one: {diag}. {ask}"
    return Draft(body, why=f"dormant {days}d: lead with free value, not a re-pitch", on_yes=on_yes)


def h_festival(x: Ctx) -> Draft:
    fest = x.p.get("festival")
    date = x.p.get("date")
    if not fest or not date:
        return h_generic(x)
    days = x.p.get("days_until")
    if days is None:
        days = x.days_to(date)
    rel = x.p.get("category_relevance")
    if rel and x.cat.slug and x.cat.slug not in rel:
        return Draft(f"{x.sal}, {fest} is on {nice_date(date)}. It's usually a quieter week for {x.noun}s, "
                     f"so it's a good time to plan staff leave. Want a reminder a week before?",
                     why="festival not demand-relevant for this category")
    beat = next((b for b in x.cat.raw.get("seasonal_beats") or []
                 if "festival" in b.get("note", "").lower() or "diwali" in b.get("note", "").lower()), None)
    offer = _premium_offer(x)
    lead = 21
    post_on = ""
    dt = parse_dt(date)
    if dt:
        post_on = nice_date((dt - timedelta(days=lead)).isoformat())
    beat_txt = f" {beat['note'].capitalize()} ({beat['month_range']})." if beat else ""
    offer_txt = f" around your '{offer['title']}'" if offer else ""
    if days is not None and days > 45:
        body = (f"{x.sal}, {fest} is {days} days out ({nice_date(date)}).{beat_txt} "
                f"The {x.noun}s that win it open pre-bookings ~3 weeks early. "
                f"Want me to schedule a {fest} pre-booking post{offer_txt} for {post_on}?")
    else:
        body = (f"{x.sal}, {fest} is {f'{days} days away' if days is not None else 'coming up'} ({nice_date(date)}).{beat_txt} "
                f"Want me to put up a {fest} post{offer_txt} today?")
    return Draft(squash(body), why=f"{fest} in {days}d; seasonal beat for {x.cat.slug}",
                 on_yes=f"Scheduled: {fest} post{offer_txt} goes live {post_on or 'today'}. Reply EDIT to change the copy.")


def h_curious(x: Ctx) -> Draft:
    cat_offers = [o["title"].split(" @")[0].split(":")[0] for o in (x.cat.raw.get("offer_catalog") or [])
                  if o.get("type") == "service_at_price"][:3]
    opts = " ".join(f"{i}) {t}" for i, t in enumerate(cat_offers, 1)) + f" {len(cat_offers) + 1}) something else"
    lapsed = x.m.agg.get("lapsed_90d_plus") or x.m.agg.get("lapsed_180d_plus")
    tail = f" + a WhatsApp offer for your {num(lapsed)} lapsed {x.people}" if lapsed else ""
    body = (f"{x.sal}, quick one for this week's post: what are people asking for most at {x.m.name}? "
            f"{opts}. Reply with a number and I'll turn it into a Google post{tail}.")
    return Draft(body, cta=CTA_CHOICE, why="curious-ask: zero-effort numeric reply that feeds the next post",
                 on_yes="Got it, drafting the post + offer around that now.")


def h_ipl(x: Ctx) -> Draft:
    match = x.p.get("match", "tonight's match")
    t = parse_dt(x.p.get("match_time_iso"))
    when = t.strftime("%-I:%M%p").lower().replace(":00", "") if t else "tonight"
    weeknight = x.p.get("is_weeknight")
    insight = x.cat.digest_matching("ipl")
    combo = x.cat.catalog_offer("match")
    deliv = x.m.agg.get("delivery_orders_30d")
    dine = x.m.agg.get("dine_in_orders_30d")
    bogo = x.m.offer_matching("tue", "thu", "weekday")
    if weeknight is False:
        stat = ("Weekend matches shift orders to home watch parties; dine-in covers drop ~12% vs a normal Saturday"
                if insight else "Weekend matches usually shift orders to home delivery")
        mix = f" Your delivery is already {num(deliv)} orders/30d vs {num(dine)} dine-in, so lean into delivery." if deliv and dine else ""
        bogo_txt = f" Note: your '{bogo['title']}' won't apply tonight." if bogo else ""
        offer_txt = f"'{combo['title']}' for delivery" if combo else "a match-night delivery combo"
        body = (f"{x.sal}, {match} tonight at {when}. {stat}.{mix}{bogo_txt} "
                f"Want me to put {offer_txt} live by 6pm?")
        why = "weekend IPL → home-watch shift; pivot to delivery (category digest + own order mix)"
    else:
        offer_txt = f"'{combo['title']}'" if combo else "a match-night combo"
        stat = "Weeknight matches drive ~18% more dine-in covers" if insight else "Match nights bring extra footfall"
        body = (f"{x.sal}, {match} tonight at {when}. {stat}. "
                f"Want me to put {offer_txt} on your listing before 6pm"
                + (f", alongside '{bogo['title']}'" if bogo else "") + "?")
        why = "weeknight IPL → dine-in push"
    return Draft(body, why=why, on_yes=f"Live: {offer_txt} on {x.m.name}'s listing until midnight. I'll share orders tomorrow.")


def h_review_theme(x: Ctx) -> Draft:
    theme = x.p.get("theme") or g(x.m.review_theme(sentiment="neg"), "theme", default="")
    if not theme:
        return h_generic(x)
    n = x.p.get("occurrences_30d") or g(x.m.review_theme(theme), "occurrences_30d")
    quote = x.p.get("common_quote") or g(x.m.review_theme(theme), "common_quote", default="")
    praise = x.m.best_praise
    trend = " and it's rising" if x.p.get("trend") == "rising" else ""
    q = f": \"{quote}\"" if quote else ""
    praise_txt = (f" Good news: {humanize(praise['theme'])} got {praise.get('occurrences_30d')} positive mentions, "
                  f"so the product isn't the problem.") if praise else ""
    count = f"in {n} reviews this month" if n else "in recent reviews"
    on_all = f"on all {n}" if n else "on each of them"
    body = (f"{x.sal}, '{humanize(theme)}' came up {count}{trend}{q}.{praise_txt} "
            f"Want me to draft a polite public reply you can post {on_all}?")
    return Draft(body, why=f"negative review theme {theme} x{n}; protect rating before it compounds",
                 on_yes=(f"Reply draft: \"Thank you for flagging this, and sorry about the wait. "
                         f"We've changed how we handle {humanize(theme).replace('delivery late', 'deliveries')} "
                         f"at peak hours. Please give us another try and tell us how it goes. {x.m.first_name or x.m.name}\" "
                         f"Reply POST to publish it {'on all ' + str(n) if n else 'on each one'}."))


def h_milestone(x: Ctx) -> Draft:
    metric = humanize(x.p.get("metric", "reviews")).replace("review count", "Google reviews")
    now_v, goal = x.p.get("value_now"), x.p.get("milestone_value")
    gap = (goal - now_v) if isinstance(goal, (int, float)) and isinstance(now_v, (int, float)) else None
    praise = x.m.best_praise
    praise_txt = f" Your {x.people} keep praising {humanize(praise['theme'])} ({praise['occurrences_30d']} mentions this month)." if praise else ""
    body = (f"{x.sal}, {x.m.name} is at {num(now_v)} {metric}"
            + (f", just {gap} away from {num(goal)}" if gap is not None else "") + f".{praise_txt} "
            f"Want me to draft a one-tap review request you can send to today's regulars?")
    return Draft(body, why=f"milestone {now_v}/{goal}: tiny gap, easy win",
                 on_yes=(f"Draft: \"Thanks for being a regular at {x.m.name}! If you enjoyed today, a quick Google review "
                         f"helps us a lot: [review link]\" Reply SEND and I'll share it with today's {x.people}."))


def h_planning(x: Ctx) -> Draft:
    topic = x.p.get("intent_topic", "")
    topic_h = humanize(topic)
    last = x.p.get("merchant_last_message") or g(x.m.last_merchant_msg, "body", default="")
    quote = f" (\"{last}\")" if last else ""
    plan = ""
    if "thali" in topic:
        base = x.m.offer_matching("thali")
        price = None
        if base:
            mt = re.search(r"₹\s?([\d,]+)", base["title"])
            price = int(mt.group(1).replace(",", "")) if mt else None
        trend = x.cat.top_trend("thali")
        trend_txt = f" '{trend['query']}' searches are {pct(trend['delta_yoy'], True)} YoY." if trend else ""
        if price:
            plan = (f"Corporate Thali Pack: minimum 10 thalis/day, delivered by 12:45pm, "
                    f"{inr(price)} list → {inr(round(price * 0.9))} for 10+ and {inr(round(price * 0.85))} for 25+, "
                    f"billed weekly with GST invoice.{trend_txt}")
        else:
            plan = f"Corporate Thali Pack: minimum 10/day, fixed lunch window, weekly billing.{trend_txt}"
        cta = "Want me to turn this into a one-page flyer for offices in " + (x.m.locality or "your area") + "?"
    else:
        # reuse Vera's own earlier proposal if one exists, so we stay consistent
        prior = next((h["body"] for h in reversed(x.m.history)
                      if h.get("from") == "vera" and re.search(r"\d", h.get("body", ""))), "")
        mt = re.search(r"Suggest (.*?)\.", prior)
        if mt:
            plan = f"As discussed: {mt.group(1)}."
        else:
            offer = x.m.best_offer
            plan = f"Start small: a 4-week pilot, 3 slots/week" + (f", launched with '{offer['title']}'" if offer else "") + "."
        cta = f"Want me to draft the Google post + a WhatsApp to your {x.people} for it?"
    body = f"{x.sal}, picking up on {topic_h}{quote}. Here's a first cut: {plan} {cta}"
    return Draft(squash(body), why=f"merchant asked about {topic_h}: answer with a concrete draft, not a question",
                 on_yes=f"Drafts for {topic_h} are ready: 1) Google post 2) WhatsApp broadcast. Reply 1, 2 or BOTH to publish.")


def h_seasonal_dip(x: Ctx) -> Draft:
    metric = x.p.get("metric", "views")
    delta = x.p.get("delta_pct")
    note = humanize(x.p.get("season_note", "")).replace(" apr jun", " (Apr-Jun)").replace("post resolution window", "post-New-Year")
    churn = x.m.agg.get("monthly_churn_pct")
    members = x.m.agg.get("total_active_members")
    peer_churn = x.cat.peer.get("monthly_churn_pct")
    retention = ""
    if churn and members:
        lose = round(churn * members)
        retention = (f" Better lever right now is retention: churn is {pct(churn)}/month"
                     + (f" vs {pct(peer_churn)} for peers" if peer_churn else "")
                     + f", about {lose} of your {num(members)} {x.people} a month.")
    body = (f"{x.sal}, {metric} are down {pct(delta)} this week, but that's the usual {note or 'seasonal'} dip, "
            f"not something you did.{retention} Want me to draft a 4-week 'summer streak' challenge for current {x.people}?")
    return Draft(squash(body), why="expected seasonal dip → reassure + shift to retention",
                 on_yes=(f"Draft: \"Summer Streak at {x.m.name}: 12 sessions in 4 weeks = 1 free PT session. "
                         f"Reply JOIN to enter.\" Reply SEND to share with all {num(members or 0)} {x.people}."))


def h_supply(x: Ctx) -> Draft:
    item = x.cat.digest_item(x.p.get("alert_id")) or (x.cat.digest_by_kind("alert") or [None])[0]
    mol = x.p.get("molecule", "the affected molecule")
    batches = join_and(x.p.get("affected_batches") or [])
    mfr = x.p.get("manufacturer")
    risk = ""
    if item and "no safety risk" in item.get("summary", "").lower():
        risk = " No safety risk beyond weaker LDL control." if "ldl" in item.get("summary", "").lower() else " No acute safety risk."
    asked = x.m.last_merchant_msg and "list" in x.m.last_merchant_msg.get("body", "").lower()
    chronic = x.m.agg.get("chronic_rx_count")
    lst = (f" As you asked on {nice_date(x.m.last_merchant_msg['ts'])}, I've filtered your repeat-Rx list for {mol}."
           if asked else (f" I can filter your {num(chronic)} chronic-Rx customers for {mol}." if chronic else ""))
    body = (f"{x.sal}, {mol} recall{f' ({item.get('source')})' if item else ''}: batches {batches}"
            + (f" by {mfr}" if mfr else "") + f" are flagged for sub-potency.{risk}{lst} "
            f"Want me to send affected customers a WhatsApp to bring their strips back for a replacement?")
    return Draft(body, why="urgent supply recall + merchant already asked for the list",
                 on_yes=(f"Sending now: \"Namaste, {x.m.name} here. One batch of your {mol} is part of a precautionary recall. "
                         f"Please bring the strip to the store or reply here, and we'll replace it free. "
                         f"Keep taking your medicine until then.\" I'll share who confirmed by evening."))


def h_category_seasonal(x: Ctx) -> Draft:
    trends = []
    for tr in x.p.get("trends") or []:
        mt = re.match(r"(.+?)_demand_([+-]?\d+)", str(tr))
        if mt:
            sign = "+" if not mt.group(2).startswith(("-", "+")) else ""
            trends.append(f"{humanize(mt.group(1)).replace('cold cough', 'cold & cough')} {sign}{mt.group(2)}%")
    item = x.cat.digest_matching("summer", "monsoon", "seasonal") if not trends else x.cat.digest_matching(*[t.split()[0] for t in trends[:1]])
    act = item.get("actionable", "").rstrip(".") if item else ""
    offer = x.m.best_offer
    season = humanize(x.p.get("season", "season")).replace(" 2026", "")
    body = (f"{x.sal}, {season} shift is here: {join_and(trends)}."
            + (f" {act}." if act else "")
            + f" Want me to post a '{season.split()[0].title()} essentials' update on your Google profile"
            + (f" with your '{offer['title']}' offer" if offer else "") + "?")
    return Draft(squash(body), why="category seasonal demand shift with shelf action",
                 on_yes=f"Posted draft: '{season.split()[0].title()} essentials' + {offer['title'] if offer else 'store timings'}. Reply OK to publish.")


def h_gbp(x: Ctx) -> Draft:
    uplift = x.p.get("estimated_uplift_pct")
    path = humanize(x.p.get("verification_path", "")).replace(" or ", " or a ")
    views = x.m.perf.get("views")
    peer = x.cat.peer.get("avg_views_30d")
    cmp = f" You're at {num(views)} views/month vs ~{num(peer)} for similar {x.noun}s." if views and peer and views < peer else ""
    body = (f"{x.sal}, {x.m.name} is still unverified on Google."
            + (f" Verifying typically lifts visibility ~{pct(uplift)}." if uplift else "")
            + f"{cmp} It's a {path or 'quick check'}, about 5 minutes for you. Want me to start it now?")
    return Draft(body, why="unverified GBP: cheapest high-uplift fix", on_yes=best_lever(x)[2] if x.m.verified is False
                 else "Verification started. I'll confirm once Google approves.")


def h_competitor(x: Ctx) -> Draft:
    name = x.p.get("competitor_name")
    dist = x.p.get("distance_km")
    if not name:
        return h_generic(x)
    their = x.p.get("their_offer", "")
    opened = x.p.get("opened_date")
    service = their.split(" @")[0] if their else ""
    mine = x.m.offer_matching(service.split()[-1].lower()) if service else None
    gap = ""
    if mine and their:
        a = re.search(r"₹\s?([\d,]+)", their)
        b = re.search(r"₹\s?([\d,]+)", mine["title"])
        if a and b:
            diff = int(b.group(1).replace(",", "")) - int(a.group(1).replace(",", ""))
            if diff > 0:
                gap = f", {inr(diff)} under your '{mine['title']}'"
    praise = x.m.best_praise
    praise_txt = (f" Don't race them on price. Your reviews say \"{praise['common_quote']}\" "
                  f"({praise['occurrences_30d']} mentions this month), and a new {x.noun} can't copy that.") if praise and praise.get("common_quote") else \
        " Don't race them on price; lead with what your regulars already value."
    body = (f"{x.sal}, {name} opened " + (f"{dist} km away" if dist is not None else "nearby")
            + (f" on {nice_date(opened)}" if opened else "")
            + (f" with '{their}'{gap}" if their else "") + f".{praise_txt} "
            f"Want me to draft a post that leads with that?")
    return Draft(body, why="competitor undercut: differentiate on proven strength, avoid price war",
                 on_yes="Post drafted around your review strengths + current offer. Reply OK to publish.")


def h_generic(x: Ctx) -> Draft:
    """Unknown trigger kind: still ground the message in whatever the payload carries."""
    kind_h = humanize(x.kind) or "update"
    facts = []
    for k, v in x.p.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if k.endswith("_pct") or "delta" in k:
                facts.append(f"{humanize(k).replace(' pct', '')} {pct(v, True)}")
            else:
                facts.append(f"{humanize(k)} {num(v)}" if abs(v) >= 1000 else f"{humanize(k)} {v}")
        elif isinstance(v, str) and len(v) < 40 and not k.endswith(("_id", "_iso")):
            facts.append(f"{humanize(k)}: {humanize(v)}")
    diag, ask, on_yes = best_lever(x)
    fact_txt = f" ({'; '.join(facts[:3])})" if facts else ""
    body = f"{x.sal}, new {kind_h} for {x.m.name}{fact_txt}." + (f" Biggest gap right now: {diag}." if diag else "") + f" {ask}"
    return Draft(squash(body), why=f"unrecognised trigger '{x.kind}': grounded in payload + top listing gap", on_yes=on_yes)


# =====================================================================
# customer-facing handlers (send_as = merchant_on_behalf)
# =====================================================================

def _hi(x: Ctx) -> str:
    return f"Hi {x.c.addressee}" if x.c.addressee else "Hi"


def c_recall(x: Ctx) -> Draft:
    service = humanize(x.p.get("service_due", "check-up"))
    due = nice_date(x.p.get("due_date"))
    slots = _slot_labels(x)
    offer = x.m.offer_matching(service.split()[-1]) if service else None
    offer_txt = f" ({offer['title'].split(' @')[0].lower()} is {offer['title'].split('@ ')[-1]} this month)" if offer and "@" in offer["title"] else ""
    who = x.c.name if x.c.is_minor and x.c.guardian else ""
    for_who = f"{who}'s " if who else "your "
    if x.c.hinglish:
        slot_txt = f" Aapke liye 2 slots rakhe hain: 1) {slots[0]} 2) {slots[1]}. Bas 1 ya 2 reply karein." if len(slots) >= 2 \
            else " Reply YES karein, hum slot bhej denge."
        offer_hi = f" ({offer['title'].split(' @')[0]} sirf {offer['title'].split('@ ')[-1]} mein)" if offer_txt else ""
        body = f"{_hi(x)}, {x.signer} se. {(who + ' ki') if who else 'Aapki'} {service} {due} ko due hai{offer_hi}.{slot_txt}"
    else:
        slot_txt = f" We've kept two slots for you: 1) {slots[0]} or 2) {slots[1]}. Reply 1 or 2 to confirm." if len(slots) >= 2 \
            else " Reply YES and we'll share a slot."
        body = f"{_hi(x)}, {x.signer} here. {for_who.capitalize()}{service} is due on {due}{offer_txt}.{slot_txt}"
    return Draft(squash(body), cta=CTA_SLOT if len(slots) >= 2 else CTA_YES_NO, to_customer=True,
                 why=f"recall due {due}; 2 pre-held slots matching preference '{x.c.prefs.get('preferred_slots', '-')}'",
                 on_yes=f"Booked ✅ See you then at {x.m.name}. We'll send a reminder 2 hours before.")


def c_bridal(x: Ctx) -> Draft:
    wd = x.p.get("wedding_date") or x.c.prefs.get("wedding_date")
    if not wd:
        return c_generic(x)
    days = x.p.get("days_to_wedding") or x.days_to(wd)
    trial = nice_date(x.p.get("trial_completed")) if x.p.get("trial_completed") else ""
    step = humanize(x.p.get("next_step_window_open", "skin prep program"))
    mt = re.match(r"(.*?)\s*(\d+)\s*day$", step)
    if mt:
        step = f"{mt.group(2)}-day {mt.group(1)}"
    start = ""
    dt = parse_dt(wd)
    if dt:
        start = nice_date((dt - timedelta(days=45)).isoformat())
    pref = x.c.prefs.get("preferred_slots", "")
    pref_txt = " on a Saturday" if "saturday" in pref else ""
    body = (f"{_hi(x)}, {x.m.name} here 💐 Hope you loved your bridal trial{f' on {trial}' if trial else ''}! "
            + (f"{days} days to go for {nice_date(wd)}. " if days is not None else f"Your big day is {nice_date(wd)}. ")
            + f"We'd suggest starting the {step} around {start} so your skin peaks right on the day. "
            f"Shall we block a quick consult{pref_txt} to plan it? Reply YES.")
    return Draft(squash(body), to_customer=True, why=f"bridal follow-up; wedding in {days}d; plan next step date",
                 on_yes="Lovely! We'll hold this Saturday's consult slot; reply with a time that works (morning/afternoon).")


def c_lapsed(x: Ctx) -> Draft:
    days = x.p.get("days_since_last_visit")
    focus = humanize(x.p.get("previous_focus") or x.c.prefs.get("training_focus", ""))
    months = x.p.get("previous_membership_months")
    free = x.cat.catalog_offer("body composition", "consultation", "check", audience="all") or x.m.best_offer
    offer_txt = f" Come in for a {free['title'].replace('Free ', 'free ').lower()} on us" if free and "free" in free["title"].lower() \
        else (f" Come back with '{free['title']}'" if free else " Come back this week")
    pref = humanize(x.c.prefs.get("preferred_slots", ""))
    body = (f"{_hi(x)}, {x.m.name} here. It's been {days} days since your last session"
            + (f", and {months} months of {focus} work is too good to lose" if months and focus else "") + "."
            f"{offer_txt}, so we can see where you are and reset your plan."
            + (f" Is {pref} still best for you?" if pref else "") + " Reply YES and we'll hold a slot.")
    return Draft(squash(body), to_customer=True, why=f"lapsed {days}d; free re-entry, no discount pressure",
                 on_yes="Great to have you back! Your slot is held. We'll message the exact time tomorrow morning.")


def c_trial_followup(x: Ctx) -> Draft:
    slots = _slot_labels(x)
    trial = nice_date(x.p.get("trial_date"))
    kid = x.c.name if x.c.is_minor else ""
    subject = f"{kid}'s" if kid else "your"
    nxt = f" Next session: {slots[0]}." if slots else ""
    body = (f"{_hi(x)}, {x.m.name} here 🙏 Thanks for bringing {kid or 'yourself'} for {subject.replace('your', 'your')} trial on {trial}.{nxt} "
            f"Shall we save {('his/her' if kid else 'your')} spot? Reply YES.")
    if kid:
        body = (f"{_hi(x)}, {x.m.name} here 🙏 Thanks for bringing {kid} for the trial on {trial}.{nxt} "
                f"Shall we save {kid}'s spot? Reply YES.")
    return Draft(squash(body), to_customer=True, why="trial follow-up with the next concrete slot",
                 on_yes=f"Spot saved ✅ See you {slots[0] if slots else 'soon'}!")


def c_refill(x: Ctx) -> Draft:
    mols = x.p.get("molecule_list") or []
    runs = nice_date(x.p.get("stock_runs_out_iso"))
    saved = x.p.get("delivery_address_saved") or x.c.prefs.get("delivery_address") == "saved"
    senior = x.c.senior and x.m.offer_matching("senior")
    name = x.c.name
    surname = re.sub(r"^(Mr|Mrs|Ms|Shri|Smt)\.?\s+", "", name) if name else ""
    hon = f"{surname} ji" if surname and x.c.lang.startswith("hi") else name
    recall = x.cat.digest_by_kind("alert")
    recall_mol = next((m for m in mols if any(m in (r.get("title", "") + r.get("summary", "")).lower() for r in recall)), None)
    via = "son" in x.c.prefs.get("channel", "")
    if x.c.lang.startswith("hi"):
        body = (f"Namaste{'' if via else ' ' + hon}, {x.m.name} se. {hon + ' ki' if via else 'Aapki'} dawaiyan ({", ".join(mols)}) {runs} tak khatam ho jayengi. "
                + ("Saved address par kal subah delivery kar dein?" if saved else "Kal tak store se le sakte hain?")
                + (f" Senior citizen 15% OFF lagega." if senior else "")
                + (f" {recall_mol.title()} ka batch hum recall list se check karke hi bhejenge." if recall_mol else "")
                + " Reply HAAN.")
    else:
        body = (f"{_hi(x)}, {x.m.name} here. {('Your' if not via else hon + chr(39) + 's')} medicines ({join_and(mols)}) run out on {runs}. "
                + ("Shall we deliver to your saved address tomorrow morning?" if saved else "Shall we keep them ready for pickup?")
                + (" Senior citizen 15% OFF applies." if senior else "")
                + (f" We'll check the {recall_mol} batch against this month's recall before dispatch." if recall_mol else "")
                + " Reply YES.")
    return Draft(squash(body), to_customer=True, why=f"chronic refill runs out {runs}; one-tap delivery",
                 on_yes="Order placed ✅ Delivery tomorrow morning. Pay on delivery.")


def c_generic(x: Ctx) -> Draft:
    offer = x.m.best_offer
    body = (f"{_hi(x)}, {x.m.name} here."
            + (f" '{offer['title']}' is on this week." if offer else " We'd love to see you again soon.")
            + " Reply YES and we'll hold a slot for you.")
    return Draft(body, to_customer=True, why=f"customer trigger '{x.kind}' with generic but grounded hook",
                 on_yes="Done ✅ We'll confirm the time shortly.")


# kind -> (handler, consent scopes that permit it; () = any consent)
MERCHANT_HANDLERS: dict[str, Callable[[Ctx], Draft]] = {
    "research_digest": h_research,
    "regulation_change": h_regulation,
    "compliance": h_regulation,
    "cde_opportunity": h_cde,
    "perf_dip": h_perf_dip,
    "perf_spike": h_perf_spike,
    "renewal_due": h_renewal,
    "winback_eligible": h_winback,
    "dormant_with_vera": h_dormant,
    "festival_upcoming": h_festival,
    "curious_ask_due": h_curious,
    "ipl_match_today": h_ipl,
    "review_theme_emerged": h_review_theme,
    "milestone_reached": h_milestone,
    "active_planning_intent": h_planning,
    "seasonal_perf_dip": h_seasonal_dip,
    "supply_alert": h_supply,
    "category_seasonal": h_category_seasonal,
    "gbp_unverified": h_gbp,
    "competitor_opened": h_competitor,
}

CUSTOMER_HANDLERS: dict[str, tuple[Callable[[Ctx], Draft], tuple[str, ...]]] = {
    "recall_due": (c_recall, ("recall_reminders", "appointment_reminders")),
    "wedding_package_followup": (c_bridal, ("bridal_package_followup", "appointment_reminders")),
    "customer_lapsed_hard": (c_lapsed, ("winback_offers", "renewal_reminders", "promotional_offers")),
    "customer_lapsed_soft": (c_lapsed, ("winback_offers", "renewal_reminders", "promotional_offers")),
    "trial_followup": (c_trial_followup, ("kids_program_updates", "program_updates", "appointment_reminders")),
    "chronic_refill_due": (c_refill, ("refill_reminders", "delivery_notifications")),
}


def _no_consent(x: Ctx) -> Draft:
    who = x.c.addressee or "this customer"
    return Draft(
        f"{x.sal}, {who} is due for a {humanize(x.kind)} message, but they haven't opted in to this kind of WhatsApp "
        f"from {x.m.name}, so I haven't sent anything. Want me to remind you to mention it at their next visit instead?",
        why="customer trigger blocked by consent; routed to merchant instead",
        on_yes=f"Noted: I'll flag {who} on your visit list.")


_BROKEN = re.compile(r"\bNone\b|\bnan\b|[{}]|\(\)|''|\b(?:in|on|by|for|at|of|is|to) [.,?]")


def _safe(handler: Callable[[Ctx], Draft], fallback: Callable[[Ctx], Draft], x: Ctx) -> Draft:
    """Never ship a crash or a half-filled template: degrade to the grounded
    generic handler, then to a minimal message that is always well-formed."""
    for fn in (handler, fallback):
        try:
            d = fn(x)
            if d and d.body and not _BROKEN.search(d.body):
                return d
        except Exception:  # noqa: BLE001 - malformed judge input must not 500
            pass
    return Draft(f"{x.sal}, I have a quick idea to get {x.m.name} more {x.people} this week. Want me to share it?",
                 why="fallback: input too sparse for a grounded message",
                 on_yes=best_lever(x)[2] if x.m.raw else "")


def _scrub(text: str, taboo: list[str]) -> str:
    for t in taboo:
        base = re.sub(r"\s*\(.*?\)", "", t).strip()
        if base:
            text = re.sub(re.escape(base), "", text, flags=re.IGNORECASE)
    return squash(text)


def suppression_key(trigger: dict, merchant_id: str = "", customer_id: str = "") -> str:
    """Trigger's own key, else a stable one derived from kind + trigger id + recipient."""
    if trigger.get("suppression_key"):
        return trigger["suppression_key"]
    mid = merchant_id or trigger.get("merchant_id") or ""
    cid = customer_id or trigger.get("customer_id") or "merchant"
    return f"{trigger.get('kind') or 'generic'}:{trigger.get('id') or ''}:{mid}:{cid}"


def compose(category: dict | None, merchant: dict | None, trigger: dict | None,
            customer: dict | None = None, now: str | None = None) -> dict:
    trigger = trigger or {}
    m = Merchant(merchant)
    cat = Category(category, slug=(merchant or {}).get("category_slug"))
    c = Customer(customer)
    x = Ctx(cat=cat, m=m, t=trigger, c=c, now=now)
    kind = x.kind

    customer_scoped = trigger.get("scope") == "customer" or (kind in CUSTOMER_HANDLERS and c.exists)
    if customer_scoped and c.exists:
        handler, scopes = CUSTOMER_HANDLERS.get(kind, (c_generic, ()))
        if not c.allows(*scopes):
            handler = _no_consent
        fallback = c_generic if handler is not _no_consent else _no_consent
    elif customer_scoped:
        handler, fallback = h_generic, h_generic
    else:
        handler, fallback = MERCHANT_HANDLERS.get(kind, h_generic), h_generic
    draft = _safe(handler, fallback, x)

    body = _scrub(draft.body, cat.taboo)
    send_as = SEND_AS_MERCHANT if draft.to_customer else SEND_AS_VERA
    sup = suppression_key(trigger, m.id, c.id)
    rationale = (f"[{kind or 'unknown'} | urgency {trigger.get('urgency', '-')}] {draft.why}. "
                 f"Recipient: {'customer ' + (c.addressee or c.id) if draft.to_customer else 'merchant ' + (m.first_name or m.id)}; "
                 f"one CTA ({draft.cta}).")
    return {
        "body": body,
        "cta": draft.cta,
        "send_as": send_as,
        "suppression_key": sup,
        "rationale": rationale,
        "template_name": f"vera_{kind or 'generic'}_v1",
        "template_params": [x.c.addressee if draft.to_customer else x.sal, m.name],
        "on_yes": _scrub(draft.on_yes, cat.taboo) if draft.on_yes else "",
        "recipient": "customer" if draft.to_customer else "merchant",
    }
