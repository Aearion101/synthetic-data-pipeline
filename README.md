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

    # test suite (no key, no network): T22 needs the oci SDK installed; without
    # it the SDK-shape test reports sdk_ok=None and fails (Instinct 0346)
    python3 test_stage0.py

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
- Tolerant JSON parsing: the pair parser (parse_pair, both runner_v2.py and
  generate.py) uses json.JSONDecoder(strict=False), so a reply with a literal line
  break inside a string value (code blocks, lists, dialogue) parses where a strict
  parser would raise; json.dumps re-escapes output on the way out (Claude 0341 F1,
  Instinct 0346/0349).
- Budget-aware: counts sent + received characters and prices them against the config rate
  (cheap Llama is about $0.18 per million characters), printing a running estimate and
  stopping at the configured budget.
- Rate-limit friendly: paces calls at `requests_per_minute` and backs off hard on 429s.
- Dedupe: `dedupe.py` drops exact and near-exact duplicates before anything eats the data.
- Source-script scan after generation: `scan_offlist.py` (Instinct 0545 ruling on
  Claude 0535) runs BEFORE any pair enters a tune. The pack's quoted-source wrapper
  line sits on all rows, including non-translation rows where the model writes the
  source passage itself, so prompt-time flags cannot see an off-list quotation there.
  It joins each pair id to its pack row by the base id before the first `#`, skips
  rows whose language is zh/ja/ko/ru (an off-list passage is expected there), and
  reports Han/Kana/Hangul/Cyrillic hits on every other language, Polish included
  (Latin script; its native-speaker QC in the pack README stays separate). A hit is
  a decision input with counts, not a defect verdict: the wrapper line makes
  off-list quoting legal on any row. Usage: `python3 scan_offlist.py --pairs
  data/batch_001.jsonl --pack pack/v2.1/bulk_prompts_v2.1.onestage.train.jsonl`
  (offline, no key, no model call; `--fail-on-hit` exits 1 on findings). Coverage
  limit: only those four script ranges, so an off-list Latin-script source (Polish
  or any other) quoted inside a non-pl row is not visible to it. Tests: T33.

Note: `pack/v2.1/README.md` is the producer's file, checked in byte-identical to its
Dropbox original (Instinct 0446/0447), so this limit note lives here and in
`scan_offlist.py`'s docstring rather than inside that file.

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
