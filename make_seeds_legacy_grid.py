#!/usr/bin/env python3
"""Build the starter seed corpus for synthetic generation. No API calls: a pure grid of
domains x subtopics, plus per-language variants. Output is seeds.jsonl, one record per
line: {"id", "topic", "lang", "domain"}. generate.py reads it and expands n_pairs per
seed. Edit or extend freely; the same topic list always yields the same seed ids.
"""
import argparse
import json
import zlib

# domain -> list of concrete subtopic prompts (the seed's topic text)
GRID = {
    "home": [
        "how to descale a coffee maker", "fixing a dripping faucet", "choosing insulation for an attic",
        "bleeding radiators in a hydronic heating system", "painting a room properly (prep to cleanup)",
        "unclogging a slow bathroom drain", "when to replace vs sharpen kitchen knives",
        "weatherproofing a drafty front door", "maintaining a lawn mower for the season",
        "hanging heavy shelves into drywall without studs",
    ],
    "cooking": [
        "why bread dough needs kneading", "searing meat properly", "making stock from scraps",
        "cooking rice without it going sticky or burnt", "knife skills for vegetables",
        "what acid does to a dish and how to use it", "fermenting vegetables safely at home",
        "substituting ingredients when baking", "grilling over charcoal vs gas",
        "storing herbs so they last weeks",
    ],
    "tech-basics": [
        "how DNS resolves a domain name", "what a VPN actually does and does not protect",
        "reading a wifi speed test correctly", "choosing a password manager",
        "what happens when you press a URL enter key end to end", "how email delivery works",
        "backing up a laptop sensibly (3-2-1 rule)", "recognizing a phishing email",
        "why updates matter for routers", "how two-factor authentication works",
    ],
    "programming": [
        "when to use a list vs a set", "reading a stack trace efficiently",
        "git rebase vs merge in practice", "writing a good unit test",
        "what a race condition is with a real example", "choosing between polling and webhooks",
        "why code review exists and how to do it well", "debugging a memory leak step by step",
        "SQL indexes and when they stop helping", "what idempotency means for an API",
    ],
    "networking": [
        "subnetting a /24 for three teams", "what MTU mismatch does to traffic",
        "how BGP picks a path", "reading a traceroute output", "what a VLAN actually isolates",
        "TCP vs UDP in plain terms", "how TLS handshake protects a connection",
        "why latency and bandwidth are different", "DHCP lease lifecycle",
        "what happens in a DNS outage",
    ],
    "embedded": [
        "debouncing a mechanical switch in firmware", "reading a datasheet timing diagram",
        "choosing between I2C, SPI and UART", "power budgeting a battery sensor node",
        "why brownout detection matters", "interrupt priorities and pitfalls",
        "using an ADC properly (reference, sampling, noise)", "bootloaders and firmware updates",
        "watchdog timers done right", "driving an LED matrix without flicker",
    ],
    "finance-basics": [
        "compound interest with a worked example", "what an emergency fund is for",
        "reading a payslip's deductions", "how credit interest is calculated",
        "fixed vs variable rate loans", "what inflation does to savings",
        "how a budget is actually built and kept", "insurance deductibles explained",
        "currency exchange spreads and fees", "why diversification matters",
    ],
    "everyday-reasoning": [
        "planning a week of meals around leftovers", "how to compare two job offers fairly",
        "deciding whether to repair or replace an appliance", "packing for a two-week trip in one bag",
        "organizing a small move across town", "dispute a wrong charge on a bill effectively",
        "choosing between renting and buying tools", "splitting shared household costs fairly",
        "learning a new skill with 30 minutes a day", "giving useful feedback on a friend's writing",
    ],
    "writing": [
        "writing a clear email asking for something", "what makes a good subject line",
        "summarizing a long document without losing nuance", "editing your own first draft",
        "writing instructions someone can actually follow", "apologizing well in writing",
        "making a request to a busy person", "how tone shifts meaning in short messages",
        "structuring an argument a skeptic will accept", "taking useful meeting notes",
    ],
    "indonesia": [
        "common Bahasa Indonesia greetings and when to use them", "how to use 'anda' vs 'kamu' politely",
        "what gotong royong means in practice", "navigating Indonesian markets politely",
        "Indonesian public holidays and what closes", "using ride-hailing apps in Indonesian cities",
        "basic Bahasa Indonesia numbers and bargaining", "eating etiquette in Indonesia",
        "how to address elders respectfully in Indonesia", "Indonesian street food safety basics",
    ],
}

LANGS = {
    "en": None,  # default, no variant suffix
    "id": " (answered in Bahasa Indonesia)",
    "es": " (answered in Spanish)",
}

def build(n_per_topic=1, langs=("en",)):
    seeds = []
    i = 0
    for domain, topics in sorted(GRID.items()):
        for topic in topics:
            for lang in langs:
                t = topic + (LANGS[lang] or "")
                for v in range(n_per_topic):
                    i += 1
                    sid = f"seed-{i:05d}"
                    seeds.append({
                        "id": sid,
                        "topic": t,
                        "lang": lang,
                        "domain": domain,
                    })
    return seeds

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="seeds.jsonl")
    ap.add_argument("--langs", default="en", help="comma list from: en,id,es")
    ap.add_argument("--variants", type=int, default=1, help="variants per topic-language")
    args = ap.parse_args()
    langs = tuple(l.strip() for l in args.langs.split(",") if l.strip())
    unknown = [l for l in langs if l not in LANGS]
    if unknown:
        raise SystemExit(f"unknown langs: {unknown}; choices: {list(LANGS)}")
    seeds = build(args.variants, langs)
    with open(args.out, "w", encoding="utf-8") as f:
        for s in seeds:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    print(f"wrote {len(seeds)} seeds -> {args.out}")

if __name__ == "__main__":
    main()
