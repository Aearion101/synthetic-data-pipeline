#!/usr/bin/env python3
"""Stage-0 stub tests for runner_v2. No key, no network, no spend.
Portable and spawn-safe as well as fork-safe: the suite is one function called
under a __main__ guard, and worker processes take their config as an argument
rather than reading a module global (Instinct 2145/2146, DeepSeek 2140/2235/2334).
Covers Instinct 2020's five asks, the independent-review regression sets
(DeepSeek 2235/2236/2335, Claude 2238/2241/2235: T1/T2 corrected assertions,
N6 fail-fast, F6 parse-and-filter), and main()'s own path via a spy client
factory. Known-red check: T11 asserts at most one attempt per seed across
workers, which cannot hold without a cross-process ledger lock; it is marked
expected-failure with that reason and stays red until the lock lands
(DeepSeek 2137 spend_cap flock, lead's call on the rail)."""
import json, os, subprocess, sys, tempfile, multiprocessing

import runner_v2 as R

PASS, FAIL, XFAIL = [], [], []
def check(name, cond, why=""):
    (PASS if cond else FAIL).append(name)
    if not cond: print(f"FAIL: {name} :: {why}")

def xfail_check(name, cond, why):
    if not cond:
        XFAIL.append(f"{name} (expected-red: {why})")
    else:
        FAIL.append(name + " [xfail now passes -- remove the expectation]")

def fresh(extra=None):
    d = tempfile.mkdtemp()
    cfg = {
        "seeds_path": os.path.join(d, "seeds.jsonl"),
        "out_path": os.path.join(d, "data", "batch.jsonl"),
        "ledger_path": os.path.join(d, "ledger.jsonl"),
        "models": [{"model_id": "alpha.model", "weight": 1},
                   {"model_id": "zeta.model", "weight": 1}],
        "budget_usd": 300.0, "price_per_million_chars": 0.18,
        "attempt_char_cap": 120000,
        "max_tokens": 1024, "temperature": 0.7,
        "compartment_id": "ocid1.test", "region": "us-ashburn-1",
        "pairs_per_seed": 1, "template": "pair-from-topic-v1", "run_id": "test",
    }
    if extra: cfg.update(extra)
    return cfg

PAIR = '{"instruction":"Name the capital of France.","response":"The capital of France is Paris."}'
def write_seeds(cfg, n, topic="rain"):
    with open(cfg["seeds_path"], "w") as f:
        for i in range(1, n + 1):
            f.write(json.dumps({"id": f"seed-{i:05d}", "topic": topic}) + "\n")

class SpyClient:
    def __init__(self, script):
        self.script = list(script); self.i = 0
    def generate(self, model_id, prompt, cfg):
        v = self.script[min(self.i, len(self.script) - 1)]; self.i += 1
        if v == "boom":   raise RuntimeError("stub boom")
        if v == "timeout": raise TimeoutError("stub timeout")
        return v, {"output_tokens": 42}

def run_main(cfg, script, monkey_factory=None):
    """Drive main() on its own path with a spy factory."""
    write_seeds(cfg, sum(1 for _ in open(cfg["seeds_path"])) if os.path.exists(cfg["seeds_path"]) else 2)
    d = os.path.dirname(cfg["seeds_path"])
    p = os.path.join(d, "cfg.json")
    json.dump(cfg, open(p, "w"))
    calls = []
    client = [None]
    def factory(c):
        calls.append(1)
        if client[0] is None: client[0] = SpyClient(script)
        return client[0]                      # one stub client across the run, so the
                                              # script advances call by call
    saved = R.build_client
    R.build_client = factory if monkey_factory is None else monkey_factory
    sys.argv = ["runner_v2.py", "--config", p]
    try:
        R.main()
    finally:
        R.build_client = saved
    return calls

def suite():
    MAX_OUT = 113 * 1024                      # 115,712 reply-term bytes (not 116,712:
                                              # that figure is a 1,000-byte prompt total)
    # T1 (corrected, Claude 2235 N1): at most one attempt per pair across fires.
    # Driven through main(), which owns the writer and seeds_done.
    cfg = fresh(); write_seeds(cfg, 2)
    run_main(cfg, [PAIR, PAIR])
    r1 = R.Ledger(cfg["ledger_path"]).rows()
    c1 = R.Ledger(cfg["ledger_path"]).attempts()
    run_main(cfg, [PAIR, PAIR])
    rows = R.Ledger(cfg["ledger_path"]).rows()
    per_seed = {}
    for r in rows:
        if r["status"] != "pending":
            per_seed.setdefault(r["seed_id"], set()).add(r["attempt_id"])
    check("T1 cross-fire: every pair attempted at most once (no re-bill)",
          all(len(v) == 1 for v in per_seed.values()) and c1 == 2,
          f"attempts_run1={c1} per_seed={ {k: len(v) for k, v in per_seed.items()} }")
    check("T1 cross-fire: fire 2 books no new attempt (writer + seeds_done)",
          R.Ledger(cfg["ledger_path"]).attempts() == c1,
          f"attempts_now={R.Ledger(cfg['ledger_path']).attempts()}")
    def out_recs(c):
        try:
            return [json.loads(l) for l in open(c, encoding="utf-8") if l.strip()]
        except FileNotFoundError:
            return []
    out_lines = out_recs(cfg["out_path"])
    check("T1 writer: out_path holds one record per completed pair",
          len(out_lines) == 2 and all(x["source"] == "synthetic" for x in out_lines),
          f"records={len(out_lines)}")
    check("T1 writer: record carries generate.py provenance shape",
          all(set(x) >= {"id", "instruction", "response", "source", "generator", "created_utc"}
              for x in out_lines))

    # T1b: deterministic slot independent of PYTHONHASHSEED
    outs = set()
    here = os.path.dirname(os.path.abspath(__file__))
    for seed_env in ["0", "12345"]:
        env2 = dict(os.environ); env2["PYTHONHASHSEED"] = seed_env
        p = subprocess.run([sys.executable, "-c",
          "import runner_v2 as R; print(R.pick_model({'models':[{'model_id':'a'},{'model_id':'b'}]},'seed-00001',[]))"],
          capture_output=True, text=True, env=env2, cwd=here)
        outs.add(p.stdout.strip())
    check("T1b stable slot independent of PYTHONHASHSEED", len(outs) == 1, f"picks={outs}")

    # T2: concurrent workers, one shared ledger, structural checks
    cfg = fresh(); write_seeds(cfg, 5)
    q = multiprocessing.Queue()
    ps = [multiprocessing.Process(target=worker, args=(cfg, 5, q)) for _ in range(2)]
    [p.start() for p in ps]; [p.join(30) for p in ps]
    raw = open(cfg["ledger_path"]).read()
    def _json_ok(line):
        try: json.loads(line); return True
        except json.JSONDecodeError: return False
    lines = [l for l in raw.splitlines() if l.strip()]
    badl = [l for l in lines if not _json_ok(l)]
    rows = [r for l in lines if _json_ok(l) for r in [json.loads(l)]]
    from collections import Counter
    c = Counter(r["attempt_id"] for r in rows)
    check("T2 shared ledger: every append line parses (atomic appends)",
          len(badl) == 0 and len(rows) >= 10, f"rows={len(rows)} bad={len(badl)}")
    check("T2 every attempt_id has a reservation row (no model:null billing)",
          all("model" in r for r in rows) and all(r.get("model") for r in rows),
          f"models={[r.get('model') for r in rows]}")
    per_seed = {}
    for r in rows:
        if r["status"] != "pending":
            per_seed.setdefault(r["seed_id"], set()).add(r["attempt_id"])
    xfail_check("T2 corrected: at most one attempt per seed across workers",
                all(len(v) == 1 for v in per_seed.values()),
                "needs a cross-process ledger lock (DeepSeek 2137 spend_cap flock)")

    # T2b: pending claim blocks a second worker on the same seed (main()'s own key)
    cfg = fresh()
    write_seeds(cfg, 1)
    run_main(cfg, ["boom"])
    held = R.Attempt("w2", "held-seed", cfg["models"][0]["model_id"])
    held.input_chars = 1000; held.output_chars = MAX_OUT
    l2 = R.Ledger(cfg["ledger_path"]); l2.append(held)
    check("T2b pending reservation blocks same seed (close-wins lookup)",
          R.pick_model(cfg, "held-seed", l2.effective_rows()) is None)
    check("T2b closed attempt resumes to its own model (two-model allow-list)",
          R.pick_model(cfg, "seed-00001", l2.effective_rows()) == "alpha.model")

    # T3: cap holds across restart
    cfg = fresh(); write_seeds(cfg, 5)
    run_main(cfg, [PAIR] * 5)
    a1 = R.Ledger(cfg["ledger_path"]).attempts()
    cfg2 = dict(cfg); cfg2["max_attempts"] = 5
    d = os.path.dirname(cfg["seeds_path"]); p = os.path.join(d, "cfg3.json")
    json.dump(cfg2, open(p, "w"))
    sys.argv = ["runner_v2.py", "--config", p, "--max-attempts", "5"]
    R.main()
    check("T3 max_attempts holds across restart", R.Ledger(cfg["ledger_path"]).attempts() == 5,
          f"attempts={R.Ledger(cfg['ledger_path']).attempts()}")

    # T4: timeout/unknown keep their FULL reservation, not prompt-only
    cfg = fresh(); write_seeds(cfg, 3)
    run_main(cfg, ["boom", "timeout", PAIR])
    rows = R.Ledger(cfg["ledger_path"]).rows()
    check("T4 unknown counted as spent", any(r["status"] == "unknown" for r in rows))
    check("T4 timeout counted as spent", any(r["status"] == "timeout" for r in rows))
    base = len(b'prompt for rain (pair 1)')
    for st in ("unknown", "timeout"):
        r = [x for x in rows if x["status"] == st][0]
        check(f"T4 {st} attempt charged its full pre-call reservation (input + 113 x max_tokens)",
              r["output_chars"] == MAX_OUT and r["input_chars"] == base,
              f"in={r['input_chars']} out={r['output_chars']} expected_out={MAX_OUT}")
    comp = [x for x in rows if x["status"] == "completed"]
    check("T4 completed attempt settled to its actual byte count",
          comp and comp[0]["output_chars"] < MAX_OUT)
    l = R.Ledger(cfg["ledger_path"])
    check("T4 spent_chars includes the reservations",
          l.spent_chars() >= 2 * (base + MAX_OUT))

    # T5: no retry beyond the allowance
    cfg = fresh(); write_seeds(cfg, 1)
    run_main(cfg, ["boom", "boom", "boom"])
    check("T5 failed call not retried beyond allowance: exactly one attempt",
          R.Ledger(cfg["ledger_path"]).attempts() == 1,
          f"attempts={R.Ledger(cfg['ledger_path']).attempts()}")

    # T6: per-attempt recording + usage
    cfg = fresh(); write_seeds(cfg, 1)
    run_main(cfg, [PAIR])
    rows = [r for r in R.Ledger(cfg["ledger_path"]).rows() if r["status"] == "completed"]
    check("T6 input+output bytes recorded per attempt",
          rows and rows[0]["input_chars"] > 0 and 0 < rows[0]["output_chars"] < MAX_OUT)
    check("T6 usage field captured verbatim", rows and rows[0]["usage"] == {"output_tokens": 42})

    # T7: main() skips a seed held pending by another worker and books no call
    cfg = fresh(); write_seeds(cfg, 1)
    l = R.Ledger(cfg["ledger_path"])
    held = R.Attempt("w0", "seed-00001", cfg["models"][0]["model_id"])
    held.input_chars = 1000; held.output_chars = MAX_OUT
    l.append(held)
    calls = run_main(cfg, [PAIR])
    check("T7 main() skips a seed held pending by another worker (no call made)", calls == [],
          f"calls={len(calls)}")

    # T8: a client without .generate raises before the reservation row exists
    cfg = fresh(); write_seeds(cfg, 1)
    l = R.Ledger(cfg["ledger_path"])
    class NoGen:
        def chat(self, *a): pass
    try:
        R.run_once(cfg, lambda: NoGen(), {"id": "s9", "topic": "t"}, "alpha.model", "prompt",
                   l, max_out_chars=MAX_OUT)
        raised = False
    except TypeError:
        raised = True
    check("T8 client missing .generate raises TypeError, is not booked as unknown",
          raised and l.attempts() == 0, f"raised={raised} rows={l.attempts()}")

    # T9: config missing attempt_char_cap fails fast before any call
    cfg = fresh(); write_seeds(cfg, 1); cfg.pop("attempt_char_cap")
    d = os.path.dirname(cfg["seeds_path"]); p = os.path.join(d, "cfg9.json")
    json.dump(cfg, open(p, "w")); sys.argv = ["runner_v2.py", "--config", p]
    try:
        R.main(); fast = False
    except SystemExit as e:
        fast = "attempt_char_cap" in str(e)
    check("T9 missing attempt_char_cap fails fast with a named error", fast is True)

    # T10 (N6): config missing max_tokens fails fast; no prompt-only charge returns
    cfg = fresh(); write_seeds(cfg, 1); cfg.pop("max_tokens")
    d = os.path.dirname(cfg["seeds_path"]); p = os.path.join(d, "cfg10.json")
    json.dump(cfg, open(p, "w")); sys.argv = ["runner_v2.py", "--config", p]
    try:
        R.main(); fast = False
    except SystemExit as e:
        fast = "max_tokens" in str(e)
    check("T10 missing max_tokens fails fast with a named error (no silent prompt-only charge)",
          fast is True and R.Ledger(cfg["ledger_path"]).attempts() == 0,
          f"fast={fast}")

    # T11 (F6): an unparseable reply closes as unparseable, never counts as made
    cfg = fresh(); write_seeds(cfg, 1)
    run_main(cfg, ["total garbage, not JSON"])
    rows = R.Ledger(cfg["ledger_path"]).rows()
    eff = R.Ledger(cfg["ledger_path"]).effective_rows()
    out_lines = out_recs(cfg["out_path"])
    check("T11 unparseable reply closes as unparseable (last row wins)",
          any(r["status"] == "unparseable" for r in eff),
          f"statuses={[r['status'] for r in eff]}")
    check("T11 unparseable reply writes no record and makes no pair",
          len(out_lines) == 0, f"records={len(out_lines)}")

    # T12 (F6): a too-short pair is filtered before it counts as made
    cfg = fresh(); write_seeds(cfg, 1)
    run_main(cfg, ['{"instruction":"hi","response":"there"}'])
    out_lines = out_recs(cfg["out_path"])
    check("T12 too-short pair filtered (below the 15-byte floor), no record written",
          len(out_lines) == 0, f"records={len(out_lines)}")

    # T13: adapter extraction walks nested structures without touching the oci SDK
    ad = object.__new__(R.OciAdapter)
    ad.cfg = {}
    fake = type("FakeData", (), {})()
    fake.to_dict = lambda: {"data": {"choices": [{"text": "short"}, {"text": "the longest text here"}]}}
    got = ad._extract_text(fake)
    check("T13 adapter _extract_text walks nested data and takes the longest text",
          got == "the longest text here", f"got={got!r}")
    check("T13 adapter exposes .generate(model_id, prompt, cfg)",
          callable(getattr(R.OciAdapter, "generate", None)))

def worker(cfg, n, q):
    """Same five seeds from two workers. Config arrives as an argument so the
    spawned child does not re-run the suite."""
    ledger = R.Ledger(cfg["ledger_path"])
    script = [PAIR] * 20
    for k in range(n):
        sid = f"seed-{k + 1:05d}"
        seed = {"id": sid, "topic": "rain"}
        model = R.pick_model(cfg, sid, ledger.effective_rows())
        if model is None:
            continue                      # another worker holds this seed: skip, never bill null
        prompt = f"prompt for rain (pair 1)"
        R.run_once(cfg, lambda: SpyClient(script), seed, model, prompt, ledger,
                   max_out_chars=113 * cfg["max_tokens"])
    q.put(True)

if __name__ == "__main__":
    suite()
    print("\n".join(XFAIL) if XFAIL else "")
    print(f"RESULT: {len(PASS)} passed, {len(XFAIL)} expected-red, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
