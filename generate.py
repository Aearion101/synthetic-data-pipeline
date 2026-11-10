#!/usr/bin/env python3
"""Synthetic instruction-pair generation on Oracle Cloud Generative AI (hosted, on-demand).

Output is model-neutral: one plain JSONL instruction pair per line, no chat template baked
in. Tune-time loaders own formatting, so the same file feeds any model. Languages come from
the seed corpus; nothing here narrows them.

    pip install oci
    python3 generate.py --config gen_config.json [--dry-run] [--limit N]

Auth is the standard OCI SDK path (~/.oci/config, OCI_CLI_* env vars, or instance
principals). Oracle bills hosted GenAI per character, so the script counts sent + received
characters, prices them against the per-million rate in the config, and stops cleanly once
the budget is spent.

Every record carries full provenance (model id, prompt template, parameters, timestamp) so
the license side stays clean: Cohere-generated data is fine for the open release, and any
public tune built on Llama-generated data has to carry "Llama" in its name (Meta license).
Grok stays out of this pipeline by decision: xAI's terms bar helping rivals.
"""
import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone

try:
    import oci
except ImportError:
    sys.exit("pip install oci first")

# --------------------------------------------------------------------------- prompts

TEMPLATES = {}

def template(name):
    def deco(fn):
        TEMPLATES[name] = fn
        return fn
    return deco

@template("pair-from-topic-v1")
def pair_from_topic_v1(topic):
    return (
        "You are writing one training example for an instruction-tuning dataset.\n"
        f"Topic: {topic}\n\n"
        "Write ONE instruction and its response. The instruction must be self-contained and "
        "answerable without any document or link. The response must be correct, complete, and "
        "written in plain prose.\n"
        'Reply with ONLY a JSON object, no markdown fences, exactly:\n'
        '{"instruction": "...", "response": "..."}'
    )

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
    m = re.search(r"\{", text)
    if not m:
        raise ValueError("no JSON object in reply")
    # strict=False (Claude 0341 F1, Instinct 0346/0349 ruling): tolerant
    # decoding accepts JSON a strict parser would reject; json.dumps
    # re-escapes output on the way out.
    obj, _ = json.JSONDecoder(strict=False).raw_decode(text[m.start():])
    if not (isinstance(obj, dict) and str(obj.get("instruction", "")).strip()
            and str(obj.get("response", "")).strip()):
        raise ValueError("bad pair shape")
    return str(obj["instruction"]).strip(), str(obj["response"]).strip()

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
    n_max = args.limit if args.limit else 10**9

    if args.dry_run:
        calls = sum(s.get("n_pairs", cfg.get("pairs_per_seed", 4)) for s in seeds)
        print(f"dry run: {len(seeds)} seeds, {calls} planned calls, {len(rr)} model slots")
        return

    if cfg.get("compartment_id", "SET_ME") == "SET_ME":
        sys.exit("set compartment_id in the config first")

    client = oci.generative_ai_inference.GenerativeAiInferenceClient(
        oci.config.from_file(profile_name=cfg.get("profile", "DEFAULT")))

    chars = 0
    cost = 0.0
    made = 0
    with open(out_path, "a", encoding="utf-8") as out:
        for seed in seeds:
            if made >= n_max or cost >= budget:
                break
            for k in range(seed.get("n_pairs", cfg.get("pairs_per_seed", 4))):
                model_id = rr[(seed["id"].__hash__() + k) % len(rr)]
                rec_id = f"{seed['id']}#{k+1}#{model_id.rsplit('.', 1)[-1][:24]}"
                if rec_id in done:
                    continue
                if cost >= budget or made >= n_max:
                    break
                prompt = TEMPLATES[cfg.get("template", "pair-from-topic-v1")](seed["topic"])
                for attempt in range(cfg.get("max_retries", 4) + 1):
                    try:
                        reply = call_model(client, cfg, model_id, prompt)
                        break
                    except Throttled:
                        wait = 15 * (attempt + 1)
                        print(f"throttled, waiting {wait}s")
                        time.sleep(wait)
                    except Exception as e:
                        if attempt == cfg.get("max_retries", 4):
                            print(f"SKIP {rec_id}: {e}")
                            reply = None
                            break
                        time.sleep(2 ** attempt)
                if reply is None:
                    continue
                n_chars = len(prompt) + len(reply)
                chars += n_chars
                cost = chars / 1e6 * price
                try:
                    instr, resp = parse_pair(reply)
                except (ValueError, json.JSONDecodeError) as e:
                    print(f"SKIP {rec_id}: parse: {e}")
                    continue
                if len(instr) < 15 or len(resp) < 15:
                    print(f"SKIP {rec_id}: too short")
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
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                out.flush()
                done.add(rec_id)
                made += 1
                print(f"[{made}] {rec_id} chars={chars} est=${cost:.2f} of ${budget:.0f}")
                time.sleep(gap)

    print(f"done: {made} pairs, {chars} chars, est ${cost:.2f} of ${budget:.0f} -> {out_path}")

if __name__ == "__main__":
    main()
