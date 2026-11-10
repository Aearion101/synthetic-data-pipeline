#!/usr/bin/env python3
"""runner_v2: Stage-0 test implementation of the Fo-1830 fix list. Local only, no repo push.

Fixes implemented vs repo generate.py (Aearion101/synthetic-data-pipeline HEAD):
 1 import os; 2 deterministic resume (sha256 of seed id, not hash()); 3 cumulative
 ledger across runs (budget/attempt caps read from ledger, not per-run); 4 model
 allow-list from config; 5 provenance fields (seed id/source, language fields, run id,
 per-attempt input/output chars and usage); 6 pilot mode with hard cumulative attempt
 cap (--max-attempts); 7 reservation row written BEFORE each call; interrupted or
 unknown outcomes stay in the ledger and count as spent; 8 region from config, passed
 to client construction and validated on first authenticated call (stub test covers
 the path); 9 no Cohere ids accepted (allow-list refuses); 10 README wording is a
 repo edit, not done here.
Oracle bills prompt+response characters (Instinct 2020): input chars counted as
 UTF-8 bytes, response chars counted from the returned text, and any usage field the
 response returns is recorded verbatim for reconciliation against Oracle's usage view.

Fixes from the independent PR #1 reviews (DeepSeek 2235/2236, Claude 2238/2241) and
 Instinct 2146:
 11 the pre-call reservation is real: output_chars = 113 x max_tokens before the call;
    a pending/timeout/unknown/failed attempt keeps its FULL reservation, only a
    completed row settles it to the actual count (the prompt-only undercharge is gone);
 12 resume key fixed: rows keyed by the bare seed id, lookups use effective_rows()
    (close wins), a held seed is skipped, and a None model can no longer crash main();
 13 the client is built and interface-checked outside the try, before the reservation
    row: misconfiguration raises, the ledger never books an attempt that never ran;
 14 main() fails fast when attempt_char_cap is absent from the config (DeepSeek F6).
 Still open by the lead's call (DeepSeek 2235): F1 record writer, F5 OCI adapter,
    F6 badjson-as-made, F7 retry policy.
"""
import argparse, hashlib, json, os, sys, time, uuid
from datetime import datetime, timezone

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def stable_slot(seed_id, n):
    return int(hashlib.sha256(seed_id.encode()).hexdigest(), 16) % n

class Attempt:  # reservation row, written before the call
    FIELDS = ["attempt_id","run_id","seed_id","model","status","input_chars",
              "output_chars","usage","started_utc","ended_utc","error"]
    def __init__(self, run_id, seed_id, model):
        self.attempt_id = f"{run_id}-{uuid.uuid4().hex[:8]}"
        self.run_id, self.seed_id, self.model = run_id, seed_id, model
        self.status = "pending"          # pending -> completed|failed|timeout|unknown
        self.input_chars = self.output_chars = 0
        self.usage = None
        self.started_utc, self.ended_utc, self.error = now(), None, None
    def row(self):
        return {k: getattr(self, k) for k in self.FIELDS}

class Ledger:
    """Append-only JSONL. Budget and attempt caps are computed from ALL rows, so a
    restart cannot reset the spend and a killed process leaves its pending row."""
    def __init__(self, path):
        self.path = path
    def append(self, a):
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(a.row(), ensure_ascii=False) + "\n")
            fh.flush(); os.fsync(fh.fileno())
    def rows(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                return [json.loads(l) for l in fh if l.strip()]
        except FileNotFoundError:
            return []
    def effective_rows(self):
        """One row per attempt: the closing row when it exists, else the pending
        reservation. Reservations and closes share attempt_id by design."""
        best = {}
        for r in self.rows():
            aid = r["attempt_id"]
            cur = best.get(aid)
            if cur is None or (cur["status"] == "pending" and r["status"] != "pending"):
                best[aid] = r
        return list(best.values())
    def spent_chars(self):
        # unknown/pending/failed/timeout all count: a call may be billable though
        # its response never landed (ChatGPT 1703, Instinct 2020).
        return sum(r["input_chars"] + r["output_chars"] for r in self.effective_rows()
                   if r["status"] in ("completed","failed","timeout","unknown","pending"))
    def attempts(self):
        return len(self.effective_rows())

def record_chars(text):  # exact count of billed characters, prompt+response
    # A byte-billed service counts UTF-8 bytes; in Thai/Arabic/Indonesian seeds
    # one code point is 2-3 bytes, so len() under-reserves the input term by up
    # to 3x. Read as bytes; the reply term is separately bounded by 113 x max_tokens.
    return len(text.encode("utf-8"))

def run_once(cfg, client_factory, seed, model_id, prompt, ledger, max_out_chars=None):
    """One attempt: reserve, call, close the row. Returns the row's final status."""
    a = Attempt(cfg["run_id"], seed["id"], model_id)
    a.input_chars = record_chars(prompt)
    if max_out_chars and a.input_chars + max_out_chars > cfg["attempt_char_cap"]:
        return None  # caller handles pre-call cap refusal
    # Build and interface-check the client OUTSIDE the try, before the reservation
    # row exists: a missing oci package, missing credentials or a client without
    # .generate is a misconfiguration, not possible spend, so it must raise instead
    # of being booked as "unknown" (Claude 2238 finding 5).
    client = client_factory()
    if not hasattr(client, "generate"):
        raise TypeError(
            f"client for {model_id} has no .generate(model_id, prompt, cfg); "
            "the real OCI path needs a chat()/generate_text() adapter (stage 1+)")
    # Reserve the reply term before the call. An attempt whose outcome is unknown
    # keeps this reservation; only a completed row settles it to the actual count.
    # (committed = settled if settled else reserved)
    a.output_chars = max_out_chars or 0
    ledger.append(a)                       # reservation BEFORE the call
    try:
        reply, usage = client.generate(model_id, prompt, cfg)
        a.output_chars = record_chars(reply)
        a.usage = usage
        a.status = "completed"; a.ended_utc = now()
    except TimeoutError:
        a.status = "timeout"; a.ended_utc = now(); a.error = "timeout"
    except Exception as e:
        # response may or may not have been billable: unknown, counted as spent
        a.status = "unknown"; a.ended_utc = now(); a.error = str(e)[:200]
    ledger.append(a)                       # closing row (supersedes pending)
    return a

class StubClient:
    """No key, no network. Scripted behaviors per call, from a test script."""
    def __init__(self, script):
        self.script = script  # SHARED list: attempts pop from it in order
    class Reply:
        def __init__(self, d): self._d = d
        def to_dict(self): return self._d
    def generate(self, model_id, prompt, cfg):
        if not self.script:
            raise RuntimeError("stub script exhausted")
        action = self.script.pop(0)
        if action == "ok":
            reply = '{"instruction": "Write a short haiku about rain.", "response": "Soft rain on the roof,\\n.each drop a small metronome,\\nthe night keeps its time."}'
            return reply, {"output_tokens": 42}
        if action == "timeout":
            raise TimeoutError("stub timeout")
        if action == "boom":          # unknown: billable-or-not undecidable
            raise RuntimeError("stub connection dropped mid-call")
        if action == "badjson":
            return "here is prose, no json at all", None
        raise ValueError("bad stub action")

def build_client(cfg):  # real path (stage 1+): region from config, validated on first call
    import oci
    conf = oci.config.from_file(profile_name=cfg.get("profile", "DEFAULT"))
    client = oci.generative_ai_inference.GenerativeAiInferenceClient(conf,
        service_endpoint=f"https://inference.generativeai.{cfg['region']}.oci.oraclecloud.com")
    return client

def pick_model(cfg, seed_id, ledger_rows):
    allowed = {m["model_id"] for m in cfg["models"]}
    if not allowed:
        raise ValueError("empty allow-list")
    for r in ledger_rows:               # deterministic resume: same seed, same model
        if r["seed_id"] == seed_id:
            if r["status"] == "pending":
                return None             # another worker holds this pair in flight
            if r["status"] in ("completed","unknown","timeout","failed"):
                return r["model"]
    ids = sorted(allowed)
    return ids[stable_slot(seed_id, len(ids))]

def load_seed_ids(out_path):
    done = set()
    try:
        with open(out_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line: continue
                try: done.add(json.loads(line)["id"])
                except (KeyError, json.JSONDecodeError): continue
    except FileNotFoundError:
        pass
    return done

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="gen_config.json")
    ap.add_argument("--max-attempts", type=int, default=0,
                    help="hard cumulative cap on raw attempts, read from the ledger")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = json.load(open(args.config))
    cfg["run_id"] = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    ledger = Ledger(cfg["ledger_path"])
    price = cfg.get("price_per_million_chars", 0.18)
    spent = ledger.spent_chars(); cost = spent / 1e6 * price
    attempts = ledger.attempts()
    if args.dry_run:
        print(f"dry run: ledger has {attempts} attempts, {spent} chars, est ${cost:.2f}")
        return
    if attempts and args.max_attempts and attempts >= args.max_attempts:
        print(f"cap already reached across runs: {attempts} >= {args.max_attempts}"); return
    budget_chars = cfg["budget_usd"] / price * 1e6
    # Bound the reply term before the call: the longest Llama-4 token decodes to
    # 113 characters (DeepSeek 2137), so a reply of max_tokens tokens is at most
    # 113 x max_tokens characters.
    max_out_chars = cfg.get("max_chars_per_token", 113) * cfg["max_tokens"] if cfg.get("max_tokens") else None
    if not cfg.get("attempt_char_cap"):
        sys.exit("config needs attempt_char_cap: the per-attempt reservation ceiling in "
                 "chars, at least input bytes + 113 x max_tokens (116,712 at 1,024 tokens)")
    seeds_done = load_seed_ids(cfg["out_path"])
    made = 0
    for seed in cfg_seeds(cfg):
        for k in range(seed.get("n_pairs", cfg.get("pairs_per_seed", 4))):
            # Rows are keyed by the bare seed id, and a closing row supersedes its
            # reservation: effective_rows(), not rows(), or the pending row wins.
            model_id = pick_model(cfg, seed["id"], ledger.effective_rows())
            if model_id is None:
                continue                    # another worker holds this seed in flight
            rec_id = f"{seed['id']}#{k+1}#{model_id.rsplit('.',1)[-1][:24]}"
            if rec_id in seeds_done: continue
            if args.max_attempts and ledger.attempts() >= args.max_attempts:
                break
            if ledger.spent_chars() + cfg["attempt_char_cap"] > budget_chars:
                break
            prompt = f"prompt for {seed['topic']} (pair {k+1})"
            a = run_once(cfg, lambda: build_client(cfg), seed, model_id, prompt, ledger,
                         max_out_chars=max_out_chars)
            if a and a.status == "completed":
                made += 1
    print(f"done: run {cfg['run_id']} made {made} pairs; ledger {ledger.attempts()} attempts, "
          f"{ledger.spent_chars()} chars, est ${ledger.spent_chars()/1e6*price:.2f}")

def cfg_seeds(cfg):
    return [json.loads(l) for l in open(cfg["seeds_path"]) if l.strip() and not l.startswith("#")]

if __name__ == "__main__":
    main()
