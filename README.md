# Recall

Paste in a textbook chapter, lecture notes or any block of text and get a multiple-choice
quiz back. The answer key stays on the server, your answers are marked by
[Jev](https://api.typesafe.ai) rather than string matching, and anything you get wrong comes
back with an explanation.

**Bring your own keys.** This edition ships with no credentials of any kind: the first time
you start it, the app asks you for your own API keys and writes them to a local
`config.env` (chmod 600). Nothing else is phoned home.

* Python 3.9+ standard library only — no pip install, no framework, no build step
* Single-page mobile UI, works as a phone-friendly web app
* Optional paid mode: 1¢ per 5 questions, $1 top-ups, anonymous cookie accounts
* Grading happens server-side; the answer key never reaches the browser

## Quickstart

```bash
git clone https://github.com/<you>/recall-quiz && cd recall-quiz
python3 server.py
```

It prints something like:

```
  Recall needs your API keys before it can run.
  Open the setup page and paste them in:
      http://127.0.0.1:3460/setup?token=8f3c...
```

Open that link, paste your keys, and the app starts working immediately — no restart, no
rebuild. Everything except the health endpoint is switched off until then, and the setup
page is only reachable from the host itself (or with the one-time `?token=`), so nobody can
claim an instance you left running on the open internet.

## The keys it asks for

| Setting | Needed for | Where to get it |
|---|---|---|
| `TYPESAFE_API_KEY` | **Required.** Marks the answers | [api.typesafe.ai](https://api.typesafe.ai) → API keys |
| `OLLAMA_API_KEY` | Writes the questions (fastest, free tier) | [ollama.com](https://ollama.com) → Settings → Keys |
| `NVIDIA_API_KEY` | Fallback generator, used if Ollama fails | [build.nvidia.com](https://build.nvidia.com) |

One generator key is enough; two gives you automatic failover. Anything in the environment
overrides the config file, so systemd `EnvironmentFile=` / `docker -e` / CI secrets all work
and the wizard is purely a convenience.

## Going live on a domain

The app runs happily on `http://localhost:3460`. To take payments you need a public HTTPS
URL, because the provider calls *you* back at `PUBLIC_BASE_URL + /api/webhook/<provider>`.
Any tunnel or reverse proxy works; with Cloudflare Tunnel it's one command:

```bash
cloudflared tunnel --url http://127.0.0.1:3460
```

then set **Public base URL** on the setup page to the `https://…` hostname it gives you.
Note that a quick tunnel gets a **new hostname every restart** — fine for testing, but
buyers' credit lives in a cookie scoped to the hostname, so use a stable hostname on a real
tunnel before charging anyone.

## Payments (optional)

Left as `mock`, the app runs a **built-in test checkout**: a local page that walks the entire
paid flow — invoice → signed webhook → credit → charge per quiz — with no money and no
account. That's what the test suite uses.

| Provider | What the buyer pays with | Onboarding |
|---|---|---|
| `mock` | nothing — test checkout page | none |
| `plisio` | crypto (BTC, ETH, USDT, TON, LTC, SOL …) | email only, no ID, 0.5% |
| `cryptomus` | cards, settled in crypto | **merchant KYC: ID + selfie** |

**The honest version of the KYC question:** card networks require the acquiring bank to
verify the merchant, so a legitimate card rail always ends in someone's ID. If you refuse
KYC, the option left is crypto-in — and that simply moves the check to your buyer, who needs
to hold crypto to pay you. Pick whichever side of the transaction you'd rather verify.

Set **Public base URL** to your domain, save it in the provider's own site settings too if
they ask for one, and the webhook path is `/api/webhook/<provider>`.

## Keeping the money honest

* `/admin?token=<ADMIN_TOKEN>&cents=100` — credit your own browser's account (safe when
  `ADMIN_TOKEN` is unset: the page then refuses everyone)
* `python3 reconcile.py` — ask the provider about every uncredited invoice and settle the
  paid ones. Webhooks can be dropped by an edge proxy or a restart, and a customer who paid
  must never be left with nothing. Install the timer to run it every 10 minutes:
  ```bash
  mkdir -p ~/.config/systemd/user && cp systemd/* ~/.config/systemd/user/
  systemctl --user enable --now recall-reconcile.timer
  ```
* Settlement is idempotent — a replayed webhook or a reconcile that races a late callback
  cannot credit the same order twice

## Running it as a service

```bash
sudo useradd -r -s /usr/sbin/nologin recall
sudo cp -r . /opt/recall && sudo chown -R recall: /opt/recall
sudo cp systemd/recall.service /etc/systemd/system/
sudo systemctl enable --now recall        # then open http://<host>:3460/setup
```

## Configuration

Every setting lives on the setup page, and the same names work as environment variables —
see `.env.example` for the full list with defaults. The interesting ones:

| Setting | Default | Meaning |
|---|---|---|
| `PRICE_PER_5_CENTS` | `1` | minor units per 5 questions (1 = 1¢) |
| `TOPUP_CENTS` | `100` | top-up step ($1.00) |
| `FREE_CREDIT_CENTS` | `0` | free trial credit per new account |
| `PAYMENT_PROVIDER` | `mock` | `mock`, `plisio` or `cryptomus` |
| `ADMIN_TOKEN` | auto | owner access to `/admin` and reconcile |
| `BILLING_DB` | `./billing.db` | SQLite ledger |
| `RECALL_CONFIG` | `./config.env` | where the wizard stores settings |

Accounts are anonymous on purpose: a random token in an `HttpOnly` cookie, no email, no
password, no identity data. That also means credit is scoped to the hostname — move the
domain and balances don't follow.

## HTTP API

```
GET  /                     the app (setup wizard until configured)
GET  /setup                first-run wizard   (localhost, or ?token=<SETUP_TOKEN>)
GET  /api/setup            field list + which secrets are set (never their values)
POST /api/setup            save settings
GET  /api/health           which providers/keys are live
GET  /api/me               balance, prices, payment state
POST /api/generate         {text, count} -> questions (answer key withheld)
POST /api/grade            {sid, answers} -> scored, per-question verdicts
POST /api/explain          why an answer was wrong (lazy)
POST /api/checkout         {units} -> hosted checkout url
POST /api/webhook/<prov>   provider callback, signature-verified, idempotent
GET  /admin?token=&cents=  owner credit page
GET  /api/admin/reconcile  settle paid-but-uncredited orders
```

## Tests

```bash
python3 tests/test_selfhost.py     # no keys needed: wizard -> working app -> paid flow
```

It boots a throwaway instance with its own config and ledger, walks the first-run setup,
checks that setup can't be claimed through a proxy, and drives a complete signed payment
through the built-in test checkout.

## Notes and limits

* Generation and grading both call out to your own providers — the app is stateless apart
  from the SQLite ledger and the in-memory quiz sessions (`SESSION_TTL`, 4h)
* Quiz sessions are held in RAM, so a restart loses in-flight quizzes (paid credit is safe —
  it's only charged when generation succeeds)
* One process, no worker pool: fine for a personal or small-classroom deployment behind a
  tunnel, not a high-traffic service

## Licence

MIT — see `LICENSE`.
