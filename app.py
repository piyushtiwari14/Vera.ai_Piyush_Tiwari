"""Vera message engine: HTTP surface for the magicpin AI Challenge judge harness."""

from __future__ import annotations

import time

from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse

from vera import __version__
from vera.composer import compose
from vera.engine import Store, bundle, reply, tick

app = FastAPI(title="Vera: deterministic merchant message engine", version=__version__)
store = Store()
START = time.time()


@app.exception_handler(Exception)
async def _never_crash(request: Request, exc: Exception):
    """Malformed judge input gets a well-formed, safe answer instead of a bare 500."""
    path = request.url.path
    if path.endswith("/tick"):
        return JSONResponse({"actions": [], "skipped": [], "error": type(exc).__name__})
    if path.endswith("/reply"):
        return JSONResponse({"action": "wait", "wait_seconds": 3600, "rationale": "could not parse reply safely"})
    return JSONResponse({"accepted": False, "reason": "bad_request", "error": type(exc).__name__}, status_code=400)


@app.get("/")
def root():
    return {"service": "vera-message-engine", "version": __version__,
            "endpoints": ["/v1/healthz", "/v1/metadata", "/v1/context", "/v1/tick", "/v1/reply", "/v1/compose"]}


@app.get("/v1/healthz")
def healthz():
    return {"status": "ok", "uptime_seconds": int(time.time() - START),
            "version": __version__, "contexts_loaded": store.counts(),
            "seed_fallback_loaded": {k: len(v) for k, v in store.seed.items()}}


@app.get("/v1/metadata")
def metadata():
    return {
        "team_name": "Piyush Tiwari",
        "team_members": ["Piyush Tiwari"],
        "contact_email": "piyushtiwari1404@gmail.com",
        "version": __version__,
        "model": "none: deterministic rule-and-template engine (no LLM at runtime)",
        "approach": ("Per-trigger-kind decision handlers pick the single strongest signal "
                     "(trigger + merchant metrics + category digest/peer stats + customer consent), "
                     "write one grounded message with one CTA, and pre-compute the deliverable "
                     "sent when the recipient says yes."),
        "deterministic": True,
    }


@app.post("/v1/context")
def context(payload: dict = Body(...)):
    if not isinstance(payload, dict):
        return JSONResponse({"accepted": False, "reason": "bad_request"}, status_code=400)
    try:
        version = int(payload.get("version", 1))
    except (TypeError, ValueError):
        return JSONResponse({"accepted": False, "reason": "invalid_version"}, status_code=400)
    status, res = store.put(payload.get("scope"), payload.get("context_id"), version, payload.get("payload") or {})
    return JSONResponse(res, status_code=status)


@app.post("/v1/tick")
def tick_ep(payload: dict = Body(default={})):
    return tick(store, payload)


@app.post("/v1/reply")
def reply_ep(payload: dict = Body(...)):
    return reply(store, payload)


@app.post("/v1/compose")
def compose_ep(payload: dict = Body(...)):
    """Direct access to compose(). Accepts inline objects or ids of stored contexts."""
    trig = payload.get("trigger")
    if isinstance(trig, str):
        trig = store.get("trigger", trig)
    trig = trig or {}
    cat, mer, cus = bundle(store, trig) if trig else (None, None, None)
    def pick(key, fallback, scope):
        v = payload.get(key)
        if isinstance(v, str):
            return store.get(scope, v)
        return v if v is not None else fallback
    return compose(pick("category", cat, "category"), pick("merchant", mer, "merchant"), trig,
                   pick("customer", cus, "customer"), now=payload.get("now"))


@app.post("/v1/teardown")
def teardown():
    store.reset()
    return {"success": True}
