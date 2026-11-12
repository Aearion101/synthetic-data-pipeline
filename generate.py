#!/usr/bin/env python3
"""Synthetic instruction-pair generation on Oracle Cloud Generative AI (hosted, on-demand).

LEGACY (Instinct 1051): no execution_status gate, no ledger, no attempt cap, no
hold_reason, an unstable hash()-based model pick, and it prices only after the
reply returns. Do NOT run it on pack rows: the tested entry point is
runner_v2.py.

Output is model-neutral: one plain JSONL instruction pair per line, no chat template baked
in. Tune-time loaders own formatting, so the same file feeds any model. Languages come from
the seed corpus; nothing here narrows them.

DISABLED (Instinct 1147, lead go): every invocation exits with a refusal before any auth or
network call; --dry-run is included in the refusal and there is no --legacy-explicit escape.
Kept only as a historical reference. The tested entry point is runner_v2.py (README.md).

Auth is the standard OCI SDK path (~/.oci/config, OCI_CLI_* env vars, or instance
principals). Oracle bills hosted GenAI per character, so the script counts sent + received
characters, prices them against the per-million rate in the config, and stops cleanly once
the budget is spent.

Multi-worker safety:
  * A worker CLAIMS a record before its first (billable) call. Claims live in a claims/
    directory as one small JSON file per record; a second worker's claim of the same
    record is refused, so two workers can never bill the same seed.
  * Claims are leases. A crashed run leaves its claim behind, but the lease expires
    (lease_ttl_s, default 900s) and the next worker takes over; nothing blocks forever.
  * Spend is one shared ledger file (flock-guarded), so N workers bill against one
    budget, not N budgets.

Every record carries full provenance (model id, prompt template, parameters, timestamp) so
the license side stays clean: Cohere-generated data is fine for the open release, and any
public tune built on Llama-generated data has to carry "Llama" in its name (Meta license).
Grok stays out of this pipeline by decision: xAI's terms bar helping rivals.
"""
import argparse
import fcntl
import json
import os
import random
import re
import sys
import time
import zlib
from datetime import datetime, timezone


def legacy_refusal():
    """Instinct 1147: generate.py is legacy and disabled. Every invocation
    exits here, before any auth or network call and before the config is even
    read; --dry-run is included in the refusal, and there is no
    --legacy-explicit escape (legacy billing is not needed for the current
    task). The tested entry point is runner_v2.py."""
    sys.stderr.write(
        "generate.py is legacy and disabled: no execution_status gate, no ledger, "
        "no attempt cap, no hold_reason, and it prices only after the reply returns. "
        "Do not run it on pack rows. The tested entry point is runner_v2.py (see "
        "README.md); run the dry-run against runner_v2.py instead.\n")
    sys.exit(2)

try:
    import oci
except ImportError:
    oci = None  # tests import this module without the SDK; the run itself checks below

import prompts as P

# --------------------------------------------------------------------------- helpers

def load_seeds(path):
    seeds = []
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            rec = json.loads(line)
            if "topic" not in rec:
                raise ValueError(f"seeds line {i+1} has no topic")
            rec.setdefault("id", f"seed-{i+1:05d}")
            seeds.append(rec)
    return seeds

def load_seeded_ids(out_path):
    """Ids already in the output file, so a rerun resumes instead of duplicating."""
    done = set()
    try:
        with open(out_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    done.add(json.loads(line)["id"])
                except (KeyError, json.JSONDecodeError):
                    continue
    except FileNotFoundError:
        pass
    return done

def extract_text(data):
    """Read the reply text out of an OCI response, tolerating SDK shape differences."""
    d = data.to_dict() if hasattr(data, "to_dict") else {}
    found = []
    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in ("text", "content") and isinstance(v, str) and v.strip():
                    found.append(v)
                else:
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(d)
    if not found:
        raise RuntimeError("no text found in reply")
    return max(found, key=len)

def parse_pair(text):
    """generate.py's parse: the first brace candidate that validates as the
    FULL outer pair (instruction and response keys), both fields non-empty.
    Tolerant (strict=False): literal line feeds inside string values parse.

    Instinct 0646 hardening (reviewer findings on 964c79c, both copies in
    sync):
    1. A reply whose preamble contains a brace (or a valid non-pair JSON
       object) no longer fails as billed-unparseable when a real pair exists
       later: every '{' is a candidate, and the first candidate that decodes
       AND validates as the full outer pair wins. A candidate that merely
       decodes does not count.
    2. A dict or list response is stored as JSON text (json.dumps,
       ensure_ascii=False), never Python repr; a non-string instruction, or a
       response that is neither string nor dict/list, is a bad pair shape."""
    import re
    dec = json.JSONDecoder(strict=False)
    last_err = ValueError("no JSON object in reply")
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
        except json.JSONDecodeError as e:
            last_err = e
            continue
        # A candidate counts only when it validates as the full outer pair:
        # dict, non-empty string instruction, and a response that is a
        # non-empty string or a non-empty dict/list (Instinct 0646 fix 1).
        if not isinstance(obj, dict):
            last_err = ValueError("bad pair shape")
            continue
        instr = obj.get("instruction", "")
        resp = obj.get("response", "")
        if not isinstance(instr, str) or not instr.strip():
            last_err = ValueError("bad pair shape")  # Instinct 0646: non-string instruction rejected
            continue
        if isinstance(resp, str):
            if not resp.strip():
                last_err = ValueError("bad pair shape")
                continue
            return instr.strip(), resp.strip()
        if isinstance(resp, (dict, list)):
            if not resp:
                last_err = ValueError("bad pair shape")
                continue
            # JSON text, not Python repr (Instinct 0646 fix 2).
            return instr.strip(), json.dumps(resp, ensure_ascii=False)
        last_err = ValueError("bad pair shape")
    raise last_err

def call_model(client, cfg, model_id, prompt):
    """One inference call. Cohere rides chat(); Llama rides generate_text()."""
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
            return extract_text(client.chat(detail).data)
        return extract_text(client.generate_text(detail).data)
    except oci.exceptions.ServiceError as e:
        if e.status == 429:
            raise Throttled(str(e))
        raise

class Throttled(Exception):
    pass

def round_robin(models):
    """Expand model weights into a cycling list: [{w:3},{w:1}] -> m0,m0,m0,m1,m0,m0,..."""
    seq = []
    for m in models:
        seq.extend([m["model_id"]] * max(1, int(m.get("weight", 1))))
    return seq

def pick_model(rr, rec_id, k):
    """Deterministic across processes (hash() is randomized; crc32 is not)."""
    return rr[zlib.crc32(f"{rec_id}:{k}".encode()) % len(rr)]

# --------------------------------------------------------------------------- claims (leases)

def claims_dir_for(cfg, out_path):
    base = cfg.get("state_path") or os.path.dirname(os.path.abspath(out_path))
    return os.path.join(base, "claims")

def _claim_id(rec_id):
    return rec_id.replace("/", "_")

def claim_path(claims_dir, rec_id):
    return os.path.join(claims_dir, _claim_id(rec_id) + ".claim")

def _claim_lock(claims_dir, rec_id):
    """Every claim operation on a record serializes on this lock file, so two workers
    can never both hold a record even when they race (NFS-safe flock)."""
    os.makedirs(claims_dir, exist_ok=True)
    return open(os.path.join(claims_dir, "." + _claim_id(rec_id) + ".lock"), "a+")

def _claim_write(path, worker_id):
    tmp = path + f".tmp{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump({"worker": worker_id, "ts": time.time()}, f)
    os.replace(tmp, path)

def claim_acquire(claims_dir, rec_id, worker_id, ttl_s=900.0):
    """True iff THIS worker now holds the lease on rec_id.

    A live lease (written within ttl_s) is refused, so two workers can never bill the
    same record. An EXPIRED lease - a run that crashed and never released - is taken
    over by the next claimer, so a dead run blocks nothing forever.
    """
    path = claim_path(claims_dir, rec_id)
    with _claim_lock(claims_dir, rec_id) as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            with open(path) as f:
                d = json.load(f)
            live = time.time() - float(d.get("ts", 0)) <= ttl_s
        except (OSError, ValueError):
            live = False
        if live:
            return False
        _claim_write(path, worker_id)
        return True

def claim_release(claims_dir, rec_id, worker_id):
    path = claim_path(claims_dir, rec_id)
    with _claim_lock(claims_dir, rec_id) as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            with open(path) as f:
                d = json.load(f)
            if d.get("worker") == worker_id:
                os.unlink(path)
        except (OSError, ValueError):
            pass

def claim_touch(claims_dir, rec_id, worker_id):
    """Renew the lease (long retry backoffs must not let it lapse mid-work)."""
    path = claim_path(claims_dir, rec_id)
    with _claim_lock(claims_dir, rec_id) as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            with open(path) as f:
                d = json.load(f)
            if d.get("worker") != worker_id:
                return
            d["ts"] = time.time()
            with open(path, "w") as f:
                json.dump(d, f)
        except (OSError, ValueError):
            pass

# --------------------------------------------------------------------------- shared budget

def spend_path_for(cfg, out_path):
    base = cfg.get("state_path") or os.path.dirname(os.path.abspath(out_path))
    return cfg.get("spend_path") or os.path.join(base, "spend_chars.txt")

def spend_read(path):
    try:
        with open(path) as f:
            return float(f.read().strip() or 0)
    except FileNotFoundError:
        return 0.0

def spend_add(path, n_chars):
    """Add chars to the shared ledger under an exclusive lock; returns the new total."""
    with open(path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        total = float(f.read().strip() or 0)
        total += n_chars
        f.seek(0)
        f.truncate()
        f.write(str(total))
        f.flush()
        os.fsync(f.fileno())
        fcntl.flock(f, fcntl.LOCK_UN)
    return total

def budget_ok(chars, est_next_chars, price, budget):
    return (chars + est_next_chars) / 1e6 * price < budget

# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--config", default="gen_config.json")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="cap on pairs generated this run")
    args = ap.parse_args()

    cfg = json.load(open(args.config))
    seeds = load_seeds(cfg["seeds_path"])
    out_path = cfg["out_path"]
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    done = load_seeded_ids(out_path)
    rr = round_robin(cfg["models"])
    price = cfg.get("price_per_million_chars", 0.18)
    budget = cfg.get("budget_usd", 300.0)
    gap = 60.0 / cfg.get("requests_per_minute", 12)
    ttl = float(cfg.get("lease_ttl_s", 900))
    claims_dir = claims_dir_for(cfg, out_path)
    spend_path = spend_path_for(cfg, out_path)
    worker_id = f"{os.uname().nodename}:{os.getpid()}:{int(time.time())}"
    n_max = args.limit if args.limit else 10**9

    if args.dry_run:
        calls = sum(s.get("n_pairs", cfg.get("pairs_per_seed", 4)) for s in seeds)
        print(f"dry run: {len(seeds)} seeds, {calls} planned calls, {len(rr)} model slots")
        return

    if oci is None:
        sys.exit("pip install oci first")
    if cfg.get("compartment_id", "SET_ME") == "SET_ME":
        sys.exit("set compartment_id in the config first")

    client = oci.generative_ai_inference.GenerativeAiInferenceClient(
        oci.config.from_file(profile_name=cfg.get("profile", "DEFAULT")))

    chars = spend_read(spend_path)
    cost = chars / 1e6 * price
    made = 0
    print(f"[{worker_id}] resuming: {len(done)} done, ${cost:.2f} already spent of ${budget:.0f}")
    with open(out_path, "a", encoding="utf-8") as out:
        for seed in seeds:
            if made >= n_max or cost >= budget:
                break
            for k in range(seed.get("n_pairs", cfg.get("pairs_per_seed", 4))):
                model_id = pick_model(rr, seed["id"], k)
                rec_id = f"{seed['id']}#{k+1}#{model_id.rsplit('.', 1)[-1][:24]}"
                if rec_id in done:
                    continue
                if cost >= budget or made >= n_max:
                    break
                tname = P.pick_template(cfg, seed["id"], k)
                prompt = P.TEMPLATES[tname](seed, k)
                est_next = len(prompt) + int(cfg["max_tokens"]) * 4
                if not budget_ok(chars, est_next, price, budget):
                    print(f"budget guard: ${cost:.2f} + next call would pass ${budget:.0f}, stopping")
                    break
                if not claim_acquire(claims_dir, rec_id, worker_id, ttl):
                    continue  # another worker holds (or races) this record
                for attempt in range(cfg.get("max_retries", 4) + 1):
                    try:
                        reply = call_model(client, cfg, model_id, prompt)
                        break
                    except Throttled:
                        wait = 15 * (attempt + 1)
                        print(f"throttled, waiting {wait}s")
                        claim_touch(claims_dir, rec_id, worker_id)
                        time.sleep(wait)
                    except Exception as e:
                        if attempt == cfg.get("max_retries", 4):
                            print(f"SKIP {rec_id}: {e}")
                            reply = None
                            break
                        claim_touch(claims_dir, rec_id, worker_id)
                        time.sleep(2 ** attempt)
                if reply is None:
                    claim_release(claims_dir, rec_id, worker_id)
                    continue
                n_chars = len(prompt) + len(reply)
                chars = spend_add(spend_path, n_chars)
                cost = chars / 1e6 * price
                try:
                    instr, resp = parse_pair(reply)
                except (ValueError, json.JSONDecodeError) as e:
                    print(f"SKIP {rec_id}: parse: {e}")
                    claim_release(claims_dir, rec_id, worker_id)
                    continue
                if len(instr) < 15 or len(resp) < 15:
                    print(f"SKIP {rec_id}: too short")
                    claim_release(claims_dir, rec_id, worker_id)
                    continue
                rec = {
                    "id": rec_id,
                    "instruction": instr,
                    "response": resp,
                    "source": "synthetic",
                    "generator": {
                        "model": model_id,
                        "template": tname,
                        "temperature": cfg["temperature"],
                        "max_tokens": cfg["max_tokens"],
                    },
                    "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                out.flush()
                os.fsync(out.fileno())
                done.add(rec_id)
                claim_release(claims_dir, rec_id, worker_id)
                made += 1
                print(f"[{made}] {rec_id} chars={chars} est=${cost:.2f} of ${budget:.0f}")
                time.sleep(gap)

    print(f"done: {made} pairs, {chars} chars, est ${cost:.2f} of ${budget:.0f} -> {out_path}")

if __name__ == "__main__":
    legacy_refusal()   # Instinct 1147: refuses before any auth or network call;
                       # main() below is kept as a historical reference only.
