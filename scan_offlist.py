#!/usr/bin/env python3
"""Post-generation source-script scan (Instinct 0545 ruling on Claude 0535).

The pack's quoted-source wrapper line ("A quoted source passage inside the
instruction may be in another language when the task requires it") sits on all
4,000 v2.1 rows, including non-translation rows where the model writes the
source passage itself. Prompt-time flags cannot see that. This script runs
AFTER generation, BEFORE any pair enters a tune: it scans each generated pair's
`instruction` for Han, Kana, Hangul or Cyrillic characters on rows whose pack
language is NOT zh/ja/ko/ru, and reports every hit with its pair id and script.

Join rule: a pair id is `<base>#<k>#<model>`, so the base pack row id is the
part before the first `#`. Rows whose pack language is zh/ja/ko/ru are skipped
(an off-list passage is expected there). Polish is Latin-script, so the scan
still applies to pl rows; any hit there needs the native-speaker QC the pack
README already requires. A hit is not proof of a defect: the wrapper makes
off-list quoting legal on any row. The scan exists so a tune-side consumer can
decide with counts instead of a guess.

Usage:
    python3 scan_offlist.py --pairs data/batch_001.jsonl --pack pack/v2.1/bulk_prompts_v2.1.onestage.train.jsonl
    python3 scan_offlist.py --pairs data/batch_001.jsonl --pack pack/... [--fail-on-hit]

Offline only: reads two local JSONL files, writes nothing, no network, no key,
no model call. Exit 0 either way unless --fail-on-hit and hits were found.

Stated limit: the scan covers Han/Kana/Hangul/Cyrillic code ranges only. It
does not catch off-list Latin-script sources (Polish or others quoted inside a
non-pl row), and it does not judge whether a hit is a legitimate quotation.
"""
import argparse
import json
import sys

OFF_LIST_LANGS = {"zh", "ja", "ko", "ru"}
SCRIPTS = [
    ("Han", [(0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0xF900, 0xFAFF)]),
    ("Kana", [(0x3040, 0x30FF)]),
    ("Hangul", [(0xAC00, 0xD7AF), (0x1100, 0x11FF), (0x3130, 0x318F)]),
    ("Cyrillic", [(0x0400, 0x04FF)]),
]


def base_id(pair_id):
    """spv2-00000#1#alpha.model -> spv2-00000; a bare id passes through."""
    return str(pair_id).split("#", 1)[0]


def script_hits(text):
    """{script_name: sample_hit} for the first hit of each script, else {}."""
    text = str(text)
    hits = {}
    for name, ranges in SCRIPTS:
        for lo, hi in ranges:
            for ch in text:
                if lo <= ord(ch) <= hi:
                    hits[name] = ch
                    break
            if name in hits:
                break
    return hits


def load_language_map(pack_path):
    """{base row id: language} from a prompt-pack JSONL."""
    langs = {}
    with open(pack_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            rid = base_id(row.get("id", ""))
            if rid:
                langs[rid] = row.get("language")
    return langs


def scan_pairs(pairs_path, lang_map):
    """Findings for pairs whose instruction shows an off-list script.

    Returns a list of {id, language, scripts}. Rows the pack map does not know
    are reported as language=None findings ONLY if they carry their own
    "language" field; fully unknown ids are listed separately as unmapped
    rather than guessed.
    """
    findings, unmapped = [], []
    with open(pairs_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            pair = json.loads(line)
            pid = pair.get("id", "")
            hits = script_hits(pair.get("instruction", ""))
            if not hits:
                continue
            rid = base_id(pid)
            lang = lang_map.get(rid, pair.get("language"))
            if lang is None:
                unmapped.append({"id": pid, "scripts": hits})
                continue
            if lang in OFF_LIST_LANGS:
                continue
            findings.append({"id": pid, "language": lang, "scripts": hits})
    return findings, unmapped


def scan_pairs_lines(pair_rows, lang_map):
    """In-process twin of scan_pairs() for tests: rows as dicts, no files.

    Same join, same skip rule, same finding shape."""
    findings, unmapped = [], []
    for pair in pair_rows:
        pid = pair.get("id", "")
        hits = script_hits(pair.get("instruction", ""))
        if not hits:
            continue
        rid = base_id(pid)
        lang = lang_map.get(rid, pair.get("language"))
        if lang is None:
            unmapped.append({"id": pid, "scripts": hits})
        elif lang not in OFF_LIST_LANGS:
            findings.append({"id": pid, "language": lang, "scripts": hits})
    return findings, unmapped


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pairs", required=True, help="generated pairs JSONL")
    ap.add_argument("--pack", required=True, help="prompt-pack JSONL for languages")
    ap.add_argument("--fail-on-hit", action="store_true",
                    help="exit 1 when any finding is reported")
    args = ap.parse_args()
    lang_map = load_language_map(args.pack)
    findings, unmapped = scan_pairs(args.pairs, lang_map)
    for f_ in findings:
        print(f"HIT {f_['id']} language={f_['language']} scripts={sorted(f_['scripts'])}")
    for u in unmapped:
        print(f"UNMAPPED {u['id']} scripts={sorted(u['scripts'])}")
    print(f"done: {len(findings)} hit(s), {len(unmapped)} unmapped, "
          f"{len(lang_map)} pack rows mapped")
    if args.fail_on_hit and (findings or unmapped):
        sys.exit(1)


if __name__ == "__main__":
    main()
