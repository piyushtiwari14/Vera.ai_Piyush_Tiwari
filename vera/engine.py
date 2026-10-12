"""Stateful layer: versioned context store, tick planner and reply engine."""

from __future__ import annotations

import os
import re
import threading
from datetime import datetime, timezone
from typing import Any

from .composer import CTA_CHOICE, CTA_SLOT, compose, suppression_key
from .facts import Category, Customer, Merchant
from .fmt import humanize, parse_dt
from .seed import load_seed

SCOPES = ("category", "merchant", "customer", "trigger")
MAX_ACTIONS_PER_TICK = 20


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Store:
    """In-memory, thread-safe. Pushed context always wins over seed fallback."""

    def __init__(self, preload_seed: bool | None = None):
        self.lock = threading.RLock()
        if preload_seed is None:
            preload_seed = os.getenv("VERA_PRELOAD_SEED", "1") != "0"
        self.preload_seed = preload_seed
        self.reset()

    def reset(self) -> None:
        with self.lock:
            self.ctx: dict[str, dict[str, dict]] = {s: {} for s in SCOPES}
            self.versions: dict[tuple[str, str], int] = {}
            self.seed = load_seed() if self.preload_seed else {s: {} for s in SCOPES}
            self.convs: dict[str, dict] = {}
            self.sent_keys: set[str] = set()
            self.opted_out: set[str] = set()   # merchant ids / customer ids that said stop
            self.ack_seq = 0

    # ------------------------------------------------------------ context
    def put(self, scope: str, context_id: str, version: int, payload: dict) -> tuple[int, dict]:
        with self.lock:
            if scope not in SCOPES:
                return 400, {"accepted": False, "reason": "invalid_scope", "valid_scopes": list(SCOPES)}
            if not context_id:
                return 400, {"accepted": False, "reason": "missing_context_id"}
            key = (scope, context_id)
            cur = self.versions.get(key)
            if cur is not None and version < cur:
                return 409, {"accepted": False, "reason": "stale_version", "current_version": cur}
            if not isinstance(payload, dict):
                payload = {}
            self.ack_seq += 1
            ack = {"accepted": True, "ack_id": f"ack_{scope}_{context_id}_v{version}",
                   "stored_at": _utcnow(), "version": version}
            if cur is not None and version == cur:
                ack["noop"] = True  # idempotent re-post
                return 200, ack
            self.ctx[scope][context_id] = payload if isinstance(payload, dict) else {}
            self.versions[key] = version
            # secondary index: a merchant/customer/trigger payload may carry its own id
            id_field = {"merchant": "merchant_id", "customer": "customer_id", "trigger": "id",
                        "category": "slug"}[scope]
            alt = (payload or {}).get(id_field)
            if alt and alt != context_id:
                self.ctx[scope][alt] = self.ctx[scope][context_id]
            return 200, ack

    def get(self, scope: str, cid: str | None) -> dict | None:
        if not cid:
            return None
        with self.lock:
            return self.ctx[scope].get(cid) or self.seed.get(scope, {}).get(cid)

    def counts(self) -> dict:
        with self.lock:
            return {s: len({id(v) for v in self.ctx[s].values()}) for s in SCOPES}


# ======================================================================
# tick
# ======================================================================

def _resolve_trigger(store: Store, ref: Any) -> dict | None:
    if isinstance(ref, dict):
        if "kind" in ref:
            return ref
        return store.get("trigger", ref.get("id") or ref.get("trigger_id") or ref.get("context_id"))
    return store.get("trigger", str(ref))


def bundle(store: Store, trigger: dict) -> tuple[dict | None, dict | None, dict | None]:
    merchant = store.get("merchant", trigger.get("merchant_id"))
    category = None
    if merchant:
        category = store.get("category", merchant.get("category_slug"))
    if not category:
        category = store.get("category", (trigger.get("payload") or {}).get("category"))
    customer = store.get("customer", trigger.get("customer_id"))
    return category, merchant, customer


def tick(store: Store, payload: dict) -> dict:
    now = payload.get("now") or payload.get("current_time") or payload.get("ts")
    refs = payload.get("available_triggers")
    if refs is None:
        refs = payload.get("triggers") or payload.get("trigger_ids") or []
    if not isinstance(refs, list):
        refs = [refs]
    triggers = [t for t in (_resolve_trigger(store, r) for r in refs if r is not None) if isinstance(t, dict)]

    # Highest urgency first; then soonest expiry; then id for a stable order.
    def rank(t: dict):
        exp = parse_dt(t.get("expires_at"))
        return (-(t.get("urgency") or 0), exp.timestamp() if exp else 9e12, str(t.get("id")))

    actions, skipped = [], []
    used_merchant, used_customer = set(), set()
    with store.lock:
        for t in sorted(triggers, key=rank):
            tid = t.get("id") or t.get("trigger_id") or ""
            category, merchant, customer = bundle(store, t)
            if not merchant:
                skipped.append({"trigger_id": tid, "reason": "merchant_context_missing"})
                continue
            mid = merchant.get("merchant_id") or t.get("merchant_id")
            cid = t.get("customer_id")
            sup = suppression_key(t, mid, (customer or {}).get("customer_id") or "")
            if sup in store.sent_keys:
                skipped.append({"trigger_id": tid, "reason": "suppressed_already_sent"})
                continue
            if mid in store.opted_out or (cid and cid in store.opted_out):
                skipped.append({"trigger_id": tid, "reason": "recipient_opted_out"})
                continue
            try:
                out = compose(category, merchant, t, customer, now=now)
            except Exception:  # noqa: BLE001 - one bad trigger must not sink the tick
                skipped.append({"trigger_id": tid, "reason": "compose_error"})
                continue
            lane = (out["recipient"], cid if out["recipient"] == "customer" else mid)
            if out["recipient"] == "customer" and cid in used_customer:
                skipped.append({"trigger_id": tid, "reason": "one_message_per_customer_per_tick"})
                continue
            if out["recipient"] == "merchant" and mid in used_merchant:
                skipped.append({"trigger_id": tid, "reason": "one_message_per_merchant_per_tick"})
                continue
            if len(actions) >= MAX_ACTIONS_PER_TICK:
                skipped.append({"trigger_id": tid, "reason": "tick_action_cap"})
                continue
            (used_customer if lane[0] == "customer" else used_merchant).add(lane[1])
            conv_id = f"conv_{tid}"
            store.convs[conv_id] = {
                "conversation_id": conv_id, "trigger": t, "merchant_id": mid, "customer_id": cid,
                "recipient": out["recipient"], "cta": out["cta"], "on_yes": out["on_yes"],
                "body": out["body"], "stage": "sent", "auto_replies": 0, "unclear": 0, "turns": [],
            }
            store.sent_keys.add(out["suppression_key"])
            actions.append({
                "conversation_id": conv_id,
                "merchant_id": mid,
                "customer_id": cid,
                "send_as": out["send_as"],
                "trigger_id": tid,
                "template_name": out["template_name"],
                "template_params": out["template_params"],
                "body": out["body"],
                "cta": out["cta"],
                "suppression_key": out["suppression_key"],
                "rationale": out["rationale"],
            })
    return {"actions": actions, "skipped": skipped}


# ======================================================================
# reply
# ======================================================================

def _has(text: str, words: list[str]) -> bool:
    return any(re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", text) for w in words)


OPT_OUT = ["stop", "unsubscribe", "don't message", "dont message", "do not message", "do not contact",
           "remove me", "block me", "block this", "spam", "band karo", "mat bhejo", "message mat", "never message",
           "leave me alone"]
HANDOFF = ["talk to a human", "talk to human", "real person", "speak to someone", "call me", "call karo",
           "call kar do", "phone karo", "account manager", "talk to your team", "customer care", "representative",
           "baat karni hai", "baat karwao", "agent se"]
AUTO = ["thank you for contacting", "thanks for contacting", "thank you for reaching out", "thanks for reaching",
        "will get back to you", "we will respond", "will respond shortly", "get back shortly",
        "automated", "auto-reply", "auto reply", "autoreply", "out of office", "business hours",
        "our team will", "currently unavailable", "this is an automated", "we are closed",
        "aapka message mil gaya", "hum jaldi"]
LATER = ["later", "busy", "baad mein", "baad me", "not now", "abhi nahi", "abhi nahin", "tomorrow", "kal",
         "next week", "in a meeting", "call later", "remind me", "few days"]
NO = ["no", "nope", "not interested", "nahi", "nahin", "na", "don't want", "dont want", "no thanks",
      "not needed", "zarurat nahi", "nahi chahiye", "pass"]
PRICE = ["expensive", "costly", "too much", "mehenga", "mehnga", "budget", "afford", "too high", "cheaper"]
YES = ["yes", "yeah", "yep", "yup", "haan", "han", "ha", "ji", "ok", "okay", "okk", "sure", "go ahead", "send",
       "do it", "karo", "kar do", "kardo", "chalega", "please", "done", "confirm", "book", "let's do",
       "lets do", "proceed", "approved", "great", "perfect", "👍", "✅", "theek hai", "thik hai", "bhej do",
       "start", "post", "publish", "join"]
QUESTION_START = ("how", "what", "why", "when", "where", "which", "who", "kya", "kaise", "kitna", "kitne",
                  "kab", "kyun", "can you", "could you", "is it", "does", "do you")


def classify(msg: str, cta: str | None = None) -> str:
    t = (msg or "").strip().lower()
    if not t:
        return "empty"
    if _has(t, OPT_OUT):
        return "opt_out"
    if _has(t, AUTO):
        return "auto_reply"
    if _has(t, HANDOFF):
        return "handoff"
    if cta in (CTA_SLOT, CTA_CHOICE) and re.fullmatch(r"(option\s*)?[1-9]\W*", t):
        return "pick"
    if _has(t, PRICE):
        return "price_objection"
    if _has(t, LATER):
        return "later"
    if _has(t, NO) and not _has(t, ["yes", "haan", "ok", "okay", "sure"]):
        return "decline"
    if _has(t, YES):
        return "accept"
    if "?" in t or t.startswith(QUESTION_START):
        return "question"
    return "unclear"


def _wait_seconds(t: str) -> int:
    t = t.lower()
    if _has(t, ["next week"]):
        return 7 * 86400
    if _has(t, ["tomorrow", "kal"]):
        return 86400
    if _has(t, ["few days"]):
        return 3 * 86400
    if _has(t, ["evening", "shaam"]):
        return 6 * 3600
    return 3 * 3600


def reply(store: Store, payload: dict) -> dict:
    msg = payload.get("message") or payload.get("body") or payload.get("text") or payload.get("reply") or ""
    if isinstance(msg, dict):
        msg = msg.get("body") or msg.get("text") or ""
    conv_id = payload.get("conversation_id") or ""
    with store.lock:
        conv = store.convs.get(conv_id)
        if conv is None:
            # Unknown conversation (e.g. merchant-initiated): build a minimal one.
            mid = payload.get("merchant_id")
            conv = {"conversation_id": conv_id, "trigger": {}, "merchant_id": mid,
                    "customer_id": payload.get("customer_id"), "recipient":
                    "customer" if payload.get("from_role") == "customer" else "merchant",
                    "cta": None, "on_yes": "", "body": "", "stage": "inbound",
                    "auto_replies": 0, "unclear": 0, "turns": []}
            if conv_id:
                store.convs[conv_id] = conv
        conv["turns"].append({"from": payload.get("from_role", conv["recipient"]), "body": msg})
        intent = classify(msg, conv.get("cta"))
        res = _respond(store, conv, intent, msg)
        res["intent"] = intent
        if conv_id:
            res["conversation_id"] = conv_id
        if res.get("body"):
            conv["turns"].append({"from": "vera", "body": res["body"]})
        return res


def _respond(store: Store, conv: dict, intent: str, msg: str) -> dict:
    t = conv.get("trigger") or {}
    category, merchant, customer = bundle(store, t) if t else (None, store.get("merchant", conv.get("merchant_id")), None)
    m = Merchant(merchant)
    cat = Category(category, slug=(merchant or {}).get("category_slug"))
    cust = Customer(customer)
    to_customer = conv.get("recipient") == "customer"
    hinglish = cust.hinglish if to_customer else False
    who = (cust.addressee if to_customer else m.salutation(cat)) or ""
    kind_h = humanize(t.get("kind")) or "this"

    if intent != "auto_reply":
        conv["auto_replies"] = 0

    if intent == "opt_out":
        store.opted_out.add(conv.get("customer_id") if to_customer else conv.get("merchant_id"))
        conv["stage"] = "closed"
        body = ("Theek hai, ab aapko aise messages nahi aayenge. 🙏" if hinglish
                else f"Understood{', ' + who if who else ''}. I won't message you about this again.")
        return {"action": "end", "body": body, "rationale": "explicit opt-out: stop and suppress future sends"}

    if intent == "handoff":
        conv["stage"] = "handed_off"
        if to_customer:
            body = (f"Zaroor! {m.name} ki team aapko jaldi call karegi. 🙏" if hinglish
                    else f"Of course! The {m.name} team will call you shortly.")
        else:
            body = (f"Sure{', ' + who if who else ''}. I've asked your magicpin account manager to call you today "
                    f"and shared this thread so you don't have to repeat yourself.")
        return {"action": "end", "body": body, "handoff": True,
                "handoff_to": "merchant" if to_customer else "magicpin_account_manager",
                "rationale": "explicit request for a human: hand off with context, stop automated follow-ups"}

    if intent == "auto_reply":
        conv["auto_replies"] += 1
        if conv["auto_replies"] >= 2:
            conv["stage"] = "closed"
            return {"action": "end", "rationale": "repeated auto-reply: no human on the line; stop to avoid a bot loop"}
        return {"action": "wait", "wait_seconds": 4 * 3600,
                "rationale": "auto-reply detected: wait for a human instead of replying to a bot"}

    if intent == "later":
        secs = _wait_seconds(msg)
        conv["stage"] = "snoozed"
        return {"action": "wait", "wait_seconds": secs,
                "rationale": f"recipient asked for later; snooze {secs // 3600}h, no nudge in between"}

    if intent == "decline":
        conv["stage"] = "closed"
        body = ("Koi baat nahi! Jab bhi zarurat ho, bas message kar dijiye. 🙏" if hinglish
                else f"No problem{', ' + who if who else ''}. I'll drop this one. If anything changes, just reply here.")
        return {"action": "end", "body": body, "rationale": "clear decline: close gracefully, no second push"}

    if intent == "price_objection":
        conv["stage"] = "objection"
        if to_customer:
            body = ("Bilkul samajh sakte hain. Pehla step free hai, koi commitment nahi. Slot rakh dein? Reply HAAN."
                    if hinglish else "Totally fair. The first step is free and there's no commitment. Shall we hold a slot? Reply YES.")
        else:
            body = (f"Fair point{', ' + who if who else ''}. No need to decide on spend today: let's do the free part first "
                    f"and you judge it on results in 7 days. Shall I start?")
        return {"action": "send", "body": body, "cta": "binary_yes_no",
                "rationale": "price objection: remove the cost barrier, keep one yes/no"}

    if intent in ("accept", "pick"):
        label = ""
        if intent == "pick":
            n = int(re.search(r"\d", msg).group())
            slots = (t.get("payload") or {}).get("available_slots") or (t.get("payload") or {}).get("next_session_options") or []
            if 0 < n <= len(slots):
                label = slots[n - 1].get("label", "")
        stage = conv.get("stage")
        if stage in ("delivered",):
            conv["stage"] = "closed"
            body = (f"Done ✅ {('Booked for ' + label + '. ') if label else ''}"
                    + ("Dhanyavaad! 🙏" if hinglish else "I'll share results here once there's data."))
            return {"action": "end", "body": body, "rationale": "deliverable approved: confirm and close the loop"}
        conv["stage"] = "delivered"
        body = conv.get("on_yes") or "Great, on it. I'll share the draft here shortly."
        if label:
            body = (f"Booked ✅ {label}. Hum 2 ghante pehle reminder bhej denge. 🙏" if hinglish
                    else f"Booked ✅ {label}. We'll send a reminder 2 hours before.")
            conv["stage"] = "closed"
            return {"action": "end", "body": body, "rationale": "slot picked: confirm booking and close"}
        if intent == "pick" and conv.get("cta") == CTA_CHOICE:
            body = (f"Got it, option {msg.strip()[0]}. Drafting a Google post around it now; "
                    f"you'll get it here for a one-tap OK.")
        return {"action": "send", "body": body, "cta": "binary_yes_no",
                "rationale": f"accepted {kind_h}: deliver the concrete next step now instead of asking again"}

    if intent == "question":
        conv["stage"] = "answering"
        deliver = conv.get("on_yes") or ""
        lead = deliver.split("\n")[0][:220] if deliver else ""
        if to_customer:
            conv["stage"] = "handed_off"
            body = ("Accha sawaal! Hum {name} team se confirm karke abhi batate hain. 🙏" if hinglish
                    else "Good question! Let me check with the {name} team and get right back to you.").format(name=m.name)
            return {"action": "send", "body": body, "handoff": True, "handoff_to": "merchant",
                    "rationale": "customer question needs the merchant's answer: hand off instead of guessing (no fake claims)"}
        else:
            body = (f"Good question. Here's exactly what happens if you say yes: {lead} "
                    f"Nothing goes out without your OK. Go ahead?") if lead else \
                (f"Good question{', ' + who if who else ''}. I'll keep it to one step on your side. Shall I go ahead?")
        return {"action": "send", "body": body.strip(), "cta": "binary_yes_no",
                "rationale": "question: answer with the concrete deliverable, keep one CTA"}

    # unclear / empty
    conv["unclear"] += 1
    if conv["unclear"] >= 2:
        return {"action": "wait", "wait_seconds": 24 * 3600,
                "rationale": "still unclear after one clarification: back off for a day"}
    body = ("Bas confirm kar dijiye: aage badhein? HAAN ya NA reply karein." if hinglish
            else "Just to confirm: should I go ahead? Reply YES or NO.")
    return {"action": "send", "body": body, "cta": "binary_yes_no",
            "rationale": "unclear reply: one short yes/no clarification"}
