import copy
import re

import pytest
from fastapi.testclient import TestClient

import app as app_module
from vera.composer import compose
from vera.engine import Store, classify
from vera.seed import load_seed

NOW = "2026-04-26T10:00:00+05:30"
SEED = load_seed()


def _bundle(tid):
    t = SEED["trigger"][tid]
    m = SEED["merchant"][t["merchant_id"]]
    return SEED["category"][m["category_slug"]], m, t, SEED["customer"].get(t.get("customer_id"))


@pytest.fixture()
def client():
    # routes read the module-level `store`, so swapping it isolates each test
    app_module.store = Store(preload_seed=False)
    c = TestClient(app_module.app)
    for scope, items in SEED.items():
        for cid, payload in items.items():
            r = c.post("/v1/context", json={"scope": scope, "context_id": cid, "version": 1, "payload": payload})
            assert r.status_code == 200, r.text
    return c


# ---------------------------------------------------------------- compose

@pytest.mark.parametrize("tid", sorted(SEED["trigger"]))
def test_every_seed_trigger_composes_cleanly(tid):
    cat, m, t, cu = _bundle(tid)
    out = compose(cat, m, t, cu, now=NOW)
    body = out["body"]
    assert 60 < len(body) < 700
    assert not re.search(r"\bNone\b|\{|\}|\bnan\b|  ", body), body
    for taboo in cat["voice"]["vocab_taboo"]:
        assert taboo.split(" (")[0].lower() not in body.lower()
    assert out["suppression_key"] == t["suppression_key"]
    assert out["send_as"] in ("vera", "merchant_on_behalf")
    assert out["rationale"]
    assert "?" in body or "reply" in body.lower()   # exactly one ask, phrased as a question or reply instruction


@pytest.mark.parametrize("tid", sorted(SEED["trigger"]))
def test_compose_is_deterministic(tid):
    args = _bundle(tid)
    assert compose(*args, now=NOW) == compose(*copy.deepcopy(args), now=NOW)


def test_customer_messages_go_out_as_merchant_and_merchant_ones_as_vera():
    for tid, t in SEED["trigger"].items():
        out = compose(*_bundle(tid), now=NOW)
        expected = "merchant_on_behalf" if t["scope"] == "customer" else "vera"
        assert out["send_as"] == expected, tid


def test_no_consent_routes_to_merchant():
    cat, m, t, cu = _bundle("trg_003_recall_due_priya")
    cu = copy.deepcopy(cu)
    cu["consent"] = {"opted_in_at": None, "scope": []}
    out = compose(cat, m, t, cu, now=NOW)
    assert out["send_as"] == "vera"
    assert "haven't opted in" in out["body"]


def test_grounding_follows_injected_facts():
    cat, m, t, cu = _bundle("trg_004_perf_dip_bharat")
    t = copy.deepcopy(t)
    t["payload"]["delta_pct"] = -0.37
    t["payload"]["vs_baseline"] = 40
    body = compose(cat, m, t, cu, now=NOW)["body"]
    assert "37%" in body and "40" in body and "50%" not in body


def test_unknown_trigger_kind_still_grounded():
    cat, m, _, _ = _bundle("trg_001_research_digest_dentists")
    t = {"id": "x", "scope": "merchant", "kind": "brand_new_kind", "merchant_id": m["merchant_id"],
         "payload": {"metric": "directions", "delta_pct": -0.21}, "urgency": 2}
    out = compose(cat, m, t, None, now=NOW)
    assert "-21%" in out["body"] and "Dr. Meera" in out["body"]


def test_weekend_ipl_pivots_to_delivery_weeknight_to_dine_in():
    cat, m, t, cu = _bundle("trg_010_ipl_match_delhi")
    assert "delivery" in compose(cat, m, t, cu, now=NOW)["body"]
    t2 = copy.deepcopy(t)
    t2["payload"]["is_weeknight"] = True
    assert "dine-in" in compose(cat, m, t2, cu, now=NOW)["body"]


# ---------------------------------------------------------------- HTTP

def test_health_and_metadata(client):
    assert client.get("/v1/healthz").json()["status"] == "ok"
    md = client.get("/v1/metadata").json()
    assert md["team_name"] and md["deterministic"] is True


def test_context_versioning(client):
    base = {"scope": "merchant", "context_id": "m_x", "payload": {"merchant_id": "m_x"}}
    assert client.post("/v1/context", json={**base, "version": 3}).json()["accepted"]
    same = client.post("/v1/context", json={**base, "version": 3})
    assert same.status_code == 200 and same.json().get("noop")
    stale = client.post("/v1/context", json={**base, "version": 2})
    assert stale.status_code == 409 and stale.json()["reason"] == "stale_version"
    bad = client.post("/v1/context", json={**base, "scope": "nope", "version": 1})
    assert bad.status_code == 400


def test_tick_limits_and_suppression(client):
    ids = sorted(SEED["trigger"])
    r = client.post("/v1/tick", json={"now": NOW, "available_triggers": ids}).json()
    acts = r["actions"]
    assert 0 < len(acts) <= 20
    merchant_lanes = [a["merchant_id"] for a in acts if a["send_as"] == "vera"]
    assert len(merchant_lanes) == len(set(merchant_lanes))
    # highest urgency (supply recall, u=5) goes first
    assert acts[0]["trigger_id"] == "trg_018_supply_atorvastatin_recall"
    # same triggers again: everything already sent is suppressed
    again = client.post("/v1/tick", json={"now": NOW, "available_triggers": [a["trigger_id"] for a in acts]}).json()
    assert again["actions"] == []


def test_tick_deterministic_across_fresh_stores(client):
    ids = sorted(SEED["trigger"])
    a = client.post("/v1/tick", json={"now": NOW, "available_triggers": ids}).json()
    client.post("/v1/teardown")
    for scope, items in SEED.items():
        for cid, payload in items.items():
            client.post("/v1/context", json={"scope": scope, "context_id": cid, "version": 1, "payload": payload})
    b = client.post("/v1/tick", json={"now": NOW, "available_triggers": ids}).json()
    assert a == b


def _start(client, tid):
    acts = client.post("/v1/tick", json={"now": NOW, "available_triggers": [tid]}).json()["actions"]
    assert acts, tid
    return acts[0]["conversation_id"]


def test_reply_accept_delivers_then_closes(client):
    cid = _start(client, "trg_001_research_digest_dentists")
    r1 = client.post("/v1/reply", json={"conversation_id": cid, "from_role": "merchant", "message": "Yes please"}).json()
    assert r1["action"] == "send" and "124" in r1["body"]
    r2 = client.post("/v1/reply", json={"conversation_id": cid, "from_role": "merchant", "message": "ok send"}).json()
    assert r2["action"] == "end"


def test_reply_slot_pick_books(client):
    cid = _start(client, "trg_003_recall_due_priya")
    r = client.post("/v1/reply", json={"conversation_id": cid, "from_role": "customer", "message": "2"}).json()
    assert r["action"] == "end" and "Thu 6 Nov, 5pm" in r["body"]


def test_reply_auto_reply_waits_then_ends(client):
    cid = _start(client, "trg_004_perf_dip_bharat")
    msg = "Thank you for contacting Bharat Dental Care. We will respond shortly."
    assert client.post("/v1/reply", json={"conversation_id": cid, "message": msg}).json()["action"] == "wait"
    assert client.post("/v1/reply", json={"conversation_id": cid, "message": msg}).json()["action"] == "end"


def test_reply_opt_out_suppresses_future(client):
    cid = _start(client, "trg_009_winback_glamour")
    r = client.post("/v1/reply", json={"conversation_id": cid, "message": "STOP messaging me"}).json()
    assert r["action"] == "end"
    later = client.post("/v1/tick", json={"now": NOW, "available_triggers": ["trg_025_dormancy_glamour"]}).json()
    assert later["actions"] == [] and later["skipped"][0]["reason"] == "recipient_opted_out"


@pytest.mark.parametrize("msg,intent", [
    ("haan bhej do", "accept"), ("Yes good idea, what would it look like", "accept"),
    ("abhi busy hu, baad mein", "later"), ("not interested", "decline"), ("bahut mehenga hai", "price_objection"),
    ("how much will this cost?", "question"), ("unsubscribe", "opt_out"),
    ("This is an automated message", "auto_reply"), ("hmm", "unclear"),
])
def test_classify(msg, intent):
    assert classify(msg) == intent


def test_later_waits_with_right_duration(client):
    cid = _start(client, "trg_012_milestone_mylari")
    r = client.post("/v1/reply", json={"conversation_id": cid, "message": "kal baat karte hain"}).json()
    assert r["action"] == "wait" and r["wait_seconds"] == 86400


def test_injected_digest_item_matches_new_cohort():
    cat, m, t, _ = _bundle("trg_001_research_digest_dentists")
    cat, m, t = copy.deepcopy(cat), copy.deepcopy(m), copy.deepcopy(t)
    cat["digest"].append({"id": "d_new", "kind": "research", "title": "Night guards cut bruxism wear",
                          "source": "IJDR Nov 2026", "trial_n": 640, "patient_segment": "bruxism_patients",
                          "summary": "Custom night guards reduced enamel wear by 41% over 12 months.",
                          "actionable": "Offer night-guard fitting to bruxism patients"})
    m["customer_aggregate"]["bruxism_patient_count"] = 37
    t["payload"]["top_item_id"] = "d_new"
    body = compose(cat, m, t, None, now=NOW)["body"]
    assert "IJDR Nov 2026" in body and "640" in body and "41%" in body and "37 bruxism patients" in body


@pytest.mark.parametrize("msg", ["Can someone call me?", "mujhe kisi se baat karni hai", "talk to a human please"])
def test_handoff(client, msg):
    cid = _start(client, "trg_005_renewal_due_bharat")
    r = client.post("/v1/reply", json={"conversation_id": cid, "message": msg}).json()
    assert r["action"] == "end" and r["handoff"] is True


def test_customer_question_hands_off_to_merchant(client):
    cid = _start(client, "trg_015_winback_rashmi")
    r = client.post("/v1/reply", json={"conversation_id": cid, "from_role": "customer",
                                       "message": "Is the trainer still Ravi?"}).json()
    assert r["action"] == "send" and r["handoff_to"] == "merchant" and "PowerHouse Fitness" in r["body"]


def test_trigger_without_suppression_key_is_sent_once(client):
    t = {"id": "t_nokey", "scope": "merchant", "kind": "perf_dip", "merchant_id": "m_002_bharat_dentist_mumbai",
         "payload": {"metric": "views", "delta_pct": -0.2}, "urgency": 3}
    client.post("/v1/context", json={"scope": "trigger", "context_id": "t_nokey", "version": 1, "payload": t})
    first = client.post("/v1/tick", json={"available_triggers": ["t_nokey"]}).json()
    second = client.post("/v1/tick", json={"available_triggers": ["t_nokey"]}).json()
    assert len(first["actions"]) == 1 and second["actions"] == []


def test_malformed_inputs_never_500(client):
    assert client.post("/v1/context", json={"scope": "merchant", "context_id": "m", "version": 1,
                                            "payload": [1, 2]}).status_code == 200
    assert client.post("/v1/tick", json={"available_triggers": [123, None, {"foo": 1}, "nope"]}).json()["actions"] == []
    bad = {"id": "tb", "kind": "perf_dip", "merchant_id": "m_002_bharat_dentist_mumbai", "payload": "bad"}
    assert client.post("/v1/tick", json={"available_triggers": [bad]}).status_code == 200
    assert client.post("/v1/reply", json={"conversation_id": "zz", "message": None}).json()["action"] in ("send", "wait")
