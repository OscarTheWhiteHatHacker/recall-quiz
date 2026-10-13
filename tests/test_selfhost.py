#!/usr/bin/env python3
"""End-to-end test for the self-hosted edition: no keys -> wizard -> working app.

Run it from the repo root:  python3 tests/test_selfhost.py

Starts a throwaway instance (own port, own config file, own ledger), walks the first-run
setup the way a new operator would, then drives the paid flow through the built-in mock
provider so the whole thing is verifiable without a single real API key.
"""
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import tempfile
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


def main():
    proc = start()
    try:
        print("\n1. a fresh install with no keys")
        s, h, _ = req("/api/health")
        check("health responds", s == 200 and h.get("ok") is True, s)
        check("reports not configured", h.get("configured") is False, h.get("configured"))
        check("names the keys it needs", "TYPESAFE_API_KEY" in (h.get("missing") or []), h.get("missing"))
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
                 "PAYMENT_PROVIDER": "mock", "PUBLIC_BASE_URL": BASE, "PORT": str(PORT)}
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

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("failed: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
