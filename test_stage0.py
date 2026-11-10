#!/usr/bin/env python3
"""Stage-0 stub tests for runner_v2. No key, no network, no spend.
Covers Instinct 2020's five asks: (1) restart+resume across simulated fire
boundaries; (2) concurrent workers sharing one ledger; (3) cap holds across
restart; (4) failed/timeout/unknown count as spent; (5) no retry beyond allowance."""
import json, os, shutil, subprocess, sys, multiprocessing
import runner_v2 as R

PASS=[]; FAIL=[]
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(f"{name}: {'PASS' if cond else 'FAIL'} {detail}")

def fresh(d):
    shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
    cfg = {"run_id":"", "seeds_path":f"{d}/seeds.jsonl", "out_path":f"{d}/out.jsonl",
           "ledger_path":f"{d}/ledger.jsonl", "budget_usd":1.0,
           "price_per_million_chars":0.18, "attempt_char_cap":100000,
           "models":[{"model_id":"meta.llama-4-scout-17b-16e-instruct","weight":1}],
           "pairs_per_seed":1, "temperature":0.8, "max_tokens":1024}
    open(cfg["seeds_path"],"w").write(json.dumps({"id":"seed-00001","topic":"rain"})+"\n")
    return cfg

def make_run(cfg, script, n, cap=0):
    """Direct driver of one fire, mirroring main()'s ledger-based caps."""
    ledger = R.Ledger(cfg["ledger_path"]); made=[]
    for k in range(n):
        if cap and ledger.attempts() >= cap: break
        model = R.pick_model(cfg, "seed-00001#%d"%(k+1), ledger.rows())
        prompt = "prompt for rain (pair %d)"%(k+1)
        a = R.run_once(cfg, lambda: R.StubClient(script), {"id":"seed-00001","topic":"rain"},
                       model, prompt, ledger)
        made.append(a)
    return ledger, made

# T1: restart+resume across simulated fire boundaries
d="/tmp/t1"; cfg=fresh(d)
script = ["ok","ok"]                      # shared list; each fire pops the next action
make_run(cfg, script, 1)                  # fire 1: 1 attempt, completed
make_run(cfg, script, 1)                  # fire 2: next pair, same process sim of restart
rows = R.Ledger(cfg["ledger_path"]).rows()
ids = [r["attempt_id"] for r in rows]
check("T1 resume: no duplicate attempts across fires (2 distinct, reserve+close each)",
      len(set(ids))==2 and len(rows)==4, f"rows={len(rows)} distinct={len(set(ids))}")
check("T1 resume: completed pair not re-run (2 completions)",
      sum(1 for r in rows if r["status"]=="completed")==2)

# T1b: deterministic slot independent of PYTHONHASHSEED
outs=set()
for seed_env in ["0","12345"]:
    env2=dict(os.environ); env2["PYTHONHASHSEED"]=seed_env
    p=subprocess.run([sys.executable,"-c",
      "import runner_v2 as R; print(R.pick_model({'models':[{'model_id':'a'},{'model_id':'b'}]},'seed-00001#1',[]))"],
      capture_output=True,text=True,env=env2,cwd="/workspace/sdp-local")
    outs.add(p.stdout.strip())
check("T1b stable slot independent of PYTHONHASHSEED", len(outs)==1, f"picks={outs}")

# T2: concurrent workers sharing one ledger (10 attempts, 2 workers, distinct pairs)
def worker(d, n, q):
    cfg=json.load(open(f"{d}/cfg.json"))
    ledger=R.Ledger(cfg["ledger_path"])
    for k in range(n):
        model=R.pick_model(cfg,"wseed#%d"%(k+1), ledger.rows())
        R.run_once(cfg, lambda: R.StubClient(script), {"id":"wseed","topic":"t"}, model, "p", ledger)
    q.put(True)
d="/tmp/t2"; cfg=fresh(d)
json.dump(cfg, open(f"{d}/cfg.json","w"))
script=["ok"]*20
q=multiprocessing.Queue()
ps=[multiprocessing.Process(target=worker,args=(d,5,q)) for _ in range(2)]
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

# T2b: pending claim blocks a second worker on the same pair
d="/tmp/t2b"; cfg=fresh(d)
l1,m1 = make_run(cfg, ["boom"], 1)          # attempt 1 closes as unknown (not pending)
# simulate an in-flight worker: reservation row with status pending
cfg["run_id"]="w2"
a=R.Attempt("w2","held-1",cfg["models"][0]["model_id"]); a.input_chars=10
l2=R.Ledger(cfg["ledger_path"]); l2.append(a)
check("T2b pending reservation blocks same pair", R.pick_model(cfg,"held-1",l2.rows()) is None)
check("T2b closed attempt resumes to its own model",
      R.pick_model(cfg,"seed-00001#1",l2.rows())==cfg["models"][0]["model_id"])

# T3: cap holds across restart
d="/tmp/t3"; cfg=fresh(d)
make_run(cfg, ["ok"]*5, 3)                 # run 1: 3 attempts
l=R.Ledger(cfg["ledger_path"])
check("T3 after run 1: 3 attempts", l.attempts()==3)
make_run(cfg, ["ok"]*5, 3, cap=5)          # run 2 asks 3 more, cumulative cap 5
l=R.Ledger(cfg["ledger_path"])
check("T3 cap holds across restart: attempts==5", l.attempts()==5, f"attempts={l.attempts()}")

# T4: failed/timeout/unknown count as spent
d="/tmp/t4"; cfg=fresh(d)
l, m = make_run(cfg, ["boom","timeout","ok"], 3)
rows=l.rows()
st=[r["status"] for r in rows]
check("T4 unknown counted as spent", any(r["status"]=="unknown" for r in rows), f"statuses={st}")
check("T4 timeout counted as spent", any(r["status"]=="timeout" for r in rows))
base=len("prompt for rain (pair 1)")
check("T4 spent_chars charges unknown/timeout attempts",
      l.spent_chars() >= 2*base, f"spent={l.spent_chars()} base={base}")

# T5: no retry beyond the allowance
d="/tmp/t5"; cfg=fresh(d)
l, m = make_run(cfg, ["boom"], 1)
rows=l.rows()
check("T5 failed call not retried beyond allowance: exactly 2 rows", len(rows)==2, f"rows={len(rows)}")

# T6: per-attempt char recording (Instinct 2020 cost correction)
d="/tmp/t6"; cfg=fresh(d)
l, m = make_run(cfg, ["ok"], 1)
r=[x for x in l.rows() if x["status"]=="completed"][0]
check("T6 input+output chars recorded per attempt",
      r["input_chars"]>0 and r["output_chars"]>0, f"in={r['input_chars']} out={r['output_chars']}")
check("T6 usage field captured verbatim", r["usage"]=={"output_tokens":42}, f"usage={r['usage']}")

print("\n".join(PASS+FAIL))
print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
