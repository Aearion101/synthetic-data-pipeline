# Synthetic prompt pack v1

4,000 unique, fully rendered stage-1 generation prompts for model-neutral,
multilingual instruction-pair training data (240 content families). Crew-authored seeds,
machine-rendered prompts. No GroupMe, private chat, or HPE data anywhere in
this pack.

**Execution status: every row is `pending_user_approval`.** This pack approves
nothing. Model-support flags below are documentation facts from the published
model cards, not permission to run. Live generation, spend, and language scope
remain Dylan's decisions; the Grok/Gemini model-term holds and the Llama 4
account/legal checks (EU restriction, naming condition) still apply separately.

## Layout

- `bulk_prompts_v1.train.jsonl` - 3,141 prompts
- `bulk_prompts_v1.holdout.jsonl` - 859 prompts
- `taxonomy.yaml` - domains, aspects, task types, difficulties, audiences,
  formats, language strata (generated, do not hand-edit)
- `stage2_answer_template.txt` - answer-generation prompt template (stage 2)
- `bulk_prompts_v1.onestage.train.jsonl` / `bulk_prompts_v1.onestage.holdout.jsonl` -
  one-stage render for the current runner (see "One-stage render" below)
- `bulk_prompts_v2.*.jsonl` / `bulk_prompts_v2.1.*.jsonl` - dated revisions
  (see "Pack v2" / "Pack v2.1" below). **v2.1 is the current pack for
  check-in; v1 and v2 are kept for provenance and both reproduce
  byte-identically.**
- `../tools/prompt_pack_tools.py` + `pack_data.py` - builder/validator
  (Python 3.8+ stdlib only, deterministic under `--seed 42`)

## Row schema (one JSON object per line)

| field | meaning |
|---|---|
| `id` | stable row id `spv1-NNNNN` |
| `topic` | `domain - aspect` English seed string (satisfies generate.py's reader as-is) |
| `prompt` | fully rendered stage-1 prompt, ready to send |
| `prompt_language` | language of the prompt text itself (`en` in v1: English meta-scaffolding, same pattern as generate.py's pair-from-topic-v1). The *output* language the model is told to write in is `language`. |
| `task_type` | one of 14 task types |
| `domain`, `aspect` | content seed; `seed_family_id` = `domain::aspect` |
| `language`, `language_name`, `language_stratum` | target output language; stratum A/B |
| `difficulty` | beginner / intermediate / advanced |
| `audience` | 1 of 8 personas |
| `split` | train / holdout (by family hash, see below) |
| `support` | `{llama4_documented, lfm3_documented, qwen3_documented}` booleans |
| `execution_status` | always `pending_user_approval` in v1 |
| `template_id` | stage-1 template variant used (v1/v2/v3) |
| `provenance` | pack, generator version, seed, source statement, date |

## Language strata (sourced proposal)

The strata are a sourced proposal, not a verified match to a user-approved
target set - Dylan's Nov 10 brief keeps language scope model-neutral and
unnarrowed, so this pack errs on keeping documented languages in, tagged.

- **Stratum A** - Llama 4's 12 documented languages (Meta Llama 4 model card:
  https://dev.meta.ai/llama/docs/model-cards-and-prompt-formats/llama4 and
  https://raw.githubusercontent.com/meta-llama/llama-models/main/models/llama4/MODEL_CARD.md;
  same list in Oracle's docs:
  https://docs.oracle.com/en-us/iaas/releasenotes/generative-ai/llama-4.htm):
  en 1200, id 400, es/fr/de/pt/it 240 each, ar/hi/tl/th/vi 160 each.
- **Stratum B** - documented by LFM3-1.7B
  (https://docs.liquid.ai/lfm/models/lfm3-1.7b, 16 languages) and Qwen3
  (model card e.g. https://huggingface.co/Qwen/Qwen3-14B states 100+
  languages/dialects and enumerates the list; Alibaba's Qwen3 announcement
  https://www.alibabagroup.com/en-US/document-1853940226976645120 says 119)
  but NOT in Llama 4's 12: zh, ja, ko, ru, pl, 80 each. Kept so these
  tune-target languages are not silently dropped; flagged
  `llama4_documented: false` so a Llama execution filter can exclude them
  until Dylan rules. Coverage of the full Qwen3/LFM3 lists beyond these 17 is
  an explicit open gap.

Distribution is a reversible design choice by the pack author, not a user
preference. This matches the Oct 31 decision to train only on model-supported
languages with language tags preserved.

## Holdout design

Split is by **content family** (`seed_family_id` = `domain::aspect`),
`sha256(family) % 5 == 0` -> holdout (51 of 240 families). Every language and
difficulty of a family lands in the same split, so no row of a held-out family
leaks into train through translation or rephrasing within the pack. This is
family-level isolation only: shared scaffold phrasing and cross-family topical
similarity mean it is NOT full semantic isolation. v1: 3,141 train / 859
holdout rows; v2: 3,169 / 831.

## Pack v3.1 (2026-11-11, review-fix candidate - NOT yet reviewed or checked in)

Answers the v3 crew review. Same 800 aspects and the same quota WEIGHTS as
v3, but NOT the same final mix: the cap of 3 variants limits English to
2,400 of its 4,080 quota (uniqueness), so v3.1's actual per-language counts
are en 2,400 / id 1,360 / es,fr,de,pt,it 816 / ar,hi,tl,th,vi 544 /
zh,ja,ko,ru,pl 272 (11,920 rows: 9,727 train / 2,193 holdout after the rev3
regrouping). Build: `build --seed 42
--pack-version v3.1 --total-rows 13600 --max-per-family 3` then
`render-onestage --pack-version v3.1`. Validator: 0 errors across all 10 file
sets; v1, v2, v2.1 AND v3 still build byte-identical (verified).

- NEAR-TWIN LEAKAGE FIX: 99 hand-written topic groups (214 aspects, listed
  in pack_data.py TOPIC_GROUPS_V31) merge aspects that cover the same ground.
  v3.1 hashes the split per GROUP (seed_family_id = grp:<group>; grouped rows
  also carry topic_group). BOUNDED CLAIM: members of these declared groups
  cannot straddle train/holdout (hash-verified, 99/99 single-split, including
  the 9 pairs the crew found straddling in rev2); the mapping does NOT claim
  to catch every possible twin - semantic review remains the gate for pairs
  nobody has flagged.
- SENSITIVE-ASPECT FIX: 16 tagged aspects (pack_data.py
  SENSITIVE_ASPECTS_V31) exclude math_reasoning and code tasks: the crew's 9
  grave aspects (wars, transatlantic slave trade, great depression,
  colonialism, saying goodbye to a pet), the milder pet-health pair (pet
  first aid basics, signs a pet is sick), the two decolonization aspects
  (rev2 review found math rows on them), and 3 conservative producer adds
  (french revolution, mongol empire, estate basics). Verified: 0 math/code
  rows on all 16.
- CAP 3: --max-per-family 3; verified max rows per (family, language) = 3.
- FULL-ROW REVIEW INPUT: v3.1_review_full_rows_chunk1-6.jsonl carry complete
  rows (status, support flags, off-list flags, topic_group, provenance),
  each under 5 MiB.
- NESTED-JSON FIXTURE: tools/fixture_nested_json_v31.json - one real v3.1
  row using "a JSON object with keys the requester names" (108 such rows in
  v3.1 - 126 after rev3) with safe/trip mock replies against the documented
  parse_pair contract. RESOLVED runner-side: the hardening landed at head
  1d2bec6 (T34/T35), so these rows are not a pack blocker; the file stays as
  a regression fixture.
- Coverage stays quota-proportional per the weights above. Translate: 870
  rows, 255 off-list-source flagged. All rows pending_user_approval. Longest
  prompt 1,050 bytes -> 113 x 1,024 + 1,050 + 200 = 116,962 <= 120,000 cap.
  rev3 history: rev1 (12 tags, no rev2-review groups) and rev2 (14 tags, 92
  groups) are superseded; rev3 adds the crew's 9 straddling pairs and the 2
  decolonization tags. Review rev3 files only.

## Pack v3 (2026-11-11, scale-out candidate - NOT yet reviewed or checked in)

Offline expansion candidate toward the bulk-credit goal. v2.1 remains the
17:00 artifact; v3 is a separate dated version pending crew/coordinator
review, and volume is a dial, not a credit-fit promise.

- 800 content families (40 aspects per domain; +560 new hand-authored aspects
  over v2.1), 13,600 rows (10,828 train / 2,772 holdout, 160 of 800 families
  held out by the same family-hash rule).
- Coverage is quota-proportional, NOT one row per (family, language): 7,410 of
  the 13,600 possible family-language cells are covered, with 1-7 rows per
  cell. Per language (rows / families covered of 800 / max rows per family):
  en 4,080 / 800 / 7; id 1,360 / 677 / 3; es,fr,de,pt,it 816 / 527-538 / 3;
  ar,hi,tl,th,vi 544 / 399-422 / 2; zh,ja,ko,ru,pl 272 / 237-245 / 2.
  Lower-quota languages see fewer than half the families.
- Near-sibling honesty: rows sharing a (family, language) cell differ only in
  the task-type / audience / difficulty clause over the same aspect (rendered
  template similarity 0.91-0.94 within a 7-row cell). 653 of 800 English
  families carry MORE than the ~3-variant guideline below; whether those extra
  variants are useful training data depends on the generator and has not been
  output-tested. A conservative `--max-per-family 3` build of the same pack
  yields 11,920 rows (9,478 train / 2,442 holdout) - 12% smaller, no cell
  above 3 variants. Choose per the quality bar; this build keeps volume.
- Producer audit flags ~40 rows (0.3%) where a procedural/format task (code,
  math_reasoning, dialogue, classify) lands on an introspective aspect (e.g.
  sleep hygiene, gym confidence, math anxiety). Most are workable via the
  "genuinely help" framing, but a reviewer may want them dropped or
  regenerated; left as-is pending review.
- New builder controls (version-gated): `--total-rows N` scales the language
  quotas proportionally (required for v3, so volume is always an explicit
  decision); `--max-per-family N` overrides the per-(family, language) cap.
  Volume is a hypothesis for calibration, NOT a promise: final sizing should
  follow the first authorized run's measured output length and rate.
- Same contracts as v2.1: translation contract, off-list source flags
  (982 v3 translation rows total - 769 train / 213 holdout - of which 286
  carry one: 217 train / 69 holdout), zh-Hans, advanced-audience filter,
  "The person asking is:", all rows pending_user_approval.
- Validator: 0 errors, 0 exact-dups, 0 id collisions across all 8 file sets.
  Longest v3 one-stage prompt 1,056 bytes -> 113 x 1,024 + 1,056 + 200 =
  116,968 <= 120,000 cap.
- v1, v2 and v2.1 all still build byte-identically (verified after the v3
  builder change). Build v3: `python3 prompt_pack_tools.py build --out-dir
  prompts --seed 42 --pack-version v3 --total-rows 13600` then
  `render-onestage --pack-dir prompts --pack-version v3`.
- Quality status: producer spot-checked a stratified sample of new-aspect
  rows (they read as natural, task-compatible prompts); a full crew/coordinator
  sample review has NOT happened yet. Larger volumes via `--total-rows` above
  13,600 mean multiple variants per (family, language); beyond ~3 variants,
  rows become near-siblings and are not recommended.

## Pack v2.1 (2026-11-11, wording + README fixes)

Patch on v2, files `bulk_prompts_v2.1.{train,holdout}.jsonl` and
`bulk_prompts_v2.1.onestage.{train,holdout}.jsonl`; **v2.1 is the current pack
for check-in.** Semantics, ids, counts, splits and languages are identical to
v2; v1 and v2 both still build byte-identically (`--pack-version v1` / `v2`).

- Audience wording fixed on all rows: prompts now say "The person asking is:"
  (the three stage-1 template variants and the one-stage wrapper all said
  "Audience:" / "Who is asking:"; validator rejects stale wording in v2.1
  sets).
- v2.1 one-stage prompt bytes max 1,053: worst-case reservation
  113 x 1,024 + 1,053 + 200 = 116,965 <= 120,000 cap.
- Support-flag semantics, stated explicitly: `support.llama4_documented`
  documents the ROW's language only. 86 translation rows (81 with a stratum-A
  target, 5 with a stratum-B target) quote a source passage in a stratum-B
  language and keep `support.llama4_documented: true` where the row language
  is documented; any Llama 4 execution filter MUST also exclude rows with
  `off_list_source_language: true` (i.e. require
  `llama4_pair_fully_documented: true`). The flag does not broaden execution
  eligibility; every row remains `pending_user_approval`.

## Pack v2 (2026-11-11, review fixes)

Dated revision from the coordinator/ChatGPT 03:45 review. v1 files are
untouched; v2 supersedes them for use. `build --pack-version v1` still
reproduces v1 byte-identically.

- **Translation contract (was a real bug):** 153 of 269 v1 translation prompts
  required the response in a language other than the row's, contradicting the
  wrapper. v2 has a single contract: the response is ALWAYS in the row's
  language; a quoted source passage inside the instruction may be in another
  language, and the wrapper says so explicitly. All 281 v2 translation rows
  (223 train + 58 holdout) carry `source_language` / `target_language`
  metadata; `target_language` always equals `language` (validator-enforced).
- **Off-list languages kept, flagged (not deleted):** 86 v2 translation rows
  (69 train + 17 holdout) draw their SOURCE passage from a stratum-B language
  (zh/ja/ko/ru/pl - off the Llama 4 list; comprehension, not production). They carry
  `off_list_source_language: true` and `llama4_pair_fully_documented: false`
  so an execution filter can exclude them until Dylan rules. No row was
  silently removed.
- **Audience/difficulty mismatch:** v1 had 197 advanced rows with a beginner
  audience. v2 excludes beginner audiences from advanced difficulty
  (validator-enforced; 0 such rows).
- **Chinese script specified:** zh rows render as "Chinese (Simplified)" and
  carry `language_variant: "zh-Hans"`.
- **Leakage claim narrowed:** family-level holdout prevents the same content
  family appearing in both splits in any language, but rows share scaffold
  phrasing and template structure across families, so the split is NOT full
  semantic isolation. Treat held-out results accordingly.
- **One-stage wrapper is canonical:** the "3 template phrasings" diversity
  applies only to the two-stage stage-1 files; the one-stage render uses one
  canonical wrapper for contract uniformity (the per-row task clause still
  varies).
- **max_tokens_hint = 1024** in v2 one-stage rows, matching the shipped
  example config (1024 tokens / 120,000-char attempt cap): worst-case
  reservation 113 x 1024 + 1,041 (longest prompt) + 200 = 116,953 <= 120,000.
  Labeled an assumption; the config value is Fo's call.

## One-stage render (17:00 runner contract)

Coordinator ruling (2026-11-11 01:49, reversible integration choice, not a user
preference): the 17:00 deliverable is ONE stage. `prompt_pack_tools.py
render-onestage` emits `bulk_prompts_v1.onestage.{train,holdout}.jsonl` from the
stable stage-1 files. Same ids, families, splits, languages and metadata; the
`prompt` field becomes a meta-prompt asking for ONE JSON object
`{"instruction": "...", "response": "..."}` written in the row's
`language_name`, matching generate.py's existing `parse_pair` contract.
Added fields per row: `stage1_prompt` (original), `prompt_mode`
("one-stage-1.0.0"), `prompt_sha256`, `prompt_bytes` (UTF-8; validator checks
both), `max_tokens_hint` = 1500 in the v1 render (1024 in v2, see "Pack v2"),
labeled an assumption - coordinate the actual config value with Fo;
pairs_per_seed=1 so the bare-seed claim key is not exercised with multi-pair
seeds. Rendered prompt sizes: v1 train min 622 / mean 710 / max 922 bytes;
v1 holdout min 625 / mean 706 / max 898 bytes; v2 train max 1,030 bytes;
v2 holdout max 1,041 bytes. Run plan (v2): `python3 prompt_pack_tools.py build --out-dir
prompts --seed 42 --pack-version v2.1 && python3 prompt_pack_tools.py
render-onestage --pack-dir prompts --pack-version v2.1 && python3
prompt_pack_tools.py validate --pack-dir prompts`. The
two-stage files and template below stay for the later A-to-B enhancement;
the runner does not need them at 17:00.

## Two-stage generation flow

1. **Stage 1 (this pack):** send `prompt`; the model returns ONE user
   instruction written in `language_name`.
2. **Stage 2:** send `stage2_answer_template.txt` with `{instruction}` and
   `{language_name}` filled; the model returns the answer. The pair
   (instruction, answer) is the training row - plain JSONL, no chat template
   baked in (same contract as generate.py's existing pair-from-topic-v1).

**How the current runner consumes the pack (one-stage flow):** the 17:00
runner reads the `bulk_prompts_*.onestage.*.jsonl` files and sends each row's
rendered `prompt` field directly (`require_rendered_prompt` in the example
config; `topic` remains only for legacy compatibility). The runner gates on
`execution_status`, so every row ships `pending_user_approval` and is skipped
until an authorized fixture/run flips it deliberately. This flow is verified
OFFLINE only: the pack-side stub (`../tools/stub_e2e_test.py`) proves two-stage
assembly plumbing for the later enhancement, and the runner-side fixture demos
(independent reviewer) processed real v2 one-stage rows through the actual
runner code with an SDK-shaped stub client. No live OCI call has been made;
wire-level behavior stays untested until an authorized call.

## Task/domain compatibility and output limits

Tasks that make no sense for a domain are filtered, not just reworded: `code`
is excluded from `history`, `geography` and `everyday_life` (validator enforces
it). Passage-carrying tasks (summarize, rewrite, extract, classify, translate)
instruct the model to invent specific concrete content, not generic filler.
Recommended per-stage output limits for the runner config (coordinate with Fo;
each stage needs its own reservation in the ledger): stage 1 ~150-300 max
tokens (one instruction), stage 2 scaled by difficulty (~400 beginner / ~800
intermediate / ~2048 advanced) rather than a blanket 1,024 for every call.

## Diversity and quality validation (validator output, this build)

(prompts are English scaffolding with an explicit 'write in <language>'
directive plus task/audience/difficulty clauses; stage-1 output language
adherence is checked at QC, see below)

- 4,000 rows; **0 exact-duplicate prompts** (normalized text), 0 id collisions
- Max 1 row per (family, task, language); family coverage 240/240
(v1 build) Task mix 246-321 rows per task type (14 types); difficulty 1,621
  beginner / 1,611 intermediate / 768 advanced
- Language quotas hit exactly per the table above (both v1 and v2.x)
- (v1 build) Stage-1 prompt input total ~1.74M chars (~$0.31 at Llama 4's
  $0.18/M-char list rate - input side only)
- (v2/v2.1 build) 3,169 train / 831 holdout; task mix 231-321; difficulty
  1,586 beginner / 1,658 intermediate / 756 advanced

Semantic diversity comes from distinct content seeds (240 hand-authored
aspects across 20 domains) crossed with task/audience/difficulty, plus 3
phrasings of the meta-prompt, with incompatible task/domain combos filtered
(see above). It is still template-rendered English scaffolding: a human spot
review of a sample per stratum is recommended before any approved run, and
native-speaker review of stage-1 OUTPUTS (especially stratum B and ar/hi/tl/th/vi)
is the QC gate before those pairs go into a tune.

## Quality review report (pack author, 2026-11-11)

- Method: stratified 96-row sample of the full build (3 per task type, 2 per
  language, 1 per domain, deduped), each prompt read in full; plus a targeted
  re-read of every code/fact_qa/how_to row in previously weak domain combos
  after repair.
- Found and fixed: contrived code asks on lifestyle domains (rewritten, then
  hard-filtered from history / geography / everyday_life); fact_qa reworded to
  concrete checkable answers; how_to reworded to practical step-by-step
  guidance; passage tasks now demand specific invented content.
- Residual: this validated prompt-side instruction quality only, not model
  output language. Native-speaker QC sampling per stratum before any tune is
  still required, especially stratum B.

## Scale-out and cost honesty

4,000 prompts produce ~4,000 pairs; at typical pair sizes that is well under
$300 of Llama 4 character billing (~1.66B chars at list). The taxonomy supports
bounded unique expansion (more aspects per domain, more audience/format draws,
~242k possible cells, capped per family) if Dylan approves a bigger queue -
rebuild with added aspects in `pack_data.py`, never by re-running the same
prompts. Volume targets are a generation decision, separate from pack readiness.

## Provenance and license notes

Seeds are crew-authored for this pack; prompts are machine-rendered by
`prompt_pack_tools.py` (version recorded per row). Training-data license
position follows the generation model's terms, as already noted in
generate.py: Cohere-generated data is clear for open release; a public tune
built on Llama-generated data must carry "Llama" in its name. Grok stays out
of the pipeline by decision.

# Check-in addendum for pack/v3.1-rev3 (append to the producer README, do not replace it)

**Status of this directory.** A separate candidate pack. It does NOT replace the frozen pack/v2.1 and activates nothing: every row is `pending_user_approval`. It has been structurally checked and crew-reviewed offline (rebuild reproduces the hashes below; 0 validator errors; 99 topic groups with 0 straddling the split; 16 sensitive aspects with 0 math/code rows; 0 exact duplicates). Generated-output quality is untested: no model has been called on these prompts. It is not credit-ready and makes no claim about the $300 credit.

**Counts and hashes (one-stage files).**
- 11,920 rows: 9,727 train, 2,193 holdout.
- train sha256 384894bf61b77840514aad6ab7877586a9c13f272c8a9f228dad83010407aa93
- holdout sha256 7e2b3cc866a19df937445d2d0f211ad51b2cdbbda349c24ea9c20fbb0f6fc324
- Language mix: en 2,400; id 1,360; es/fr/de/pt/it 816 each; ar/hi/tl/th/vi 544 each; zh/ja/ko/ru/pl 272 each (English capped by the 3-per-aspect-language rule, so it is below the v3 quota; not padded).

**Holdout claim, bounded.** The split is hashed per declared topic group (99 groups, 214 aspects), so members of a declared group cannot straddle train and holdout. This is topic-group isolation only. It is NOT a clean semantic evaluation set: undeclared near-twin aspects may still straddle.

**Known residuals.** (1) "fractions to decimals" (11 rows, holdout, group math-decimals) vs "fractions refresher" (14 rows, ungrouped, train). (2) Loose pairs the crew saw and left alone: "angles and triangles" (holdout) vs "basic geometry shapes" (train); "speed distance and time" (holdout) vs "time calculations" (train); gardening on a balcony (holdout, group gardening-container) vs urban gardening (train, ungrouped); organizing digital photos (holdout, group digital-photos) vs backing up photos and backing up a lifetime of photos (train, group tech-photo-backup; two declared groups on opposite sides); insurance basics (holdout, group finance-insurance) vs choosing travel insurance (train, ungrouped). Row ids are not stable across candidate revisions: cite aspects by name.

**Reviewed by (offline, attributed):** coordinator rebuild (byte-identical, v1/v2/v2.1/v3 also reproduce), ChatGPT duplicate and cross-split screens, Claude group/sensitive-tag review. No runner, nested-JSON execution or OCI test of these rows.

**Consumer note (2026-11-11, Instinct 1051/1052).** The consumer of these rows
is runner_v2.py, the hardened, ledgered runner the repo's test suite covers.
generate.py references above are historical: generate.py is legacy, has no
execution_status gate, ledger or attempt cap, and must not be run on pack
rows.
