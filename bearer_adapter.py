#!/usr/bin/env python3
"""bearer_adapter: token-auth client for the OCI Generative AI OpenAI-compatible
Chat Completions endpoint. DOC-GROUNDED against Oracle's own pages on
2026-11-11, NOT yet run live.

Grounding (Oracle docs, fetched 2026-11-11):
- API keys: https://docs.oracle.com/en-us/iaas/Content/generative-ai/api-keys.htm
  An OCI Generative AI API key is a secret string (two interchangeable secrets
  per key), sent as "Authorization: Bearer <secret>". It is NOT an OCI IAM API
  signing key: no PEM, no fingerprint, no request signing, no ~/.oci/config.
  The key must be in the same region as the model it calls.
- Chat Completions endpoint (the owner's own pointer, chat-completions-api.htm):
    base URL https://inference.generativeai.<region>.oci.oraclecloud.com/openai/v1
    path /chat/completions, auth "API key or IAM session", format documented as
    the same as the OpenAI Chat Completions API.
  A second documented base URL shape serves the same path:
    https://inference.generativeai.<region>.oci.oraclecloud.com/20231130/actions/v1/chat/completions
  (api-keys.htm). The /openai/v1 shape is the default here because it is the one
  the owner pointed at; api_base_url in the config overrides either.
- Meta Llama is supported on the Chat Completions API ONLY: the API-keys page
  states "meta models are not supported with the Responses API". Llama 4 (the
  project's model, Grok/Gemini excluded over output-training terms) therefore
  rides /chat/completions, never /responses.
- Llama 4 on-demand model ids from the model cards:
  meta.llama-4-maverick-17b-128e-instruct-fp8, meta.llama-4-scout-17b-16e-instruct.
  Regional availability (model-endpoint-regions.htm, fetched 2026-11-11): in
  us-ashburn-1 both Llama 4 models are DEDICATED-CLUSTER ONLY; on-demand is
  documented in us-chicago-1. The owner has stated the region is Ashburn; which
  model id his Ashburn tokens can call is an open owner question, so this
  adapter hardcodes NO model id: the config allow-list carries it, unchanged.
  On-demand Llama 4 response length is capped at 4,000 tokens per run.

Protection contract with runner_v2 (unchanged by this adapter):
- The adapter performs NO retries: one runner attempt is one HTTP request, so
  the runner's ledger stays the only retry layer (same rule as
  NoneRetryStrategy on the SDK path).
- Construction validates configuration and the token's PRESENCE only; it makes
  no network call, so a misconfiguration raises in run_once BEFORE the
  reservation row is written (the claim is released, nothing is booked).
- HTTP 429 raises runner_common.Throttled (the single throttle type shared
  with the runner in both script and import modes), so the existing bounded
  backoff-and-new-attempt path applies unchanged.
- A timeout raises TimeoutError; any other failure (connection drop, 4xx/5xx,
  an unparseable 200 body) propagates, and run_once closes the attempt
  "unknown": counted as spent until reconciliation, never silently retried.
- The token comes from an environment variable (default OCI_GENAI_API_KEY),
  never the config file, is never logged, and is scrubbed from any error text.
- usage (token counts) and finish_reason are returned verbatim for the attempt
  row, so reconciliation can settle reservations against the service's real
  metering.
- The billing metric for THIS endpoint is UNVERIFIED. Oracle's price list
  (apexapps.oracle.com price API, list lastUpdated 2026-11-02; oracle.com
  pricing page) meters on-demand Meta Llama 4 Maverick (B111036) and Scout
  (B111035) per 10,000 transactions at $0.0018, and pay-on-demand.htm states
  on-demand chat bills prompt characters + response characters with
  1 transaction = 1 character (so $0.18 per 1M chars, the runner's default
  price). Gemini 2.5, xAI Grok and gpt-oss are priced per 1,000,000 tokens
  (input/output separate); dedicated clusters per AI-unit-hour. NO Oracle doc
  states which unit the OpenAI-compatible /chat/completions endpoint meters.
  This adapter therefore makes NO conservatism claim about the char-based
  dollar estimate: runner_v2 refuses a bearer run without an owner-set
  max_attempts REQUEST COUNT, which is the only proven pre-call bound until a
  reconciled live call confirms the metric, and it refuses billing_metric
  values the ledger cannot enforce (anything but "chars").
- The output bound is always sent: max_tokens_param may switch the field name
  ("max_tokens" or "max_completion_tokens") if the endpoint rejects one, but
  no value of the config drops the bound.
- Redirects are refused: urllib's default redirect handler would re-send the
  Authorization header to a redirect target, so a 3xx is an error, never a
  followed request.

UNVERIFIED against the live service (the first live smoke test, which has NO
approval yet and would be one owner-approved call, must confirm):
- that the endpoint accepts max_tokens (Oracle's curl example omits it);
- the exact model id string the owner's account and region expose;
- whether the owner's Ashburn-scoped key reaches the model he names (Oracle's
  availability table lists Llama 4 in Ashburn as dedicated-cluster only).
"""
import json, os, socket, urllib.request, urllib.error

# runner_common, never runner_v2: when runner_v2 runs as a script it is
# __main__, and importing it here would load a SECOND copy whose
# OciAdapter.Throttled is a different class object - a 429 would then slip
# past the runner's except and be mislabeled "unknown" (review defect 1).
from runner_common import Throttled, content_text

TOKEN_ENV = "OCI_GENAI_API_KEY"
DEFAULT_BASE_TEMPLATE = ("https://inference.generativeai.{region}"
                         ".oci.oraclecloud.com/openai/v1")
CHAT_PATH = "/chat/completions"
MAX_TOKENS_PARAMS = ("max_tokens", "max_completion_tokens")
# Response-side boundary (review 2026-11-11, ChatGPT/Gemini/Fo): only these
# usage keys with int values and these finish_reason values are retained from
# a server reply. Everything else the server sends is dropped at the adapter.
USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")
FINISH_REASONS = ("stop", "length", "content_filter", "tool_calls")


class BearerConfigError(ValueError):
    """Named misconfiguration: raised at construction, before any reservation."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx answer is an error, never a followed request: urllib's default
    redirect handling would re-send the Authorization header to the redirect
    target, leaking the token to any host the endpoint names."""
    def redirect_request(self, *args, **kwargs):
        return None


def default_base_url(region):
    return DEFAULT_BASE_TEMPLATE.format(region=region)


# Price-list billing families (Oracle price API, list lastUpdated 2026-11-02) crossed
# with the models api-keys.htm documents for the token-authenticated Chat
# Completions endpoint (Meta Llama, xAI Grok, OpenAI gpt-oss only). Exactly two
# model ids are verified char-priced on the price list (Maverick B111036, Scout
# B111035, per 10,000 transactions = characters) AND present on the owner's
# account access list; those are the ONLY ids the char-ledger runner accepts.
# Other meta.llama-* ids, xai.*/openai.gpt-oss* (per 1M tokens), ocid1.*
# (dedicated, AI-unit-hour) and anything unknown are refused (review defect 2:
# a family-prefix allow would overclaim char pricing for unverified ids).
CHAR_PRICED_MODEL_IDS = (
    "meta.llama-4-maverick-17b-128e-instruct-fp8",
    "meta.llama-4-scout-17b-16e-instruct",
)


def model_billing_error(model_id):
    """None when the char ledger can enforce this exact model on this endpoint,
    else a named refusal. Fail-closed: only the two verified char-priced ids
    pass; every other family or unverified id never runs."""
    if not isinstance(model_id, str):
        return f"model id {model_id!r} is not a string"
    if model_id in CHAR_PRICED_MODEL_IDS:
        return None
    if model_id.startswith("meta.llama"):
        return (f"model {model_id!r} is not one of the two ids verified char-priced "
                f"on the price list {CHAR_PRICED_MODEL_IDS}; refusing until its "
                "billing unit is confirmed")
    if model_id.startswith(("xai.", "openai.gpt-oss")):
        return (f"model {model_id!r} is priced per 1,000,000 tokens on Oracle's price "
                "list; this ledger enforces character accounting only, so its dollar "
                "gate would be the wrong unit. Refusing: token metering needs a "
                "different accounting path, not a relabeled price")
    if model_id.startswith("ocid1."):
        return (f"model {model_id!r} is a dedicated endpoint OCID, billed per "
                "AI-unit-hour; character accounting cannot bound it. Refusing")
    return (f"model {model_id!r} is not a documented char-priced model for this "
            "endpoint (api-keys.htm lists Meta Llama, xAI Grok and gpt-oss for token "
            "auth; the two verified char-priced ids are "
            f"{CHAR_PRICED_MODEL_IDS}). Refusing until its billing unit is confirmed")


class BearerAdapter:
    """Same seam as OciAdapter: .generate(model_id, prompt, cfg) ->
    (reply_text, usage_dict_or_None, finish_reason)."""

    def __init__(self, cfg):
        self.cfg = cfg
        region = cfg.get("region")
        if not region and not cfg.get("api_base_url"):
            raise BearerConfigError(
                "bearer auth: config needs region (to derive the endpoint) or an "
                "explicit api_base_url; refusing before any call")
        base = (cfg.get("api_base_url") or default_base_url(region)).rstrip("/")
        self.url = base + CHAT_PATH
        self.token_env = cfg.get("token_env", TOKEN_ENV)
        token = os.environ.get(self.token_env, "")
        if not token.strip():
            raise BearerConfigError(
                f"bearer auth: environment variable {self.token_env} is unset or "
                "empty. Export the OCI Generative AI API key secret there on your "
                "own machine (export OCI_GENAI_API_KEY=...); never put it in the "
                "config file or any chat. Refusing before any call.")
        self._token = token
        # A token pasted with a stray CR/LF/control char (CRLF clipboard text)
        # makes http.client raise ValueError naming the header VALUE - the key
        # itself - which run_once would book into the ledger as a spent
        # "unknown" attempt with no request sent (review C1, 2026-11-11).
        # Reject here, at construction, before any reservation: the claim is
        # released, nothing is booked, and this message never echoes the token.
        bad = [c for c in token if ord(c) < 0x21 or ord(c) > 0x7e]
        if bad:
            raise BearerConfigError(
                f"bearer auth: {self.token_env} contains a whitespace or control "
                "character (a stray CR/LF from the paste). Re-enter it with the "
                "hidden prompt, without surrounding whitespace. Refusing before "
                "any call; the token is never echoed.")
        self.timeout = float(cfg.get("request_timeout_seconds", 120))
        self.max_tokens_param = cfg.get("max_tokens_param", "max_tokens")
        if self.max_tokens_param not in MAX_TOKENS_PARAMS:
            raise BearerConfigError(
                f"bearer auth: max_tokens_param must be one of {MAX_TOKENS_PARAMS} "
                f"(got {self.max_tokens_param!r}): the output bound is always sent, "
                "never dropped, because the reply-term reservation is sized from it")
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _scrub(self, text):
        return text.replace(self._token, "<redacted>") if self._token in text else text

    def generate(self, model_id, prompt, cfg):
        body = {
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
            self.max_tokens_param: cfg["max_tokens"],
            "temperature": cfg["temperature"],
        }
        req = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {self._token}",
                     "Content-Type": "application/json"},
            method="POST")
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            # Fixed diagnostic: HTTP status only, never the response body. A body
            # slice was previously scrubbed after truncation, which let a token
            # prefix survive (review 2026-11-11, ChatGPT/Gemini); remote content
            # is not diagnostics, so none is kept at all.
            msg = f"HTTP {e.code} from chat completions"
            if e.code == 429:
                raise Throttled(msg) from None
            raise RuntimeError(msg) from None
        except (socket.timeout, TimeoutError) as e:
            raise TimeoutError(f"chat completions timed out after {self.timeout}s") from None
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise TimeoutError(f"chat completions timed out after {self.timeout}s") from None
            # e.reason is a local OSError in practice; the message is fixed
            # anyway so no transport text is ever trusted. Malformed status
            # lines (BadStatusLine, UnknownProtocol) are not OSErrors and land
            # in the generic branch below.
            raise RuntimeError("connection error to chat completions") from None
        except Exception:
            # Fixed text, no interpolation: a transport exception's str can
            # carry REMOTE bytes (BadStatusLine/UnknownProtocol embed the
            # server-controlled status line), and an exact-match scrub cannot
            # catch a truncated credential prefix (DeepSeek R9 / ChatGPT 1803 /
            # Gemini 1822). Local header-validation errors (review C1) are
            # structurally gated by the construction-time control-char refusal.
            raise RuntimeError("request failed before a response") from None
        try:
            d = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            # Fixed diagnostic: the decode error quotes the unparsed body slice,
            # which is remote content; none of it is kept.
            raise RuntimeError("chat completions returned a non-JSON body") from None
        usage_raw = d.get("usage") if isinstance(d, dict) else None
        usage = None
        if isinstance(usage_raw, dict):
            # Retain only the expected typed fields: a fixed key allowlist with
            # int values. Arbitrary server-controlled keys or string/nested
            # values are dropped, not scrubbed after the fact, so nothing the
            # server chooses crosses in the usage dict.
            usage = {k: usage_raw[k] for k in USAGE_KEYS
                     if isinstance(usage_raw.get(k), int)
                     and not isinstance(usage_raw.get(k), bool)}
        choices = d.get("choices") if isinstance(d, dict) else None
        if choices:
            ch = choices[0]
            msg = ch.get("message") if isinstance(ch, dict) else None
            text = content_text(msg.get("content") if isinstance(msg, dict) else None)
            finish = ch.get("finish_reason") if isinstance(ch, dict) else None
            # finish_reason is a fixed enum, not a free string: an untrusted
            # value is dropped rather than relayed.
            if finish not in FINISH_REASONS:
                finish = None
            if text:
                # Fail closed before any retained field reaches a writer: if the
                # server echoes the credential anywhere in the reply, the attempt
                # fails here and this fixed message (no remote content) is all
                # the ledger ever sees.
                if self._token in text:
                    raise RuntimeError(
                        "chat completions reply contained the credential; "
                        "refusing to relay or record any of it")
                return text, usage, finish
        raise RuntimeError("reply arrived without a message text field; refusing to guess")
