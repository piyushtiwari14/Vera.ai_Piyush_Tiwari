"""Print compose() output for every seed trigger: python scripts/demo.py [trigger_id ...]"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vera.composer import compose  # noqa: E402
from vera.seed import load_seed  # noqa: E402

NOW = "2026-04-26T10:00:00+05:30"


def main(ids):
    s = load_seed()
    for tid, t in s["trigger"].items():
        if ids and tid not in ids:
            continue
        m = s["merchant"].get(t["merchant_id"], {})
        out = compose(s["category"].get(m.get("category_slug")), m, t,
                      s["customer"].get(t.get("customer_id")), now=NOW)
        print(f"=== {tid}  [{out['send_as']} / {out['cta']}]")
        print(out["body"])
        if "--full" in sys.argv:
            print(json.dumps({k: v for k, v in out.items() if k != "body"}, ensure_ascii=False, indent=1))
        print()


if __name__ == "__main__":
    main([a for a in sys.argv[1:] if not a.startswith("--")])
