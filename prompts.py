"""Prompt templates for synthetic instruction-pair generation.

Every template takes the seed record and returns ONE string prompt. The model must
answer with a single JSON object {"instruction": ..., "response": ...}; generate.py
parses and validates that shape. Output stays model-neutral: nothing here is shaped
for a specific base model or chat template.

Templates are picked deterministically per record (crc32), so reruns and parallel
workers produce the same plan for the same seed corpus.
"""
import random

# Length bands for pair-constraints-v1 (response, in words)
_BANDS = [
    ("short", 25, 60),
    ("medium", 60, 140),
    ("long", 140, 260),
]

# Audiences the constraint template rotates through
_AUDIENCES = [
    "a curious teenager",
    "a working professional new to the topic",
    "an experienced practitioner",
    "someone with no background at all",
]

# Formats the constraint template rotates through
_FORMATS = [
    "plain prose paragraphs",
    "a short numbered list followed by one closing paragraph",
    "plain prose with one concrete example worked through",
]

def _rng_for(seed_id, k):
    """Per-record deterministic RNG: same seed corpus = same prompts every run."""
    return random.Random(f"{seed_id}#{k}")

def _json_only_line():
    return ('Reply with ONLY a JSON object, no markdown fences, exactly:\n'
            '{"instruction": "...", "response": "..."}')

def _lang_directive(seed):
    lang = (seed.get("lang") or "en").strip()
    if lang in ("", "en"):
        return ""
    return (f"\nWrite both the instruction and the response entirely in {lang}. "
            "The instruction must still be self-contained.\n")

def _pair_from_topic_v1(seed, k):
    return (
        "You are writing one training example for an instruction-tuning dataset.\n"
        f"Topic: {seed['topic']}\n\n"
        "Write ONE instruction and its response. The instruction must be self-contained and "
        "answerable without any document or link. The response must be correct, complete, and "
        "written in plain prose.\n"
        + _lang_directive(seed)
        + _json_only_line()
    )

def _pair_constraints_v1(seed, k):
    rng = _rng_for(seed["id"], k)
    name, lo, hi = rng.choice(_BANDS)
    audience = rng.choice(_AUDIENCES)
    fmt = rng.choice(_FORMATS)
    return (
        "You are writing one training example for an instruction-tuning dataset.\n"
        f"Topic: {seed['topic']}\n\n"
        "Write ONE instruction and its response. The instruction must be self-contained and "
        "answerable without any document or link.\n\n"
        f"Constraints for the response:\n"
        f"- audience: write for {audience}\n"
        f"- length: {lo}-{hi} words ({name})\n"
        f"- format: {fmt}\n"
        "- correct, complete, plain prose, no filler\n"
        + _lang_directive(seed)
        + _json_only_line()
    )

def _pair_why_v1(seed, k):
    return (
        "You are writing one training example for an instruction-tuning dataset.\n"
        f"Topic: {seed['topic']}\n\n"
        "Write ONE instruction and its response. The instruction must be self-contained and "
        "answerable without any document or link.\n\n"
        "The response must answer directly first, then add a short paragraph starting with "
        "'Why this works:' that explains the reasoning behind the answer in plain prose. "
        "Keep the whole response under 250 words.\n"
        + _lang_directive(seed)
        + _json_only_line()
    )

def _pair_edge_v1(seed, k):
    """Wrinkled cases: misconceptions, tradeoffs, what-ifs. Produces harder pairs."""
    rng = _rng_for(seed["id"], k)
    angle = rng.choice([
        "a common misconception about this topic that a beginner would believe, and correct it",
        "the single most common mistake people make with this, and how to avoid it",
        "a tradeoff someone would realistically face here, and when each side wins",
        "what changes when this is done at 10x the usual scale, stated concretely",
    ])
    return (
        "You are writing one training example for an instruction-tuning dataset.\n"
        f"Topic: {seed['topic']}\n\n"
        f"Write ONE instruction and its response built around {angle}. The instruction must "
        "be self-contained and answerable without any document or link. The response must be "
        "correct, complete, plain prose, under 220 words.\n"
        + _lang_directive(seed)
        + _json_only_line()
    )

TEMPLATES = {
    "pair-from-topic-v1": _pair_from_topic_v1,
    "pair-constraints-v1": _pair_constraints_v1,
    "pair-why-v1": _pair_why_v1,
    "pair-edge-v1": _pair_edge_v1,
}

def pick_template(cfg, seed_id, k):
    names = cfg.get("templates") or list(TEMPLATES)
    return names[__import__("zlib").crc32(f"{seed_id}:{k}".encode()) % len(names)]
