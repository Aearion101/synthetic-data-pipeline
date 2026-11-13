#!/usr/bin/env python3
"""One-shot Oracle GenAI Chat Completions check, shaped like wowbot's llm.py request.

Standalone: stdlib only. Does not import or touch the generator, adapter, config or ledger.
Makes exactly ONE request (no retry, no redirects), reads at most 64 KB, prints a few lines.
The key is read from a hidden prompt, never from argv, and is scrubbed from everything printed.

  python3 oci_cc_check.py                       # Ashburn, xai.grok-4.20-non-reasoning
  python3 oci_cc_check.py --model <id>          # model id sent verbatim
  python3 oci_cc_check.py --region us-ashburn-1
"""
import argparse, getpass, json, re, sys, urllib.error, urllib.request

DEFAULT_MODEL = "xai.grok-4.20-non-reasoning"
DEFAULT_REGION = "us-ashburn-1"
MAX_READ = 65536
SHOW = 600


def host_url(region):
    if not re.fullmatch(r"[a-z]{2}-[a-z0-9-]+-\d", region):
        raise SystemExit("bad region name")
    return f"https://inference.generativeai.{region}.oci.oraclecloud.com/openai/v1/chat/completions"


def scrub(text, key):
    """Remove the key and any Bearer token BEFORE truncating."""
    if key:
        text = text.replace(key, "<key>")
    text = re.sub(r"(?i)bearer\s+\S+", "Bearer <key>", text)
    return text


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def run(url, model, key, out=print, timeout=30):
    """Returns an exit code. One POST, no retry."""
    body = {"model": model, "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
            "temperature": 0.1, "max_tokens": 16, "stream": False}
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {key}",
        "HTTP-Referer": "https://github.com/Aearion101/wowbot", "X-Title": "wowbot"})
    opener = urllib.request.build_opener(_NoRedirect)
    out(f"POST {url}  model={model}")
    try:
        with opener.open(req, timeout=timeout) as r:
            rid = scrub(r.headers.get("opc-request-id") or "", key)[:120]
            raw = r.read(MAX_READ).decode("utf-8", "replace")
            out(f"HTTP {r.status} opc-request-id: {rid}")
        try:
            j = json.loads(raw)
            text = j["choices"][0]["message"]["content"]
            out("reply: " + scrub(str(text), key)[:200])
            out("usage: " + scrub(json.dumps(j.get("usage")), key)[:200])
        except Exception:
            out("200 but unexpected body: " + scrub(raw, key)[:SHOW])
        return 0
    except urllib.error.HTTPError as e:
        rid = scrub(e.headers.get("opc-request-id") or "", key)[:120]
        raw = e.read(MAX_READ).decode("utf-8", "replace")
        out(f"HTTP {e.code} opc-request-id: {rid}")
        if 300 <= e.code < 400:
            out("redirect refused (not followed): Location=" + scrub(e.headers.get("Location") or "", key)[:200])
        out(scrub(raw, key)[:SHOW])
        return 1
    except Exception as e:
        out("transport error: " + type(e).__name__ + " " + scrub(str(e), key)[:300])
        return 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--region", default=DEFAULT_REGION)
    a = ap.parse_args(argv)
    key = getpass.getpass("OCI key (hidden): ").strip()
    if not key:
        raise SystemExit("no key entered")
    return run(host_url(a.region), a.model, key)


if __name__ == "__main__":
    sys.exit(main())
