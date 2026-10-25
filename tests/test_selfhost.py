#!/usr/bin/env python3
"""End-to-end test for the self-hosted edition: no keys -> wizard -> working app.

Run it from the repo root:  python3 tests/test_selfhost.py

Starts a throwaway instance (own port, own config file, own ledger), walks the first-run
setup the way a new operator would, then drives the paid flow through the built-in mock
provider so the whole thing is verifiable without a single real API key.
"""
import hashlib
import hmac
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("TEST_PORT", "3487"))
BASE = "http://127.0.0.1:%d" % PORT
TMP = tempfile.mkdtemp(prefix="recall-selfhost-")
CONFIG = os.path.join(TMP, "config.env")
DB = os.path.join(TMP, "billing.db")
MOCK_SECRET = "test-mock-secret"
PASS, FAIL = [], []


def check(label, ok, detail=""):
    (PASS if ok else FAIL).append(label)
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", label, ("  -> " + str(detail)) if detail else ""))


def req(path, data=None, headers=None, cookies=None, method=None):
    url = BASE + path
    body = json.dumps(data).encode() if isinstance(data, (dict, list)) else data
    h = dict(headers or {})
    if cookies:
        h["Cookie"] = cookies
    r = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw), resp.headers
            except ValueError:
                return resp.status, raw, resp.headers
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw), e.headers
        except ValueError:
            return e.code, raw, e.headers


def start():
    env = dict(os.environ)
    env.update({"RECALL_CONFIG": CONFIG, "BILLING_DB": DB, "PORT": str(PORT),
                "MOCK_WEBHOOK_SECRET": MOCK_SECRET, "PYTHONUNBUFFERED": "1"})
    p = subprocess.Popen([sys.executable, "-u", os.path.join(ROOT, "server.py")],
                         cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True)
    for _ in range(60):
        time.sleep(0.25)
        try:
            urllib.request.urlopen(BASE + "/api/health", timeout=2).read()
            return p
        except Exception:
            if p.poll() is not None:
                raise RuntimeError("server exited:\n" + (p.stdout.read() if p.stdout else ""))
    raise RuntimeError("server did not come up")


def env_deployment():
    """The other deployment style: keys in the environment, no wizard at all."""
    print("\n7. keys supplied through the environment (systemd / docker style)")
    env_tmp = tempfile.mkdtemp(prefix="recall-env-")
    env = dict(os.environ)
    env.update({"RECALL_CONFIG": os.path.join(env_tmp, "unused.env"),
                "BILLING_DB": os.path.join(env_tmp, "billing.db"), "PORT": str(PORT + 1),
                "TYPESAFE_API_KEY": "env-jev", "OLLAMA_API_KEY": "env-ollama",
                "PAYMENT_PROVIDER": "mock", "PYTHONUNBUFFERED": "1"})
    p2 = subprocess.Popen([sys.executable, "-u", os.path.join(ROOT, "server.py")], cwd=ROOT,
                          env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base2 = "http://127.0.0.1:%d" % (PORT + 1)
    try:
        for _ in range(60):
            time.sleep(0.25)
            try:
                with urllib.request.urlopen(base2 + "/api/health", timeout=2) as resp:
                    h = json.loads(resp.read().decode())
                break
            except Exception:
                if p2.poll() is not None:
                    raise RuntimeError("env-mode server exited")
        else:
            raise RuntimeError("env-mode server did not come up")
        check("health reports configured with env keys only", h.get("configured") is True, h)
        with urllib.request.urlopen(base2 + "/", timeout=10) as resp:
            body = resp.read().decode()
        check("app is served without any wizard run",
              resp.status == 200 and "bring your own keys" not in body, resp.status)
        check("no config file was written", not os.path.exists(os.path.join(env_tmp, "unused.env")))
    finally:
        p2.terminate()
        try:
            p2.wait(timeout=10)
        except Exception:
            p2.kill()
        shutil.rmtree(env_tmp, ignore_errors=True)


def free_deployment():
    """The default: an instance with payments off is simply free — nothing to buy."""
    print("\n8. payments off (the default) — a free instance")
    free_tmp = tempfile.mkdtemp(prefix="recall-free-")
    env = dict(os.environ)
    env.pop("PAYMENTS_ENABLED", None)
    env.pop("TYPESAFE_API_KEY", None)          # no Jev: the model marks the answers
    env.update({"RECALL_CONFIG": os.path.join(free_tmp, "config.env"),
                "BILLING_DB": os.path.join(free_tmp, "billing.db"), "PORT": str(PORT + 2),
                "OLLAMA_API_KEY": "free-ollama",
                "PYTHONUNBUFFERED": "1"})
    p3 = subprocess.Popen([sys.executable, "-u", os.path.join(ROOT, "server.py")], cwd=ROOT,
                          env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base3 = "http://127.0.0.1:%d" % (PORT + 2)
    try:
        for _ in range(60):
            time.sleep(0.25)
            try:
                with urllib.request.urlopen(base3 + "/api/health", timeout=2) as resp:
                    h = json.loads(resp.read().decode())
                break
            except Exception:
                if p3.poll() is not None:
                    raise RuntimeError("free-mode server exited")
        else:
            raise RuntimeError("free-mode server did not come up")
        check("health says payments are off", h.get("payments_enabled") is False, h)
        check("no Jev key is needed to be ready (model marks the answers)",
              h.get("jev_key") is False and h.get("configured") is True, h)
        with urllib.request.urlopen(base3 + "/api/me", timeout=10) as resp:
            me = json.loads(resp.read().decode())
        check("/api/me reports a free instance", me.get("payments_enabled") is False
              and me.get("free") is True, me)
        check("no account cookie is set when there is nothing to buy",
              not (resp.headers.get("Set-Cookie") or ""))
        code = _status(base3 + "/api/checkout", {"units": 1})
        check("checkout is refused", code == 404, code)
        check("the owner credit page is gone", _status(base3 + "/admin?token=x&cents=100") == 404)
        check("the ledger was never created",
              not os.path.exists(os.path.join(free_tmp, "billing.db")))
        check("the app itself still serves", _status(base3 + "/") == 200)
    finally:
        p3.terminate()
        try:
            p3.wait(timeout=10)
        except Exception:
            p3.kill()
        shutil.rmtree(free_tmp, ignore_errors=True)


def _status(url, data=None):
    body = json.dumps(data).encode() if isinstance(data, dict) else None
    r = urllib.request.Request(url, data=body,
                               headers={"Content-Type": "application/json"} if body else {})
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def _post_at(base, path, payload):
    """POST JSON to any local base and return (status, parsed_body)."""
    r = urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:200]}


def visitor_keys_deployment():
    """A public instance that holds no keys at all: visitors paste their own."""
    print("\n9. keys from visitors — the instance holds none")
    tmp = tempfile.mkdtemp(prefix="recall-byok-")
    config = os.path.join(tmp, "config.env")
    env = dict(os.environ)
    env.pop("TYPESAFE_API_KEY", None)
    env.update({"RECALL_CONFIG": config, "BILLING_DB": os.path.join(tmp, "billing.db"),
                "PORT": str(PORT + 3), "KEYS_FROM_VISITORS": "yes",
                "OLLAMA_API_KEY": "operator-key-must-be-ignored",
                "PYTHONUNBUFFERED": "1"})
    p = subprocess.Popen([sys.executable, "-u", os.path.join(ROOT, "server.py")], cwd=ROOT,
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = "http://127.0.0.1:%d" % (PORT + 3)
    try:
        for _ in range(60):
            time.sleep(0.25)
            try:
                with urllib.request.urlopen(base + "/api/health", timeout=2) as resp:
                    h = json.loads(resp.read().decode())
                break
            except Exception:
                if p.poll() is not None:
                    raise RuntimeError("visitor-keys server exited")
        else:
            raise RuntimeError("visitor-keys server did not come up")
        check("health says keys come from visitors", h.get("keys_from_visitors") is True, h)
        check("the operator's own keys are ignored",
              h.get("providers") == [] and h.get("nvidia_key") is False, h)
        check("no key is needed for the instance to be ready", h.get("configured") is True, h)
        s, body = _post_at(base, "/api/generate", {"text": "x" * 200, "count": 3})
        check("generating without keys asks for them",
              s == 400 and body.get("need_keys") is True, (s, body))
        s, body = _post_at(base, "/api/generate",
                           {"text": "x" * 200, "count": 3, "keys": {"ollama": "visitor-key"}})
        check("a visitor's key opens the gate (the provider is actually called)",
              s == 400 and body.get("provider_error") is True
              and "rejected" in str(body.get("error")), (s, str(body)[:160]))
        check("nothing was written to disk (keys are never stored)",
              not os.path.exists(config) and not os.path.exists(os.path.join(tmp, "billing.db")))
    finally:
        p.terminate()
        try:
            p.wait(timeout=10)
        except Exception:
            p.kill()
        shutil.rmtree(tmp, ignore_errors=True)


class _StubProvider(http.server.BaseHTTPRequestHandler):
    """A fake Ollama + Jev that answers only callers holding the visitor's key.

    Lets the whole quiz flow be tested with no network and no real credentials: if the app
    used the operator's key (or no key), every call would come back 401.
    """
    protocol_version = "HTTP/1.1"
    ollama_key = "visitor-ollama-key"
    jev_key = "visitor-jev-key"
    calls = []

    def log_message(self, *args):
        pass

    def _json(self, status, obj):
        payload = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        auth = self.headers.get("Authorization") or ""
        self.calls.append((self.path, auth))
        want = self.jev_key if self.path.endswith("/systemone") else self.ollama_key
        if auth != "Bearer " + want:
            return self._json(401, {"error": {"message": "Unauthorized"}})
        if self.path.endswith("/systemone"):
            return self._json(200, {"answers": {"verdict": {"choice": "correct", "confidence": 1.0}},
                                    "usage": {}})
        quiz = {"questions": [
            {"q": "What drives evaporation in the water cycle?", "type": "mc",
             "options": ["The sun", "The moon", "Wind", "Rain"], "key": "The sun"},
            {"q": "What is precipitation?", "type": "short",
             "key": "Water falling from the clouds", "alts": []},
        ]}
        return self._json(200, {"choices": [{"message": {"content": json.dumps(quiz)}}]})


def stub_flow_deployment():
    """The happy path on visitor keys, with fake providers standing in for Ollama and Jev."""
    print("\n10. a whole quiz on visitor keys (stub providers, no network)")
    stub = http.server.ThreadingHTTPServer(("127.0.0.1", PORT + 5), _StubProvider)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    tmp = tempfile.mkdtemp(prefix="recall-stub-")
    env = dict(os.environ)
    env.pop("TYPESAFE_API_KEY", None)
    env.update({"RECALL_CONFIG": os.path.join(tmp, "config.env"),
                "BILLING_DB": os.path.join(tmp, "billing.db"), "PORT": str(PORT + 4),
                "KEYS_FROM_VISITORS": "yes", "OLLAMA_API_KEY": "operator-key-is-ignored",
                "OLLAMA_URL": "http://127.0.0.1:%d/v1/chat/completions" % (PORT + 5),
                "NVIDIA_URL": "http://127.0.0.1:%d/v1/chat/completions" % (PORT + 5),
                "JEV_URL": "http://127.0.0.1:%d/v1/systemone" % (PORT + 5),
                "PYTHONUNBUFFERED": "1"})
    p = subprocess.Popen([sys.executable, "-u", os.path.join(ROOT, "server.py")], cwd=ROOT,
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = "http://127.0.0.1:%d" % (PORT + 4)
    keys = {"ollama": _StubProvider.ollama_key, "typesafe": _StubProvider.jev_key}
    try:
        for _ in range(60):
            time.sleep(0.25)
            try:
                urllib.request.urlopen(base + "/api/health", timeout=2).read()
                break
            except Exception:
                if p.poll() is not None:
                    raise RuntimeError("stub-flow server exited")
        else:
            raise RuntimeError("stub-flow server did not come up")
        s, d = _post_at(base, "/api/generate", {"text": "x" * 200, "count": 5, "keys": keys})
        questions = d.get("questions") or []
        check("a quiz is generated with the visitor's key", s == 200 and len(questions) == 2,
              (s, str(d)[:140]))
        check("the answer key never reaches the browser",
              all(not ({"key", "alts", "answer_text"} & set(q)) for q in questions), questions)
        answers = {q["id"]: "The sun" for q in questions}
        s, g = _post_at(base, "/api/grade", {"sid": d.get("sid"), "answers": answers, "keys": keys})
        check("grading runs on the visitor's Jev key", s == 200 and g.get("graders") == ["jev"],
              (s, str(g)[:160]))
        used = {auth for _, auth in _StubProvider.calls}
        check("the operator's own key was never used",
              used == {"Bearer " + _StubProvider.ollama_key,
                       "Bearer " + _StubProvider.jev_key}, used)
    finally:
        stub.shutdown()
        p.terminate()
        try:
            p.wait(timeout=10)
        except Exception:
            p.kill()
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    proc = start()
    try:
        print("\n1. a fresh install with no keys")
        s, h, _ = req("/api/health")
        check("health responds", s == 200 and h.get("ok") is True, s)
        check("reports not configured", h.get("configured") is False, h.get("configured"))
        check("names the keys it needs", "OLLAMA_API_KEY or NVIDIA_API_KEY" in (h.get("missing") or []),
              h.get("missing"))
        s, body, _ = req("/")
        check("root serves the setup wizard", s == 200 and "bring your own keys" in body, s)
        s, body, _ = req("/api/me")
        check("app API refuses until configured", s == 503 and body.get("setup") == "/setup", s)
        s, r, _ = req("/api/checkout", {"units": 1}, {"Content-Type": "application/json"})
        check("checkout refuses until configured", s == 503, s)

        print("\n2. the wizard is not claimable from the internet")
        s, r, _ = req("/api/setup", headers={"X-Forwarded-For": "203.0.113.9"})
        check("forwarded request without a token is refused",
              s == 403, "%s %s" % (s, str(r)[:60]))

        print("\n3. entering keys")
        s, r, _ = req("/api/setup")
        check("wizard status is readable locally", s == 200 and "fields" in r, s)
        check("secret values are never returned",
              all(f["value"] == "" for f in r["fields"] if f["secret"]),
              [f["key"] for f in r["fields"] if f["secret"] and f["value"]])
        saved = {"TYPESAFE_API_KEY": "jev-test-key", "OLLAMA_API_KEY": "ollama-test-key",
                 "PAYMENTS_ENABLED": "yes", "PAYMENT_PROVIDER": "mock",
                 "PUBLIC_BASE_URL": BASE, "PORT": str(PORT)}
        s, r, _ = req("/api/setup", saved, {"Content-Type": "application/json"})
        check("settings save and the app becomes ready", s == 200 and r.get("ready") is True,
              "%s %s" % (s, r.get("missing")))
        mode = oct(os.stat(CONFIG).st_mode & 0o777)
        check("config file is written 0600", mode == "0o600", mode)
        text = open(CONFIG).read()
        check("keys landed in the config file", "TYPESAFE_API_KEY=jev-test-key" in text)
        check("an admin token was generated for the operator",
              any(l.startswith("ADMIN_TOKEN=") and len(l.split("=", 1)[1]) >= 16
                  for l in text.splitlines()))
        s, r, _ = req("/api/setup")
        check("secrets read back as simply 'set'",
              all(f["is_set"] for f in r["fields"]
                  if f["secret"] and f["key"] in ("TYPESAFE_API_KEY", "OLLAMA_API_KEY")))

        print("\n4. blank secret means 'keep it' (not 'erase it')")
        s, r, _ = req("/api/setup", {"TYPESAFE_API_KEY": "", "PRICE_PER_5_CENTS": "2"},
                      {"Content-Type": "application/json"})
        check("saving other fields keeps the key",
              "TYPESAFE_API_KEY=jev-test-key" in open(CONFIG).read())
        check("pricing change is applied live", r["status"]["fields"] and any(
            f["key"] == "PRICE_PER_5_CENTS" and f["value"] == "2" for f in r["status"]["fields"]))
        req("/api/setup", {"PRICE_PER_5_CENTS": "1"}, {"Content-Type": "application/json"})

        print("\n5. the app now works")
        s, body, _ = req("/")
        check("root serves the app", s == 200 and "bring your own keys" not in body, s)
        s, me, hdrs = req("/api/me")
        check("accounts still work unconfigured-after-setup", s == 200 and me["balance_cents"] == 0, s)
        check("payments are reported as on when enabled", me.get("payments_enabled") is True, me)
        cookie = (hdrs.get("Set-Cookie") or "").split(";")[0]

        print("\n6. the whole paid flow, through the built-in test checkout")
        s, co, _ = req("/api/checkout", {"units": 1}, {"Content-Type": "application/json"}, cookie)
        check("checkout creates an order", s == 200 and "url" in co, s)
        order = co.get("order_id", "")
        s, page, _ = req("/mockpay/" + order)
        check("test checkout page renders", s == 200 and "Pay" in page, s)
        sig = hashlib.md5((order + MOCK_SECRET).encode()).hexdigest()
        form = urllib.parse.urlencode({"order_id": order, "status": "paid", "sign": sig}).encode()
        s, r, _ = req("/api/webhook/mock", form, {"Content-Type": "application/x-www-form-urlencoded"})
        check("signed test payment is accepted", s == 200 and r.get("credited") is True, "%s %s" % (s, r))
        s, me, _ = req("/api/me", cookies=cookie)
        check("credit lands in the account", s == 200 and me["balance_cents"] == 100, me.get("balance_cents"))
        sig_bad = hashlib.md5((order + "wrong").encode()).hexdigest()
        form = urllib.parse.urlencode({"order_id": order, "status": "paid", "sign": sig_bad}).encode()
        s, r, _ = req("/api/webhook/mock", form, {"Content-Type": "application/x-www-form-urlencoded"})
        check("a tampered signature is rejected", s in (400, 403), s)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(TMP, ignore_errors=True)

    env_deployment()
    free_deployment()
    visitor_keys_deployment()
    stub_flow_deployment()

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("failed: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
