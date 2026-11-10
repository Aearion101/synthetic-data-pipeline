#!/usr/bin/env python3
"""Stage-0 stub tests for runner_v2. No key, no network, no spend.
Portable and spawn-safe as well as fork-safe: the suite is one function called
under a __main__ guard, and worker processes take their config as an argument
rather than reading a module global (Instinct 2145/2146, DeepSeek 2140/2235/2334).
Covers Instinct 2020's five asks, the independent-review regression sets
(DeepSeek 2235/2236/2335, Claude 2238/2241/2235: T1/T2 corrected assertions,
N6 fail-fast, F6 parse-and-filter), and main()'s own path via a spy client
factory. The Instinct 0048 round (DeepSeek 0038's review of 42cad8e): T21
unparseable spend accounting, T22 the real SDK reply shape (ChatResult ->
GenericChatResponse -> list-of-ChatContent content), T23 pair-scoped claim
keys, T24 the incremental ledger tail parse. T14's lock-holder process target
is module level so the suite really is spawn-safe as this docstring claims."""
import json, os, subprocess, sys, tempfile, multiprocessing, fcntl

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
        if v == "throttle": raise R.OciAdapter.Throttled("stub 429")
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
    check("T2 corrected: at most one attempt per seed across workers (reserve-and-check)",
          all(len(v) == 1 for v in per_seed.values()),
          f"per_seed={ {k: len(v) for k, v in per_seed.items()} }")

    # T2b: pending claim blocks a second worker on the same seed (main()'s own key)
    cfg = fresh()
    write_seeds(cfg, 1)
    run_main(cfg, ["boom"])
    held = R.Attempt("w2", "held-seed", cfg["models"][0]["model_id"])
    held.input_chars = 1000; held.output_chars = MAX_OUT
    held.pair_key = "held-seed#1"
    l2 = R.Ledger(cfg["ledger_path"]); l2.append(held)
    check("T2b pending reservation blocks its own pair (close-wins lookup)",
          R.pick_model(cfg, "held-seed", l2.effective_rows(), "held-seed#1") is None)
    slot_model = sorted(m["model_id"] for m in cfg["models"])[R.stable_slot("held-seed", 2)]
    check("T2b a pending pair does not block the seed's other pairs (pair-scoped)",
          R.pick_model(cfg, "held-seed", l2.effective_rows(), "held-seed#2") == slot_model,
          f"got={R.pick_model(cfg, 'held-seed', l2.effective_rows(), 'held-seed#2')} want={slot_model}")
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
    base = len(b'prompt for rain (pair 1)') + 200  # + prompt_overhead_chars default
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
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        calls = run_main(cfg, [PAIR])
    check("T7 main() skips a seed held pending by another worker (no call made)", calls == [],
          f"calls={len(calls)}")
    check("T7 the skipped pair is NAMED at run end, never silently dropped",
          "pairs skipped this run" in buf.getvalue() and "seed-00001#1" in buf.getvalue(),
          f"out={buf.getvalue()[-200:]!r}")

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

    # T13: adapter reads the REAL message field, never a longest-string guess
    ad = object.__new__(R.OciAdapter)
    ad.cfg = {}
    fake = type("FakeData", (), {})()
    fake.to_dict = lambda: {"choices": [{"message": {"content": "the real reply"}}], "usage": {"output_tokens": 9}}
    got, usage = ad._reply_and_usage(fake)
    check("T13 adapter reads choices[0].message.content and passes usage verbatim",
          got == "the real reply" and usage == {"output_tokens": 9}, f"got={got!r} usage={usage!r}")
    fake2 = type("FakeData", (), {})()
    fake2.to_dict = lambda: {"something": "else"}
    refused = False
    try:
        ad._reply_and_usage(fake2)
    except RuntimeError as e:
        refused = "refusing to guess" in str(e)
    check("T13 adapter refuses to guess when no message field exists",
          refused, f"refused={refused}")
    check("T13 adapter exposes .generate(model_id, prompt, cfg)",
          callable(getattr(R.OciAdapter, "generate", None)))

    # T14: one-worker run lock: a second process refuses and books nothing
    cfg = fresh(); write_seeds(cfg, 2)
    d = os.path.dirname(cfg["seeds_path"]); p = os.path.join(d, "cfg14.json")
    json.dump(cfg, open(p, "w"))
    fd = R.acquire_run_lock(cfg["ledger_path"])
    check("T14 run lock acquired by first worker", fd is not None)
    second = R.acquire_run_lock(cfg["ledger_path"])
    check("T14 second lock attempt in-process refused (non-blocking)",
          second is None, f"second={second}")
    fcntl.flock(fd, fcntl.LOCK_UN); fd.close()
    q2 = multiprocessing.Queue()
    ph = multiprocessing.Process(target=holder, args=(cfg["ledger_path"], q2))
    ph.start(); import time as _t; _t.sleep(0.5)
    check("T14 cross-process: child holds, parent refuses",
          R.acquire_run_lock(cfg["ledger_path"]) is None)
    ph.join(10)
    check("T14 child reported holding the lock", q2.get() == "held")

    # T15: cap refusal returns the sentinel, books nothing, kills nothing
    cfg = fresh(); write_seeds(cfg, 2); cfg["attempt_char_cap"] = 300
    run_main(cfg, ["ok"])
    check("T15 cap refusal: no attempt booked, run survives",
          R.Ledger(cfg["ledger_path"]).attempts() == 0 and len(out_recs(cfg["out_path"])) == 0,
          f"attempts={R.Ledger(cfg['ledger_path']).attempts()}")

    # T16: held seeds are LISTED at run end, never auto-released
    import io, contextlib
    cfg = fresh(); write_seeds(cfg, 1)
    l = R.Ledger(cfg["ledger_path"])
    heldrow = R.Attempt("w0", "seed-00001", cfg["models"][0]["model_id"])
    heldrow.input_chars = 1000; heldrow.output_chars = MAX_OUT
    l.append(heldrow)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        run_main(cfg, [PAIR])
    out_text = buf.getvalue()
    check("T16 held seed listed for manual reconciliation at run end",
          "held seeds awaiting manual reconciliation" in out_text and "seed-00001" in out_text,
          f"out={out_text[-200:]}")

    # T17: prompt-pack consumption: execution_status gates, rendered prompt is used
    cfg = fresh()
    with open(cfg["seeds_path"], "w") as f:
        f.write(json.dumps({"id": "seed-00001", "topic": "rain", "language": "en",
                            "split": "holdout", "execution_status": "held"}) + "\n")
        f.write(json.dumps({"id": "seed-00002", "topic": "rain", "language": "id",
                            "split": "dev", "execution_status": "eligible",
                            "prompt": "RENDERED PROMPT GOES HERE"}) + "\n")
    d = os.path.dirname(cfg["seeds_path"]); p = os.path.join(d, "cfg17.json")
    json.dump(cfg, open(p, "w"))
    seen_prompts = []
    class RecSpy(SpyClient):
        def generate(self, model_id, prompt, cfg):
            seen_prompts.append(prompt)
            return super().generate(model_id, prompt, cfg)
    rec_client = [None]
    def rec_factory(c):
        if rec_client[0] is None: rec_client[0] = RecSpy([PAIR])
        return rec_client[0]
    sys.argv = ["runner_v2.py", "--config", p]
    saved_bc = R.build_client; R.build_client = rec_factory
    try: R.main()
    finally: R.build_client = saved_bc
    recs = out_recs(cfg["out_path"])
    check("T17 execution_status=held row skipped and counted, eligible row ran once",
          len(seen_prompts) == 1 and recs and recs[0]["id"].startswith("seed-00002"),
          f"prompts={seen_prompts} recs={len(recs)}")
    check("T17 rendered prompt field is what the model received",
          seen_prompts and seen_prompts[0] == "RENDERED PROMPT GOES HERE",
          f"got={seen_prompts[:1]}")
    check("T17 record carries language, split and prompt_source=rendered",
          recs and recs[0].get("language") == "id" and recs[0].get("split") == "dev"
          and recs[0].get("prompt_source") == "rendered",
          f"rec={ {k: recs[0].get(k) for k in ('language','split','prompt_source')} if recs else None }")

    # T18: num_generations other than 1 fails fast
    cfg = fresh(); write_seeds(cfg, 1); cfg["num_generations"] = 4
    d = os.path.dirname(cfg["seeds_path"]); p = os.path.join(d, "cfg18.json")
    json.dump(cfg, open(p, "w")); sys.argv = ["runner_v2.py", "--config", p]
    try:
        R.main(); fast = False
    except SystemExit as e:
        fast = "num_generations" in str(e)
    check("T18 num_generations != 1 fails fast before any call", fast is True)

    # T20: a 429 closes its attempt as throttled; the retry is a NEW reserved attempt
    cfg = fresh(); write_seeds(cfg, 1)
    run_main(cfg, ["throttle", PAIR])
    rows = R.Ledger(cfg["ledger_path"]).rows()
    eff = R.Ledger(cfg["ledger_path"]).effective_rows()
    check("T20 throttled attempt closes as throttled and keeps its reservation",
          any(r["status"] == "throttled" for r in eff),
          f"statuses={[r['status'] for r in eff]}")
    check("T20 retry after 429 is a new reserved attempt and the pair is made",
          len(eff) == 2 and len(out_recs(cfg["out_path"])) == 1,
          f"attempts={len(eff)} recs={len(out_recs(cfg['out_path']))}")

    # T21 (DeepSeek 0038 R2): an unparseable reply is a real charge with no pair
    cfg = fresh(); write_seeds(cfg, 1)
    run_main(cfg, ["badjson"])
    l = R.Ledger(cfg["ledger_path"])
    eff = [x for x in l.effective_rows() if x["status"] == "unparseable"]
    check("T21 unparseable keeps its chars in the spend counter (never est $0.00 after a call)",
          l.attempts() == 1 and eff
          and l.spent_chars() == eff[0]["input_chars"] + eff[0]["output_chars"],
          f"attempts={l.attempts()} spent={l.spent_chars()} "
          f"expected={eff[0]['input_chars'] + eff[0]['output_chars'] if eff else None}")

    # T22 (Instinct 0048 B): the reply is read from the REAL SDK shape:
    # ChatResult.chat_response (GenericChatResponse) -> choices[0].message.content,
    # a LIST of ChatContent (TextContent), not a plain string. The dict-shape and
    # list-content cases always run; the built-from-oci-classes case runs when the
    # package is present (skipped honestly when it is not).
    def fake_data(d):
        return type("FakeData", (), {"to_dict": lambda self: d})()
    ad = object.__new__(R.OciAdapter); ad.cfg = {}
    got, usage = ad._reply_and_usage(fake_data({
        "chat_response": {"api_format": "GENERIC",
            "choices": [{"index": 0, "message": {"role": "ASSISTANT",
                "content": [{"type": "TEXT", "text": "the real reply"}]},
                "finish_reason": "stop"}],
            "usage": {"completion_tokens": 7}}}))
    check("T22 GENERIC reply from chat_response.choices[0].message.content (dict shape)",
          got == "the real reply" and usage == {"completion_tokens": 7},
          f"got={got!r} usage={usage!r}")
    got3, _ = ad._reply_and_usage(fake_data({
        "choices": [{"message": {"content": [{"type": "TEXT", "text": "part one "},
                                             {"type": "IMAGE", "url": "x"},
                                             {"type": "TEXT", "text": "part two"}]}}]}))
    check("T22 content list of TEXT parts unwraps in order, non-TEXT skipped",
          got3 == "part one part two", f"got={got3!r}")
    refused2 = False
    try:
        ad._reply_and_usage(fake_data({"choices": [{"message": {"content": []}}]}))
    except RuntimeError as e:
        refused2 = "refusing to guess" in str(e)
    check("T22 an empty content list still refuses to guess", refused2)
    sdk_ok = None
    try:
        import oci
        m = oci.generative_ai_inference.models
        res = m.ChatResult(model_id="meta.llama-4-scout-17b-16e-instruct")
        ch = m.ChatChoice(index=0, finish_reason="stop",
                          message=m.Message(role="ASSISTANT",
                                            content=[m.TextContent(text="sdk-shaped reply")]))
        res.chat_response = m.GenericChatResponse(
            api_format=m.GenericChatResponse.API_FORMAT_GENERIC, choices=[ch],
            usage=m.Usage(completion_tokens=7))
        got2, usage2 = ad._reply_and_usage(res)
        sdk_ok = (got2 == "sdk-shaped reply"
                  and isinstance(usage2, dict) and usage2.get("completion_tokens") == 7)
    except ImportError:
        pass
    check("T22 reply extraction on the real SDK model classes (oci present; None = oci missing)",
          sdk_ok is True, f"sdk_ok={sdk_ok}")

    # T23 (Instinct 0048 A): pair-scoped claims - pairs 2..N of a seed run after
    # pair 1 completes, and neither a second fire nor a direct re-claim re-bills.
    cfg = fresh({"pairs_per_seed": 4}); write_seeds(cfg, 1)
    run_main(cfg, [PAIR] * 8)
    l = R.Ledger(cfg["ledger_path"])
    eff = l.effective_rows()
    pks = sorted({r.get("pair_key") for r in eff if r["status"] == "completed"})
    check("T23 all four pairs of one seed claim and complete (pair-scoped keys)",
          len(out_recs(cfg["out_path"])) == 4
          and pks == [f"seed-00001#{i}" for i in (1, 2, 3, 4)],
          f"recs={len(out_recs(cfg['out_path']))} pks={pks}")
    a1 = l.attempts()
    run_main(cfg, [PAIR] * 8)
    check("T23 second fire books no new attempt for any pair",
          R.Ledger(cfg["ledger_path"]).attempts() == a1,
          f"attempts={R.Ledger(cfg['ledger_path']).attempts()}")
    l3 = R.Ledger(cfg["ledger_path"])
    check("T23 the ledger itself refuses a re-claim of a completed pair",
          R.claim_seed(cfg, "seed-00001#1", "alpha.model", l3) is None)

    # T24 (DeepSeek 0038 R3): the ledger re-reads only appended bytes; another
    # process's append under the lock is visible to the next claim without a
    # full re-parse, and a partial trailing line never crashes the read.
    cfg = fresh(); write_seeds(cfg, 1)
    lp = cfg["ledger_path"]
    l = R.Ledger(lp)
    a1r = R.Attempt("r1", "s1", "alpha.model"); a1r.pair_key = "s1#1"; l.append(a1r)
    n0 = len(l.rows())
    other = R.Ledger(lp)
    a2r = R.Attempt("r2", "s2", "zeta.model"); a2r.pair_key = "s2#1"
    other.append(a2r)
    l.refresh()
    check("T24 tail refresh sees another process's append",
          len(l.rows()) == n0 + 1 and l.rows()[-1]["seed_id"] == "s2",
          f"rows={len(l.rows())}")
    check("T24 a fresh Ledger's full parse agrees with the incremental view",
          len(R.Ledger(lp).rows()) == len(l.rows()))
    check("T24 a pending pair blocks only itself, not its sibling pairs",
          R.claim_seed(cfg, "s2#1", "alpha.model", l) is None
          and R.claim_seed(cfg, "s1#2", "alpha.model", l) is not None)
    with open(lp, "a", encoding="utf-8") as fh:
        fh.write('{"attempt_id": "half"')   # no trailing newline: a writer mid-append
    l.refresh()
    check("T24 a partial trailing line is left unparsed, not crashed on",
          all(r.get("attempt_id") for r in l.rows()))

    # T25 (spawn-run diagnosis 2026-11-11): a tail offset that no longer lands
    # on a line boundary heals by one full re-parse instead of crashing the
    # claim or silently skipping rows; a corrupt line mid-file still raises.
    l2 = R.Ledger(lp)
    l2.rows()                                  # cache at the file's true end
    l2._offset += 40                           # force the offset mid-row
    l2.refresh()
    check("T25 a stale tail offset heals by full re-parse, no row skipped",
          len(l2.rows()) == len(l.rows()) and all(r.get("attempt_id") for r in l2.rows()))
    l3 = R.Ledger(lp)
    with open(lp, "a", encoding="utf-8") as fh:
        fh.write("not json\n")
    raised = False
    try:
        l3.rows()
    except json.JSONDecodeError:
        raised = True
    check("T25 a corrupt complete line still raises (fail loud, never guess)", raised)

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
        claim = R.claim_seed(cfg, f"{sid}#1", model, ledger)
        if claim is None:
            continue                      # reserve-and-check lost the race: skip
        R.run_once(cfg, lambda: SpyClient(script), seed, model, prompt, ledger,
                   max_out_chars=113 * cfg["max_tokens"], attempt_row=claim,
                   overhead_chars=200)
    q.put(True)

def holder(path, qu):
    """T14's child: holds the run lock long enough for the parent to see the
    refusal. Module level so a spawn child can unpickle the target (DeepSeek
    0038 R1, Instinct 0048 D); a closure inside suite() cannot be pickled."""
    import runner_v2 as RR
    f = RR.acquire_run_lock(path)
    qu.put("held" if f else "none")
    import time; time.sleep(2)
    if f: fcntl.flock(f, fcntl.LOCK_UN); f.close()

if __name__ == "__main__":
    suite()
    print("\n".join(XFAIL) if XFAIL else "")
    print(f"RESULT: {len(PASS)} passed, {len(XFAIL)} expected-red, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
