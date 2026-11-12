#!/usr/bin/env python3
"""Build a small, language-spread seeds file from a prompt-pack train file. Stdlib only.

Why this exists: `head -n N` on the pack is all English for the first rows, and a
row the runner will hold (undocumented language) still counts toward N, so the
planned request count is not N. This picks N rows the runner will actually run,
round-robin across languages, keeps each WHOLE pack row (rendered prompt and
hold fields intact), sets execution_status to "eligible" and an explicit
n_pairs, so planned pairs = seeds x pairs-per-seed with no hidden default of 4.

Running it with --approve is the owner's go for exactly the rows it writes.
It makes no network call and never touches a ledger. It refuses to overwrite.

    python3 make_seeds.py --pack pack/v3.1-rev3/bulk_prompts_v3.1-rev3.onestage.train.jsonl \
        --n 20 --out seeds_pilot.jsonl --approve
"""
import argparse, json, sys
from collections import OrderedDict

from runner_v2 import hold_reason

PENDING = "pending_user_approval"


def select_rows(rows, n):
    """Round-robin over languages (sorted), pack order within a language.
    Only pending rows the runner would not hold. Returns (picked, skipped_held, skipped_status)."""
    by_lang = OrderedDict()
    held = status = 0
    for r in rows:
        if r.get("execution_status") != PENDING:
            status += 1
            continue
        if hold_reason(r) is not None:
            held += 1
            continue
        by_lang.setdefault(r.get("language", "?"), []).append(r)
    langs = sorted(by_lang)
    idx = {l: 0 for l in langs}
    picked = []
    while len(picked) < n:
        progressed = False
        for l in langs:
            if idx[l] < len(by_lang[l]):
                picked.append(by_lang[l][idx[l]])
                idx[l] += 1
                progressed = True
                if len(picked) == n:
                    break
        if not progressed:
            break
    return picked, held, status


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pack", required=True)
    ap.add_argument("--n", type=int, required=True, help="number of seed rows to write (>= 1)")
    ap.add_argument("--pairs-per-seed", type=int, default=1)
    ap.add_argument("--out", required=True)
    ap.add_argument("--approve", action="store_true",
                    help="required: marks the written rows eligible (the owner's go)")
    a = ap.parse_args(argv)
    if a.n < 1 or a.pairs_per_seed < 1:
        print("make_seeds: --n and --pairs-per-seed must be >= 1", file=sys.stderr)
        return 2
    if not a.approve:
        print("make_seeds: refusing without --approve (that flag is the owner's go for these rows)", file=sys.stderr)
        return 2
    rows = []
    with open(a.pack, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    print(f"make_seeds: {a.pack} line {i} is not valid JSON", file=sys.stderr)
                    return 2
    picked, held, status = select_rows(rows, a.n)
    if len(picked) < a.n:
        print(f"make_seeds: only {len(picked)} runnable rows available, wanted {a.n}; nothing written", file=sys.stderr)
        return 2
    try:
        out = open(a.out, "x", encoding="utf-8")
    except FileExistsError:
        print(f"make_seeds: {a.out} already exists; refusing to overwrite", file=sys.stderr)
        return 2
    with out:
        for r in picked:
            w = dict(r)
            w["execution_status"] = "eligible"
            w["n_pairs"] = a.pairs_per_seed
            out.write(json.dumps(w, ensure_ascii=False) + "\n")
    langs = {}
    for r in picked:
        langs[r.get("language", "?")] = langs.get(r.get("language", "?"), 0) + 1
    print(f"wrote {len(picked)} seeds to {a.out}")
    print(f"planned pairs = {len(picked)} x {a.pairs_per_seed} = {len(picked) * a.pairs_per_seed}")
    print("languages: " + ", ".join(f"{k}={v}" for k, v in sorted(langs.items())))
    print(f"skipped in pack: held by runner rule = {held}, not pending = {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
