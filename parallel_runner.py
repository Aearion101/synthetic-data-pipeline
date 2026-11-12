"""Bounded network concurrency for runner_v2. The calling thread alone owns
ledger/config/output state. Worker threads own only an adapter and one call.
No live requests are made by this module's tests.
"""
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import fcntl
import json
import os
import sys
import signal
from contextlib import contextmanager
import threading
import time
from datetime import datetime, timezone

from runner_common import Throttled


@contextmanager
def defer_sigint():
    """Keep OS SIGINT pending across a short ledger/future ownership transfer.

    POSIX is already required by the runner's flock. Only SIGINT is masked;
    restore the caller's exact mask, delivering a pending interrupt immediately
    after the reservation is drain-visible (or settlement is durable). Workers
    need no SIGINT handling; Python delivers it in the main coordinator thread.
    """
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


class Pacer:
    """One admission gate for all request starts, including retries.

    Positive configured spacing is NOT an Oracle limit. A 429 pushes the shared
    next admission back and doubles spacing (up to at least 60s). There is no
    automatic speed increase on success: only an owner config change after
    measurement may raise throughput. Already admitted calls cannot be undone.
    Waiters recheck the shared deadline after every wake, so a 429 also delays
    workers that were already waiting. No ahead-of-time slots are issued.
    """
    def __init__(self, interval, clock=time.monotonic):
        self.interval = float(interval)
        self.ceiling = max(60.0, self.interval)
        self.clock = clock
        self.condition = threading.Condition()
        self.next_start = 0.0
        self.stopped = False

    def acquire(self):
        with self.condition:
            while not self.stopped:
                now = self.clock()
                delay = self.next_start - now
                if delay <= 0:
                    self.next_start = now + self.interval
                    return True
                self.condition.wait(timeout=delay)
            return False

    def throttled(self):
        with self.condition:
            self.interval = min(self.ceiling, self.interval * 2)
            self.next_start = max(self.next_start, self.clock() + self.interval)
            self.condition.notify_all()

    def stop(self):
        with self.condition:
            self.stopped = True
            self.condition.notify_all()


def reserve(R, cfg, ledger, pair_key, model, input_chars, output_chars,
            max_attempts, budget_chars):
    """Atomically reserve count and estimated chars before submitting a call.

    The coordinator is the only local ledger owner; the flock also preserves
    the original inter-process claim contract. A claimed row carries the FULL
    input/reply reservation immediately, never a zero-byte window. All unknown
    or in-flight work remains spent/blocked on resume, never auto-released.
    """
    with open(ledger._lockfile(), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            ledger.refresh()
            rows = ledger.effective_rows()
            # Legacy pre-pair claims conservatively hold the entire seed.
            seed_id = pair_key.rsplit("#", 1)[0]
            if any(r["status"] in R.BLOCKED_STATUSES and
                   (r.get("pair_key") == pair_key or
                    (r["seed_id"] == seed_id and not r.get("pair_key")))
                   for r in rows):
                return None, "held"
            if max_attempts and ledger.attempts() >= max_attempts:
                return None, "attempts_cap"
            amount = input_chars + output_chars
            if amount > cfg["attempt_char_cap"]:
                return None, "refused_cap"
            if ledger.spent_chars() + amount > budget_chars:
                return None, "budget"
            a = R.Attempt(cfg["run_id"], seed_id, model)
            a.pair_key = pair_key
            a.input_chars, a.output_chars = input_chars, output_chars
            a.status = "pending"
            ledger._append_unlocked(a.row())
            return a, None
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _call(client, model, prompt, cfg, pacer):
    # No shared Ledger, file handle, or mutable runner state in a worker.
    if not pacer.acquire():
        return "not_sent", None
    try:
        return "completed", client.generate(model, prompt, cfg)
    except Throttled:
        pacer.throttled()           # immediately affects every waiting worker
        return "throttled", None
    except TimeoutError:
        return "timeout", None
    except Exception:
        # Bearer adapter already sanitizes, but do not reopen that boundary by
        # carrying exception objects/tracebacks or diagnostic text across here.
        return "unknown", None


class PersistenceError(RuntimeError):
    """Fixed diagnostic only: never carry file contents or a remote response."""


class CoordinatorError(RuntimeError):
    """Unexpected coordinator failure after stopping admissions and draining."""


def preflight_seeds(R, cfg):
    """Discover missing eligible rendered prompts before any reservation.

    Held/ineligible rows remain held, not accidentally activated by validation.
    Materialize once so the same validated rows feed this coordinator run.
    """
    seeds = R.cfg_seeds(cfg)
    for seed in seeds:
        if (seed.get("execution_status", "eligible") == "eligible"
                and not R.hold_reason(seed)
                and cfg.get("require_rendered_prompt")
                and (not isinstance(seed.get("prompt"), str) or not seed["prompt"].strip())):
            raise R.ConfigError("SEED PREFLIGHT FAILED: eligible seed lacks a rendered prompt; no calls made")
    return seeds


def output_id(row):
    pair_key = row.get("pair_key")
    model = row.get("model")
    if not isinstance(pair_key, str) or not isinstance(model, str):
        return None
    return f"{pair_key}#{model.rsplit('.', 1)[-1][:24]}"


def check_resume(R, cfg, ledger):
    """Detect, never hide, disagreements between output and ledger on startup.

    Old completed-without-output rows are unrecoverable locally: do not quietly
    call them successes or buy them again. Output-with-pending rows are the safe
    reverse window: durable pair present, conservative reservation still held.
    Report them, skip them, and leave accounting for manual reconciliation.
    """
    preflight_seeds(R, cfg)
    # Never append after an interrupted partial record: a human must reconcile
    # that tail first. Refusal messages contain no record text or credentials.
    for path in (cfg["ledger_path"], cfg["out_path"]):
        if os.path.exists(path) and os.path.getsize(path):
            with open(path, "rb") as source_file:
                source_file.seek(-1, os.SEEK_END)
                if source_file.read(1) != b"\n":
                    raise R.ConfigError("incomplete ledger/output tail; reconcile before resuming")
    if os.path.exists(cfg["out_path"]):
        try:
            with open(cfg["out_path"], encoding="utf-8") as output:
                for line in output:
                    if line.strip():
                        row = json.loads(line)
                        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                                or not all(isinstance(row.get(key), str)
                                           and len(row[key].encode("utf-8")) >= 15
                                           for key in ("instruction", "response"))):
                            raise ValueError()
        except (ValueError, UnicodeDecodeError):
            raise R.ConfigError("invalid output record; reconcile before resuming") from None
    outputs = {}
    if os.path.exists(cfg["out_path"]):
        with open(cfg["out_path"], encoding="utf-8") as output:
            for line in output:
                if line.strip():
                    rec = json.loads(line)
                    if rec["id"] in outputs:
                        raise PersistenceError("duplicate output ids; reconciliation required before resuming")
                    outputs[rec["id"]] = rec
    missing = uncertain_with_output = uncertain_without_output = 0
    for row in ledger.effective_rows():
        record = outputs.get(output_id(row))
        matches = (record is not None and
                   record.get("attempt_id", row["attempt_id"]) == row["attempt_id"])
        if row["status"] == "completed" and not matches:
            missing += 1
        if row["status"] in ("pending", "claimed", "unknown", "timeout"):
            if matches:
                uncertain_with_output += 1
            else:
                uncertain_without_output += 1
    if missing:
        raise PersistenceError(
            f"RECONCILIATION REQUIRED: {missing} completed attempt(s) lack matching "
            "output records. No new calls made; do not delete the ledger or "
            "automatically retry these spent attempts")
    if uncertain_with_output or uncertain_without_output:
        sys.stderr.write(
            f"RESUME RECONCILIATION: {uncertain_with_output} uncertain attempt(s) "
            f"have output; {uncertain_without_output} have no matching output. "
            "All remain counted and blocked from automatic retries. "
            "Reconcile against saved outputs and tenancy billing records.\n")


def persist_output(path, record):
    """Commit the full pair before allowing the ledger to say completed."""
    created = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as output:
        output.write(json.dumps(record, ensure_ascii=False) + "\n")
        output.flush()
        os.fsync(output.fileno())
    if created:
        # Sync the directory entry too, where the filesystem supports it. An
        # error is a failed commit, never permission to report completion.
        fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def run_parallel(R, cfg, ledger, *, workers, interval, max_attempts,
                 budget_chars, max_out_chars, overhead, max_retries, n_max):
    """Bounded submission: at most workers futures, not 12,000 queued calls.

    The coordinator serializes all ledger and output operations. Request and
    estimated-char reservations include every in-flight future and retry.
    --limit also includes potential results already in flight. On interrupt,
    no new work is admitted; waiters stop, admitted calls settle, and uncertain
    work remains spent. A killed process's pending rows stay held on restart.
    """
    seeds = preflight_seeds(R, cfg)
    made = cap_refusals = 0
    skipped, held = {}, []
    seeds_done = R.load_seed_ids(cfg["out_path"])
    pacer = Pacer(interval)
    retries = deque()
    exhausted = budget_hit = attempts_cap_hit = False
    interrupted = False
    persistence_failed = False
    coordinator_failed = False
    in_flight_keys = set()

    def jobs():
        for seed in seeds:
            status = seed.get("execution_status", "eligible")
            reason = R.hold_reason(seed) if status == "eligible" else status
            if reason:
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            if cfg.get("require_rendered_prompt") and not seed.get("prompt"):
                raise R.ConfigError("eligible seed is missing its rendered prompt")
            prompt = seed.get("prompt") or f"prompt for {seed['topic']} (pair 1)"
            for k in range(seed.get("n_pairs", cfg.get("pairs_per_seed", 4))):
                yield (seed, k, prompt, 0)

    source = iter(jobs())
    futures = {}
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="oracle-call")

    def settle(future, item):
        nonlocal made
        a, seed, k, prompt, try_n, rec_id = item
        in_flight_keys.discard(a.pair_key)
        status, result = future.result()
        if status == "not_sent":
            R.release_claim(ledger, a)
            return
        a.status, a.ended_utc = status, R.now()
        if status == "completed":
            reply, a.usage, a.finish_reason = result
            a.output_chars = R.record_chars(reply)
        else:
            a.error = {"throttled": "429 throttled", "timeout": "timeout",
                       "unknown": "request failed; outcome unknown"}[status]
        # A successful reply is NOT completed until its validated output is
        # synced. Other outcomes may close immediately, with spent accounting.
        if status != "completed":
            ledger.append(a)
        if status == "throttled":
            if try_n < max_retries and not interrupted:
                retries.append((seed, k, prompt, try_n + 1))
            return
        if status != "completed":
            return
        try:
            instruction, response = R.parse_pair(reply)
        except (ValueError, json.JSONDecodeError):
            R.close_attempt(ledger, a.row(), "unparseable", "reply is not a valid pair")
            return
        if min(len(instruction.encode("utf-8")), len(response.encode("utf-8"))) < 15:
            R.close_attempt(ledger, a.row(), "unparseable", "below 15-byte floor")
            return
        rec = {
            "id": rec_id, "attempt_id": a.attempt_id,
            "instruction": instruction, "response": response,
            "source": "synthetic", "language": seed.get("language"),
            "split": seed.get("split"),
            "prompt_source": "rendered" if seed.get("prompt") else "template",
            "seed": {kk: vv for kk, vv in seed.items() if kk != "prompt"},
            "generator": {"model": a.model,
                          "template": cfg.get("template", "pair-from-topic-v1"),
                          "temperature": cfg["temperature"], "max_tokens": cfg["max_tokens"],
                          "num_generations": 1, "prompt_overhead_chars": overhead},
            "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        # Reverse the unsafe v1 window: output durable FIRST, then completed
        # ledger close. Any write/flush/fsync/close failure keeps the persisted
        # pending reservation. Startup reports the mismatch, never re-bills it.
        persist_output(cfg["out_path"], rec)
        seeds_done.add(rec_id)
        ledger.append(a)
        made += 1

    try:
        while True:
            while (not interrupted and not budget_hit and not attempts_cap_hit
                   and len(futures) < workers and made + len(futures) < n_max):
                if retries:
                    seed, k, prompt, try_n = retries.popleft()
                elif not exhausted:
                    try:
                        seed, k, prompt, try_n = next(source)
                    except StopIteration:
                        exhausted = True
                        break
                else:
                    break
                pk = f"{seed['id']}#{k + 1}"
                if pk in in_flight_keys:
                    held.append(pk)
                    continue
                model = R.pick_model(cfg, seed["id"], ledger.effective_rows(), pk)
                if model is None:
                    held.append(pk)
                    continue
                rec_id = f"{pk}#{model.rsplit('.', 1)[-1][:24]}"
                if rec_id in seeds_done:
                    continue
                # Construct and interface-check locally BEFORE any reservation.
                client = R.build_client(cfg)
                if not hasattr(client, "generate"):
                    raise TypeError("client has no generate method")
                # SIGINT must not strand a reservation between its durable
                # append and drain-map registration. The pending OS signal is
                # delivered only after the future owns that reservation.
                with defer_sigint():
                    a, reason = reserve(R, cfg, ledger, pk, model,
                                        R.record_chars(prompt) + overhead,
                                        max_out_chars, max_attempts, budget_chars)
                    if not reason:
                        in_flight_keys.add(pk)
                        try:
                            future = pool.submit(_call, client, model, prompt, cfg, pacer)
                        except BaseException:
                            # submit may enqueue BEFORE raising (e.g. a failed
                            # Thread.start while an existing worker is alive).
                            # No returned Future proves nothing about dispatch.
                            # Stop admissions, but retain the full pending/count
                            # reservation: queued work might already have called.
                            # Never label this uncertain handoff released/free.
                            pacer.stop()
                            in_flight_keys.discard(pk)
                            raise
                        futures[future] = (a, seed, k, prompt, try_n, rec_id)
                if reason:
                    if reason == "attempts_cap":
                        attempts_cap_hit = True
                    elif reason == "budget":
                        if futures:
                            retries.appendleft((seed, k, prompt, try_n))
                            break
                        budget_hit = True
                    elif reason == "refused_cap":
                        cap_refusals += 1
                    else:
                        held.append(pk)
                    continue
            if not futures:
                break
            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                # Do not lose drain ownership to SIGINT between pop and settle,
                # or interrupt the output-before-completed commit halfway.
                with defer_sigint():
                    settle(future, futures.pop(future))
    except BaseException as error:
        interrupted = isinstance(error, KeyboardInterrupt)
        persistence_failed = isinstance(error, OSError)
        coordinator_failed = not interrupted and not persistence_failed
        pacer.stop()
        # Every exceptional exit drains, not just I/O and Ctrl-C. Stop waiters
        # first; then persist successful admitted replies and release not_sent
        # reservations. One failed settlement must not discard other replies.
        # The failing settlement itself is NOT replayed: its output may already
        # be durable. Its pending row is conservatively left for reconciliation.
        for future, item in list(futures.items()):
            try:
                with defer_sigint():
                    settle(future, item)
            except BaseException as drain_error:
                if isinstance(drain_error, OSError):
                    persistence_failed = True
                elif isinstance(drain_error, KeyboardInterrupt):
                    interrupted = True
                else:
                    coordinator_failed = True
        futures.clear()
    finally:
        pacer.stop()
        pool.shutdown(wait=True)
    if persistence_failed:
        raise PersistenceError(
            "PERSISTENCE FAILED: stopped new admissions; output or ledger commit "
            "did not finish. Pending/unknown attempts remain spent and blocked. "
            "Check disk/permissions, then resume with the SAME files; "
            "reconcile reported output/ledger discrepancies, never delete the ledger") from None
    if coordinator_failed:
        raise CoordinatorError(
            "COORDINATOR FAILED: stopped admissions and drained outstanding calls "
            "where possible. Unsettled reservations remain spent and blocked; "
            "resume with the SAME files and reconcile any reported uncertainty") from None
    return dict(made=made, skipped_status=skipped, cap_refusals=cap_refusals,
                held_seen=held, budget_hit=budget_hit,
                attempts_cap_hit=attempts_cap_hit, interrupted=interrupted)
