"""Stress test: every trigger kind x every merchant x sparse/missing data. Prints failures (expect 0)."""
import copy, sys, re, collections
sys.path.insert(0, __import__('os').path.join(__import__('os').path.dirname(__file__), '..'))
from vera.composer import compose
from vera.seed import load_seed
s=load_seed(); fails=collections.Counter(); ex={}; n=0
BAD=re.compile(r"\bNone\b|\{|\}|\(\)|''|\b(in|on|by|for|at|of|is|to) [.,?]|  |\s[.,]")
for t in s["trigger"].values():
    for m in s["merchant"].values():
        for c in (None, list(s["customer"].values())[0], {}):
            for variant in ("full","empty_payload","empty_merchant","no_category"):
                tt=copy.deepcopy(t); mm=copy.deepcopy(m); cat=s["category"].get(m["category_slug"])
                if variant=="empty_payload": tt["payload"]={}
                if variant=="empty_merchant": mm={"merchant_id":m["merchant_id"]}
                if variant=="no_category": cat=None
                n+=1
                try:
                    out=compose(cat, mm, tt, c, now=None)
                    b=out["body"]
                    if BAD.search(b): raise AssertionError(b)
                except Exception as e:
                    k=(t["kind"],variant); fails[k]+=1; ex.setdefault(k, repr(e)[:260])
print("cases",n,"fails",sum(fails.values()))
for k,v in fails.most_common(40): print(v,k,ex[k])
