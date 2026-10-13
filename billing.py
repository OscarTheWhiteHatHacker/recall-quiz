"""Recall billing — anonymous credit accounts, $1 top-ups, 1¢ per 5 questions.

Money model
-----------
* Everything is tracked in **minor units** (cents/pence): 100 = one currency unit.
* A quiz costs `ceil(questions / 5) * PRICE_PER_5` minor units (default 1¢ per 5).
* Users top up in whole units (default $1.00 = 100 minor units); credits never expire.
* Accounts are **anonymous**: a random token in an HttpOnly cookie, no email, no password,
  no identity data — which is the whole point of picking a no-ID payment route.

Providers
---------
`mock`      — local/test checkout page, no keys, no money. Lets the whole paid flow be
              exercised end-to-end (including signed webhooks) before going live.
`plisio`    — crypto checkout. Email-only onboarding (their FAQ: "our service does not
              require identification"), 0.5% flat, settles to a balance you withdraw to a
              wallet. Buyers need crypto, and they are the ones who had to pass a check
              somewhere to get it.
`cryptomus` — card checkout settling in crypto. Despite the "no KYC" marketing, merchant
              onboarding asks for ID + a selfie, so it is not a no-ID route. Kept because
              the integration is done and the call is yours to make.

Card rail reality, for whoever is choosing: the card networks require the acquiring bank to
verify the merchant, so any legitimate card route ends in somebody's ID. Refusing KYC means
crypto in, which moves the check to the buyer rather than removing it.

Add a provider by implementing `create_invoice()` / `verify_webhook()` and registering it.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CURRENCY = os.environ.get("CURRENCY", "USD").upper()
CURRENCY_SYMBOL = os.environ.get("CURRENCY_SYMBOL", "$" if CURRENCY == "USD" else "£")
PRICE_PER_5 = int(os.environ.get("PRICE_PER_5_CENTS", "1"))      # 1 minor unit per 5 questions
TOPUP_CENTS = int(os.environ.get("TOPUP_CENTS", "100"))          # $1.00 per top-up step
FREE_CENTS = int(os.environ.get("FREE_CREDIT_CENTS", "0"))       # signup credit (0 = paywall from the start)
MIN_TOPUP_CENTS = TOPUP_CENTS
MAX_BALANCE_CENTS = int(os.environ.get("MAX_BALANCE_CENTS", "10000"))

DB_PATH = os.environ.get("BILLING_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "billing.db"))

_LOCK = threading.RLock()
_CONN = None


# ---------------------------------------------------------------- storage
def reload() -> None:
    """Re-read pricing/currency settings (the wizard can change them at runtime)."""
    global CURRENCY, CURRENCY_SYMBOL, PRICE_PER_5, TOPUP_CENTS, FREE_CENTS, MAX_BALANCE_CENTS
    CURRENCY = os.environ.get("CURRENCY", "USD").upper()
    CURRENCY_SYMBOL = os.environ.get("CURRENCY_SYMBOL", "$" if CURRENCY == "USD" else "£")
    PRICE_PER_5 = int(os.environ.get("PRICE_PER_5_CENTS", "1"))
    TOPUP_CENTS = int(os.environ.get("TOPUP_CENTS", "100"))
    FREE_CENTS = int(os.environ.get("FREE_CREDIT_CENTS", "0"))
    MAX_BALANCE_CENTS = int(os.environ.get("MAX_BALANCE_CENTS", "10000"))


def db():
    global _CONN
    with _LOCK:
        if _CONN is None:
            _CONN = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)
            _CONN.row_factory = sqlite3.Row
            _CONN.execute("PRAGMA journal_mode=WAL")
            _CONN.execute("PRAGMA foreign_keys=ON")
            _CONN.executescript("""
            CREATE TABLE IF NOT EXISTS accounts(
              token     TEXT PRIMARY KEY,
              created   REAL NOT NULL,
              credits   INTEGER NOT NULL DEFAULT 0,
              spent     INTEGER NOT NULL DEFAULT 0,
              quizzes   INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS purchases(
              order_id     TEXT PRIMARY KEY,
              token        TEXT NOT NULL,
              provider     TEXT NOT NULL,
              amount       INTEGER NOT NULL,
              currency     TEXT NOT NULL,
              status       TEXT NOT NULL,
              provider_ref TEXT,
              created      REAL NOT NULL,
              credited     REAL);
            CREATE TABLE IF NOT EXISTS spends(
              id        INTEGER PRIMARY KEY AUTOINCREMENT,
              token     TEXT NOT NULL,
              sid       TEXT,
              questions INTEGER NOT NULL,
              price     INTEGER NOT NULL,
              created   REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS spends_token ON spends(token);
            """)
            _CONN.commit()
    return _CONN


def new_token() -> str:
    return secrets.token_urlsafe(24)


def ensure_account(token: str) -> sqlite3.Row:
    db()
    with _LOCK:
        row = _CONN.execute("SELECT * FROM accounts WHERE token=?", (token,)).fetchone()
        if row is None:
            _CONN.execute("INSERT INTO accounts(token, created, credits) VALUES(?,?,?)",
                          (token, time.time(), FREE_CENTS))
            _CONN.commit()
            row = _CONN.execute("SELECT * FROM accounts WHERE token=?", (token,)).fetchone()
        return row


def balance(token: str) -> int:
    return int(ensure_account(token)["credits"])


def price_for(questions: int) -> int:
    """1¢ per 5 questions, rounded up: 3-5q = 1¢, 6-10q = 2¢."""
    q = max(1, int(questions))
    return ((q + 4) // 5) * PRICE_PER_5


def charge(token: str, questions: int, sid: str = "") -> tuple:
    """Atomically debit the quiz price. Returns (ok, price, balance_after)."""
    price = price_for(questions)
    db()
    with _LOCK:
        row = _CONN.execute("SELECT credits FROM accounts WHERE token=?", (token,)).fetchone()
        if row is None:
            return False, price, 0
        if row["credits"] < price:
            return False, price, int(row["credits"])
        _CONN.execute("UPDATE accounts SET credits=credits-?, spent=spent+?, quizzes=quizzes+1 "
                      "WHERE token=?", (price, price, token))
        _CONN.execute("INSERT INTO spends(token, sid, questions, price, created) VALUES(?,?,?,?,?)",
                      (token, sid, questions, price, time.time()))
        _CONN.commit()
        left = _CONN.execute("SELECT credits FROM accounts WHERE token=?", (token,)).fetchone()["credits"]
        return True, price, int(left)


def grant(token: str, cents: int, note: str = "") -> int:
    """Add credit (webhook payment, admin grant, free trial). Returns new balance."""
    db()
    with _LOCK:
        ensure_account(token)
        _CONN.execute("UPDATE accounts SET credits=credits+? WHERE token=?", (int(cents), token))
        _CONN.commit()
        return int(_CONN.execute("SELECT credits FROM accounts WHERE token=?", (token,)).fetchone()["credits"])


# ---------------------------------------------------------------- purchases
def create_purchase(order_id: str, token: str, provider: str, amount: int) -> None:
    db()
    with _LOCK:
        _CONN.execute("INSERT OR REPLACE INTO purchases(order_id, token, provider, amount, "
                      "currency, status, created) VALUES(?,?,?,?,?,?,?)",
                      (order_id, token, provider, int(amount), CURRENCY, "pending", time.time()))
        _CONN.commit()


def settle_purchase(order_id: str, provider_ref: str = "", status: str = "paid"):
    """Credit a paid purchase exactly once. Returns (token, amount) or None if it was
    unknown, already credited, or not a successful status."""
    ok_statuses = {"paid", "paid_over", "complete", "succeeded"}
    db()
    with _LOCK:
        row = _CONN.execute("SELECT * FROM purchases WHERE order_id=?", (order_id,)).fetchone()
        if row is None or row["credited"] is not None or status not in ok_statuses:
            if row is not None and row["credited"] is None:
                _CONN.execute("UPDATE purchases SET status=? WHERE order_id=?", (status, order_id))
                _CONN.commit()
            return None
        _CONN.execute("UPDATE purchases SET status=?, provider_ref=?, credited=? WHERE order_id=?",
                      (status, provider_ref, time.time(), order_id))
        ensure_account(row["token"])          # anonymous accounts are created lazily
        _CONN.execute("UPDATE accounts SET credits=credits+? WHERE token=?",
                      (int(row["amount"]), row["token"]))
        _CONN.commit()
        return row["token"], int(row["amount"])


def set_purchase_ref(order_id: str, ref: str) -> None:
    """Remember the provider's invoice reference so it can be reconciled later."""
    if not ref:
        return
    db()
    with _LOCK:
        _CONN.execute("UPDATE purchases SET provider_ref=? WHERE order_id=? AND provider_ref IS NULL",
                      (str(ref), order_id))
        _CONN.commit()


def pending_purchases(provider_name: str, limit: int = 50):
    """Purchases that were never credited — candidates for reconciliation."""
    db()
    # any purchase that was never credited, whatever status the provider has pushed so far
    # (pending -> new -> ... ), skipping ones that are terminally dead
    rows = _CONN.execute(
        "SELECT order_id, token, amount, provider_ref, created FROM purchases "
        "WHERE credited IS NULL AND provider=? "
        "AND (status IS NULL OR status NOT IN "
        "     ('expired','cancelled','cancelled duplicate','error','mismatch','refunded')) "
        "ORDER BY created DESC LIMIT ?", (provider_name, limit)).fetchall()
    return [dict(r) for r in rows]


def account_summary(token: str) -> dict:
    row = ensure_account(token)
    return {"balance_cents": int(row["credits"]), "spent_cents": int(row["spent"]),
            "quizzes": int(row["quizzes"])}


# ---------------------------------------------------------------- providers
class MockProvider:
    name = "mock"
    label = "Test checkout (no real payment)"

    def create_invoice(self, order_id, amount_cents, base_url, token):
        return {"url": base_url.rstrip("/") + "/mockpay/" + order_id,
                "ref": "mock-" + order_id}

    def verify_webhook(self, body: bytes, headers) -> tuple:
        """Accepts JSON or form-encoded callbacks: {"order_id","status","sign"} where
        sign = md5(order_id + MOCK_WEBHOOK_SECRET). Also honours provider-style
        order ids sent as `metadata[order_id]`."""
        text = body.decode("utf-8", "replace").strip()
        data = {}
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except Exception:
                data = {}
        else:
            flat = urllib.parse.parse_qs(text)
            data = {k: v[0] for k, v in flat.items()}
            if "metadata[order_id]" in data:
                data["order_id"] = data["metadata[order_id]"]
        secret = os.environ.get("MOCK_WEBHOOK_SECRET", "")
        order_id = str(data.get("order_id", ""))
        expect = hashlib.md5((order_id + secret).encode()).hexdigest()
        ok = bool(secret) and hmac.compare_digest(expect, str(data.get("sign", "")))
        data["_paid"] = str(data.get("status", "")) in ("paid", "succeeded", "complete")
        return ok, data


class CryptomusProvider:
    """Cards (Visa/MC/Apple Pay) via Cryptomus hosted checkout, settled in crypto.

    Onboarding is email + wallet address; they own the card acquiring and the AML rules
    that come with it. Docs: https://doc.cryptomus.com
    """
    name = "cryptomus"
    label = "Card payment (crypto settlement)"

    API = os.environ.get("CRYPTOMUS_API", "https://api.cryptomus.com/v1/payment")

    def _keys(self):
        return (os.environ.get("CRYPTOMUS_MERCHANT_ID", "").strip(),
                os.environ.get("CRYPTOMUS_API_KEY", "").strip())

    @staticmethod
    def sign(payload: dict, api_key: str) -> str:
        """md5(base64(json) + api_key) — Cryptomus' documented request/webhook scheme."""
        raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        b64 = base64.b64encode(raw.encode()).decode()
        return hashlib.md5((b64 + api_key).encode()).hexdigest()

    def create_invoice(self, order_id, amount_cents, base_url, token):
        merchant, key = self._keys()
        if not merchant or not key:
            raise RuntimeError("CRYPTOMUS_MERCHANT_ID / CRYPTOMUS_API_KEY not set")
        amount = "%.2f" % (amount_cents / 100.0)
        body = {"amount": amount, "currency": CURRENCY, "order_id": order_id,
                "url_callback": base_url.rstrip("/") + "/api/webhook/cryptomus",
                "url_return": base_url.rstrip("/") + "/?paid=" + order_id,
                "lifetime": 3600, "is_test": os.environ.get("CRYPTOMUS_TEST") == "1"}
        req = urllib.request.Request(
            self.API, data=json.dumps(body).encode(),
            headers={"merchant": merchant, "sign": self.sign(body, key),
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.load(r)
        if data.get("state") != 0 or "url" not in (data.get("result") or {}):
            raise RuntimeError("cryptomus error: %s" % json.dumps(data)[:200])
        return {"url": data["result"]["url"], "ref": data["result"].get("uuid", "")}

    def verify_webhook(self, body: bytes, headers) -> tuple:
        """Accept the documented md5(base64(json) + key) sign, with or without 'sign' in
        the signed payload (implementations differ), and only over final paid statuses."""
        _, key = self._keys()
        if not key:
            return False, {"error": "cryptomus key not configured"}
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except Exception:
            return False, {"error": "bad json"}
        got = str(data.get("sign", ""))
        without = {k: v for k, v in data.items() if k != "sign"}
        # Two separator styles are seen in the wild; either is accepted.
        candidates = []
        for sep in ((",", ":"), (", ", ": ")):
            raw = json.dumps(without, separators=sep, ensure_ascii=False)
            b64 = base64.b64encode(raw.encode()).decode()
            candidates.append(hashlib.md5((b64 + key).encode()).hexdigest())
        if not any(hmac.compare_digest(c, got) for c in candidates):
            return False, {"error": "bad signature"}
        data["order_id"] = str(data.get("order_id", ""))
        data["status"] = str(data.get("status", ""))
        final = data.get("is_final") in (True, "true", 1, "1")
        data["_paid"] = final and data["status"] in ("paid", "paid_over")
        return True, data


class PlisioProvider:
    """Crypto checkout with no merchant identity verification.

    Plisio's own FAQ: "Do I need to confirm my identity? No. Our service does not require
    identification." There is no card acquiring in this path, so no card-network merchant
    due diligence applies either — the buyer pays in crypto. 0.5% flat, settles to the
    Plisio balance/wallet, 20+ coins. Docs: https://plisio.net/documentation
    """
    name = "plisio"
    label = "Crypto checkout (no ID needed)"
    note = "Pay in crypto — USDT, BTC, ETH, TON, LTC and more"
    API = os.environ.get("PLISIO_API", "https://api.plisio.net/api/v1")
    PAID_STATUSES = {"completed"}

    def _key(self):
        key = os.environ.get("PLISIO_SECRET_KEY", "").strip()
        if not key:
            raise RuntimeError("PLISIO_SECRET_KEY not set (Plisio -> Site settings -> API)")
        return key

    def create_invoice(self, order_id, amount_cents, base_url, token):
        key = self._key()
        base = base_url.rstrip("/")
        params = {
            "api_key": key,
            "order_name": "Recall credit",
            "order_number": order_id,
            "source_currency": CURRENCY,
            "source_amount": "%.2f" % (amount_cents / 100.0),
            # ?json=true makes Plisio POST JSON: the only callback form that can be
            # verified language-independently (HMAC-SHA1 over the compact body)
            "callback_url": base + "/api/webhook/plisio?json=true",
            "success_callback_url": base + "/?paid=" + order_id,
            "fail_callback_url": base + "/",
            "description": "Recall quiz credit",
        }
        psys = os.environ.get("PLISIO_PSYS", "").strip()
        if psys:
            params["allowed_psys_cids"] = psys
        url = self.API + "/invoices/new?" + urllib.parse.urlencode(params)
        # Plisio's edge blocks requests without a browser User-Agent (403, Cloudflare 1010)
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/126 Safari/537.36"})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            # surface Plisio's own message (e.g. "check your secret key ... domain is
            # verified") so the checkout error the user sees is actionable
            detail = ""
            try:
                err = json.loads(e.read().decode("utf-8", "replace"))
                detail = (err.get("data") or {}).get("message") or err.get("message") or ""
            except Exception:
                pass
            raise RuntimeError("plisio: %s" % (detail or ("HTTP %s" % e.code)))
        if str(data.get("status")) != "success" or not (data.get("data") or {}).get("invoice_url"):
            raise RuntimeError("plisio error: %s" % json.dumps(data)[:200])
        d = data["data"]
        return {"url": d["invoice_url"], "ref": str(d.get("txn_id", ""))}

    def check_invoice(self, ref):
        """(status, paid) for an invoice we created — used to reconcile a missed webhook."""
        key = self._key()
        u = "%s/operations/%s?%s" % (self.API, urllib.parse.quote(str(ref)),
                                     urllib.parse.urlencode({"api_key": key}))
        req = urllib.request.Request(u, headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/126 Safari/537.36"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = ((json.loads(e.read().decode("utf-8", "replace")) or {})
                          .get("data") or {}).get("message", "")
            except Exception:
                pass
            # raise rather than pretending "unpaid" — a silent empty status hides a
            # broken lookup, and this path exists to protect real money
            raise RuntimeError("plisio lookup HTTP %s %s" % (e.code, detail))
        except Exception as e:
            raise RuntimeError("plisio lookup failed: %s" % e)
        d = data.get("data") or {}
        st = str(d.get("status") or "")
        return st, st in self.PAID_STATUSES

    def verify_webhook(self, body, headers):
        """verify_hash = HMAC-SHA1 over the compact JSON body with verify_hash removed,
        original key order preserved, keyed by the secret key (Plisio's ?json=true scheme)."""
        key = os.environ.get("PLISIO_SECRET_KEY", "").strip()
        if not key:
            return False, {"error": "plisio key not configured"}
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except Exception:
            return False, {"error": "bad json"}
        if not isinstance(data, dict):
            return False, {"error": "bad json"}
        got = str(data.pop("verify_hash", "") or "")
        verified = False
        for ascii_mode in (True, False):
            payload = json.dumps(data, separators=(",", ":"), ensure_ascii=ascii_mode)
            calc = hmac.new(key.encode(), payload.encode(), hashlib.sha1).hexdigest()
            if got and hmac.compare_digest(calc, got):
                verified = True
                break
        if not verified:
            return False, {"error": "bad signature"}
        status = str(data.get("status", ""))
        data["order_id"] = str(data.get("order_number", ""))
        data["txid"] = str(data.get("txn_id", ""))
        data["_paid"] = status in self.PAID_STATUSES
        return True, data


def provider(name: str = ""):
    name = (name or os.environ.get("PAYMENT_PROVIDER", "mock")).lower()
    return {"mock": MockProvider(), "cryptomus": CryptomusProvider(),
            "plisio": PlisioProvider()}.get(name, MockProvider())


def payments_ready() -> bool:
    """True when the configured provider can actually take money."""
    p = provider()
    if p.name == "mock":
        return True
    if p.name == "cryptomus":
        return bool(os.environ.get("CRYPTOMUS_MERCHANT_ID", "").strip()
                    and os.environ.get("CRYPTOMUS_API_KEY", "").strip())
    if p.name == "plisio":
        return bool(os.environ.get("PLISIO_SECRET_KEY", "").strip())
    return False


def price_table() -> dict:
    return {"per_5_questions": PRICE_PER_5, "currency": CURRENCY,
            "symbol": CURRENCY_SYMBOL,
            "topup_cents": TOPUP_CENTS, "free_cents": FREE_CENTS,
            "provider": provider().name, "provider_label": provider().label,
            "provider_note": getattr(provider(), "note", ""),
            "payments_ready": payments_ready()}


def fmt(cents: int) -> str:
    if PRICE_PER_5 == 1 and cents <= 10:
        return "%d%s" % (cents, "¢" if CURRENCY == "USD" else "p")
    return "%s%.2f" % (CURRENCY_SYMBOL, cents / 100.0)
