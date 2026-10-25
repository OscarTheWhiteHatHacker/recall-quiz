#!/usr/bin/env python3
"""Recall — paste a textbook, get a quiz with an invisible answer key.

Flow:
  POST /api/generate  {text, count, mode}  -> writes questions, keeps the answer
                                              keys SERVER-SIDE only (never sent
                                              to the browser)
  POST /api/grade     {sid, answers}       -> Jev (api.typesafe.ai) decides each
                                              answer correct/incorrect against
                                              the key; wrong answers get an AI
                                              explanation of why

Models:
  generation + explanations : z-ai/glm-5.3-flash on integrate.api.nvidia.com (free)
  grading decision          : jev-latest on api.typesafe.ai (~$0.00002/decision)

No third-party python deps — stdlib only.

    python3 server.py                 # 0.0.0.0:3460
    PORT=9000 python3 server.py
"""
import http.client
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Settings come from config.env (written by the first-run wizard) or the environment,
# and they must be in os.environ before the constants below read it.
import config as _config                                                    # noqa: E402
_config.load_into_env()

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "3460"))

NVIDIA_URL = GEN_MODEL = OLLAMA_URL = OLLAMA_MODEL = JEV_URL = ""


def reload_ai_settings() -> None:
    """(Re)read the AI/endpoint settings — called at import and after the wizard saves."""
    global NVIDIA_URL, GEN_MODEL, OLLAMA_URL, OLLAMA_MODEL, JEV_URL
    NVIDIA_URL = _config.get("NVIDIA_URL")
    GEN_MODEL = _config.get("GEN_MODEL")
    OLLAMA_URL = _config.get("OLLAMA_URL")
    OLLAMA_MODEL = _config.get("OLLAMA_MODEL")
    JEV_URL = _config.get("JEV_URL")


reload_ai_settings()

_ = (NVIDIA_URL, GEN_MODEL)   # (old literals removed; values now come from config)
# Ollama Cloud (free tier) is roughly 10x faster than the NVIDIA free endpoint:
# measured 7.5s for a 5-question paper vs 87s, and 0.9s per explanation vs a
# 60s timeout. It is tried first, the NVIDIA endpoint is the fallback.

MAX_TEXT = 40000          # chars of pasted textbook we accept
MAX_ANSWER = 4000         # chars per answer
SESSION_TTL = 4 * 3600    # seconds a quiz lives in memory
MAX_SESSIONS = 60
GEN_TIMEOUT = 120.0
JEV_TIMEOUT = 20.0


# --------------------------------------------------------------------------
# keys: read at runtime (never hard-code; a written key can get corrupted)
# --------------------------------------------------------------------------
def _load_env_file(path: Path) -> None:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").replace("\\n", "\n")
    except OSError:
        return
    for line in raw.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v.strip().strip('"').strip("'"))


_load_env_file(ROOT / ".env")          # keys live here (see .env.example)

import billing                          # noqa: E402  (reads the env above at import)


def nvidia_key() -> str:
    return os.environ.get("NVIDIA_API_KEY", "").strip()


def jev_key() -> str:
    """The Jev grading key, if the operator supplied one. Empty = grade with the AI model."""
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if key:
        return key
    f = ROOT / ".jev_key"                 # optional: drop the key in a file instead
    try:
        if f.exists():
            return f.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


# --------------------------------------------------------------------------
# pooled keep-alive HTTPS POSTs (a fresh TLS handshake costs ~100ms of a
# ~230ms round trip, so connections are checked out/in instead of rebuilt)
# --------------------------------------------------------------------------
_POOLS: dict = {}
_POOL_LOCK = threading.Lock()
_POOL_MAX = 6
_IDLE_MAX = 15.0     # seconds a pooled connection may sit idle before it is
                     # discarded: upstream (NVIDIA/Cloudflare) drops keep-alives
                     # much sooner, and a stale conn = "Remote end closed" 502


def _parse(url: str):
    p = urllib.parse.urlparse(url)
    cls = http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection
    return cls, p.hostname, p.port or (443 if p.scheme == "https" else 80), (p.path or "/")


def _checkout(url: str):
    now = time.time()
    with _POOL_LOCK:
        pool = _POOLS.setdefault(url, [])
        while pool:
            item = pool.pop()
            if now - item[2] <= _IDLE_MAX:
                return item
            try:
                item[0].close()          # too old — upstream has likely dropped it
            except Exception:
                pass
    cls, host, port, path = _parse(url)
    return [cls(host, port, timeout=GEN_TIMEOUT), path, now]


def _checkin(url: str, item) -> None:
    item[2] = time.time()
    with _POOL_LOCK:
        pool = _POOLS.setdefault(url, [])
        if len(pool) < _POOL_MAX:
            pool.append(item)
        else:
            try:
                item[0].close()
            except Exception:
                pass


def http_post(url: str, headers: dict, body: bytes, timeout: float, retries: int = 1):
    """Returns (status, bytes). Retries once on a dropped/5xx connection."""
    last = (502, b'{"error":"unreachable"}')
    for attempt in range(retries + 1):
        conn, path, _ts = _checkout(url)
        try:
            conn.timeout = timeout
            conn.request("POST", path, body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            if resp.status >= 500 and attempt < retries:
                conn.close()
                last = (resp.status, data)
                continue
            _checkin(url, [conn, path, time.time()])
            return resp.status, data
        except Exception as exc:
            try:
                conn.close()
            except Exception:
                pass
            last = (502, json.dumps({"error": str(exc)}).encode())
    return last


# --------------------------------------------------------------------------
# AI helpers
# --------------------------------------------------------------------------
def ai_providers():
    """Ordered (label, url, key, model) chain — first one that answers wins."""
    out = []
    okey = os.environ.get("OLLAMA_API_KEY", "").strip()
    if okey:
        out.append(("ollama/" + OLLAMA_MODEL, OLLAMA_URL, okey, OLLAMA_MODEL))
    nkey = nvidia_key()
    if nkey:
        out.append(("nvidia/" + GEN_MODEL, NVIDIA_URL, nkey, GEN_MODEL))
    return out


def ai_chat(messages, *, max_tokens=512, temperature=0.4, json_mode=False, timeout=GEN_TIMEOUT):
    """One chat call down the provider chain. Returns (content, provider_label)."""
    providers = ai_providers()
    if not providers:
        raise RuntimeError("no AI provider configured (OLLAMA_API_KEY / NVIDIA_API_KEY)")
    errors = []
    for label, url, key, model in providers:
        payload = {"model": model, "messages": messages,
                   "max_tokens": max_tokens, "temperature": temperature}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json",
                   "Accept": "application/json", "Connection": "keep-alive"}
        body = json.dumps(payload).encode()
        try:
            status, data = http_post(url, headers, body, timeout, retries=1)
            if status == 400 and json_mode:      # endpoint rejects response_format
                payload.pop("response_format", None)
                status, data = http_post(url, headers, json.dumps(payload).encode(), timeout, retries=1)
            if status >= 400:
                raise RuntimeError("HTTP %d: %s" % (status, data[:160].decode("utf-8", "replace")))
            obj = json.loads(data.decode("utf-8", "replace"))
            content = obj["choices"][0]["message"]["content"]
            if not content or not content.strip():
                raise RuntimeError("empty completion")
            return content, label
        except Exception as exc:
            errors.append("%s -> %s" % (label, exc))
            sys.stderr.write("[ai] %s failed: %s\n" % (label, exc))
    raise RuntimeError("all AI providers failed: " + " | ".join(errors))


def _extract_json(text: str):
    """Parse the first JSON object found in a model reply (tolerates prose fences)."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except Exception:
                        break
    raise ValueError("no JSON in model reply")


GEN_SYSTEM = (
    "You are an exam writer. You receive a textbook excerpt and write quiz "
    "questions that test real understanding of it. You always reply with a "
    "single JSON object and nothing else."
)


def build_prompt(text: str, count: int, mode: str) -> str:
    if mode == "short":
        want = "every question must be type \"short\" (free-text answer)"
    elif mode == "mc":
        want = "every question must be type \"mc\" (multiple choice with 4 options)"
    else:
        want = ("mix them: about half type \"short\" (free-text) and half "
                "type \"mc\" (multiple choice with 4 options)")
    return f"""Write exactly {count} quiz questions from the textbook excerpt below. {want}

Rules:
- Test understanding and key facts from the excerpt, not trivia.
- Questions must be self-contained and unambiguous.
- For "short": "key" is a concise model answer (10 words max), "alts" lists 0-4
  equally acceptable phrasings (synonyms, abbreviations, unit forms).
- For "mc": write 4 plausible options covering the topic. "key" must be the
  EXACT text of the correct option, copied character-for-character from
  "options". Distractors must be wrong but believable.

Reply with exactly this JSON shape:
{{"questions":[
  {{"type":"short","q":"...","key":"...","alts":["..."]}},
  {{"type":"mc","q":"...","options":["...","...","...","..."],"key":"..."}}
]}}

Textbook excerpt:
---
{text}
---"""


def generate_questions(text: str, count: int, mode: str):
    """Returns (questions, provider_label) — keys stay server-side and are
    stripped before the payload ever reaches the browser."""
    reply, provider = ai_chat([{"role": "system", "content": GEN_SYSTEM},
                               {"role": "user", "content": build_prompt(text, count, mode)}],
                              max_tokens=2500, temperature=0.5, json_mode=True)
    raw = _extract_json(reply).get("questions", [])
    if not isinstance(raw, list) or not raw:
        raise ValueError("model returned no questions")

    out = []
    for item in raw[:count]:
        if not isinstance(item, dict):
            continue
        q = str(item.get("q") or "").strip()
        qtype = str(item.get("type") or "short").lower()
        if not q:
            continue
        if qtype.startswith("mc") and item.get("options"):
            options = [str(o).strip() for o in item["options"] if str(o).strip()]
            key = str(item.get("key") or "").strip()
            # locate the keyed option even if the model paraphrased it
            idx = _match_option(options, key)
            if idx is None or len(options) < 3:
                # couldn't trust the key -> demote to short answer, keep the key text
                out.append({"type": "short", "q": q, "key": key or "; ".join(options[:2]), "alts": []})
                continue
            correct_text = options.pop(idx)
            order = _shuffle_indices(len(options) + 1, correct_text)
            # rebuild with the correct option placed at a random index
            new_options = options[:]
            new_options.insert(order, correct_text)
            out.append({"type": "mc", "q": q, "options": new_options, "key": order,
                        "answer_text": correct_text})
        else:
            key = str(item.get("key") or "").strip()
            if not key:
                continue
            alts = [str(a).strip() for a in (item.get("alts") or []) if str(a).strip()][:4]
            out.append({"type": "short", "q": q, "key": key, "alts": alts})
    if not out:
        raise ValueError("no usable questions in model reply")
    for i, item in enumerate(out, 1):
        item["id"] = "q%d" % i
    return out, provider


def _match_option(options, key):
    if not key:
        return None
    kl = key.strip().lower()
    for i, o in enumerate(options):
        if o.strip().lower() == kl:
            return i
    for i, o in enumerate(options):
        if kl in o.strip().lower() or o.strip().lower() in kl:
            return i
    return None


def _shuffle_indices(n: int, seed_text: str) -> int:
    """Random position for the correct option, but never always the same one."""
    import random
    return random.randrange(n)


# --------------------------------------------------------------------------
# grading
# --------------------------------------------------------------------------
def jev_grade(q: dict, answer: str):
    """Jev's typed decision: correct | incorrect. Returns (verdict, confidence, usage)."""
    key_text = q["key_text"]
    state = {"question": q["q"], "answer_key": key_text,
             "acceptable": q.get("alts") or [], "student_answer": answer}
    if q["type"] == "mc":
        state["type"] = "multiple_choice"
    body = json.dumps({
        "model": "jev-latest",
        "state": state,
        "questions": {"verdict": {
            "type": "choice",
            "instructions": ("Decide if the student's answer is correct for this question. "
                             "The answer_key is ground truth. Accept any equivalent phrasing, "
                             "synonyms, abbreviations, units, spelling/grammar mistakes and "
                             "extra detail as long as the key fact is present. Multiple-choice "
                             "answers are already reduced to text — compare meaning, not wording."),
            "criteria": {
                "correct": "conveys the key fact / matches the keyed option in meaning",
                "incorrect": "missing the key fact, contradicts it, or unrelated",
            },
        }},
    }).encode()
    headers = {"Authorization": "Bearer " + jev_key(), "Content-Type": "application/json",
               "Accept": "application/json", "Connection": "keep-alive"}
    status, data = http_post(JEV_URL, headers, body, JEV_TIMEOUT, retries=1)
    if status >= 400:
        raise RuntimeError("jev HTTP %d: %s" % (status, data[:200].decode("utf-8", "replace")))
    obj = json.loads(data.decode("utf-8", "replace"))
    ans = obj["answers"]["verdict"]
    usage = obj.get("usage", {})
    return ans.get("choice"), float(ans.get("confidence") or 0), usage


def ai_explain(q: dict, answer: str, excerpt: str) -> str:
    """Why the student's wrong answer is wrong — 1-3 sentences, tutor voice."""
    prompt = (f"Question: {q['q']}\n"
              f"Correct answer: {q['key_text']}\n"
              f"Student answered: {answer}\n"
              f"Textbook excerpt: {excerpt[:1200]}\n\n"
              "In 1-3 direct sentences, explain why the student's answer is wrong and "
              "state the correct idea. No preamble, no restating the question.")
    try:
        out, _ = ai_chat([{"role": "user", "content": prompt}],
                         max_tokens=220, temperature=0.3, timeout=90)
        out = out.strip().strip('"')
        return out if out else "The correct answer is: %s" % q["key_text"]
    except Exception:
        return "The correct answer is: %s" % q["key_text"]


def ai_grade_fallback(q: dict, answer: str, excerpt: str):
    """Used only when Jev is unreachable: one call that both grades and explains."""
    prompt = (f"Question: {q['q']}\nCorrect answer: {q['key_text']}\n"
              f"Student answered: {answer}\nTextbook excerpt: {excerpt[:900]}\n\n"
              'Judge the student answer. Reply with JSON only: '
              '{"correct": true|false, "explanation": "1-3 sentences saying why it is '
              'WRONG (empty string if correct)"}')
    reply, _ = ai_chat([{"role": "user", "content": prompt}],
                       max_tokens=300, temperature=0.3, json_mode=True, timeout=90)
    obj = _extract_json(reply)
    return bool(obj.get("correct")), str(obj.get("explanation") or "").strip()


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------
SESSIONS = {}
S_LOCK = threading.Lock()


def _purge() -> None:
    now = time.time()
    dead = [k for k, v in SESSIONS.items() if now - v["t"] > SESSION_TTL]
    for k in dead:
        SESSIONS.pop(k, None)
    while len(SESSIONS) > MAX_SESSIONS:      # hard cap: oldest first
        oldest = min(SESSIONS, key=lambda k: SESSIONS[k]["t"])
        SESSIONS.pop(oldest, None)


def public_question(q: dict) -> dict:
    """The ONLY shape the browser ever sees — no key, no alts, no answer text."""
    base = {"id": q["id"], "type": q["type"], "q": q["q"]}
    if q["type"] == "mc":
        base["options"] = q["options"]        # correct option already shuffled in
    return base


def grade_one(sess: dict, q: dict, answer: str) -> dict:
    answer = (answer or "").strip()
    key_text = q["key_text"]
    if not answer:
        return {"id": q["id"], "correct": False, "confidence": 1.0, "grader": "blank",
                "explanation": "You didn't answer. The correct answer is: %s" % key_text,
                "student": "", "correct_answer": key_text}
    try:
        verdict, conf, usage = jev_grade(q, answer)
        grader = "jev"
        if verdict not in ("correct", "incorrect"):
            raise ValueError("unexpected verdict %r" % verdict)
    except Exception as exc:
        sys.stderr.write("[grade] jev failed for %s (%s) -> AI fallback\n" % (q["id"], exc))
        verdict, conf = ("incorrect", 0.5)
        grader = "ai-fallback"
        try:
            ok, expl = ai_grade_fallback(q, answer, sess["text"])
            verdict = "correct" if ok else "incorrect"
            return {"id": q["id"], "correct": True if ok else False, "confidence": conf,
                    "grader": grader, "explanation": expl, "student": answer,
                    "correct_answer": key_text}
        except Exception as exc2:
            sys.stderr.write("[grade] fallback failed too: %s\n" % exc2)
            return {"id": q["id"], "correct": None, "confidence": 0.0, "grader": "error",
                    "explanation": "Grading unavailable right now — try submitting again.",
                    "student": answer, "correct_answer": key_text}

    result = {"id": q["id"], "correct": verdict == "correct", "confidence": conf,
              "grader": grader, "explanation": "", "student": answer,
              "correct_answer": key_text, "needs_explanation": verdict != "correct"}
    return result


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def esc_html(value) -> str:
    """Minimal HTML escaping for the few values interpolated into templates."""
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        sys.stderr.write("[recall] %s\n" % (fmt % args))

    def _send(self, code, payload, ctype="application/json", set_cookie=None):
        if isinstance(payload, str):            # HTML / text must NOT be JSON-encoded
            payload = payload.encode("utf-8")
        elif not isinstance(payload, (bytes, bytearray)):
            payload = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(payload)

    # ---- accounts: anonymous token in an HttpOnly cookie (no email, no identity)
    COOKIE = "recall_id"

    def _cookie(self, token):
        secure = "; Secure" if (self.headers.get("X-Forwarded-Proto") == "https") else ""
        return "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax%s" % (
            self.COOKIE, token, 365 * 24 * 3600, secure)

    def _account(self):
        """Returns (token, is_new). Creates the account on first contact."""
        raw = self.headers.get("Cookie") or ""
        m = re.search(r"(?:^|;\s*)" + self.COOKIE + r"=([A-Za-z0-9_\-]+)", raw)
        if m:
            billing.ensure_account(m.group(1))
            return m.group(1), False
        token = billing.new_token()
        billing.ensure_account(token)
        return token, True

    def _public_base(self):
        base = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
        if base:
            return base
        proto = self.headers.get("X-Forwarded-Proto") or "http"
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "localhost"
        return "%s://%s" % (proto, host)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_500_000:
            raise ValueError("payload too large")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8", "replace"))

    # ---- first-run setup -------------------------------------------------
    def _is_local(self):
        """True only for a genuine loopback caller.

        A reverse tunnel (cloudflared, nginx) also connects from 127.0.0.1, so the
        presence of forwarding headers means the request really came from outside —
        otherwise anyone on the internet could claim an unconfigured instance."""
        host = self.client_address[0] or ""
        if host not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            return False
        return not (self.headers.get("X-Forwarded-For") or self.headers.get("CF-Connecting-IP")
                    or self.headers.get("X-Real-IP"))

    def _setup_allowed(self):
        if self._is_local():
            return True
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        given = (q.get("token") or [""])[0] or self.headers.get("X-Setup-Token", "")
        want = _config.get("SETUP_TOKEN", "")
        return bool(want) and hmac.compare_digest(str(given), want)

    def _setup_status(self):
        if not self._setup_allowed():
            return self._send(403, {"error": "setup is only reachable from the host itself, "
                                             "or with ?token=<SETUP_TOKEN>"})
        return self._send(200, _config.status())

    def _setup_save(self):
        if not self._setup_allowed():
            return self._send(403, {"error": "setup is only reachable from the host itself, "
                                             "or with ?token=<SETUP_TOKEN>"})
        values = self._body()
        if not isinstance(values, dict):
            raise ValueError("expected an object of settings")
        _config.save(values)
        reload_ai_settings()
        billing.reload()
        sys.stderr.write("[recall] settings saved -> %s (ready=%s)\n"
                         % (_config.CONFIG_PATH, _config.is_ready()))
        return self._send(200, {"ok": True, "ready": _config.is_ready(),
                                "missing": _config.missing(), "status": _config.status()})

    def _unconfigured(self, path):
        """Nothing works until the keys arrive — send browsers to the wizard."""
        if path == "/" or (self.headers.get("Accept") or "").startswith("text/html"):
            return self._file("setup.html")
        return self._send(503, {"error": "not configured", "setup": "/setup",
                                "missing": _config.missing()})

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/setup":
            return self._setup_status()
        if path == "/setup":
            return self._file("setup.html")
        if not _config.is_ready() and path not in ("/api/health",):
            return self._unconfigured(path)
        if path == "/api/health":
            return self._send(200, {"ok": True,
                                    "providers": [p[0] for p in ai_providers()],
                                    "nvidia_key": bool(nvidia_key()),
                                    "jev_key": bool(jev_key()),
                                    "payments_enabled": billing.PAYMENTS_ENABLED,
                                    "payment_provider": billing.provider().name if billing.PAYMENTS_ENABLED else None,
                                    "keys_ready": billing.payments_ready(),
                                    "configured": _config.is_ready(),
                                    "missing": _config.missing(),
                                    "setup": "/setup",
                                    "public_base": self._public_base()})
        if path == "/api/me":
            return self._me()
        if path.startswith("/mockpay/"):
            return self._mockpay_page(path.rsplit("/", 1)[-1])
        if path == "/admin":
            return self._admin_page()
        if path == "/api/admin/reconcile":
            return self._admin_reconcile()
        if path == "/":
            return self._file("index.html")
        return super().do_GET()

    def _file(self, name):
        f = ROOT / name
        if not f.is_file():
            return self._send(404, {"error": "not found"})
        ctype = "text/html; charset=utf-8" if name.endswith(".html") else \
            "application/javascript; charset=utf-8" if name.endswith(".js") else \
            "text/css; charset=utf-8" if name.endswith(".css") else \
            "image/png" if name.endswith(".png") else "application/octet-stream"
        data = f.read_bytes()
        self._send(200, data, ctype)

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/setup":
                return self._setup_save()
            if not _config.is_ready() and path != "/api/setup" \
                    and not path.startswith("/api/webhook/"):
                return self._send(503, {"error": "not configured", "setup": "/setup",
                                        "missing": _config.missing()})
            if path == "/api/generate":
                return self._generate()
            if path == "/api/grade":
                return self._grade()
            if path == "/api/explain":
                return self._explain()
            if path == "/api/checkout":
                return self._checkout()
            if path == "/api/admin/credit":
                return self._admin_credit()
            if path.startswith("/api/webhook/"):
                return self._webhook(path.rsplit("/", 1)[-1])
            return self._send(404, {"error": "not found"})
        except ValueError as exc:
            return self._send(400, {"error": str(exc)})
        except Exception as exc:
            sys.stderr.write("[error] %s: %s\n" % (path, exc))
            return self._send(502, {"error": str(exc)[:300]})

    # ------------------------------------------------------------------ billing
    def _me(self):
        if not billing.PAYMENTS_ENABLED:
            # free mode: nothing to buy, nothing to charge — no account is created
            return self._send(200, {"payments_enabled": False, "free": True})
        token, is_new = self._account()
        s = billing.account_summary(token)
        body = dict(s)
        body.update(billing.price_table())
        body["topup_step_cents"] = billing.TOPUP_CENTS
        body["payments_enabled"] = True
        return self._send(200, body, set_cookie=self._cookie(token) if is_new else None)

    def _checkout(self):
        if not billing.PAYMENTS_ENABLED:
            return self._send(404, {"error": "payments are turned off on this instance",
                                    "payments_enabled": False})
        body = self._body()
        token, is_new = self._account()
        try:
            units = max(1, min(20, int(body.get("units") or 1)))
        except Exception:
            units = 1
        amount = units * billing.TOPUP_CENTS
        prov = billing.provider()
        order_id = "rq_" + secrets.token_hex(8)
        try:
            inv = prov.create_invoice(order_id, amount, self._public_base(), token)
        except Exception as exc:
            sys.stderr.write("[billing] checkout failed: %s\n" % exc)
            # fail closed: no orphan pending order, no test-checkout fallback. Surface the
            # provider's own message (e.g. "check your secret key / domain is verified") —
            # it is the difference between a fixable setup error and a mystery.
            detail = str(exc).strip().replace("\n", " ")[:160]
            return self._send(502, {"error": "Payments aren't open yet — " + detail,
                                    "provider": prov.name})
        billing.create_purchase(order_id, token, prov.name, amount)
        billing.set_purchase_ref(order_id, inv.get("ref", ""))   # for reconciliation
        return self._send(200, {"url": inv["url"], "order_id": order_id,
                                "amount_cents": amount, "provider": prov.name},
                          set_cookie=self._cookie(token) if is_new else None)

    def _webhook(self, name):
        if not billing.PAYMENTS_ENABLED:
            return self._send(404, {"error": "payments are turned off on this instance"})
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)) if self.headers.get("Content-Length") else b"{}"
        if name == "mock" and billing.provider().name != "mock":
            sys.stderr.write("[billing] refused mock webhook: real provider is configured\n")
            return self._send(404, {"error": "not found"})
        prov = billing.provider(name)
        if prov.name != name:
            return self._send(404, {"error": "unknown provider"})
        ok, data = prov.verify_webhook(raw, self.headers)
        if not ok:
            sys.stderr.write("[billing] rejected %s webhook: %s\n" % (name, data.get("error")))
            return self._send(400, {"error": data.get("error", "bad request")})
        order_id = str(data.get("order_id") or "")
        status = str(data.get("status") or "")
        paid = bool(data.get("_paid") if "_paid" in data else status in ("paid", "succeeded", "complete"))
        ref = str(data.get("uuid") or data.get("id") or data.get("txid") or "")
        settled = billing.settle_purchase(order_id, provider_ref=ref,
                                          status="paid" if paid else status)
        if settled:
            sys.stderr.write("[billing] credited %d to %s (%s)\n" % (settled[1], settled[0][:8], order_id))
        if "text/html" in (self.headers.get("Accept") or ""):
            # browser form post (test checkout): bounce back to the app like a real
            # hosted checkout's return URL would
            self.send_response(303)
            self.send_header("Location", "/?paid=" + urllib.parse.quote(order_id))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return self._send(200, {"status": "ok", "credited": bool(settled),
                                "order_id": order_id})

    def _admin_credit(self):
        if not billing.PAYMENTS_ENABLED:
            return self._send(404, {"error": "payments are turned off on this instance"})
        want = os.environ.get("ADMIN_TOKEN", "").strip()
        if not want or not hmac.compare_digest(self.headers.get("X-Admin-Token", ""), want):
            return self._send(403, {"error": "admin token required"})
        body = self._body()
        token = str(body.get("token") or "")
        if not token:
            token, _ = self._account()
        cents = int(body.get("amount_cents") or billing.TOPUP_CENTS)
        return self._send(200, {"ok": True, "token": token,
                                "balance_cents": billing.grant(token, cents, "admin")})

    def _admin_reconcile(self):
        """Owner-only safety net: settle purchases whose webhook never arrived.

        A missed callback must never mean a customer paid and got nothing, so this asks
        the provider about every uncredited invoice we created and credits the paid ones
        through the same idempotent settle path the webhook uses."""
        if not billing.PAYMENTS_ENABLED:
            return self._send(404, {"error": "payments are turned off on this instance"})
        want = os.environ.get("ADMIN_TOKEN", "").strip()
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        given = self.headers.get("X-Admin-Token") or (q.get("token") or [""])[0]
        if not want or not hmac.compare_digest(str(given), want):
            return self._send(403, {"error": "admin token required"})
        prov = billing.provider()
        if not hasattr(prov, "check_invoice"):
            return self._send(400, {"error": "provider %s cannot be reconciled" % prov.name})
        rows = billing.pending_purchases(prov.name)
        checked = credited = 0
        results = []
        for row in rows:
            ref = row.get("provider_ref")
            if not ref:
                continue
            checked += 1
            try:
                status, paid = prov.check_invoice(ref)
            except Exception as exc:
                results.append({"order": row["order_id"], "error": str(exc)[:140]})
                continue
            if paid:
                done = billing.settle_purchase(row["order_id"], provider_ref=ref, status="paid")
                if done:
                    credited += 1
                    results.append({"order": row["order_id"], "credited_cents": done[1]})
            else:
                results.append({"order": row["order_id"], "status": status, "paid": False})
        sys.stderr.write("[billing] reconcile: %d checked, %d credited\n" % (checked, credited))
        return self._send(200, {"provider": prov.name, "checked": checked,
                                "credited": credited, "results": results})

    def _admin_page(self):
        """Owner-only top-up page: /admin?token=<ADMIN_TOKEN>&cents=100 credits the
        browser account in front of it, so the app can be tested on a phone without paying."""
        if not billing.PAYMENTS_ENABLED:
            return self._send(404, {"error": "payments are turned off on this instance"})
        want = os.environ.get("ADMIN_TOKEN", "").strip()
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        given = (q.get("token") or [""])[0]
        if not want or not hmac.compare_digest(given, want):
            return self._send(403, {"error": "admin token required"})
        try:
            cents = max(-10000, min(10000, int((q.get("cents") or ["100"])[0])))
        except Exception:
            cents = 100
        token, is_new = self._account()
        if cents < 0:                      # a drain can only go down to zero
            cents = -min(billing.balance(token), -cents)
        balance = billing.grant(token, cents, "admin-page")
        sys.stderr.write("[billing] admin page granted %+d, balance %d\n" % (cents, balance))
        html = ("<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
                "<body style='font:16px/1.6 system-ui;background:#0b1020;color:#eef1ff;"
                "display:grid;place-items:center;height:100dvh;margin:0;text-align:center'>"
                "<div><h1 style='font-size:20px'>Credit updated</h1>"
                "<p>%+d cents &middot; balance now <b>%s</b></p>"
                "<p><a style='color:#a78bfa' href='/'>back to the app</a></p></div>"
                % (cents, billing.fmt(balance)))
        # always hand back the cookie: without it the credit lands on a throwaway account
        return self._send(200, html, "text/html; charset=utf-8",
                          set_cookie=self._cookie(token))

    def _mockpay_page(self, order_id):
        """Local stand-in for a hosted checkout so the paid flow can be tested with no keys.
        Hard-disabled unless the service is actually running the mock provider."""
        if not billing.PAYMENTS_ENABLED:
            return self._send(404, {"error": "not found"})
        if billing.provider().name != "mock":
            return self._send(404, {"error": "not found"})
        secret = os.environ.get("MOCK_WEBHOOK_SECRET", "")
        if not secret:
            return self._send(500, {"error": "MOCK_WEBHOOK_SECRET not set"})
        sign = hashlib.md5((order_id + secret).encode()).hexdigest()
        amount = billing.TOPUP_CENTS
        try:
            row = billing.db().execute("SELECT amount FROM purchases WHERE order_id=?",
                                       (order_id,)).fetchone()
            if row:
                amount = int(row["amount"])
        except Exception:
            pass
        tpl = (ROOT / "mockpay.html").read_text(encoding="utf-8")
        html = (tpl.replace("{{ORDER}}", esc_html(order_id))
                   .replace("{{SIGN}}", sign)
                   .replace("{{AMOUNT}}", "%.2f" % (amount / 100.0))
                   .replace("{{SYMBOL}}", billing.CURRENCY_SYMBOL))
        return self._send(200, html, "text/html; charset=utf-8")

    def _generate(self):
        body = self._body()
        text = str(body.get("text") or "").strip()
        if len(text) < 120:
            raise ValueError("Paste at least a paragraph (120 characters) of material.")
        truncated = False
        if len(text) > MAX_TEXT:
            text, truncated = text[:MAX_TEXT], True
        try:
            count = int(body.get("count") or 5)
        except Exception:
            count = 5
        count = max(3, min(10, count))
        mode = str(body.get("mode") or "mixed")
        if mode not in ("mixed", "short", "mc"):
            mode = "mixed"

        # ---- paid gate: charged only when payments are on, and only if generation succeeds
        token = ""
        set_cookie = None
        price = 0
        if billing.PAYMENTS_ENABLED:
            token, is_new = self._account()
            price = billing.price_for(count)
            set_cookie = self._cookie(token) if is_new else None
            have = billing.balance(token)
            if have < price:
                return self._send(402, {"error": "Not enough credit",
                                        "need_cents": price, "balance_cents": have,
                                        "short_cents": price - have,
                                        "quizzes_left": have // max(1, billing.PRICE_PER_5),
                                        "prices": billing.price_table()},
                                  set_cookie=set_cookie)

        questions, provider = generate_questions(text, count, mode)
        left = 0
        if billing.PAYMENTS_ENABLED:
            paid, price, left = billing.charge(token, count)
            if not paid:                     # someone spent the balance mid-flight
                return self._send(402, {"error": "Not enough credit", "need_cents": price,
                                        "balance_cents": left, "short_cents": price - left,
                                        "prices": billing.price_table()}, set_cookie=set_cookie)
        sid = secrets.token_urlsafe(16)
        with S_LOCK:
            _purge()
            SESSIONS[sid] = {"t": time.time(), "text": text, "questions": questions,
                             "token": token}
        if billing.PAYMENTS_ENABLED:
            sys.stderr.write("[billing] charged %d (balance %d) for %dq\n" % (price, left, count))
        return self._send(200, {"sid": sid, "truncated": truncated,
                                "free": not billing.PAYMENTS_ENABLED,
                                "price_cents": price, "balance_cents": left,
                                "questions": [public_question(q) for q in questions]},
                          set_cookie=set_cookie)

    def _grade(self):
        body = self._body()
        sid = str(body.get("sid") or "")
        answers = body.get("answers") or {}
        with S_LOCK:
            sess = SESSIONS.get(sid)
            if sess:
                sess["t"] = time.time()
        if not sess:
            return self._send(410, {"error": "This quiz has expired — paste your text again."})

        with S_LOCK:
            _purge()

        # assign the answer key text once (mc stores an index)
        for q in sess["questions"]:
            if q["type"] == "mc":
                q["key_text"] = q["options"][q["key"]]
            else:
                q["key_text"] = q["key"]
        sess["answers"] = {k: str(v)[:MAX_ANSWER] for k, v in answers.items()}
        sess["explain"] = {}          # per-question explanation cache

        jobs = []
        with ThreadPoolExecutor(max_workers=min(8, len(sess["questions"]))) as pool:
            for q in sess["questions"]:
                ans = str(answers.get(q["id"]) or "")[:MAX_ANSWER]
                jobs.append((q, pool.submit(grade_one, sess, q, ans)))
            results = [fut.result() for _, fut in jobs]

        # keep server order
        order = {q["id"]: i for i, q in enumerate(sess["questions"])}
        results.sort(key=lambda r: order.get(r["id"], 99))

        graded = [r for r in results if r["correct"] is not None]
        right = sum(1 for r in results if r["correct"] is True)
        return self._send(200, {
            "results": results,
            "score": {"correct": right, "total": len(results), "graded": len(graded)},
            "graders": sorted({r["grader"] for r in results}),
        })

    def _explain(self):
        """Lazy 'why it's wrong' text: grading returns in ~1s, the explanations
        are fetched per question afterwards so the results screen is instant."""
        body = self._body()
        sid = str(body.get("sid") or "")
        qid = str(body.get("qid") or "")
        with S_LOCK:
            sess = SESSIONS.get(sid)
            if sess:
                sess["t"] = time.time()
            lock = sess.setdefault("explain_locks", {}).setdefault(qid, threading.Lock()) if sess else None
        if not sess:
            return self._send(410, {"error": "This quiz has expired."})
        q = next((x for x in sess["questions"] if x["id"] == qid), None)
        if not q:
            return self._send(404, {"error": "unknown question"})

        cache = sess.setdefault("explain", {})
        with lock:
            hit = cache.get(qid)
            if hit is None:
                answer = str(sess.get("answers", {}).get(qid) or "").strip()
                hit = ("You didn't answer. The correct answer is: " + q["key_text"]) if not answer \
                    else ai_explain(q, answer, sess["text"])
                cache[qid] = hit
        return self._send(200, {"qid": qid, "explanation": hit})


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    if not _config.is_ready():
        tok = _config.get("SETUP_TOKEN", "")
        print("")
        print("  Recall needs your API keys before it can run.")
        print("  Open the setup page and paste them in:")
        print("      http://127.0.0.1:%d/setup?token=%s" % (PORT, tok))
        print("  (from another machine, use http://<this-box>:%d/setup?token=%s)" % (PORT, tok))
        print("  Wrote nothing? Settings live in %s" % _config.CONFIG_PATH)
        print("")
    else:
        if not jev_key():
            sys.stderr.write("[recall] WARNING: no Jev key — grading falls back to the AI model\n")
    chain = ", ".join(p[0] for p in ai_providers()) or "none"
    pay = billing.provider()
    if billing.PAYMENTS_ENABLED:
        print("Recall -> http://%s:%d/   (gen=%s, judge=jev-latest, pay=%s, %s per 5q, %s%s top-ups)"
              % (HOST, PORT, chain, pay.name, billing.fmt(billing.PRICE_PER_5),
                 billing.CURRENCY_SYMBOL, "%.2f" % (billing.TOPUP_CENTS / 100.0)))
    else:
        print("Recall -> http://%s:%d/   (gen=%s, judge=jev-latest, payments off — free)"
              % (HOST, PORT, chain))
    if billing.PAYMENTS_ENABLED and _config.is_ready() and not billing.payments_ready():
        sys.stderr.write("[recall] WARNING: PAYMENT_PROVIDER=%s has no keys — checkout will "
                         "fail until they are set (setup page)\n" % pay.name)
    if billing.PAYMENTS_ENABLED and pay.name == "mock" and not os.environ.get("MOCK_WEBHOOK_SECRET"):
        sys.stderr.write("[recall] note: mock checkout needs MOCK_WEBHOOK_SECRET to complete "
                         "test payments\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
