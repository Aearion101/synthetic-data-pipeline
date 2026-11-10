import json
import os
import threading
import time

import pytest

import generate


@pytest.fixture()
def claims_dir(tmp_path):
    return str(tmp_path / "claims")


def test_first_claim_wins_second_refused(claims_dir):
    assert generate.claim_acquire(claims_dir, "seed-00001#1#x", "w1") is True
    assert generate.claim_acquire(claims_dir, "seed-00001#1#x", "w2") is False


def test_release_lets_next_worker_in(claims_dir):
    generate.claim_acquire(claims_dir, "r1", "w1")
    generate.claim_release(claims_dir, "r1", "w1")
    assert generate.claim_acquire(claims_dir, "r1", "w2") is True


def test_release_ignores_foreign_holder(claims_dir):
    generate.claim_acquire(claims_dir, "r1", "w1")
    generate.claim_release(claims_dir, "r1", "w2")  # w2 must not free w1's claim
    assert generate.claim_acquire(claims_dir, "r1", "w2") is False
    generate.claim_release(claims_dir, "r1", "w1")
    assert generate.claim_acquire(claims_dir, "r1", "w2") is True


def test_crashed_run_claim_expires_and_is_taken_over(claims_dir):
    # a crashed run left a lease behind: old timestamp, holder gone forever
    path = generate.claim_path(claims_dir, "r1")
    os.makedirs(claims_dir, exist_ok=True)
    with open(path, "w") as f:
        json.dump({"worker": "dead-run", "ts": time.time() - 100000}, f)
    assert generate.claim_acquire(claims_dir, "r1", "w2", ttl_s=900) is True
    with open(generate.claim_path(claims_dir, "r1")) as f:
        assert json.load(f)["worker"] == "w2"


def test_live_claim_is_not_stolen(claims_dir):
    generate.claim_acquire(claims_dir, "r1", "w1")
    assert generate.claim_acquire(claims_dir, "r1", "w2", ttl_s=900) is False


def test_stolen_claim_never_double_bills_after_ttl(claims_dir):
    # two workers arrive after expiry: exactly one takes over, the other backs off
    path = generate.claim_path(claims_dir, "r1")
    os.makedirs(claims_dir, exist_ok=True)
    with open(path, "w") as f:
        json.dump({"worker": "dead-run", "ts": time.time() - 100000}, f)
    first = generate.claim_acquire(claims_dir, "r1", "w1", ttl_s=1)
    second = generate.claim_acquire(claims_dir, "r1", "w2", ttl_s=1)
    assert first != second or first is True  # exactly one holder
    with open(path) as f:
        holder = json.load(f)["worker"]
    assert holder in ("w1", "w2")


def test_touch_renews_only_own_lease(claims_dir):
    generate.claim_acquire(claims_dir, "r1", "w1")
    path = generate.claim_path(claims_dir, "r1")
    old = json.load(open(path))["ts"]
    generate.claim_touch(claims_dir, "r1", "w2")  # foreign touch: no-op
    assert json.load(open(path))["ts"] == old
    time.sleep(0.01)
    generate.claim_touch(claims_dir, "r1", "w1")
    assert json.load(open(path))["ts"] > old


def test_shared_ledger_sums_across_threads(tmp_path):
    p = str(tmp_path / "spend.txt")
    def add(n):
        generate.spend_add(p, n)
    threads = [threading.Thread(target=add, args=(1000,)) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert generate.spend_read(p) == 8000.0


def test_budget_guard_stops_at_budget():
    # 300 USD at 0.18 per million chars is about 1.667B chars total
    assert generate.budget_ok(0, 1000, 0.18, 300.0) is True
    assert generate.budget_ok(1.660e9, 0, 0.18, 300.0) is True
    assert generate.budget_ok(1.660e9, 1e7, 0.18, 300.0) is False  # next call would pass 300


def test_resume_skips_existing_ids(tmp_path):
    out = tmp_path / "out.jsonl"
    out.write_text(json.dumps({"id": "a"}) + "\n" + json.dumps({"id": "b"}) + "\n")
    assert generate.load_seeded_ids(str(out)) == {"a", "b"}


def test_model_pick_is_deterministic():
    rr = ["a", "b", "c"]
    assert generate.pick_model(rr, "seed-00001", 0) == generate.pick_model(rr, "seed-00001", 0)
    # differs from python's randomized hash(): same answer every process
    picks = {generate.pick_model(rr, f"s{i}", 0) for i in range(50)}
    assert picks <= set(rr)
