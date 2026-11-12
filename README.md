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

## Bearer-token self-run (OCI Generative AI API key)

If you authenticate with an OCI Generative AI API key (a secret token string,
created in the Generative AI service) instead of an IAM signing-key profile,
use the bearer path: no OCI CLI, no SDK, no ~/.oci/config. The runner posts to
the OpenAI-compatible Chat Completions endpoint
(`https://inference.generativeai.<region>.oci.oraclecloud.com/openai/v1/chat/completions`,
https://docs.oracle.com/en-us/iaas/Content/generative-ai/chat-completions-api.htm)
with `Authorization: Bearer <secret>`.

One-request smoke test (Ashburn, Llama 4 Maverick, exactly one call, no
retries, cumulative count bound):

    python3 runner_v2.py --config gen_config.bearer.smoke.example.json --dry-run
    python3 -c "import getpass,os,subprocess,sys; os.environ['OCI_GENAI_API_KEY']=getpass.getpass('Oracle key (hidden): '); sys.exit(subprocess.call(['python3','runner_v2.py','--config','gen_config.bearer.smoke.example.json']))"

The getpass wrapper keeps the key out of your shell history and out of every
file. Exit codes: 0 = one pair made; 4 = the attempt failed (read the status
and error fields of smoke_ledger.jsonl, those two fields only). Dollar figures
are labelled UNVERIFIED: no Oracle doc states the billing metric of this
endpoint, so the enforceable bound is the request count (max_attempts 1), and
the $0.003 figure is conditional on character metering matching the price
list's Maverick entry. The runner refuses models whose price-list unit is not
characters (Grok/gpt-oss are token-priced, dedicated endpoints are hourly),
refuses to send the token to any non-Oracle host, refuses redirects, and
refuses a token containing stray CR/LF characters from a paste. Bulk
self-run: gen_config.bearer.example.json (requires owner-set model id,
budget_usd, attempt_char_cap, max_attempts).

## Bounded parallel calls (local candidate, offline-tested)

The one-request smoke configuration remains serial and unchanged. The bulk
example opts into two worker threads with a shared one-second minimum admission
interval. These are cautious pilot settings, not measured Oracle tenancy limits.
No live throughput or cost measurement has been made.

    "workers": 2,
    "min_request_interval_seconds": 1.0,
    "max_attempts": 5,
    "max_retries": 2

After the smoke, set the model, paths, request count, per-attempt reservation,
and estimated-char budget in your own config. The example's five-attempt cap
is a small pilot, not permission or a configuration for 12,000 requests. Keep
prompt-pack execution and language holds in place. `pairs_per_seed` defaults
to four unless overridden, and each seed's `n_pairs` wins: 12,000 seed rows do
not necessarily mean 12,000 calls.

`workers` bounds in-flight requests (1..16); `--workers N` can only lower the
config value. The process-wide pacer spaces admissions across all workers,
including retries. On HTTP 429, it doubles spacing and moves the shared next
admission back (up to at least 60 seconds). Already-admitted calls can still
finish. Waiting workers recheck the new deadline. Successful responses do not
automatically increase throughput. Raise concurrency or decrease spacing only
after measurement, not by guessing Oracle's dynamic, undocumented limit:
https://docs.oracle.com/en-us/iaas/Content/generative-ai/dynamic-throttling.htm

Before any request reservation, the parallel path preflights all eligible,
non-held rows for required rendered prompts. A bad row anywhere in the pack
refuses startup with a named error and zero calls, not a late failure after
other workers have started. Held rows remain held. On an unexpected coordinator
exception, admissions stop and outstanding futures are drained: admitted replies
are persisted where possible and never-admitted reservations are released. Exit 6
reports a fixed coordinator-failure message rather than a raw traceback. Ctrl-C
still exits 130; persistence failures still exit 5. A settlement that itself
fails is not replayed automatically because its output may already be durable;
its reservation remains blocked for reconciliation, while other futures drain.

One coordinator owns ledger and output writes. Before dispatch, it atomically
reserves the request count and full estimated input/reply term. In-flight calls
and retries count against the same cumulative bounds. `--limit` includes
potential in-flight successes, so parallelism cannot overshoot it. Only a
bounded number of jobs are submitted; the runner does not queue 12,000 calls
or launch separate processes with separate ledgers to evade the shared bounds.
A second process on the same ledger still refuses to start.

For resume, keep the SAME config, output, and ledger. Completed work is skipped.
A timeout, unknown result, or pending reservation is not automatically retried:
it may already have been billed. Valid pairs are written, flushed and synced
BEFORE the ledger marks their attempts completed. An output/ledger write failure
stops admissions and exits 5 with a fixed persistence warning; already-admitted
calls are drained where possible, and uncertain reservations stay counted.

On startup, legacy completed rows without matching output records refuse new
calls and exit 5 with a reconciliation warning, even if the request cap is already
reached. Durable output paired with a pending ledger row is visibly reported,
skipped without another call, and left conservatively counted for reconciliation.
New output rows record their attempt id; old records use their existing pair/model
id for matching. Duplicate output ids, incomplete tails and invalid pair records
are refused rather than silently repaired. Never delete the ledger to restart.

Reconcile uncertain attempts against saved output and the tenancy's billing
records before any manual change. Reordering cannot recover a reply that was
never successfully written: an abrupt kill or disk failure can still lose up to
the in-flight worker count of replies, but those reservations remain blocked
rather than being falsely completed or silently re-billed. On Ctrl-C, new
admissions stop, waiting requests are released without a call, and already-admitted
calls are allowed to settle. POSIX SIGINT is briefly deferred across reservation
and future registration, and across each output/ledger settlement: a signal in
those windows is delivered once the reservation has a drain-visible future or
the settlement has finished. If executor submission raises without returning a
future, the reservation remains pending, spent and blocked: submission may have
queued work before raising. The runner stops admissions and reports uncertainty;
it never assumes an exception proves that no request was sent, including when
submission raises KeyboardInterrupt. A partial-submit reply may be unrecoverable
without its future, but it cannot free the count allowance or authorize a retry. An abrupt kill leaves reservations held on the next
run. The already-published serial smoke path has not been changed by this follow-up.

Dollar figures remain UNVERIFIED char-based estimates, not a proven spend cap.
The shared request-count bound is enforceable; completion `usage` fields do not
prove the billing unit or actual charge. The bearer adapter's fixed diagnostics,
typed usage/finish fields, and full-credential echo refusal are unchanged. As
before, a partial credential embedded in normal-looking successful model text
cannot be reliably distinguished from ordinary output and could reach the dataset.

Offline checks (no Oracle access):

    python3 test_stage0.py
    python3 test_bearer.py
    python3 test_parallel.py

## Parallel pilot from the prompt pack (run only after the serial smoke passes)

Offline-tested only (a loopback stub, 12 seeds, 2 workers: 12 pairs, ledger all
completed, a second run on the same ledger made no call). No live Oracle call
has been made with this code. Dollar figures stay UNVERIFIED; the request count
(`max_attempts`) is the enforceable bound. Needs only Python 3 (stdlib).

1. Make a small language-spread seeds file. `--approve` is your own
   acknowledgment that these rows are the ones you chose: it marks them
   eligible and makes no call. It authorizes no one else to run them. It keeps
   whole pack rows, skips rows the runner would hold, and sets `n_pairs` so
   planned pairs = seeds x pairs-per-seed:

       python3 make_seeds.py --pack pack/v3.1-rev3/bulk_prompts_v3.1-rev3.onestage.train.jsonl --n 12 --out seeds_pilot.jsonl --approve

2. Copy `gen_config.bearer.example.json` to `gen_config.pilot.json` and set:
   `model_id` (the exact id your Ashburn account exposes), `budget_usd`,
   `attempt_char_cap` (120000 at max_tokens 1024), `max_attempts`, `seeds_path`
   `seeds_pilot.jsonl`, `out_path` `pilot_pairs.jsonl`, `ledger_path`
   `pilot_ledger.jsonl`. Keep `workers` 2 and `min_request_interval_seconds`
   1.0 until measured.

   Choose max_attempts as the total raw-request ceiling you accept across ALL
   runs on this ledger, including retries and uncertain/failed attempts. For a
   first 12-row pilot, choose max_attempts=12 and max_retries=0 only after the
   one-request smoke and billing check and only if you accept up to 12
   requests. This ceiling may yield fewer than 12 valid pairs; do not increase
   it just to finish the rows. Keep the same ledger, output and ceiling on
   resume. budget_usd remains a character-based estimate, not a proven dollar
   cap.

3. `python3 runner_v2.py --config gen_config.pilot.json --dry-run` prints the
   ledger readout only. It does not count seeds and does not prove the seeds file exists.

4. Run (key stays hidden; the exit code prints right after):

       python3 -c "import getpass,os,subprocess,sys; os.environ['OCI_GENAI_API_KEY']=getpass.getpass('Oracle key (hidden): '); sys.exit(subprocess.call(['python3','runner_v2.py','--config','gen_config.pilot.json']))"
       printf 'exit code: %s\n' "$?"

5. For a fresh full pilot, this run's done line must report the planned number
   of pairs, the output must grow by that number, and those attempts must be
   completed in the ledger. On resume, the done line counts only NEW pairs from
   that run. Check output growth against that line and cumulative completed
   pair IDs against the planned seed/pair set. Zero exit status alone proves
   neither full completion nor a successful new call. Never delete the ledger,
   reset allowance or raise the request ceiling automatically to finish
   missing pairs.

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
