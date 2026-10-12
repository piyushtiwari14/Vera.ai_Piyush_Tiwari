"""Load the challenge seed dataset (data/) into plain dicts keyed by id."""

from __future__ import annotations

import json
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"


def load_seed(data_dir: Path | str = DATA) -> dict:
    d = Path(data_dir)
    cats = {}
    for f in sorted((d / "categories").glob("*.json")):
        raw = json.loads(f.read_text(encoding="utf-8"))
        cats[raw.get("slug", f.stem)] = raw
    merchants = {m["merchant_id"]: m for m in json.loads((d / "merchants_seed.json").read_text(encoding="utf-8"))["merchants"]}
    customers = {c["customer_id"]: c for c in json.loads((d / "customers_seed.json").read_text(encoding="utf-8"))["customers"]}
    triggers = {t["id"]: t for t in json.loads((d / "triggers_seed.json").read_text(encoding="utf-8"))["triggers"]}
    return {"category": cats, "merchant": merchants, "customer": customers, "trigger": triggers}
