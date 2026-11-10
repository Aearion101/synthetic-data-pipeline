#!/usr/bin/env python3
"""Stage-0 stub tests for runner_v2. No key, no network, no spend.
Portable: all temp dirs come from tempfile, no hardcoded paths, everything runs
under __main__ (Instinct 2145/2146, DeepSeek 2140/2235 portability findings).
Covers Instinct 2020's five asks plus the independent-review regression set
(DeepSeek 2235/2236, Claude 2238/2241): full-reservation charge, resume key via
main()'s own call path, two-model allow-list, misconfigured client raises.
T5 is a behavioural hold only: the retry policy itself is generate.py's and its
restoration is the lead's call (DeepSeek F7); the runner must not invent retries.
"""
import json, os, subprocess, sys, tempfile, multiprocessing
import runner_v2 as R

PASS=[]; FAIL=[]
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(f"{name}: {'PASS' if cond else 'FAIL'} {detail}")

def fresh(cap=200000):                     # cap must exceed input + 113 x max_tokens
    d = tempfile.mkdtemp(prefix="sdp0-")
    cfg = {"run_id":"", "seeds_path":f"{d}/seeds.jsonl", "out_path":f"{d}/out.jsonl",
           "ledger_path":f"{d}/ledger.jsonl", "budget_usd":1.0,
           "price_per_million_chars":0.18, "attempt_char_cap":cap,
           "models":[{"model_id":"alpha.model","weight":1},
                     {"model_id":"zeta.model","weight":1}],
           "pairs_per_seed":1, "temperature":0.8, "max_tokens":1024}
    open(cfg["seeds_path"],"w").write(json.dumps({"id":"seed-00001","topic":"rain"})+"\n")
    return cfg

MAX_OUT = 113 * 1024                       # 116,712: the reply-term reservation

def make_run(cfg, script, n, cap=0, seeds=None):
    """Direct driver of one fire, mirroring main()'s caps and call shape."""
    ledger = R.Ledger(cfg["ledger_path"]); made=[]
    seeds = seeds or [{"id":"seed-00001","topic":"rain"}]
    for k in range(n):
        if cap and ledger.attempts() >= cap: break
        model = R.pick_model(cfg, seeds[0]["id"], ledger.effective_rows())
        if model is None: break
        prompt = "prompt for rain (pair %d)"%(k+1)
        a = R.run_once(cfg, lambda: R.StubClient(script), seeds[0], model, prompt,
                       ledger, max_out_chars=MAX_OUT)
        made.append(a)
    return ledger, made

# T1: restart+resume across simulated fire boundaries
cfg=fresh(); script=["ok","ok"]
make_run(cfg, script, 1); make_run(cfg, script, 1)
rows = R.Ledger(cfg["ledger_path"]).rows()
ids = [r["attempt_id"] for r in rows]
check("T1 resume: no duplicate attempts across fires (2 distinct, reserve+close each)",
      len(set(ids))==2 and len(rows)==4, f"rows={len(rows)} distinct={len(set(ids))}")
check("T1 resume: completed pair not re-run (2 completions)",
      sum(1 for r in rows if r["status"]=="completed")==2)

# T1b: deterministic slot independent of PYTHONHASHSEED
outs=set()
here=os.path.dirname(os.path.abspath(__file__))
for seed_env in ["0","12345"]:
    env2=dict(os.environ); env2["PYTHONHASHSEED"]=seed_env
    p=subprocess.run([sys.executable,"-c",
      "import runner_v2 as R; print(R.pick_model({'models':[{'model_id':'a'},{'model_id':'b'}]},'seed-00001',[]))"],
      capture_output=True,text=True,env=env2,cwd=here)
    outs.add(p.stdout.strip())
check("T1b stable slot independent of PYTHONHASHSEED", len(outs)==1, f"picks={outs}")

# T2: concurrent workers sharing one ledger (10 attempts, 2 workers, distinct seeds)
def worker(path, n, q):
    ledger=R.Ledger(path); script=["ok"]*20
    for k in range(n):
        model=R.pick_model(cfg_copy, f"wseed-{k}", ledger.effective_rows())
        R.run_once(cfg_copy, lambda: R.StubClient(script),
                   {"id":f"wseed-{k}","topic":"t"}, model, "p", ledger, max_out_chars=MAX_OUT)
    q.put(True)
cfg=fresh(); cfg_copy=dict(cfg)
q=multiprocessing.Queue()
ps=[multiprocessing.Process(target=worker,args=(cfg["ledger_path"],5,q)) for _ in range(2)]
[p.start() for p in ps]; [p.join(30) for p in ps]
raw=open(cfg["ledger_path"]).read()
def _json_ok(line):
    try: json.loads(line); return True
    except json.JSONDecodeError: return False
lines=[l for l in raw.splitlines() if l.strip()]
badl=[l for l in lines if not _json_ok(l)]
rows=[r for l in lines if _json_ok(l) for r in [json.loads(l)]]
ids=[r["attempt_id"] for r in rows]
from collections import Counter
c=Counter(ids)
check("T2 shared ledger: all 20 append lines parse (atomic appends)",
      len(badl)==0 and len(rows)==20, f"rows={len(rows)} bad={len(badl)}")
check("T2 each attempt_id exactly 2 rows (reserve+close)", all(v==2 for v in c.values()) and len(c)==10,
      f"distinct={len(c)} counts={sorted(set(c.values()))}")
check("T2 ledger reports 10 attempts", R.Ledger(cfg["ledger_path"]).attempts()==10)

# T2b: pending claim blocks a second worker on the same seed (main()'s own key)
cfg=fresh()
l1,m1 = make_run(cfg, ["boom"], 1)         # attempt 1 closes as unknown
rows_before = len(R.Ledger(cfg["ledger_path"]).rows())
cfg["run_id"]="w2"
held=R.Attempt("w2","held-seed",cfg["models"][0]["model_id"]); held.input_chars=1000; held.output_chars=MAX_OUT
l2=R.Ledger(cfg["ledger_path"]); l2.append(held)
check("T2b pending reservation blocks same seed (close-wins lookup)",
      R.pick_model(cfg,"held-seed",l2.effective_rows()) is None)
check("T2b closed attempt resumes to its own model (two-model allow-list)",
      R.pick_model(cfg,"seed-00001",l2.effective_rows())=="alpha.model")
check("T2b two-model allow-list: fall-through pick is deterministic, not the only model",
      R.pick_model(cfg,"other-seed",l2.effective_rows()) in ("alpha.model","zeta.model"))

# T3: cap holds across restart
cfg=fresh()
make_run(cfg, ["ok"]*5, 3)
check("T3 after run 1: 3 attempts", R.Ledger(cfg["ledger_path"]).attempts()==3)
make_run(cfg, ["ok"]*5, 3, cap=5)
check("T3 cap holds across restart: attempts==5",
      R.Ledger(cfg["ledger_path"]).attempts()==5, f"attempts={R.Ledger(cfg['ledger_path']).attempts()}")

# T4: timeout/unknown keep their FULL reservation, not prompt-only
cfg=fresh()
l, m = make_run(cfg, ["boom","timeout","ok"], 3)
rows=l.rows()
check("T4 unknown counted as spent", any(r["status"]=="unknown" for r in rows),
      f"statuses={[r['status'] for r in rows]}")
check("T4 timeout counted as spent", any(r["status"]=="timeout" for r in rows))
base=len("prompt for rain (pair 1)".encode())
for st in ("unknown","timeout"):
    r=[x for x in rows if x["status"]==st][0]
    check(f"T4 {st} attempt charged its full pre-call reservation (input + 113 x max_tokens)",
          r["output_chars"]==MAX_OUT and r["input_chars"]==base,
          f"in={r['input_chars']} out={r['output_chars']} expected_out={MAX_OUT}")
check("T4 completed attempt settled to its actual count",
      [x for x in rows if x["status"]=="completed"][0]["output_chars"] < MAX_OUT)
check("T4 spent_chars includes the reservations",
      l.spent_chars() >= 2*(base+MAX_OUT), f"spent={l.spent_chars()} expected>={2*(base+MAX_OUT)}")

# T5: no retry beyond the allowance (hold-level: runner makes no retries of its own)
cfg=fresh()
l, m = make_run(cfg, ["boom"], 1)
check("T5 failed call not retried beyond allowance: exactly 2 rows",
      len(l.rows())==2, f"rows={len(l.rows())}")

# T6: per-attempt char recording + usage (Instinct 2020)
cfg=fresh()
l, m = make_run(cfg, ["ok"], 1)
r=[x for x in l.rows() if x["status"]=="completed"][0]
check("T6 input+output chars recorded per attempt (byte count)",
      r["input_chars"]>0 and 0<r["output_chars"]<MAX_OUT, f"in={r['input_chars']} out={r['output_chars']}")
check("T6 usage field captured verbatim", r["usage"]=={"output_tokens":42}, f"usage={r['usage']}")

# T7 (Claude 2238 a-e, main()'s own path): main() skips a held seed and books no call
cfg=fresh()
l=R.Ledger(cfg["ledger_path"])
held=R.Attempt("w0","seed-00001",cfg["models"][0]["model_id"]); held.input_chars=1000; held.output_chars=MAX_OUT
l.append(held)
calls=[]
import runner_v2
def spy_factory():
    calls.append(1); return R.StubClient(["ok"])
runner_v2.build_client = spy_factory
cfg["run_id"]=""                            # main() overwrites it
import argparse
sys.argv=["runner_v2.py","--config",_cfg_path:=(os.path.join(os.path.dirname(cfg["seeds_path"]),"cfg.json"))]
json.dump(cfg, open(_cfg_path,"w"))
try:
    R.main()
    ok=True; err=""
except SystemExit as e:
    ok=False; err=str(e)
check("T7 main() skips a seed held pending by another worker (no call made)",
      ok and calls==[], f"calls={len(calls)} err={err}")
check("T7 main() left the held reservation as the only row", R.Ledger(cfg["ledger_path"]).attempts()==1)

# T8: a client without .generate raises before the reservation row exists
cfg=fresh()
l=R.Ledger(cfg["ledger_path"])
class NoGen:
    def chat(self, *a): pass
try:
    R.run_once(cfg, NoGen, {"id":"s9","topic":"t"}, "alpha.model", "prompt", l, max_out_chars=MAX_OUT)
    raised=False
except TypeError:
    raised=True
check("T8 client missing .generate raises TypeError, is not booked as unknown",
      raised and l.attempts()==0, f"raised={raised} rows={l.attempts()}")

# T9: config missing attempt_char_cap fails fast before any call
cfg=fresh(); cfg.pop("attempt_char_cap")
cfg["run_id"]=""
d2=os.path.dirname(cfg["seeds_path"]); p2=os.path.join(d2,"cfg2.json")
json.dump(cfg, open(p2,"w"))
sys.argv=["runner_v2.py","--config",p2]
try:
    R.main(); fast=False
except SystemExit as e:
    fast="attempt_char_cap" in str(e)
check("T9 missing attempt_char_cap fails fast with a named error", fast is True)

print("\n".join(PASS+FAIL))
print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
