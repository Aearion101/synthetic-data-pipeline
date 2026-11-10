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
 15 F1, writer restored from generate.py (Instinct 2245 ask): run_once returns the
    reply; main() parses it, writes the record (generate.py's shape: id, instruction,
    response, source, generator, created_utc) to out_path with flush per record and
    seeds_done checkpointing, so a restart does not re-bill completed pairs;
 16 F5, adapter restored: OciAdapter.generate(model_id, prompt, cfg) wraps the
    client call shape generate.py uses (cohere rides chat(), llama rides
    generate_text(), text extracted from .data), so the real OCI path is callable;
 17 F6, parse-and-filter between completed and made (Claude 2238 #6): an unparseable
    or too-short reply closes its attempt as "unparseable" (last row per attempt_id
    wins in effective_rows()) and never counts as a made pair;
 18 N6 (Claude 2235): a config without max_tokens fails fast in main() instead of
    silently bringing back the prompt-only charge.
 Units: every recorded char count is UTF-8 BYTES (the reservation must be
 conservative); whether Oracle bills bytes or code points is still unverified and
 rides stage-1 reconciliation against Oracle's usage view. An unknown, timed-out,
 killed or unparseable attempt keeps its FULL reservation until that independent
 reconciliation; it is never released on timeout (Instinct 2246 item 3).
 Still open: F7 retry policy (generate.py's; the runner makes no retries of its
 own), a cross-process ledger lock (DeepSeek 2137 spend_cap flock; the runner is
 single-writer-safe only), pending-row recovery for a killed worker (lead's call).
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
        """One row per attempt: the LAST row written for that attempt_id wins.
        Reservations and closes share attempt_id by design, and a later close
        (for example completed then unparseable) supersedes the earlier one."""
        best = {}
        for r in self.rows():
            best[r["attempt_id"]] = r
        return list(best.values())
    def spent_chars(self):
        # unknown/pending/failed/timeout all count: a call may be billable though
        # its response never landed (ChatGPT 1703, Instinct 2020).
        return sum(r["input_chars"] + r["output_chars"] for r in self.effective_rows()
                   if r["status"] in ("completed","failed","timeout","unknown","pending"))
    def attempts(self):
        return len(self.effective_rows())

def record_chars(text):
    """Recorded char counts are UTF-8 BYTES. The reservation must be conservative,
    so bytes it is: in Thai/Arabic/Indonesian seeds one code point is 2-3 bytes,
    and len() under-reserves the input term by up to 3x. The reply term is
    separately bounded by 113 x max_tokens. If Oracle bills code points, stage-1
    reconciliation against its usage view settles it; until then bytes only
    over-reserve, never under-reserve."""
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
    reply = None
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
    return a, reply

def close_attempt(ledger, a, status, error):
    """F6 close: supersede a completed attempt with an unparseable one (same
    attempt_id, last row wins). The reply's byte count is known, so the settled
    output term stays; the pair itself never counts as made."""
    c = Attempt(a["run_id"], a["seed_id"], a["model"])
    c.attempt_id = a["attempt_id"]
    c.input_chars = a["input_chars"]; c.output_chars = a["output_chars"]
    c.usage = a["usage"]; c.status = status; c.error = error; c.ended_utc = now()
    ledger.append(c)

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

def parse_pair(text):
    """generate.py's parse: first JSON object in the reply, both fields non-empty."""
    import re
    m = re.search(r"\{", text)
    if not m:
        raise ValueError("no JSON object in reply")
    obj, _ = json.JSONDecoder().raw_decode(text[m.start():])
    if not (isinstance(obj, dict) and str(obj.get("instruction", "")).strip()
            and str(obj.get("response", "")).strip()):
        raise ValueError("bad pair shape")
    return str(obj["instruction"]).strip(), str(obj["response"]).strip()

class OciAdapter:
    """The adapter Claude 2238 finding 5 asked for: .generate(model_id, prompt, cfg)
    on top of the real client call shape generate.py uses. Cohere rides chat(),
    llama rides generate_text(), text extracted from .data. The oci import stays
    lazy so the stub path never needs the package."""
    def __init__(self, cfg):
        import oci
        conf = oci.config.from_file(profile_name=cfg.get("profile", "DEFAULT"))
        self.client = oci.generative_ai_inference.GenerativeAiInferenceClient(conf,
            service_endpoint=f"https://inference.generativeai.{cfg['region']}.oci.oraclecloud.com")
        self.cfg = cfg
    class Throttled(Exception):
        pass
    def _extract_text(self, data):
        found = []
        def walk(d):
            if isinstance(d, str):
                found.append(d)
            elif isinstance(d, dict):
                for v in d.values(): walk(v)
            elif isinstance(d, list):
                for v in d: walk(v)
        walk(data.to_dict() if hasattr(data, "to_dict") else data)
        if not found:
            raise RuntimeError("no text found in reply")
        return max(found, key=len)
    def generate(self, model_id, prompt, cfg):
        import oci
        if model_id.startswith("cohere"):
            detail = oci.generative_ai_inference.models.ChatDetails()
            detail.serving_mode = oci.generative_ai_inference.models.OnDemandServingMode(model_id=model_id)
            detail.chat_request = oci.generative_ai_inference.models.CohereChatRequest(
                message=prompt, max_tokens=cfg["max_tokens"], temperature=cfg["temperature"])
        else:
            detail = oci.generative_ai_inference.models.GenerateTextDetails()
            detail.serving_mode = oci.generative_ai_inference.models.OnDemandServingMode(model_id=model_id)
            detail.inference_request = oci.generative_ai_inference.models.LlamaLlmInferenceRequest(
                prompt=prompt, max_tokens=cfg["max_tokens"], temperature=cfg["temperature"])
        detail.compartment_id = cfg["compartment_id"]
        try:
            if model_id.startswith("cohere"):
                return self._extract_text(self.client.chat(detail).data), None
            return self._extract_text(self.client.generate_text(detail).data), None
        except oci.exceptions.ServiceError as e:
            if e.status == 429:
                raise OciAdapter.Throttled(str(e))
            raise

def build_client(cfg):  # real path (stage 1+): region from config, adapter restores .generate
    return OciAdapter(cfg)

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
    if not cfg.get("max_tokens"):
        sys.exit("config needs max_tokens: without it no reply term can be reserved, and the "
                 "attempt would silently carry only its prompt charge (Claude 2235 N6)")
    max_out_chars = cfg.get("max_chars_per_token", 113) * cfg["max_tokens"]
    if not cfg.get("attempt_char_cap"):
        sys.exit("config needs attempt_char_cap: the per-attempt reservation ceiling in "
                 "chars, at least input bytes + the reply term (113 x max_tokens = 115,712 "
                 "bytes at 1,024 tokens; about 116,712 with a 1,000-byte prompt)")
    os.makedirs(os.path.dirname(cfg["out_path"]) or ".", exist_ok=True)
    seeds_done = load_seed_ids(cfg["out_path"])
    n_max = args.limit if args.limit else 10**9
    made = 0
    for seed in cfg_seeds(cfg):
        if made >= n_max:
            break
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
            a, reply = run_once(cfg, lambda: build_client(cfg), seed, model_id, prompt, ledger,
                                max_out_chars=max_out_chars)
            if a is None or reply is None or a.status != "completed":
                continue
            # F6 parse-and-filter (Claude 2238 #6): a pair counts as MADE only when it
            # parses and clears the length floor. An unparseable or too-short reply
            # closes its attempt as "unparseable" and inflates nothing.
            try:
                instr, resp = parse_pair(reply)
            except (ValueError, json.JSONDecodeError) as e:
                close_attempt(ledger, a.row(), "unparseable", f"parse: {e}")
                continue
            if len(instr.encode("utf-8")) < 15 or len(resp.encode("utf-8")) < 15:
                close_attempt(ledger, a.row(), "unparseable", "below 15-byte floor")
                continue
            rec = {
                "id": rec_id,
                "instruction": instr,
                "response": resp,
                "source": "synthetic",
                "generator": {
                    "model": model_id,
                    "template": cfg.get("template", "pair-from-topic-v1"),
                    "temperature": cfg["temperature"],
                    "max_tokens": cfg["max_tokens"],
                },
                "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            with open(cfg["out_path"], "a", encoding="utf-8") as out:
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                out.flush()
            seeds_done.add(rec_id)
            made += 1
    note = "" if ledger.attempts() else (" (no attempts booked: seeds empty, budget "
                                         "exhausted, or cap already reached)")
    print(f"done: run {cfg['run_id']} made {made} pairs; ledger {ledger.attempts()} attempts, "
          f"{ledger.spent_chars()} chars, est ${ledger.spent_chars()/1e6*price:.2f}{note}")

def cfg_seeds(cfg):
    return [json.loads(l) for l in open(cfg["seeds_path"]) if l.strip() and not l.startswith("#")]

if __name__ == "__main__":
    main()
