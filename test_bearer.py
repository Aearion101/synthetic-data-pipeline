#!/usr/bin/env python3
"""Offline tests for bearer_adapter: no token, no external network, no spend.
A localhost HTTP server scripts the endpoint's replies; every protection the
runner owes (reservation, close, throttle, unknown, config refusal) is checked
through the same runner_v2 paths the real run uses. The env var used is a
test-only name, set and cleared inside the suite."""
import json, os, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import runner_v2 as R
import bearer_adapter as B

REPO = os.path.dirname(os.path.abspath(__file__))

PASS, FAIL = [], []
def check(name, cond, why=""):
    (PASS if cond else FAIL).append(name)
    if not cond: print(f"FAIL: {name} :: {why}")

TEST_ENV = "OCI_GENAI_TEST_TOKEN"
TOKEN = "test-token-not-a-real-secret-0123456789abcdef"

RECORDED = []
RECORDED2 = []          # second server: proves a redirect never forwards the token
SCRIPT = []
SCRIPT_LOCK = threading.Lock()
ORDER_LEDGER = [None]   # when set, the handler verifies the reservation row first
ORDER_OK = [None]

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        RECORDED.append({"path": self.path,
                         "auth": self.headers.get("Authorization"),
                         "content_type": self.headers.get("Content-Type"),
                         "body": json.loads(body or b"{}")})
        with SCRIPT_LOCK:
            action = SCRIPT.pop(0) if SCRIPT else ("json", 200, {"choices": []})
        kind = action[0]
        if kind == "sleep":
            time.sleep(action[1]); return  # client must time out first
        if kind == "redirect":
            self.send_response(302)
            self.send_header("Location", action[1])
            self.end_headers(); return
        if ORDER_LEDGER[0] is not None:
            # The reservation row must exist BEFORE the first request arrives.
            try:
                blob = open(ORDER_LEDGER[0]).read()
                ORDER_OK[0] = ('"status": "pending"' in blob or '"status":"pending"' in blob)
            except FileNotFoundError:
                ORDER_OK[0] = False
        _, status, payload = action
        blob = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

def serve(script):
    with SCRIPT_LOCK:
        SCRIPT[:] = list(script)
    del RECORDED[:]
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/20231130/actions/v1"

class SinkHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        RECORDED2.append({"auth": self.headers.get("Authorization")})
        blob = b'{"choices": []}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

def serve_sink():
    del RECORDED2[:]
    srv = ThreadingHTTPServer(("127.0.0.1", 0), SinkHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/openai/v1"

def fresh(extra=None):
    d = tempfile.mkdtemp()
    cfg = {
        "seeds_path": os.path.join(d, "seeds.jsonl"),
        "out_path": os.path.join(d, "data", "batch.jsonl"),
        "ledger_path": os.path.join(d, "ledger.jsonl"),
        "models": [{"model_id": "meta.llama-4-maverick-17b-128e-instruct-fp8", "weight": 1}],
        "budget_usd": 300.0, "price_per_million_chars": 0.18,
        "attempt_char_cap": 120000,
        "max_tokens": 1024, "temperature": 0.7,
        "region": "us-chicago-1",
        "auth": "bearer", "token_env": TEST_ENV,
        "pairs_per_seed": 1, "run_id": "test-bearer",
    }
    if extra: cfg.update(extra)
    open(cfg["seeds_path"], "a").close()
    return cfg

def suite():
    os.environ[TEST_ENV] = TOKEN
    try:
        run()
    finally:
        os.environ.pop(TEST_ENV, None)

def run():
    PAIR = '{"instruction":"Name the capital of France.","response":"The capital of France is Paris."}'
    OK = {"choices": [{"message": {"role": "assistant", "content": PAIR},
                       "finish_reason": "stop"}],
          "usage": {"prompt_tokens": 11, "completion_tokens": 42, "total_tokens": 53}}

    # T1 happy path: request shape and (reply, usage, finish_reason) triple
    srv, base = serve([("json", 200, OK)])
    cfg = fresh({"api_base_url": base})
    a = B.BearerAdapter(cfg)
    reply, usage, fr = a.generate("meta.llama-4-maverick-17b-128e-instruct-fp8", "ping", cfg)
    srv.shutdown()
    check("T1 reply", reply == PAIR, repr(reply))
    check("T1 usage verbatim", usage == OK["usage"], repr(usage))
    check("T1 finish_reason", fr == "stop", repr(fr))
    req = RECORDED[0]
    check("T1 auth header", req["auth"] == f"Bearer {TOKEN}", req["auth"])
    check("T1 path", req["path"] == "/20231130/actions/v1/chat/completions", req["path"])
    check("T1 body", req["body"]["model"] == "meta.llama-4-maverick-17b-128e-instruct-fp8"
          and req["body"]["messages"] == [{"role": "user", "content": "ping"}]
          and req["body"]["max_tokens"] == 1024 and req["body"]["temperature"] == 0.7,
          json.dumps(req["body"]))

    # T2 the /openai/v1 base shape joins the same way
    srv, base0 = serve([("json", 200, OK)])
    base_openai = base0.replace("/20231130/actions/v1", "/openai/v1")
    a = B.BearerAdapter(fresh({"api_base_url": base_openai}))
    a.generate("m", "p", cfg); srv.shutdown()
    check("T2 openai/v1 path", RECORDED[0]["path"] == "/openai/v1/chat/completions",
          RECORDED[0]["path"])

    # T3 429 -> the runner's one throttle type
    srv, base = serve([("json", 429, {"error": "too many requests"})])
    a = B.BearerAdapter(fresh({"api_base_url": base}))
    try:
        a.generate("m", "p", cfg); check("T3 throttle", False, "no raise")
    except R.OciAdapter.Throttled:
        check("T3 throttle", True)
    except Exception as e:
        check("T3 throttle", False, type(e).__name__)
    srv.shutdown()

    # T4 500 -> RuntimeError with a FIXED diagnostic: status only, never the
    # body. A body echoing the token (or a token longer than any slice) cannot
    # leak a prefix because no remote content is kept at all (v6, ChatGPT F2).
    srv, base = serve([("json", 500, {"echo": f"bad key {TOKEN}"})])
    a = B.BearerAdapter(fresh({"api_base_url": base}))
    try:
        a.generate("m", "p", cfg); check("T4 500", False, "no raise")
    except RuntimeError as e:
        check("T4 500", str(e) == "HTTP 500 from chat completions", str(e))
        check("T4 no body/prefix", TOKEN not in str(e) and TOKEN[:40] not in str(e)
              and "bad key" not in str(e), str(e))
    srv.shutdown()

    # T5 missing token env -> named config error at construction, zero requests
    os.environ.pop(TEST_ENV, None)
    srv, base = serve([("json", 200, OK)])
    try:
        B.BearerAdapter(fresh({"api_base_url": base}))
        check("T5 missing token", False, "constructed")
    except B.BearerConfigError as e:
        check("T5 missing token", TEST_ENV in str(e), str(e))
    check("T5 no request", len(RECORDED) == 0, repr(RECORDED))
    srv.shutdown()
    os.environ[TEST_ENV] = TOKEN

    # T6 reply without a message text field -> refuse to guess, FIXED message:
    # no remote keys are interpolated (v6, ChatGPT F1: a key that IS the
    # credential must not reach the diagnostic).
    srv, base = serve([("json", 200, {TOKEN: 1, "choices": [{"message": {},
                                                             "finish_reason": "stop"}]})])
    a = B.BearerAdapter(fresh({"api_base_url": base}))
    try:
        a.generate("m", "p", cfg); check("T6 no text", False, "no raise")
    except RuntimeError as e:
        check("T6 no text", "refusing to guess" in str(e), str(e))
        check("T6 no remote keys", TOKEN not in str(e) and TOKEN[:40] not in str(e)
              and "keys" not in str(e), str(e))
    srv.shutdown()

    # T7 content as a list of parts unwraps (never str(list))
    parts = [{"type": "text", "text": '{"instruction":"Q?"'},
             {"type": "text", "text": ',"response":"A."}'}]
    srv, base = serve([("json", 200, {"choices": [{"message": {"content": parts},
                                                   "finish_reason": "stop"}]})])
    a = B.BearerAdapter(fresh({"api_base_url": base}))
    reply, _, _ = a.generate("m", "p", cfg)
    srv.shutdown()
    check("T7 list content", reply == '{"instruction":"Q?","response":"A."}', repr(reply))

    # T8 a slow endpoint -> TimeoutError (runner closes the attempt "timeout")
    srv, base = serve([("sleep", 1.5)])
    a = B.BearerAdapter(fresh({"api_base_url": base, "request_timeout_seconds": 0.3}))
    try:
        a.generate("m", "p", cfg); check("T8 timeout", False, "no raise")
    except TimeoutError:
        check("T8 timeout", True)
    except Exception as e:
        check("T8 timeout", False, type(e).__name__)
    srv.shutdown()

    # T9 through run_once: reservation then completed close, usage/finish on the row
    srv, base = serve([("json", 200, OK)])
    cfg = fresh({"api_base_url": base})
    ledger = R.Ledger(cfg["ledger_path"])
    model = cfg["models"][0]["model_id"]
    claim = R.claim_seed(cfg, "seed-00001#1", model, ledger)
    a, reply = R.run_once(cfg, lambda: B.BearerAdapter(cfg), {"id": "seed-00001"}, model,
                          "prompt bytes", ledger, max_out_chars=113 * cfg["max_tokens"],
                          attempt_row=claim, overhead_chars=200)
    srv.shutdown()
    row = [r for r in ledger.effective_rows() if r["attempt_id"] == a.attempt_id][0]
    check("T9 completed", a.status == "completed" and reply == PAIR, a.status)
    check("T9 row usage", row["usage"] == OK["usage"], repr(row["usage"]))
    check("T9 row finish_reason", row["finish_reason"] == "stop", repr(row["finish_reason"]))
    check("T9 settled chars", row["output_chars"] == len(PAIR.encode("utf-8"))
          and row["input_chars"] == len("prompt bytes".encode()) + 200, repr(row))

    # T10 throttle through run_once: reservation kept as spent, status throttled
    srv, base = serve([("json", 429, {"error": "slow down"})])
    cfg = fresh({"api_base_url": base})
    ledger = R.Ledger(cfg["ledger_path"])
    claim = R.claim_seed(cfg, "seed-00001#1", model, ledger)
    try:
        R.run_once(cfg, lambda: B.BearerAdapter(cfg), {"id": "seed-00001"}, model,
                   "prompt bytes", ledger, max_out_chars=113 * cfg["max_tokens"],
                   attempt_row=claim, overhead_chars=200)
        check("T10 throttle raises", False, "no raise")
    except R.OciAdapter.Throttled:
        check("T10 throttle raises", True)
    srv.shutdown()
    row = ledger.effective_rows()[0]
    check("T10 throttled row", row["status"] == "throttled"
          and row["output_chars"] == 113 * cfg["max_tokens"]
          and row["status"] in R.SPENT_STATUSES, repr(row))

    # T11 connection refused -> run_once closes "unknown": counted spent, never retried
    closed = socket_closed_port()
    cfg = fresh({"api_base_url": f"http://127.0.0.1:{closed}/20231130/actions/v1"})
    ledger = R.Ledger(cfg["ledger_path"])
    claim = R.claim_seed(cfg, "seed-00001#1", model, ledger)
    a, reply = R.run_once(cfg, lambda: B.BearerAdapter(cfg), {"id": "seed-00001"}, model,
                          "prompt bytes", ledger, max_out_chars=113 * cfg["max_tokens"],
                          attempt_row=claim, overhead_chars=200)
    row = ledger.effective_rows()[0]
    check("T11 unknown", a.status == "unknown" and reply is None
          and row["output_chars"] == 113 * cfg["max_tokens"], repr(row))

    # T12 validate_config in bearer mode
    cfg = fresh()                      # derived default base URL, token env set
    R.validate_config(cfg); check("T12 valid bearer cfg", True)
    check("T12 no compartment needed", "compartment_id" not in cfg, "")
    os.environ.pop(TEST_ENV, None)
    try:
        R.validate_config(fresh())
        check("T12 env gate", False, "passed")
    except R.ConfigError as e:
        check("T12 env gate", TEST_ENV in str(e), str(e))
    os.environ[TEST_ENV] = TOKEN
    try:
        R.validate_config(fresh({"api_base_url": "https://api.example.com/v1"}))
        check("T12 host gate", False, "passed")
    except R.ConfigError as e:
        check("T12 host gate", "oci.oraclecloud.com" in str(e), str(e))
    try:
        R.validate_config(fresh({"api_base_url": "http://inference.generativeai.us-chicago-1.oci.oraclecloud.com/x"}))
        check("T12 https gate", False, "passed")
    except R.ConfigError as e:
        check("T12 https gate", "https" in str(e), str(e))
    try:
        R.validate_config(fresh({"budget_usd": None}))
        check("T12 budget gate", False, "passed")
    except R.ConfigError as e:
        check("T12 budget gate", "budget_usd" in str(e), str(e))
    try:
        R.validate_config(fresh({"attempt_char_cap": "SET_ME"}))
        check("T12 cap gate", False, "passed")
    except R.ConfigError as e:
        check("T12 cap gate", "attempt_char_cap" in str(e), str(e))
    R.validate_config(fresh({"api_base_url":
        "https://inference.generativeai.us-chicago-1.oci.oraclecloud.com/openai/v1"}))
    check("T12 openai/v1 base accepted", True)
    R.validate_config(fresh({"api_base_url": "http://127.0.0.1:8080/openai/v1"}))
    check("T12 loopback http accepted (offline rigs only)", True)
    try:
        R.validate_config(fresh({"api_base_url": "http://169.254.169.254/openai/v1"}))
        check("T12 non-loopback http refused", False, "passed")
    except R.ConfigError as e:
        check("T12 non-loopback http refused", "https" in str(e), str(e))
    try:
        R.validate_config(fresh({"auth": "oauth2"}))
        check("T12 auth value gate", False, "passed")
    except R.ConfigError as e:
        check("T12 auth value gate", "auth" in str(e), str(e))

    # T13 build_client dispatch
    a = R.build_client(fresh({"api_base_url": "http://127.0.0.1:1/20231130/actions/v1"}))
    check("T13 dispatch", isinstance(a, B.BearerAdapter), type(a).__name__)
    check("T13 default base is the article's /openai/v1",
          B.default_base_url("us-ashburn-1") ==
          "https://inference.generativeai.us-ashburn-1.oci.oraclecloud.com/openai/v1",
          B.default_base_url("us-ashburn-1"))

    # T14 non-JSON 200 body -> RuntimeError (attempt closes unknown, booked spent)
    srv, base = serve([("json", 200, b"<html>not json</html>")])
    a = B.BearerAdapter(fresh({"api_base_url": base}))
    try:
        a.generate("m", "p", cfg); check("T14 bad json", False, "no raise")
    except RuntimeError as e:
        check("T14 bad json", "non-JSON" in str(e), str(e))
    srv.shutdown()

    # T15 a redirect is an error and the token is never forwarded to the target
    sink, sink_base = serve_sink()
    srv, base = serve([("redirect", sink_base + "/chat/completions")])
    a = B.BearerAdapter(fresh({"api_base_url": base}))
    try:
        a.generate("m", "p", cfg); check("T15 redirect refused", False, "no raise")
    except RuntimeError as e:
        check("T15 redirect refused", "302" in str(e), str(e))
    except Exception as e:
        check("T15 redirect refused", False, type(e).__name__)
    check("T15 token never forwarded", len(RECORDED2) == 0, repr(RECORDED2))
    srv.shutdown(); sink.shutdown()

    # T16 the output bound is always sent, under either documented field name
    srv, base = serve([("json", 200, OK)])
    a = B.BearerAdapter(fresh({"api_base_url": base,
                               "max_tokens_param": "max_completion_tokens"}))
    a.generate("m", "p", cfg); srv.shutdown()
    check("T16 max_completion_tokens", "max_completion_tokens" in RECORDED[0]["body"]
          and "max_tokens" not in RECORDED[0]["body"], json.dumps(RECORDED[0]["body"]))
    srv, base = serve([("json", 200, OK)])
    try:
        B.BearerAdapter(fresh({"api_base_url": base, "max_tokens_param": "none"}))
        check("T16 param gate", False, "constructed")
    except B.BearerConfigError as e:
        check("T16 param gate", "max_tokens_param" in str(e), str(e))
    check("T16 param gate no request", len(RECORDED) == 0, repr(RECORDED))
    srv.shutdown()

    # T17 billing fail-closed gates in validate_config (bearer mode)
    try:
        R.validate_config(fresh({"max_attempts": "SET_ME"}))
        check("T17 max_attempts type", False, "passed")
    except R.ConfigError as e:
        check("T17 max_attempts type", "max_attempts" in str(e), str(e))
    try:
        R.validate_config(fresh({"billing_metric": "tokens"}))
        check("T17 billing_metric gate", False, "passed")
    except R.ConfigError as e:
        check("T17 billing_metric gate", "billing_metric" in str(e), str(e))
    R.validate_config(fresh({"billing_metric": "chars", "max_attempts": 5}))
    check("T17 chars metric accepted", True)
    try:
        R.validate_config(fresh({"max_tokens_param": "drop"}))
        check("T17 max_tokens_param gate", False, "passed")
    except R.ConfigError as e:
        check("T17 max_tokens_param gate", "max_tokens_param" in str(e), str(e))

    # T20 the allow-list actually refuses wrong billing families, not just labels
    for bad, why in (("xai.grok-4", "tokens"),
                     ("openai.gpt-oss-120b", "tokens"),
                     ("google.gemini-2.5-flash", "not a documented char-priced"),
                     ("ocid1.dedicatedaiclusterendpoint.oc1..x", "AI-unit-hour"),
                     ("cohere.command-a-03-2025", "not a documented char-priced"),
                     ("meta.llama-3.3-70b-instruct", "not one of the two ids verified"),
                     ("SET_ME_model_id_from_your_account", "not a documented char-priced"),
                     ("foo.bar", "not a documented char-priced")):
        try:
            R.validate_config(fresh({"models": [{"model_id": bad, "weight": 1}]}))
            check(f"T20 refuses {bad}", False, "passed")
        except R.ConfigError as e:
            check(f"T20 refuses {bad}", why in str(e), str(e)[:120])
    R.validate_config(fresh())   # meta.llama-4-maverick-17b-128e-instruct-fp8 passes
    check("T20 char-priced llama family accepted", True)

    # T21 the exact ids on the owner's account access list pass
    R.validate_config(fresh({"models": [{"model_id": "meta.llama-4-scout-17b-16e-instruct", "weight": 1}]}))
    R.validate_config(fresh({"models": [
        {"model_id": "meta.llama-4-maverick-17b-128e-instruct-fp8", "weight": 1},
        {"model_id": "meta.llama-4-scout-17b-16e-instruct", "weight": 1}]}))
    check("T21 account's exact llama ids accepted", True)

    # T22 the shipped smoke-test config validates and its reservation clears the cap
    smoke = json.load(open(os.path.join(REPO, "gen_config.bearer.smoke.example.json")))
    smoke["seeds_path"] = os.path.join(REPO, smoke["seeds_path"])
    smoke["out_path"] = os.path.join(tempfile.mkdtemp(), "out.jsonl")
    smoke["ledger_path"] = os.path.join(tempfile.mkdtemp(), "ledger.jsonl")
    smoke["token_env"] = TEST_ENV
    R.validate_config(smoke)
    prompt = json.loads(open(smoke["seeds_path"]).readline())["prompt"]
    check("T22 smoke config valid", True)
    check("T22 smoke reservation clears cap",
          R.record_chars(prompt) + 200 + 113 * smoke["max_tokens"] <= smoke["attempt_char_cap"],
          f"{R.record_chars(prompt) + 200 + 113 * smoke['max_tokens']} > {smoke['attempt_char_cap']}")
    check("T22 smoke bounds", smoke["max_attempts"] == 1 and smoke["max_retries"] == 0
          and smoke["region"] == "us-ashburn-1"
          and smoke["models"][0]["model_id"] == "meta.llama-4-maverick-17b-128e-instruct-fp8",
          json.dumps({k: smoke[k] for k in ("max_attempts", "max_retries", "region")}))

    # T18 main() enforces the count bound before any call, and labels estimates
    env = dict(os.environ); env[TEST_ENV] = TOKEN
    cfg = fresh()
    p = os.path.join(os.path.dirname(cfg["seeds_path"]), "cfg.json")
    json.dump(cfg, open(p, "w"))
    r = subprocess.run([sys.executable, os.path.join(REPO, "runner_v2.py"),
                        "--config", p], capture_output=True, text=True, env=env)
    check("T18 no count bound refuses", r.returncode != 0
          and "max_attempts" in (r.stderr + r.stdout)
          and not os.path.exists(cfg["ledger_path"]),
          f"rc={r.returncode} {r.stderr[:120]}")
    cfg2 = fresh({"max_attempts": 5,
                  "api_base_url": f"http://127.0.0.1:{socket_closed_port()}/openai/v1"})
    with open(cfg2["seeds_path"], "w") as f:
        f.write(json.dumps({"id": "s1", "prompt": "Write a haiku about rain.",
                            "execution_status": "eligible"}) + "\n")
    p2 = os.path.join(os.path.dirname(cfg2["seeds_path"]), "cfg.json")
    json.dump(cfg2, open(p2, "w"))
    r = subprocess.run([sys.executable, os.path.join(REPO, "runner_v2.py"),
                        "--config", p2], capture_output=True, text=True, env=env)
    rows = [json.loads(l) for l in open(cfg2["ledger_path"])] \
        if os.path.exists(cfg2["ledger_path"]) else []
    check("T18 count-bound run books bounded unknown", r.returncode == 4
          and "FAILED run" in r.stderr
          and "UNVERIFIED" in r.stdout
          and any(row["status"] == "unknown" for row in rows)
          and len({row["attempt_id"] for row in rows}) <= 5,
          f"rc={r.returncode} out={r.stdout[-200:]} rows={len(rows)}")

    # T19 ordering proof: the reservation row exists before the first request
    srv, base = serve([("json", 200, OK)])
    cfg = fresh({"api_base_url": base})
    ledger = R.Ledger(cfg["ledger_path"])
    claim = R.claim_seed(cfg, "seed-00001#1", model, ledger)
    ORDER_LEDGER[0] = cfg["ledger_path"]
    R.run_once(cfg, lambda: B.BearerAdapter(cfg), {"id": "seed-00001"}, model,
               "prompt bytes", ledger, max_out_chars=113 * cfg["max_tokens"],
               attempt_row=claim, overhead_chars=200)
    ORDER_LEDGER[0] = None
    srv.shutdown()
    check("T19 reservation before network", ORDER_OK[0] is True, repr(ORDER_OK[0]))

    # T23 REAL CLI 429 fixture (review defect 1): as a subprocess, runner_v2 is
    # __main__ and bearer_adapter's import loads it again - the throttle type
    # must still be caught, the attempt labeled "throttled", and the bounded
    # retry must book a NEW attempt and finish the pair.
    srv, base = serve([("json", 429, {"error": "slow down"}), ("json", 200, OK)])
    cfg = fresh({"api_base_url": base, "max_attempts": 5, "max_retries": 1})
    with open(cfg["seeds_path"], "w") as f:
        f.write(json.dumps({"id": "s1", "prompt": "Write a haiku about rain.",
                            "execution_status": "eligible"}) + "\n")
    p = os.path.join(os.path.dirname(cfg["seeds_path"]), "cfg.json")
    json.dump(cfg, open(p, "w"))
    env = dict(os.environ); env[TEST_ENV] = TOKEN
    r = subprocess.run([sys.executable, os.path.join(REPO, "runner_v2.py"),
                        "--config", p], capture_output=True, text=True, env=env)
    srv.shutdown()
    rows = [json.loads(l) for l in open(cfg["ledger_path"])] \
        if os.path.exists(cfg["ledger_path"]) else []
    statuses = [row["status"] for row in rows]
    check("T23 CLI 429 caught as throttled", r.returncode == 0
          and "made 1 pairs" in r.stdout
          and len(RECORDED) == 2
          and "throttled" in statuses and "completed" in statuses
          and "unknown" not in statuses,
          f"rc={r.returncode} reqs={len(RECORDED)} statuses={statuses} out={r.stdout[-150:]}")

    # T24 the count bound holds ACROSS retries (review defect 3): with
    # max_attempts 2 and endless 429s, exactly two attempts are booked.
    srv, base = serve([( "json", 429, {"error": "slow down"})] * 6)
    cfg = fresh({"api_base_url": base, "max_attempts": 2, "max_retries": 5})
    with open(cfg["seeds_path"], "w") as f:
        f.write(json.dumps({"id": "s1", "prompt": "Write a haiku about rain.",
                            "execution_status": "eligible"}) + "\n")
    p = os.path.join(os.path.dirname(cfg["seeds_path"]), "cfg.json")
    json.dump(cfg, open(p, "w"))
    r = subprocess.run([sys.executable, os.path.join(REPO, "runner_v2.py"),
                        "--config", p], capture_output=True, text=True, env=env)
    srv.shutdown()
    rows = [json.loads(l) for l in open(cfg["ledger_path"])] \
        if os.path.exists(cfg["ledger_path"]) else []
    n_attempts = len({row["attempt_id"] for row in rows})
    check("T24 count bound across retries", r.returncode == 4
          and n_attempts == 2 and len(RECORDED) == 2
          and all(row["status"] in ("claimed", "pending", "throttled") for row in rows),
          f"rc={r.returncode} attempts={n_attempts} reqs={len(RECORDED)} out={r.stdout[-150:]}")

    # T25 C1: a control-char token is refused at construction, secret-free,
    # zero requests; T26: same through the real CLI before the run lock.
    os.environ[TEST_ENV] = TOKEN + "\r"
    srv, base = serve([("json", 200, OK)])
    try:
        B.BearerAdapter(fresh({"api_base_url": base}))
        check("T25 control-char token refused", False, "constructed")
    except B.BearerConfigError as e:
        check("T25 control-char token refused", "control" in str(e)
              and TOKEN not in str(e), str(e)[:120])
    check("T25 no request", len(RECORDED) == 0, repr(RECORDED))
    cfg = fresh({"api_base_url": base, "max_attempts": 1})
    p = os.path.join(os.path.dirname(cfg["seeds_path"]), "cfg.json")
    json.dump(cfg, open(p, "w"))
    env = dict(os.environ)
    r = subprocess.run([sys.executable, os.path.join(REPO, "runner_v2.py"),
                        "--config", p], capture_output=True, text=True, env=env)
    srv.shutdown()
    check("T26 CLI refuses before lock, token nowhere", r.returncode != 0
          and "control" in (r.stderr + r.stdout)
          and TOKEN not in r.stderr and TOKEN not in r.stdout
          and len(RECORDED) == 0
          and not os.path.exists(cfg["ledger_path"]),
          f"rc={r.returncode} err={r.stderr[:120]} reqs={len(RECORDED)}")
    os.environ[TEST_ENV] = TOKEN
    env = dict(os.environ)   # rebuild: the T26 env carried the polluted token

    # T27 C1 belt-and-suspenders: ANY transport exception is replaced with
    # fixed text before it can reach the ledger, even one quoting the
    # Authorization value. The assertion pins the property (the credential is
    # withheld), not the mechanism: an exact-match scrub misses a truncated
    # credential, and BadStatusLine/UnknownProtocol carry remote bytes
    # (DeepSeek R9 / ChatGPT 1803 / Gemini 1822).
    srv, base = serve([("json", 200, OK)])
    a = B.BearerAdapter(fresh({"api_base_url": base}))
    class BoomOpener:
        def open(self, *a2, **k):
            raise ValueError(f"Invalid header value b'Bearer {TOKEN}'")
    a._opener = BoomOpener()
    try:
        a.generate("m", "p", cfg); check("T27 transport text withheld", False, "no raise")
    except RuntimeError as e:
        check("T27 transport text withheld", TOKEN not in str(e)
              and str(e) == "request failed before a response"
              and e.__cause__ is None and e.__suppress_context__, str(e)[:120])
    srv.shutdown()

    # T28 F2: a failed bearer smoke run exits non-zero and says so.
    cfg = fresh({"max_attempts": 1, "max_retries": 0,
                 "api_base_url": f"http://127.0.0.1:{socket_closed_port()}/openai/v1"})
    with open(cfg["seeds_path"], "w") as f:
        f.write(json.dumps({"id": "s1", "prompt": "Write a haiku about rain.",
                            "execution_status": "eligible"}) + "\n")
    p = os.path.join(os.path.dirname(cfg["seeds_path"]), "cfg.json")
    json.dump(cfg, open(p, "w"))
    r = subprocess.run([sys.executable, os.path.join(REPO, "runner_v2.py"),
                        "--config", p], capture_output=True, text=True, env=env)
    check("T28 failed smoke exits 4", r.returncode == 4
          and "FAILED run" in r.stderr and "status and error fields" in r.stderr,
          f"rc={r.returncode} err={r.stderr[:150]}")

    # T29 C2: the CLI may tighten the config's count bound, never widen it.
    cfg = fresh({"max_attempts": 1})
    p = os.path.join(os.path.dirname(cfg["seeds_path"]), "cfg.json")
    json.dump(cfg, open(p, "w"))
    r = subprocess.run([sys.executable, os.path.join(REPO, "runner_v2.py"),
                        "--config", p, "--max-attempts", "50"],
                       capture_output=True, text=True, env=env)
    check("T29 CLI cannot widen bound", r.returncode != 0
          and "never widen" in (r.stderr + r.stdout)
          and not os.path.exists(cfg["ledger_path"]),
          f"rc={r.returncode} err={r.stderr[:120]}")

    # ---- v6: response-side credential boundary (ChatGPT 1706 / Gemini 1722,
    # reviewer F1/F2/F4/F5). A dummy credential AND its deliberately truncated
    # prefix must appear in no stdout/stderr/ledger/output, on error AND
    # success paths. Error paths keep fixed diagnostics only, so no remote
    # content exists to leak; the success path fails closed on the credential
    # and retains only allowlisted typed fields.

    PREFIX = TOKEN[:40]   # a meaningful truncated prefix, not the empty claim

    # T30 adapter: a 340-char credential at the START of a 500 body (longer
    # than any slice) leaks nothing - no body is kept at all.
    srv, base = serve([("json", 500, {"echo": TOKEN + " trailing"})])
    a = B.BearerAdapter(fresh({"api_base_url": base}))
    try:
        a.generate("m", "p", cfg); check("T30 long-token 500", False, "no raise")
    except RuntimeError as e:
        check("T30 long-token 500", str(e) == "HTTP 500 from chat completions"
              and TOKEN not in str(e) and PREFIX not in str(e), str(e))
    srv.shutdown()

    # T31 adapter: non-JSON 200 body -> fixed message, no decode detail, and a
    # body that contains the token leaks nothing (raw bytes kind).
    srv, base = serve([("json", 200, b"not json at all: " + TOKEN.encode())])
    a = B.BearerAdapter(fresh({"api_base_url": base}))
    try:
        a.generate("m", "p", cfg); check("T31 non-JSON body", False, "no raise")
    except RuntimeError as e:
        check("T31 non-JSON body", "non-JSON body" in str(e)
              and TOKEN not in str(e) and PREFIX not in str(e), str(e))
    srv.shutdown()

    # T32 adapter: success path fails closed when the reply text contains the
    # credential - fixed message, nothing relayed (reviewer F5).
    srv, base = serve([("json", 200, {"choices": [{"message": {
        "content": "echo " + TOKEN}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1}})])
    a = B.BearerAdapter(fresh({"api_base_url": base}))
    try:
        a.generate("m", "p", cfg); check("T32 reply-token fail-closed", False, "no raise")
    except RuntimeError as e:
        check("T32 reply-token fail-closed", "contained the credential" in str(e)
              and TOKEN not in str(e) and PREFIX not in str(e), str(e))
    srv.shutdown()

    # T33 adapter: success retains only allowlisted typed usage fields and enum
    # finish_reason; server-controlled usage keys/values and free-string finish
    # reasons are dropped (reviewer F4).
    srv, base = serve([("json", 200, {"choices": [{"message": {"content": "hi"},
        "finish_reason": "stop " + TOKEN}],
        "usage": {"prompt_tokens": 11, "note": TOKEN, TOKEN: 5,
                  "total_tokens": "53"}})])
    a = B.BearerAdapter(fresh({"api_base_url": base}))
    reply, usage, fr = a.generate("m", "p", cfg)
    srv.shutdown()
    check("T33 typed usage/enum finish", reply == "hi"
          and usage == {"prompt_tokens": 11} and fr is None,
          f"usage={usage!r} fr={fr!r}")

    # T34 runner-level CLI fixture, error path: 500 whose body leads with a
    # 340-char credential. Failed run exits 4; the credential and its prefix
    # appear in no stdout/stderr/ledger.
    srv, base = serve([("json", 500, {"echo": TOKEN + " trailing"})])
    cfg = fresh({"api_base_url": base, "max_attempts": 1})
    with open(cfg["seeds_path"], "w") as f:
        f.write(json.dumps({"id": "s1", "prompt": "Write a haiku about rain.",
                            "execution_status": "eligible"}) + "\n")
    p = os.path.join(os.path.dirname(cfg["seeds_path"]), "cfg.json")
    json.dump(cfg, open(p, "w"))
    r = subprocess.run([sys.executable, os.path.join(REPO, "runner_v2.py"),
                        "--config", p], capture_output=True, text=True, env=env)
    srv.shutdown()
    blob = r.stdout + r.stderr
    if os.path.exists(cfg["ledger_path"]):
        blob += open(cfg["ledger_path"]).read()
    check("T34 CLI 500 leak-free", r.returncode == 4
          and TOKEN not in blob and PREFIX not in blob,
          f"rc={r.returncode} blob-has-token={TOKEN in blob} prefix={PREFIX in blob}")

    # T35 runner-level CLI fixture, success path: a 200 whose reply text echoes
    # the credential fails closed; the pair is not written and the credential
    # and its prefix appear in no stdout/stderr/ledger/output.
    srv, base = serve([("json", 200, {"choices": [{"message": {
        "content": '{"instruction":"Q?","response":"' + TOKEN + '"}'},
        "finish_reason": "stop"}], "usage": {"prompt_tokens": 1}})])
    cfg = fresh({"api_base_url": base, "max_attempts": 1})
    with open(cfg["seeds_path"], "w") as f:
        f.write(json.dumps({"id": "s1", "prompt": "Write a haiku about rain.",
                            "execution_status": "eligible"}) + "\n")
    p = os.path.join(os.path.dirname(cfg["seeds_path"]), "cfg.json")
    json.dump(cfg, open(p, "w"))
    r = subprocess.run([sys.executable, os.path.join(REPO, "runner_v2.py"),
                        "--config", p], capture_output=True, text=True, env=env)
    srv.shutdown()
    blob = r.stdout + r.stderr
    if os.path.exists(cfg["ledger_path"]):
        blob += open(cfg["ledger_path"]).read()
    if os.path.exists(cfg["out_path"]):
        blob += open(cfg["out_path"]).read()
    check("T35 CLI success-echo fail-closed", r.returncode == 4
          and TOKEN not in blob and PREFIX not in blob,
          f"rc={r.returncode} blob-has-token={TOKEN in blob} prefix={PREFIX in blob}")

    # T36 v6.1: exception-chain suppression (ChatGPT acceptance: no raw
    # exception details in the displayed chain; DeepSeek 1735). Every adapter
    # raise that wrapped an underlying exception must detach it: __cause__ is
    # None and the context is suppressed, so no future traceback-printing path
    # can expose a cause's text.
    srv, base = serve([("json", 200, b"not json: " + TOKEN.encode())])
    a = B.BearerAdapter(fresh({"api_base_url": base}))
    try:
        a.generate("m", "p", cfg); check("T36 chain", False, "no raise")
    except RuntimeError as e:
        check("T36 chain", e.__cause__ is None and e.__suppress_context__
              and TOKEN not in repr(e.__context__), repr(e.__context__))
    srv.shutdown()
    class Boom2:
        def open(self, *a2, **k):
            raise ValueError("Invalid header value b'Bearer " + TOKEN + "'")
    a = B.BearerAdapter(fresh({"api_base_url": "http://127.0.0.1:9/x"}))
    a._opener = Boom2()
    try:
        a.generate("m", "p", cfg); check("T36 chain generic", False, "no raise")
    except RuntimeError as e:
        check("T36 chain generic", e.__cause__ is None and e.__suppress_context__
              and TOKEN not in str(e), str(e))
    srv2, base2 = serve([("sleep", 1.5)])
    a = B.BearerAdapter(fresh({"api_base_url": base2, "request_timeout_seconds": 0.3}))
    try:
        a.generate("m", "p", cfg); check("T36 chain timeout", False, "no raise")
    except TimeoutError as e:
        check("T36 chain timeout", e.__cause__ is None and e.__suppress_context__,
              repr(e.__cause__))
    srv2.shutdown()

    # T37/T38 v6.2: malformed REMOTE status lines (BadStatusLine and
    # UnknownProtocol) must not reach any persisted surface. These exceptions
    # embed server-controlled bytes and are not OSErrors, so they bypass
    # urllib's URLError wrapper and land in the generic branch (DeepSeek R9
    # measured 28 credential characters in the ledger; Claude 1835 named the
    # HTTP/9.x variant). Real-CLI fixtures on both shapes.
    class RawStatusHandler(BaseHTTPRequestHandler):
        status_line = b"BAD " + PREFIX.encode() + b"\r\n\r\n"
        def log_message(self, *a):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.wfile.write(self.status_line)
            self.wfile.flush()
            self.close_connection = True

    class BadStatus(RawStatusHandler):
        status_line = b"BAD " + PREFIX.encode() + b"\r\n\r\n"

    class UnknownProto(RawStatusHandler):
        status_line = b"HTTP/9." + PREFIX.encode() + b" 200 OK\r\n\r\n"

    for label, handler in (("T37 BadStatusLine", BadStatus),
                           ("T38 UnknownProtocol", UnknownProto)):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}/openai/v1"
        cfg = fresh({"api_base_url": base, "max_attempts": 1})
        with open(cfg["seeds_path"], "w") as f:
            f.write(json.dumps({"id": "s1", "prompt": "Write a haiku about rain.",
                                "execution_status": "eligible"}) + "\n")
        p = os.path.join(os.path.dirname(cfg["seeds_path"]), "cfg.json")
        json.dump(cfg, open(p, "w"))
        r = subprocess.run([sys.executable, os.path.join(REPO, "runner_v2.py"),
                            "--config", p], capture_output=True, text=True, env=env)
        srv.shutdown()
        blob = r.stdout + r.stderr
        if os.path.exists(cfg["ledger_path"]):
            blob += open(cfg["ledger_path"]).read()
        if os.path.exists(cfg["out_path"]):
            blob += open(cfg["out_path"]).read()
        check(label + " leak-free", r.returncode == 4
              and TOKEN not in blob and PREFIX not in blob,
              f"rc={r.returncode} token={TOKEN in blob} prefix={PREFIX in blob}")

def socket_closed_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port

if __name__ == "__main__":
    suite()
    print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
    import sys
    sys.exit(1 if FAIL else 0)
