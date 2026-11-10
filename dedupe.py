#!/usr/bin/env python3
"""Drop duplicate instruction pairs from JSONL before anything eats the data.

Exact dedup on normalized text (lowercase, punctuation and whitespace collapsed), plus a
near-dup pass on the first 160 characters of the instruction. Writes a cleaned file and a
dropped-count line. Near-dup beyond that (minhash) belongs to the QC pipeline plan.
"""
import json
import re
import sys

def norm(s):
    return re.sub(r"[^a-z0-9\u00c0-\u024f\u0400-\u04ff\u4e00-\u9fff\u3040-\u30ff ]", "",
                  " ".join(s.lower().split()))

def main(src, dst):
    seen_exact, seen_head = set(), set()
    kept = dropped = 0
    with open(dst, "w", encoding="utf-8") as out:
        for line in open(src, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            a, b = norm(rec["instruction"]), norm(rec["response"])
            if not a or not b:
                dropped += 1
                continue
            if a in seen_exact or (a, b) in seen_exact:
                dropped += 1
                continue
            head = a[:160]
            if head in seen_head:
                dropped += 1
                continue
            seen_exact.add(a); seen_exact.add((a, b)); seen_head.add(head)
            out.write(line + "\n")
            kept += 1
    print(f"{src} -> {dst}: kept {kept}, dropped {dropped}")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
