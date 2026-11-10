# synthetic-data-pipeline

Synthetic instruction-pair generation on Oracle Cloud Generative AI (hosted, on-demand),
paid from the $300 promo credits (expire Nov 15, 2026). The output is model-neutral: plain
JSONL instruction pairs, no chat template baked in, languages whatever the seed corpus
says. Format conversion happens at tune time, not at generation.

## Quickstart

    pip install oci
    cp gen_config.example.json gen_config.json   # set compartment_id
    # seeds.jsonl: one {"id","topic"} per line; see seeds.example.jsonl
    python3 generate.py --config gen_config.json --dry-run
    python3 generate.py --config gen_config.json
    python3 dedupe.py data/batch_001.jsonl data/batch_001.clean.jsonl

Auth is the standard OCI SDK path (~/.oci/config, OCI_CLI_* env vars, or instance
principals). Execution is gated on Dylan handing the OCI keys via secure link; nobody
creates new accounts or keys.

## What the script does

- One call per training pair: a prompt template turns a seed topic into one
  instruction/response pair, and the reply is parsed, validated, and appended to the JSONL.
- Model-neutral records: `{"id", "instruction", "response", "source", "generator",
  "created_utc"}`, with the model id, template, temperature, and timestamp on every record
  so provenance stays clean end to end.
- Resumable: a rerun skips pair ids already in the output file, so an interrupted batch
  continues instead of duplicating.
- Budget-aware: counts sent + received characters and prices them against the config rate
  (cheap Llama is about $0.18 per million characters), printing a running estimate and
  stopping at the configured budget.
- Rate-limit friendly: paces calls at `requests_per_minute` and backs off hard on 429s.
- Dedupe: `dedupe.py` drops exact and near-exact duplicates before anything eats the data.

Cost math: at $0.18/M characters, $300 buys roughly 1.6 billion characters, several
hundred million tokens of output. Embeddings run about 10x cheaper.

## License and provenance notes

- Oracle AI Terms (rev 2026-09-16): outputs may train other AI systems; you own your
  output. The remaining line is not attempting to reconstruct the models' training data.
- Third-party terms stack on top: Cohere is clean for the planned open release; Llama data
  is fine but a public tune built on Llama-generated outputs carries "Llama" in its name;
  Grok stays out of this pipeline (xAI terms bar helping rivals).
- No HPE work data goes through the account, ever (company policy).
- GPU compute is blocked on this account for 90 days, so everything here rides hosted
  inference.

## Storage plan

Working copy in S3, mirror copy in Google Drive, mirror after each batch so a bad push
cannot lose a day of work; GitHub carries the set only when it is ready to release.

## Multi-worker safety (v2)

- **Claim-before-call.** A worker claims a record before its first billable call. Claims
  live in `claims/` next to the output as one small JSON file per record. A second
  worker's claim of a live record is refused, so two workers can never bill the same seed.
- **Expired leases recover themselves.** A crashed run leaves its claim behind, but the
  lease lapses (`lease_ttl_s`, default 900s) and the next claimer takes over. Nothing
  blocks forever. Long retry backoffs renew the lease with `claim_touch`.
- **One shared budget.** Spend accumulates in one flock-guarded ledger
  (`spend_chars.txt`) read at startup, so N workers bill against one budget, not N.
  A pre-call guard estimates the next call and stops before the budget passes.
- **Deterministic plans.** Model choice and template choice hash the record id (crc32,
  not Python's randomized `hash`), so parallel workers and reruns agree on the plan.

## Prompts

`prompts.py` holds four templates; choice is deterministic per record and the weights are
the config's `templates` list (defaults to all four):

- `pair-from-topic-v1` plain instruction + response, self-contained
- `pair-constraints-v1` adds audience, length band, and format constraints (seeded per record)
- `pair-why-v1` direct answer plus a "Why this works:" reasoning paragraph
- `pair-edge-v1` misconception / tradeoff / scale-stress angles for harder pairs

`make_seeds.py` builds the starter corpus (100 topics across 10 domains, optional
language variants, `--langs en,id,es`, no API calls) into `seeds.jsonl`.

## Smoke test path

```
python3 make_seeds.py --out seeds.jsonl
python3 generate.py --config gen_config.json --limit 20     # first paid calls, ~20 pairs
python3 dedupe.py --in <out>                                # quality gate
```
