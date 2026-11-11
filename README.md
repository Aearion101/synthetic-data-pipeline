# synthetic-data-pipeline

Synthetic instruction-pair generation on Oracle Cloud Generative AI (hosted, on-demand),
paid from the $300 promo credits (expire Nov 15, 2026). The output is model-neutral: plain
JSONL instruction pairs, no chat template baked in, languages whatever the seed corpus
says. Format conversion happens at tune time, not at generation.

## Quickstart

    pip install oci
    cp gen_config.example.json gen_config.json   # set compartment_id
    # seeds.jsonl: one {"id","topic"} per line; see seeds.example.jsonl
    python3 runner_v2.py --config gen_config.json --dry-run
    python3 runner_v2.py --config gen_config.json --max-attempts N --limit N
    python3 dedupe.py data/batch_001.jsonl data/batch_001.clean.jsonl

    # test suite (no key, no network): T22 needs the oci SDK installed; without
    # it the SDK-shape test reports sdk_ok=None and fails (Instinct 0346)
    python3 test_stage0.py

generate.py is legacy and NOT for pack rows: it has no execution_status gate,
no ledger, no attempt cap, no hold_reason, an unstable hash()-based model pick,
prices the call only after the reply returns, and builds prompts from seed
topics instead of the pack's rendered prompts (Instinct 1051). The tested
entry point is runner_v2.py.

## Calibration (self-run, not activated)

A calibration run stays unactivated until the owner says go: nothing in this
section calls Oracle, marks any row eligible, or collects a secret. The
example is `gen_config.calibration.example.json`: region us-chicago-1, Llama 4
Maverick only. Maverick and Scout are on-demand in us-chicago-1; Ashburn is
dedicated-only for them (Oracle's Models-by-Region table).

1. `pip install oci`
2. The OCI profile is the owner's step: create `~/.oci/config` (profile
   "DEFAULT") following Oracle's SDK config documentation. Keys never travel
   through chat or Dropbox.
3. Build the eligible seeds file. This is the approval step: only after the
   owner says go. Keep the WHOLE row and set the status explicitly; do not
   project. A projection like {id, topic} (a) drops the rendered prompt, so
   the runner refuses every row under require_rendered_prompt, (b) drops the
   hold_reason source fields (support, off_list_source_language), so the
   language-scope hold would never fire even though about one row in seven
   of the pack is a hold, and (c) drops execution_status itself, and the
   runner treats a MISSING execution_status as eligible, so dropping the
   field approves every emitted row by accident (Claude 1134).
       jq -c 'select(.execution_status=="pending_user_approval") | .execution_status="eligible"' pack/v3.1-rev3/bulk_prompts_v3.1-rev3.onestage.train.jsonl | head -n N > seeds_calib.jsonl
   (pack/v2.1/bulk_prompts_v2.1.onestage.train.jsonl works the same way.)
4. `python3 runner_v2.py --config gen_config.calibration.example.json --dry-run`
5. Then the run:
   `python3 runner_v2.py --config gen_config.calibration.example.json --max-attempts N --limit N`
   Owner-set values are validated before auth or any network call: an unset
   compartment_id (placeholder SET_ME), budget_usd, attempt_char_cap, or a
   missing seeds file stops the run with a named config error, never a bare
   KeyError.
   Cap sizing: each attempt reserves prompt bytes + prompt_overhead_chars
   (default 200) + 113 x max_tokens (115,712 at 1,024 tokens). The
   attempt_char_cap must clear that floor plus the prompt, or every attempt
   is refused before anything is sent. The shipped 120,000 leaves about 3.3
   KB of prompt headroom at max_tokens 1024. A run that makes zero pairs
   because of cap refusals exits 3 and says "NOT a successful run"; rc 0
   cannot masquerade as calibration evidence. A zero-pair run whose rows
   were all skipped (pending approval or held) exits 0 and is the
   legitimate unactivated state, not a failure.
6. The ledger is `ledger_calib.jsonl`, an append-only JSONL of reservations,
   attempts, and spend in characters with a running USD estimate; pairs land
   at `out_path`. The runner records Oracle's usage fields verbatim, so
   ledger spend reads back against Oracle's usage page directly.

Not known before a first live call: the tenancy rate and the real billing
ceiling (the 120,000-char attempt_char_cap is the runner's own reservation
bound, not an Oracle limit), the real per-request character counts,
throughput and 429 behavior, and which dated Oracle terms govern the tenancy.
`price_per_million_chars` 0.18 is Oracle's public list price; the tenancy rate
is unverified. `requests_per_minute` is a placeholder until a live rate
check. `budget_usd` and `attempt_char_cap` are deliberately unset in the
calibration example: the runner fails fast without them, and setting them is
the owner's approval step, not a pre-filled number.

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
  Instinct 0346/0349). Instinct 0646 hardening: the parser anchors on the first brace
  candidate that decodes AND validates as the full outer pair, so a preamble with a
  stray brace (or a valid non-pair object) no longer fails a billed reply that carries
  a real pair later; a dict or list response is stored as JSON text (ensure_ascii=False)
  instead of Python repr, and a non-string instruction is a bad pair shape.
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
  (or `--pack pack/v3.1-rev3/bulk_prompts_v3.1-rev3.onestage.train.jsonl` for the v3.1-rev3 candidate).`
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
