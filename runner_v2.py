#!/usr/bin/env python3
"""runner_v2: Stage-0 test implementation of the Fo-1830 fix list; the repo's tested entry point (see README; staged on fo/stage0-runner).

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
 14 main() fails fast when attempt_char_cap is absent from the config (DeepSeek F6);
 15 F1, writer restored from generate.py (Instinct 2245 ask): run_once returns the
    reply; main() parses it, writes the record (generate.py's shape: id, instruction,
    response, source, generator, created_utc) to out_path with flush per record and
    seeds_done checkpointing, so a restart does not re-bill completed pairs;
 16 F5, adapter restored: OciAdapter.generate(model_id, prompt, cfg) wraps the client
    call shape, so the real OCI path is callable;
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

Fixes from Instinct 0002/0003/0005/0007/0008/0010 (2026-11-11):
 19 Llama goes through chat() with GenericChatRequest (api_format GENERIC), NOT
    generate_text() (Instinct 0003, doc-checked against the OCI Python SDK docs,
    not yet run live); Cohere keeps CohereChatRequest on chat().
 20 num_generations pinned to 1 in the request AND the reservation arithmetic; a
    config asking for more fails fast (each extra generation multiplies billing).
 21 NoneRetryStrategy on the OCI client so the SDK performs no hidden retries: the
    runner's ledger is the only retry layer (one runner attempt = one billed call).
 22 reply extracted from the REAL message field (choices[0].message.content for
    GENERIC chat, chat_response.text for Cohere); a reply without that field is an
    error, never a longest-string guess. (34 below widens this to the real
    ChatResult shape and list content.)
 23 usage object recorded verbatim when the response carries one.
 24 one-worker enforcement is code: a non-blocking exclusive flock on
    <ledger>.runlock at startup; a second process exits non-zero and books nothing
    (Instinct 0003 item 5; test starts a second process and asserts the refusal).
 25 cross-process reserve-and-check (DeepSeek 2137 spend_cap design): claim_seed()
    takes the ledger flock, re-reads, and appends a "claimed" row before releasing,
    so two workers cannot bill the same seed; T2 runs green on this path.
 26 every ledger append takes the same flock (short hold) so appends from two
    processes cannot interleave; effective_rows() is cached between mutations, and
    the parse is incremental (36 below), so total parsed rows grow O(N).
 27 cap refusal (input bytes + reply term over attempt_char_cap) returns a
    "refused_cap" sentinel; main() skips and logs it, no TypeError kills the run.
 28 a 429 closes its attempt as "throttled" (reservation kept, counted spent until
    stage-1 reconciliation) and main() retries after backoff as a NEW reserved
    attempt, bounded by max_retries (default 2); never a silent repeat.
 29 the budget/attempt-cap break exits the WHOLE run, not just the inner loop.
 30 held seeds (pending/claimed rows) are LISTED at run end with the manual
    reconcile step named; they are never auto-released.
 31 reservation covers the FULL billed input: prompt bytes PLUS
    prompt_overhead_chars (default 200, a stated conservative overhead for any
    system/template text the service may add; Instinct 0008 item 3). The runner
    sends a single user message and no system prompt of its own.
 32 prompt-pack consumption (Instinct 0010): seed rows may carry prompt (fully
    rendered; reserved on ITS bytes), id, language, split, execution_status. Rows
    not marked execution_status="eligible" are skipped and COUNTED, never run.
    With cfg["require_rendered_prompt"], a row without prompt fails fast. Field
    names Fo committed to the producer: prompt, id, language, split,
    execution_status (extra keys ride into provenance verbatim).

Fixes from Instinct 0048 + DeepSeek 0038 (the 42cad8e review round, 2026-11-11):
 33 claim keys are PAIR-SCOPED ("seedid#k", Instinct 0048 A): pair 1 completing
    no longer silently blocks pairs 2..N of the same seed; skipped pairs are
    NAMED at run end (held_seen printed), and the held/closed listings carry
    the pair key for reconciliation.
 34 the reply walk reads the real response shape: ChatResult.chat_response ->
    GenericChatResponse -> choices[0].message.content, whose content is a LIST
    of ChatContent (TextContent.text), never str(list); shape-checked against
    oci 2.191.0's own model classes without a live call (Instinct 0048 B).
 35 "unparseable" counts in SPENT_STATUSES: a parsed-but-unusable reply is a
    real charge with no pair, never a zero in the stage-1 reconcile number
    (DeepSeek 0038 R2, Instinct 0048 C).
 36 the ledger re-parses only appended bytes (Ledger.refresh()); the claim's
    fresh read is a tail parse, so 16,000 attempts no longer cost ~28 minutes
    of JSON parsing (DeepSeek 0038 R3; measured, not claimed).
 37 finish_reason is carried verbatim from both adapter paths (generic
    choices[0].finish_reason, Cohere chat_response.finish_reason) onto the
    attempt row, into the unparseable close note, and counted at run end
    (Claude 0234): a truncated reply (finish_reason "length") is distinguishable
    from a format refusal in the ledger itself.
 Still open: pending-row recovery for a killed worker (lead's call; listed at run
 end per 30 until then). Region, max_tokens enforcement and usage-view lag are
 stage-1 observations, not settled facts.
"""
import argparse, fcntl, hashlib, json, math, os, sys, time, uuid
from datetime import datetime, timezone

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def stable_slot(seed_id, n):
    return int(hashlib.sha256(seed_id.encode()).hexdigest(), 16) % n

class Attempt:  # reservation row, written before the call
    FIELDS = ["attempt_id","run_id","seed_id","model","status","input_chars",
              "output_chars","usage","finish_reason","started_utc","ended_utc","error","pair_key"]
    def __init__(self, run_id, seed_id, model):
        self.attempt_id = f"{run_id}-{uuid.uuid4().hex[:8]}"
        self.run_id, self.seed_id, self.model = run_id, seed_id, model
        self.status = "pending"          # claimed -> pending -> completed|failed|timeout|unknown|throttled|released|unparseable
        self.input_chars = self.output_chars = 0
        self.usage = None
        self.finish_reason = None       # verbatim from the adapter path (Claude 0234)
        self.started_utc, self.ended_utc, self.error = now(), None, None
        self.pair_key = None             # pair-scoped claim key "seedid#k" (Instinct 0048 A)
    def row(self):
        return {k: getattr(self, k) for k in self.FIELDS}

SPENT_STATUSES = ("completed","failed","timeout","unknown","pending","claimed","throttled","unparseable")
HELD_STATUSES = ("pending","claimed")
# A seed with one of these rows is never re-claimed: closed non-throttled attempts
# may already be billable, and the lead rule (Instinct 0002 item 3) forbids
# silently retrying them. "throttled" is re-claimable: the service refused the
# request, so the bounded retry books a NEW attempt after backoff.
BLOCKED_STATUSES = ("completed","failed","timeout","unknown","pending","claimed","unparseable")

class Ledger:
    """Append-only JSONL. Budget and attempt caps are computed from ALL rows, so a
    restart cannot reset the spend and a killed process leaves its pending row.
    Every append takes an flock on <path>.lock (short hold) so appends from two
    processes cannot interleave (Instinct 0002 item 1). rows() caches the parse;
    appends invalidate it, so the O(n^2) re-read is gone (Instinct 0007 item 5)."""
    def __init__(self, path):
        self.path = path
        self._rows = None
        self._offset = 0                 # file bytes already parsed into _rows
        self._eff = None
    def _lockfile(self):
        return self.path + ".lock"
    def _append_unlocked(self, row):
        blob = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
        with open(self.path, "a", encoding="utf-8") as fh:
            # Another worker may hold appends our cache never saw: its rows sit
            # between our cached offset and the file's real end. Parse that gap
            # into the cache BEFORE our write and before the offset can move past
            # it, so a claim after this append can never grant a pair another
            # worker already holds (Instinct 0349: cross-process double claim
            # from the tail cache; reviewer-reproduced).
            if self._rows is not None and os.path.getsize(self.path) > self._offset:
                self._read_lines(self._offset)     # advances _offset over the gap
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush(); os.fsync(fh.fileno())
        # The writer's own bytes are known: extend the cache instead of dropping
        # the parse (DeepSeek 0038 R3). The tail offset comes from the file's
        # real size under the same flock -- never from a recomputed length, so
        # the tail read can never start mid-row (spawn-run diagnosis 2026-11-11).
        if self._rows is not None:
            self._rows.append(row)
            self._offset = os.path.getsize(self.path)
        self._eff = None
    def append(self, a):
        lock = open(self._lockfile(), "a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._append_unlocked(a.row() if isinstance(a, Attempt) else a)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN); lock.close()
    def rows(self):
        if self._rows is None:
            self._refresh_from(0)
        return self._rows
    def refresh(self):
        """Fresh read for the reserve-and-check claim: re-parse only the bytes
        appended since the last read (the first read on a fresh Ledger parses
        the whole file). Appends always happen under the ledger flock, so a
        tail read under that same lock sees every other worker's row
        (DeepSeek 0038 R3: total parsed rows O(N), not 3 x N^2)."""
        self._refresh_from(0 if self._rows is None else self._offset)
        return self._rows
    def _refresh_from(self, offset):
        if self._rows is None:
            self._rows = []
        try:
            self._read_lines(offset)
        except json.JSONDecodeError:
            # A tail offset that no longer lands on a line boundary heals by one
            # full re-parse: rare, bounded, and it can never silently skip a row.
            # A corrupt line mid-file still raises (fail loud, never guess).
            self._rows = []
            self._read_lines(0)
        self._eff = None
        return self._rows

    def _read_lines(self, offset):
        pos = offset
        try:
            with open(self.path, "rb") as fh:
                fh.seek(offset)
                for line in fh:
                    if not line.endswith(b"\n"):
                        break            # a writer is mid-append: stop before the partial line
                    pos += len(line)
                    s = line.decode("utf-8").strip()
                    if s:
                        self._rows.append(json.loads(s))
        except FileNotFoundError:
            pass
        self._offset = pos
    def effective_rows(self):
        """One row per attempt: the LAST row written for that attempt_id wins.
        Claims, reservations and closes share attempt_id by design, and a later
        close (for example completed then unparseable) supersedes the earlier one."""
        if self._eff is None:
            best = {}
            for r in self.rows():
                best[r["attempt_id"]] = r
            self._eff = list(best.values())
        return self._eff
    def spent_chars(self):
        # unknown/pending/claimed/failed/timeout/throttled all count: a call may be
        # billable though its response never landed (ChatGPT 1703, Instinct 2020).
        # unparseable counts too: a parsed-but-unusable reply is a real charge with
        # no pair, never a zero in the stage-1 reconcile number (DeepSeek 0038 R2,
        # Instinct 0048 C). released and refused_cap do not: nothing was sent.
        return sum(r["input_chars"] + r["output_chars"] for r in self.effective_rows()
                   if r["status"] in SPENT_STATUSES)
    def attempts(self):
        return sum(1 for r in self.effective_rows() if r["status"] != "released")

def record_chars(text):
    """Recorded char counts are UTF-8 BYTES. The reservation must be conservative,
    so bytes it is: in Thai/Arabic/Indonesian seeds one code point is 2-3 bytes,
    and len() under-reserves the input term by up to 3x. The reply term is
    separately bounded by 113 x max_tokens. If Oracle bills code points, stage-1
    reconciliation against its usage view settles it; until then bytes only
    over-reserve, never under-reserve."""
    return len(text.encode("utf-8"))

def hold_reason(row):
    """Source-language hold for prompt-pack rows (Instinct 0445/0446 rulings,
    ChatGPT 0404/0502 handoff), grounded in the pack README's support-flag
    semantics. Returns a hold reason string, or None when the row may run:
    1. support.llama4_documented is False -> the row's own output language is
       not on the documented list (stratum-B targets); held until Dylan rules
       on language scope (the Oct 31 rule: train only on model-supported).
    2. off_list_source_language is True -> the quoted SOURCE passage is a
       stratum-B language even when the target is documented; the pack README
       says any execution filter MUST also exclude these, so the runner does,
       even if execution_status was marked eligible by mistake.
    3. llama4_pair_fully_documented is False -> a translation row whose pair
       is not fully documented; held.
    Absence holds nothing: the optional pair field is missing on 3,719
    non-translation rows (3,349 of them stratum-A), and rows without a support
    block at all (plain {id, topic} seeds, older fixtures) run unchanged.
    Stated limit: the guard is Llama-4-keyed per the Oct 31 rule; lfm3/qwen3
    support flags are recorded in the pack but not enforced here until Dylan
    rules. With production rows all pending_user_approval, this guard changes
    nothing today; it only stops an eligible-marked held row from billing."""
    support = row.get("support") or {}
    if support.get("llama4_documented") is False:
        return "llama4-undocumented-language"
    if row.get("off_list_source_language") is True:
        return "off-list-source-language"
    if row.get("llama4_pair_fully_documented") is False:
        return "llama4-pair-not-fully-documented"
    return None

def claim_seed(cfg, claim_key, model_id, ledger):
    """Cross-process reserve-and-check (DeepSeek 2137, adopted Instinct 0002 item 1):
    under the ledger flock, re-read the ledger and append a claimed row if the
    PAIR is free; return the claim row, or None when another worker holds it.
    The key is pair-scoped ("seedid#k", Instinct 0048 A): pair 1 completing must
    not silently block pairs 2..N of the same seed."""
    lock = open(ledger._lockfile(), "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ledger.refresh()             # fresh read under the lock: other workers'
                                     # appends cannot slip past the tail offset
        for r in ledger.effective_rows():
            if r.get("pair_key") == claim_key and r["status"] in BLOCKED_STATUSES:
                return None                      # held or already attempted: never re-bill
        c = Attempt(cfg["run_id"], claim_key.rsplit("#", 1)[0], model_id)
        c.status = "claimed"
        c.pair_key = claim_key
        ledger._append_unlocked(c.row())
        return c
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN); lock.close()

def run_once(cfg, client_factory, seed, model_id, prompt, ledger, max_out_chars=None,
             attempt_row=None, overhead_chars=0):
    """One attempt: reserve, call, close the row. Returns (row, reply); a pre-call
    cap refusal returns a refused_cap sentinel row (nothing was sent, nothing is
    booked as spend). A Throttled 429 closes its row and re-raises so main() can
    back off and book a NEW reserved attempt (Instinct 0007 item 3)."""
    a = attempt_row or Attempt(cfg["run_id"], seed["id"], model_id)
    a.input_chars = record_chars(prompt) + overhead_chars
    if max_out_chars and a.input_chars + max_out_chars > cfg["attempt_char_cap"]:
        refusal = {"attempt_id": a.attempt_id, "run_id": cfg["run_id"], "seed_id": seed["id"],
                   "model": model_id, "status": "refused_cap", "input_chars": a.input_chars,
                   "output_chars": 0, "usage": None, "started_utc": now(), "ended_utc": now(),
                   "error": f"input {a.input_chars} + reply term {max_out_chars} over "
                            f"attempt_char_cap {cfg['attempt_char_cap']}; nothing sent"}
        return refusal, None
    # Build and interface-check the client OUTSIDE the try, before the reservation
    # row exists: a missing oci package, missing credentials or a client without
    # .generate is a misconfiguration, not possible spend, so it must raise instead
    # of being booked as "unknown" (Claude 2238 finding 5). When a claim row was
    # already booked, the caller releases it on this raise.
    client = client_factory()
    if not hasattr(client, "generate"):
        raise TypeError(
            f"client for {model_id} has no .generate(model_id, prompt, cfg); "
            "the real OCI path needs the chat() adapter (stage 1+)")
    # Reserve the reply term before the call. An attempt whose outcome is unknown
    # keeps this reservation; only a completed row settles it to the actual count.
    a.output_chars = max_out_chars or 0
    if a.status == "claimed":
        a.status = "pending"
    ledger.append(a)                       # reservation BEFORE the call
    reply = None
    try:
        reply, usage, finish_reason = client.generate(model_id, prompt, cfg)
        a.output_chars = record_chars(reply)
        a.usage = usage
        a.finish_reason = finish_reason     # Claude 0234: verbatim onto the row
        a.status = "completed"; a.ended_utc = now()
    except OciAdapter.Throttled:
        a.status = "throttled"; a.ended_utc = now(); a.error = "429 throttled"
        ledger.append(a)
        raise
    except TimeoutError:
        a.status = "timeout"; a.ended_utc = now(); a.error = "timeout"
    except Exception as e:
        # response may or may not have been billable: unknown, counted as spent
        a.status = "unknown"; a.ended_utc = now(); a.error = str(e)[:200]
    ledger.append(a)                       # closing row (supersedes pending)
    return a, reply

def release_claim(ledger, a):
    """A claim that will never make a call (client misconfiguration) releases with
    zero bytes; released rows count as no spend and no attempt."""
    c = Attempt(a["run_id"] if isinstance(a, dict) else a.run_id,
                a["seed_id"] if isinstance(a, dict) else a.seed_id,
                a["model"] if isinstance(a, dict) else a.model)
    if isinstance(a, dict):
        c.attempt_id = a["attempt_id"]
        c.pair_key = a.get("pair_key")
    else:
        c.attempt_id = a.attempt_id
        c.pair_key = a.pair_key
    c.status = "released"; c.ended_utc = now(); c.error = "released without a call"
    ledger.append(c)

def unparseable_by_finish_reason(ledger):
    """Run-end count of unparseable rows by the reply's finish_reason (Claude 0234):
    "length" names a max_tokens cut, "stop" with prose a format refusal, "unset"
    a close that predates the field."""
    counts = {}
    for r in ledger.effective_rows():
        if r["status"] == "unparseable":
            k = r.get("finish_reason") or "unset"
            counts[k] = counts.get(k, 0) + 1
    return counts

def close_attempt(ledger, a, status, error):
    """F6 close: supersede a completed attempt with an unparseable one (same
    attempt_id, last row wins). The reply's byte count is known, so the settled
    output term stays; the pair itself never counts as made."""
    c = Attempt(a["run_id"], a["seed_id"], a["model"])
    c.attempt_id = a["attempt_id"]
    c.pair_key = a.get("pair_key")
    c.input_chars = a["input_chars"]; c.output_chars = a["output_chars"]
    fr = a["finish_reason"] if isinstance(a, dict) else a.finish_reason
    if status == "unparseable" and fr:
        error = f"{error}; finish_reason: {fr}"   # Claude 0234: "length" names a max_tokens cut
    c.finish_reason = fr
    c.usage = a["usage"]; c.status = status; c.error = error; c.ended_utc = now()
    ledger.append(c)

class StubClient:
    """No key, no network. Scripted behaviors per call, from a test script."""
    def __init__(self, script):
        self.script = script  # SHARED list: attempts pop from it in order
    def generate(self, model_id, prompt, cfg):
        if not self.script:
            raise RuntimeError("stub script exhausted")
        action = self.script.pop(0)
        if action == "ok":
            reply = '{"instruction": "Write a short haiku about rain.", "response": "Soft rain on the roof,\\n.each drop a small metronome,\\nthe night keeps its time."}'
            return reply, {"output_tokens": 42}, "stop"
        if action == "timeout":
            raise TimeoutError("stub timeout")
        if action == "boom":          # unknown: billable-or-not undecidable
            raise RuntimeError("stub connection dropped mid-call")
        if action == "throttle":
            raise OciAdapter.Throttled("stub 429")
        if action == "badjson":
            return "here is prose, no json at all", None, "stop"
        if action == "truncated":     # max_tokens cut the JSON mid-string (Claude 0234)
            return '{"instruction": "Write a short haiku about r', None, "length"
        raise ValueError("bad stub action")

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

class OciAdapter:
    """The real OCI path, .generate(model_id, prompt, cfg) on top of the SDK client.
    DOC-CHECKED against the OCI Python SDK docs (Instinct 0003), NOT yet run live:
    - Llama and every other GENERIC family rides client.chat() with
      GenericChatRequest(api_format=GENERIC, messages=[user message]); generate_text()
      is documented without Llama 4 support and is NOT used.
    - num_generations is pinned to 1 in the request; the reservation arithmetic
      assumes one generation, and a config asking for more fails fast in main().
    - the client is built with oci.retry.NoneRetryStrategy() so the SDK performs no
      hidden retries: one runner attempt is one billed call.
    - the reply is read from the real path: chat() returns ChatResult, whose
      chat_response is a GenericChatResponse for GENERIC chat with
      choices[0].message.content a LIST of ChatContent (TextContent.text carries
      the text), and chat_response.text for Cohere. message.content is unwrapped
      in order, never str()-ed: stringifying a list books a billed attempt whose
      reply can never parse (Instinct 0048 B). A reply without a readable text
      field is an error, never a longest-string guess across the whole response.
    - any usage object in the response is returned verbatim for reconciliation
      (GenericChatResponse.usage, ChatChoice.usage, or the Cohere usage).
    The response shape is SHAPE-CHECKED against oci 2.191.0's own model classes
    (built and walked in the suite without a live call); the request side stays
    DOC-CHECKED against the SDK docs, NOT yet run live.
    The oci import stays lazy so the stub path never needs the package."""
    def __init__(self, cfg):
        import oci
        conf = oci.config.from_file(profile_name=cfg.get("profile", "DEFAULT"))
        self.client = oci.generative_ai_inference.GenerativeAiInferenceClient(conf,
            service_endpoint=f"https://inference.generativeai.{cfg['region']}.oci.oraclecloud.com",
            retry_strategy=oci.retry.NoneRetryStrategy())
        self.cfg = cfg
    class Throttled(Exception):
        pass
    @staticmethod
    def _as_dict(o):
        """SDK model objects become plain dicts via to_dict(); objects without
        one (ChatResult itself) are read attribute-wise, so the real response
        and legacy dict fixtures walk the same code."""
        if o is None or isinstance(o, (str, int, float, bool)):
            return o
        if isinstance(o, dict):
            return o
        try:
            return o.to_dict()
        except AttributeError:
            # Some SDK model classes (oci 2.191.0: Usage, ChatResult) carry no
            # to_dict; their instance __dict__ is the attribute map, with the
            # real fields under underscore-prefixed names (ChatResult keeps
            # _chat_response and exposes chat_response as a property).
            d = getattr(o, "__dict__", None)
            if isinstance(d, dict):
                d = {k.lstrip("_"): v for k, v in d.items()}
                d.pop("attribute_map", None); d.pop("swagger_types", None)
                if d:
                    return d
            d = {n: getattr(o, n) for n in ("chat_response", "choices", "message",
                                            "content", "text", "usage") if hasattr(o, n)}
            return d if d else o
    @staticmethod
    def _content_text(content):
        """message.content arrives as a plain string OR a list of ChatContent
        (TextContent.text). A real GENERIC reply is the list shape; str(list)
        would book a billed attempt whose reply can never parse, so a list is
        unwrapped in order and non-TEXT parts are skipped (Instinct 0048 B)."""
        if content is None:
            return None
        if isinstance(content, str):
            return content if content.strip() else None
        items = content if isinstance(content, (list, tuple)) else [content]
        parts = []
        for item in items:
            if isinstance(item, str):
                text = item
            elif isinstance(item, dict):
                text = item.get("text")
            else:
                text = getattr(item, "text", None)
            if isinstance(text, str) and text:
                parts.append(text)
        if not parts:
            return None
        return "".join(parts)
    def _reply_and_usage(self, result):
        d = self._as_dict(result)
        if not isinstance(d, dict):
            raise RuntimeError("reply arrived without a message text field; refusing to "
                               f"guess. type: {type(result).__name__}")
        # GENERIC: ChatResult.chat_response.choices[i].message.content (the real
        # SDK shape), with the legacy flat {"choices":[...]} dict also accepted;
        # Cohere: chat_response.text.
        cr = d.get("chat_response")
        containers = [self._as_dict(cr) if cr is not None else None, d]
        for container in containers:
            if not isinstance(container, dict):
                continue
            choices = container.get("choices")
            if choices:
                ch = self._as_dict(choices[0])
                msg = ch.get("message") if isinstance(ch, dict) else None
                md = self._as_dict(msg) if msg is not None else None
                text = self._content_text(md.get("content") if isinstance(md, dict) else None)
                if text:
                    usage = container.get("usage")
                    if usage is None and isinstance(ch, dict):
                        usage = ch.get("usage")
                    ud = self._as_dict(usage) if usage is not None else None
                    fr = ch.get("finish_reason") if isinstance(ch, dict) else None
                    return text, (ud if isinstance(ud, dict) else None), fr
        crd = self._as_dict(cr) if cr is not None else None
        if isinstance(crd, dict):
            text = crd.get("text")
            if isinstance(text, str) and text.strip():
                ud = self._as_dict(crd.get("usage")) if crd.get("usage") is not None else None
                return text, (ud if isinstance(ud, dict) else None), crd.get("finish_reason")
        raise RuntimeError("reply arrived without a message text field; refusing to "
                           f"guess. keys: {sorted(d.keys()) if isinstance(d, dict) else type(result).__name__}")
    def generate(self, model_id, prompt, cfg):
        import oci
        if int(cfg.get("num_generations", 1)) != 1:
            raise ValueError("num_generations must be 1: the reservation arithmetic "
                             "covers one generation only, and every extra generation bills")
        if model_id.startswith("cohere"):
            detail = oci.generative_ai_inference.models.ChatDetails()
            detail.serving_mode = oci.generative_ai_inference.models.OnDemandServingMode(model_id=model_id)
            detail.chat_request = oci.generative_ai_inference.models.CohereChatRequest(
                message=prompt, max_tokens=cfg["max_tokens"], temperature=cfg["temperature"])
        else:
            detail = oci.generative_ai_inference.models.ChatDetails()
            detail.serving_mode = oci.generative_ai_inference.models.OnDemandServingMode(model_id=model_id)
            msg = oci.generative_ai_inference.models.Message(role="USER", content=[{"type": "TEXT", "text": prompt}])
            detail.chat_request = oci.generative_ai_inference.models.GenericChatRequest(
                api_format=oci.generative_ai_inference.models.GenericChatRequest.API_FORMAT_GENERIC,
                messages=[msg], max_tokens=cfg["max_tokens"], temperature=cfg["temperature"],
                num_generations=1)
        detail.compartment_id = cfg["compartment_id"]
        try:
            result = self.client.chat(detail)
            return self._reply_and_usage(result)
        except oci.exceptions.ServiceError as e:
            if e.status == 429:
                raise OciAdapter.Throttled(str(e))
            raise

def build_client(cfg):  # real path (stage 1+): region from config, adapter provides .generate
    return OciAdapter(cfg)

def pick_model(cfg, seed_id, ledger_rows, pair_key=None):
    """Deterministic resume: a seed's spent rows fix the model for every pair.
    A pending/claimed row blocks only ITS pair when the row carries a pair_key
    (Instinct 0048 A); pair_key=None keeps the conservative whole-seed read for
    direct callers and pre-pair-key rows."""
    allowed = {m["model_id"] for m in cfg["models"]}
    if not allowed:
        raise ValueError("empty allow-list")
    for r in ledger_rows:               # deterministic resume: same seed, same model
        if r["seed_id"] == seed_id:
            if r["status"] in ("pending", "claimed"):
                if pair_key is None or r.get("pair_key") in (None, pair_key):
                    return None         # another worker holds this pair in flight
            if r["status"] in SPENT_STATUSES + ("unparseable",):
                return r["model"]
    ids = sorted(allowed)
    return ids[stable_slot(seed_id, len(ids))]

def acquire_run_lock(ledger_path):
    """One-worker enforcement as code (Instinct 0003 item 5): a non-blocking
    exclusive flock on <ledger>.runlock held for the process lifetime. Returns the
    open fd the caller must keep, or None when another worker already holds it."""
    fd = open(ledger_path + ".runlock", "a")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        fd.close()
        return None


class ConfigError(Exception):
    pass

def _finite_positive(value):
    """A real, finite, positive config number. bool is excluded explicitly
    (isinstance(True, int) is True); NaN and inf fail math.isfinite, so an
    overflow literal like 1e309 cannot silently remove the dollar bound."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)

def validate_config(cfg, dry_run=False):
    """Clear, named config failure BEFORE the run lock, auth, or any network call
    (a bare KeyError on an unset owner value is a readiness defect, not a guard).
    Dry runs need only the file paths: the shipped example config must dry-run
    clean while its owner-set values stay placeholders."""
    errs = []
    for key, why in (("seeds_path", "path to the seeds JSONL"),
                     ("out_path", "path for generated pairs"),
                     ("ledger_path", "path for the append-only ledger")):
        if not cfg.get(key):
            errs.append(f"config error: missing {key}: {why}")
    if errs:
        raise ConfigError("\n".join(errs))
    if dry_run:
        return
    if not cfg.get("models") or not all(isinstance(m, dict) and m.get("model_id")
                                        for m in cfg["models"]):
        errs.append("config error: models must be a non-empty allow-list of "
                    "{\"model_id\": ...} entries")
    if not cfg.get("region"):
        errs.append("config error: missing region: the on-demand region for the "
                    "model (Llama 4 on-demand is us-chicago-1, not us-ashburn-1)")
    comp = cfg.get("compartment_id")
    if not comp or comp == "SET_ME":
        errs.append("config error: compartment_id is unset (placeholder SET_ME): "
                    "the owner sets the tenancy compartment; refusing before auth")
    budget = cfg.get("budget_usd")
    if not _finite_positive(budget):
        errs.append("config error: budget_usd must be a finite positive number "
                    f"(got {budget!r}): the dollar cap the ledger enforces; the "
                    "owner sets it as the approval step; refusing before any call")
    price = cfg.get("price_per_million_chars", 0.18)
    if not _finite_positive(price):
        errs.append("config error: price_per_million_chars must be a finite "
                    f"positive number (got {price!r})")
    elif _finite_positive(budget):
        allowance = budget / price * 1e6
        if not math.isfinite(allowance) or allowance <= 0:
            errs.append("config error: budget_usd / price_per_million_chars "
                        "overflows to a non-finite char allowance (the ledger "
                        "budget in chars); refusing before any call: a tiny "
                        "positive price would silently remove the dollar cap")
    mt = cfg.get("max_tokens")
    if not isinstance(mt, int) or isinstance(mt, bool) or mt <= 0:
        errs.append("config error: max_tokens missing or invalid: without it no reply "
                    "term can be reserved, and the attempt would silently carry only "
                    "its prompt charge (Claude 2235 N6)")
    cap = cfg.get("attempt_char_cap")
    if not _finite_positive(cap) or int(cap) != cap:
        errs.append("config error: attempt_char_cap must be a positive whole number "
                    f"of chars (got {cap!r}): the per-attempt reservation ceiling, at "
                    "least input bytes + overhead + the reply term (113 x max_tokens "
                    "= 115,712 bytes at 1,024 tokens); the owner sets it")
    sp = cfg.get("seeds_path")
    if sp and not os.path.exists(sp):
        errs.append(f"config error: seeds_path {sp} does not exist")
    if errs:
        raise ConfigError("\n".join(errs))

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
    ap.add_argument("--max-retries", type=int, default=None,
                    help="bounded retries per pair after a 429; default from config")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = json.load(open(args.config))
    cfg["run_id"] = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    if int(cfg.get("num_generations", 1)) != 1:
        sys.exit("config sets num_generations != 1: the reservation covers one "
                 "generation; every extra generation multiplies the bill")
    try:
        validate_config(cfg, dry_run=args.dry_run)
    except ConfigError as e:
        sys.exit(str(e))
    run_lock_fd = acquire_run_lock(cfg["ledger_path"])
    if run_lock_fd is None:
        sys.stderr.write("another runner process holds the run lock on this ledger; "
                         "exiting without booking anything (one-worker rule)\n")
        sys.exit(2)
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
    # 113 x max_tokens characters. max_tokens and attempt_char_cap are validated
    # in validate_config() before the run lock.
    max_out_chars = cfg.get("max_chars_per_token", 113) * cfg["max_tokens"]
    overhead = int(cfg.get("prompt_overhead_chars", 200))  # ASSUMPTION, stated: 200 bytes of per-call overhead until stage-1 reconciliation settles the real number (Instinct 0008 item 3)
    max_retries = args.max_retries if args.max_retries is not None else int(cfg.get("max_retries", 2))
    os.makedirs(os.path.dirname(cfg["out_path"]) or ".", exist_ok=True)
    seeds_done = load_seed_ids(cfg["out_path"])
    n_max = args.limit if args.limit else 10**9
    made = 0
    skipped_status = {}
    cap_refusals = 0
    held_seen = []
    budget_hit = attempts_cap_hit = False
    for seed in cfg_seeds(cfg):
        if made >= n_max or budget_hit or attempts_cap_hit:
            break
        # Prompt-pack gate (Instinct 0010): only execution_status="eligible" rows run.
        st = seed.get("execution_status", "eligible")
        if st != "eligible":
            skipped_status[st] = skipped_status.get(st, 0) + 1
            continue
        # Pack source-language hold (Instinct 0445/0446, ChatGPT 0404/0502):
        # an eligible-marked row the pack flags as not model-supported stays
        # skipped, counted by hold reason in the run-end line.
        hold = hold_reason(seed)
        if hold:
            skipped_status[hold] = skipped_status.get(hold, 0) + 1
            continue
        if cfg.get("require_rendered_prompt") and not seed.get("prompt"):
            sys.exit(f"seed {seed.get('id')} has no rendered prompt field and "
                     "require_rendered_prompt is set; refusing to run a template prompt")
        prompt = seed.get("prompt") or f"prompt for {seed['topic']} (pair 1)"
        prompt_source = "rendered" if seed.get("prompt") else "template"
        for k in range(seed.get("n_pairs", cfg.get("pairs_per_seed", 4))):
            if made >= n_max or budget_hit or attempts_cap_hit:
                break
            pk = f"{seed['id']}#{k + 1}"     # pair-scoped claim key (Instinct 0048 A)
            model_id = pick_model(cfg, seed["id"], ledger.effective_rows(), pk)
            if model_id is None:
                held_seen.append(f"{pk} (held by another worker)")
                continue                    # another worker holds this pair in flight
            rec_id = f"{seed['id']}#{k+1}#{model_id.rsplit('.',1)[-1][:24]}"
            if rec_id in seeds_done: continue
            if args.max_attempts and ledger.attempts() >= args.max_attempts:
                attempts_cap_hit = True; break
            if ledger.spent_chars() + cfg["attempt_char_cap"] > budget_chars:
                budget_hit = True; break
            # Bounded 429 retry: each retry is a NEW reserved attempt, never a
            # silent repeat of the same one (Instinct 0007 item 3).
            for try_n in range(max_retries + 1):
                claim = claim_seed(cfg, pk, model_id, ledger)
                if claim is None:
                    held_seen.append(f"{pk} (held or already attempted)")
                    break
                try:
                    a, reply = run_once(cfg, lambda: build_client(cfg), seed, model_id,
                                        prompt, ledger, max_out_chars=max_out_chars,
                                        attempt_row=claim, overhead_chars=overhead)
                except OciAdapter.Throttled:
                    if try_n < max_retries:
                        time.sleep(min(0.5 * (2 ** try_n), 8.0))
                        continue
                    break
                except Exception:
                    release_claim(ledger, claim.row())
                    raise
                if isinstance(a, dict) and a.get("status") == "refused_cap":
                    cap_refusals += 1
                    release_claim(ledger, claim.row())
                    print(f"cap refusal: {a['error']}")
                    break
                if a is None or reply is None or a.status != "completed":
                    break
                # F6 parse-and-filter (Claude 2238 #6): a pair counts as MADE only when it
                # parses and clears the length floor. An unparseable or too-short reply
                # closes its attempt as "unparseable" and inflates nothing.
                try:
                    instr, resp = parse_pair(reply)
                except (ValueError, json.JSONDecodeError) as e:
                    close_attempt(ledger, a.row(), "unparseable", f"parse: {e}")
                    break
                if len(instr.encode("utf-8")) < 15 or len(resp.encode("utf-8")) < 15:
                    close_attempt(ledger, a.row(), "unparseable", "below 15-byte floor")
                    break
                rec = {
                    "id": rec_id,
                    "instruction": instr,
                    "response": resp,
                    "source": "synthetic",
                    "language": seed.get("language"),
                    "split": seed.get("split"),
                    "prompt_source": prompt_source,
                    "seed": {kk: vv for kk, vv in seed.items() if kk not in ("prompt",)},
                    "generator": {
                        "model": model_id,
                        "template": cfg.get("template", "pair-from-topic-v1"),
                        "temperature": cfg["temperature"],
                        "max_tokens": cfg["max_tokens"],
                        "num_generations": 1,
                        "prompt_overhead_chars": overhead,
                    },
                    "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
                with open(cfg["out_path"], "a", encoding="utf-8") as out:
                    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    out.flush()
                seeds_done.add(rec_id)
                made += 1
                break
    # Held seeds are LISTED, never auto-released (Instinct 0002 item 3): a person
    # reconciles each against Oracle's usage view before the seed unblocks.
    held = sorted({(r.get("pair_key") or r["seed_id"]) for r in ledger.effective_rows()
                   if r["status"] in HELD_STATUSES})
    closed_unretried = sorted({(r.get("pair_key") or r["seed_id"]) for r in ledger.effective_rows()
                               if r["status"] in ("unknown","timeout","unparseable")})
    if skipped_status:
        print(f"skipped rows by execution_status: "
              f"{', '.join(f'{k}={v}' for k, v in sorted(skipped_status.items()))}")
    if cap_refusals:
        print(f"cap refusals (nothing sent): {cap_refusals}")
    if held_seen:
        print(f"pairs skipped this run (held or already attempted): "
              f"{', '.join(sorted(set(held_seen)))}")
    if held:
        print(f"held seeds awaiting manual reconciliation against Oracle's usage view: "
              f"{', '.join(held)}")
    if closed_unretried:
        print(f"closed attempts NOT auto-retried (unknown/timeout/unparseable; lead rule, "
              f"no silent re-bill): {', '.join(closed_unretried)}")
    unp_fr = unparseable_by_finish_reason(ledger)
    if unp_fr:
        print("unparseable by finish_reason (Claude 0234): "
              + ", ".join(f"{k}={v}" for k, v in sorted(unp_fr.items())))
    note = "" if ledger.attempts() else (" (no attempts booked: seeds empty, budget "
                                         "exhausted, cap refusals, or cap already reached)")
    print(f"done: run {cfg['run_id']} made {made} pairs; ledger {ledger.attempts()} attempts, "
          f"{ledger.spent_chars()} chars, est ${ledger.spent_chars()/1e6*price:.2f}{note}")
    if made == 0 and cap_refusals:
        # Zero pairs because every attempt was refused is NOT a successful run:
        # exit non-zero so rc 0 can never masquerade as calibration evidence.
        # (A zero-pair run whose rows were all skipped pending/held exits 0:
        # that is the legitimate unactivated state, not a failure.)
        sys.stderr.write(f"NOT a successful run: 0 pairs made, {cap_refusals} cap "
                         "refusals (nothing was sent). attempt_char_cap must clear "
                         "prompt bytes + overhead + 113 x max_tokens; raise it or "
                         "lower max_tokens.\n")
        sys.exit(3)

def cfg_seeds(cfg):
    return [json.loads(l) for l in open(cfg["seeds_path"]) if l.strip() and not l.startswith("#")]

if __name__ == "__main__":
    main()
