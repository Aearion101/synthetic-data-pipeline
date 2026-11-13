"""Local stub tests for oci_cc_check.py. No Oracle calls; loopback only."""
import http.server, threading, json
import oci_cc_check as m

KEY = "sk-SECRETKEY123"
seen = []

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); body = self.rfile.read(n)
        seen.append((self.path, self.headers.get("Authorization"), self.headers.get("OpenAI-Project"), json.loads(body)))
        p = self.path
        if p == "/ok":
            b = json.dumps({"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 9}}).encode()
            self.send_response(200); self.send_header("opc-request-id", "RID1"); self.end_headers(); self.wfile.write(b)
        elif p == "/404":
            b = ('{"code":"NotAuthorizedOrNotFound","message":"echo ' + KEY + ' Bearer ' + KEY + '"}').encode()
            self.send_response(404); self.send_header("opc-request-id", "RID2"); self.end_headers(); self.wfile.write(b)
        elif p == "/straddle":  # key straddles the 600-char cut
            b = ("x" * 595 + KEY).encode()
            self.send_response(400); self.end_headers(); self.wfile.write(b)
        elif p == "/bad":  # invalid utf-8
            self.send_response(500); self.end_headers(); self.wfile.write(b"\xff\xfe bad " + KEY.encode())
        elif p == "/redir":
            self.send_response(302); self.send_header("Location", "http://127.0.0.1:1/steal?k=" + KEY); self.end_headers()
        elif p == "/big":
            self.send_response(500); self.end_headers(); self.wfile.write(b"A" * 500000)

srv = http.server.HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_port}"

def go(path):
    lines = []; rc = m.run(base + path, "xai.test", KEY, out=lines.append); return rc, "\n".join(lines)

rc, o = go("/ok"); assert rc == 0 and "reply: ok" in o and "RID1" in o, o
p, auth, proj, body = seen[-1]
assert auth == "Bearer " + KEY and proj is None and body["stream"] is False and body["max_tokens"] == 16 and body["model"] == "xai.test"
n = len(seen)
rc, o = go("/404"); assert rc == 1 and "HTTP 404" in o and "RID2" in o and "NotAuthorizedOrNotFound" in o and "SECRET" not in o, o
rc, o = go("/straddle"); assert rc == 1 and "SECRET" not in o and "SECRETKEY"[:4] not in o.replace("<key>", ""), o
rc, o = go("/bad"); assert rc == 1 and "SECRET" not in o and "HTTP 500" in o, o
before = len(seen)
rc, o = go("/redir"); assert rc == 1 and "HTTP 302" in o and "redirect refused" in o and "SECRET" not in o, o
assert len(seen) == before + 1  # exactly one request, redirect not followed
rc, o = go("/big"); assert rc == 1 and len(o) < 1000, len(o)
# each case made exactly one request
rc, o = m.run("http://127.0.0.1:1/x", "m", KEY, out=lambda s: None), None
assert rc == 1
assert m.host_url("us-ashburn-1") == "https://inference.generativeai.us-ashburn-1.oci.oraclecloud.com/openai/v1/chat/completions"
try: m.host_url("evil.com/x"); raise AssertionError
except SystemExit: pass
print("OK")
