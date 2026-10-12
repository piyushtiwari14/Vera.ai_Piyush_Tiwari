# Vera: deterministic merchant message engine

My entry for the magicpin **Vera AI Challenge**. Given `(category, merchant, trigger, customer?)` it returns the next WhatsApp message, its CTA, the send-as identity, the suppression key and a rationale. The same input always produces byte-identical output.

## Approach

**No LLM at runtime.** The rubric scores *decisions* (signal choice, specificity, category and merchant fit), and the judge needs determinism plus a 30 s budget. A typed decision engine gets all three for free. Prompting a model would risk invented numbers and non-reproducible output.

For each trigger, a handler written for that `kind` does four things:

1. **Picks the one "why now" signal.** It reads the trigger payload, then cross-checks merchant state and category knowledge (digest, peer stats, seasonal beats, trend signals).
2. **Backs it with a number from the merchant's own data.** For example *"You have 124 high-risk adults on file"*, or *"similar clinics average 12 calls/month; you're at 4"*.
3. **Ends with exactly one low-effort CTA**: yes/no, or *reply 1 or 2*.
4. **Pre-computes `on_yes`, the actual deliverable.** That can be a draft WhatsApp, an audit checklist, a booking confirmation or a corporate-thali price sheet. When the recipient says "haan", the reply turn **delivers** instead of asking again.

Some of the decisions it makes:

| Trigger | Naive bot | This engine |
|---|---|---|
| IPL match on a **Saturday** | "Push your match offer!" | Category digest says weekend matches shift orders home (covers -12%), and the merchant does 180 delivery vs 95 dine-in orders. It pivots to a delivery combo and flags that their Tue-Thu BOGO won't apply tonight. |
| Competitor opens with ₹199 cleaning | "Match the price?" | Avoids a price war. It leads with the merchant's top review theme (*"explains everything patiently"*, 5 mentions). |
| Seasonal views dip of -30% (gym, Apr-Jun) | "Run ads!" | Says the dip is expected, then moves to retention: 10% churn vs an 8% peer average is about 24 members a month. |
| Chronic refill for a senior (customer, `hi`) | English reminder | Hindi, addressed to "Sharma ji" via the son's WhatsApp. Applies the senior-citizen 15% offer and promises to check the atorvastatin batch against this month's recall. |
| Customer without consent for that scope | Sends anyway | Doesn't message the customer. Routes a note to the merchant instead. |
| Unknown future trigger `kind` | Generic hello | Generic handler that is still grounded: it quotes the payload's numbers and the merchant's biggest fixable gap. |

Category voice comes from the category JSON. Dentists get "Dr. {name}" and clinical framing, salons get warm, practical framing, and pharmacies get precise framing. Taboo vocab (`guaranteed`, `miracle`, …) is scrubbed from every outgoing body.

## API

| Endpoint | Behaviour |
|---|---|
| `GET /v1/healthz` | Status, uptime, number of contexts loaded |
| `GET /v1/metadata` | Team and approach |
| `POST /v1/context` | Stores data by `(scope, context_id)`. Re-posting the same version is a no-op (`200`, `noop: true`). A higher version replaces the stored one. A lower version is rejected with `409 stale_version`. |
| `POST /v1/tick` | `{now, available_triggers: [ids or objects]}`. Highest urgency goes first, then the soonest expiry. Sends at most one message per merchant and one per customer per tick, and at most 20 actions. Suppression keys are never re-sent, opted-out recipients are skipped, and every skip comes back with a reason. |
| `POST /v1/reply` | Deterministic intent classifier (English and Hinglish) that drives a state machine. **accept** → send the pre-computed deliverable, then confirm and `end`. **slot pick "2"** → booked, `end`. **auto-reply** → `wait` 4h, and a second one → `end`, so it never talks to a bot. **later / "kal"** → `wait` for the right duration. **"mehenga"** → remove the cost barrier, one yes/no. **"no"** → graceful `end`. **STOP** → `end` and suppress all future sends. **"call me" / "talk to a human"** → hand off to the account manager with `handoff: true`. **A customer question** → hand off to the merchant instead of guessing. **unclear** → one clarification, then back off. |
| `POST /v1/compose` | Direct access to `compose()` for debugging |
| `POST /v1/teardown` | Clears all state |

Pushed context always wins. The seed dataset in `data/` is only a fallback for ids the judge hasn't pushed (set `VERA_PRELOAD_SEED=0` to turn this off).

## Run

```bash
pip install -r requirements.txt
uvicorn app:app --port 8000
python scripts/demo.py            # print the message for all 25 seed triggers
pip install -r requirements-dev.txt && pytest -q
python scripts/stress.py          # 3,000 sparse/mismatched-data cases, expect 0 failures
```

**Deploy:** `render.yaml` and `Procfile` are included. Point Render or Railway at this repo and submit the base URL.

## Layout

```
app.py              HTTP routes
vera/composer.py    compose(): one handler per trigger kind + customer-facing handlers + consent gate
vera/facts.py       safe, derived facts over category / merchant / customer JSON
vera/engine.py      versioned store, tick planner, reply classifier + state machine
vera/fmt.py         ₹ Indian-grouping, %, date and text helpers
data/               challenge seed dataset
tests/              80 tests: every seed trigger, injected digest items, handoffs, determinism, versioning, tick limits, reply flows
```

## Tradeoffs

- Templates cap the stylistic range compared with an LLM. In exchange there are zero hallucinated numbers, full reproducibility and about 1 ms per message.
- **Robustness over coverage gaps:** every handler degrades to a grounded generic message when a field is missing, and a final guard never ships a half-filled template (`None`, empty quotes, dangling prepositions). Malformed requests get a safe JSON answer, never a 500.
- State is in memory. Restarting the server loses conversations, which is fine for a single judge run.
