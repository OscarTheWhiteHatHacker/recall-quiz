"""Runtime configuration for Recall — the app asks for keys, it never ships them.

Everything the app needs is a setting: an AI key for grading, one for generation, and
optionally a payment provider. This module is the single source of truth for those
settings, and it can be filled in two ways:

* the environment (systemd `EnvironmentFile`, Docker `-e`, shell exports) — wins, so
  deployments can stay 12-factor, or
* the built-in first-run wizard, which writes `config.env` next to the app.

`load_into_env()` must run before the app's other modules are imported: they read
settings once at import time. Runtime edits go through `save()`, which also refreshes
`os.environ` so newly entered keys take effect without a restart.
"""
from __future__ import annotations

import os
import secrets
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("RECALL_CONFIG") or (ROOT / "config.env"))
LEGACY_PATH = ROOT / ".env"          # original single-file layout, still honoured

_LOCK = threading.RLock()

# ---------------------------------------------------------------------------
# Every setting the wizard knows about. `secret` fields are write-only over the
# API (the UI learns *whether* they are set, never what they are).
# ---------------------------------------------------------------------------
FIELDS = [
    # ---- AI -----------------------------------------------------------------
    dict(group="Grading", key="TYPESAFE_API_KEY", label="Jev / TypeSafe API key (optional)",
         hint="Marks the answers. Get one at api.typesafe.ai. Leave blank and the generation "
              "model marks them instead — no key, no cost, slightly less precise.", secret=True),
    dict(group="Grading", key="JEV_URL", label="Jev endpoint",
         default="https://api.typesafe.ai/v1/systemone"),
    dict(group="Generation", key="OLLAMA_API_KEY", label="Ollama Cloud API key",
         hint="Writes the questions. Free tier, fastest option — ollama.com → Settings → Keys.",
         secret=True),
    dict(group="Generation", key="OLLAMA_MODEL", label="Ollama model", default="gpt-oss:120b"),
    dict(group="Generation", key="OLLAMA_URL", label="Ollama endpoint",
         default="https://ollama.com/v1/chat/completions"),
    dict(group="Generation", key="NVIDIA_API_KEY", label="NVIDIA API key (fallback)",
         hint="Used automatically when Ollama is missing or fails. Free tier at build.nvidia.com.",
         secret=True),
    dict(group="Generation", key="GEN_MODEL", label="NVIDIA model", default="z-ai/glm-5.3-flash"),
    dict(group="Generation", key="NVIDIA_URL", label="NVIDIA endpoint",
         default="https://integrate.api.nvidia.com/v1/chat/completions"),
    # ---- payments -----------------------------------------------------------
    dict(group="Payments", key="PAYMENTS_ENABLED", label="Take payments", default="no",
         choices=["no", "yes"],
         hint="no = completely free: no balance, no checkout, nothing to pay. "
              "yes = charge per quiz and show a top-up button."),
    dict(group="Payments", key="PAYMENT_PROVIDER", label="Payment provider", default="mock",
         choices=["mock", "plisio", "cryptomus"],
         hint="mock = free/test checkout (no keys, no money). plisio = crypto checkout, "
              "email-only onboarding. cryptomus = cards, needs merchant KYC."),
    dict(group="Payments", key="PLISIO_SECRET_KEY", label="Plisio secret key",
         hint="Site settings → API. Also save your site URL there.", secret=True),
    dict(group="Payments", key="PLISIO_PSYS", label="Plisio coins offered",
         hint="Comma-separated; blank = let the payer choose.",
         default=""),
    dict(group="Payments", key="CRYPTOMUS_MERCHANT_ID", label="Cryptomus merchant id", secret=True),
    dict(group="Payments", key="CRYPTOMUS_API_KEY", label="Cryptomus API key", secret=True),
    dict(group="Payments", key="PUBLIC_BASE_URL", label="Public base URL",
         hint="https://your-domain — invoices come back to <this>/api/webhook/<provider>."),
    dict(group="Payments", key="MOCK_WEBHOOK_SECRET", label="Test-checkout secret", secret=True, autogen=True),
    # ---- pricing ------------------------------------------------------------
    dict(group="Pricing", key="CURRENCY", label="Currency", default="USD"),
    dict(group="Pricing", key="CURRENCY_SYMBOL", label="Currency symbol", default="$"),
    dict(group="Pricing", key="PRICE_PER_5_CENTS", label="Price per 5 questions (minor units)",
         default="1", hint="1 = one cent per 5 questions."),
    dict(group="Pricing", key="TOPUP_CENTS", label="Top-up step (minor units)", default="100",
         hint="100 = $1 per top-up."),
    dict(group="Pricing", key="FREE_CREDIT_CENTS", label="Free signup credit", default="0",
         hint="0 = paywall immediately; e.g. 5 = first five questions free."),
    dict(group="Pricing", key="MAX_BALANCE_CENTS", label="Maximum balance", default="10000"),
    # ---- server -------------------------------------------------------------
    dict(group="Server", key="ADMIN_TOKEN", label="Owner/admin token", secret=True, autogen=True,
         hint="Grants credit at /admin and reconciles payments."),
    dict(group="Server", key="HOST", label="Bind address", default="0.0.0.0"),
    dict(group="Server", key="PORT", label="Port", default="3460"),
    dict(group="Server", key="BILLING_DB", label="Ledger database path",
         default=str(ROOT / "billing.db")),
    dict(group="Server", key="SETUP_TOKEN", label="Setup link token", secret=True, autogen=True),
]
BY_KEY = {f["key"]: f for f in FIELDS}
SECRETS = {f["key"] for f in FIELDS if f.get("secret")}
GROUPS = []
for _f in FIELDS:
    if _f["group"] not in GROUPS:
        GROUPS.append(_f["group"])


def _parse(path: Path) -> dict:
    out = {}
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").replace("\\n", "\n")
    except OSError:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_into_env() -> None:
    """Merge config.env (and the legacy .env) into the environment.

    Real environment variables win, so systemd/Docker/CI can override the file
    without editing it.
    """
    for path in (LEGACY_PATH, CONFIG_PATH):
        for k, v in _parse(path).items():
            if k and v != "":
                os.environ.setdefault(k, v)
    _autogen(setdefault_only=True)


def get(key: str, default: str = "") -> str:
    f = BY_KEY.get(key) or {}
    return os.environ.get(key, f.get("default", default))


def _autogen(setdefault_only: bool = False) -> dict:
    """Create the secrets nobody should have to invent (admin token, setup token)."""
    made = {}
    for f in FIELDS:
        if not f.get("autogen"):
            continue
        if not os.environ.get(f["key"]):
            val = secrets.token_hex(16)
            os.environ[f["key"]] = val
            made[f["key"]] = val
    if made and not setdefault_only:
        save({}, touch_only=False)
    return made


def save(values: dict, touch_only: bool = True) -> None:
    """Persist settings to config.env (0600) and refresh the live environment."""
    with _LOCK:
        keep = _parse(CONFIG_PATH) if CONFIG_PATH.exists() else {}
        for k, v in (values or {}).items():
            if k not in BY_KEY:
                continue
            v = str(v).strip()
            if v == "" and k in SECRETS:
                continue                      # blank secret = "leave it alone"
            os.environ[k] = v                 # live, no restart needed
            keep[k] = v
        for f in FIELDS:
            if f.get("autogen") and not os.environ.get(f["key"]):
                os.environ[f["key"]] = secrets.token_hex(16)
            if os.environ.get(f["key"]):
                keep[f["key"]] = os.environ[f["key"]]
        lines = ["# Recall settings — written by the setup wizard. Keep this file private.",
                 "# %s" % CONFIG_PATH, ""]
        for group in GROUPS:
            lines.append("## %s" % group)
            for f in FIELDS:
                if f["group"] == group and f["key"] in keep:
                    lines.append("%s=%s" % (f["key"], keep[f["key"]]))
            lines.append("")
        CONFIG_PATH.write_text("\n".join(lines), encoding="utf-8")
        try:
            os.chmod(CONFIG_PATH, 0o600)
        except OSError:
            pass


def missing() -> list:
    """Settings without which the app cannot work at all.

    Grading has two routes: Jev when TYPESAFE_API_KEY is set, otherwise the generation
    model marks the answers as a fallback — so a generator key is the only hard requirement.
    """
    out = []
    if not (get("OLLAMA_API_KEY") or get("NVIDIA_API_KEY")):
        out.append("OLLAMA_API_KEY or NVIDIA_API_KEY")
    return out


def is_ready() -> bool:
    return not missing()


def status() -> dict:
    """Everything the wizard may see: values for non-secrets, set/not-set for secrets."""
    out = {"ready": is_ready(), "missing": missing(), "config_path": str(CONFIG_PATH),
           "groups": GROUPS, "fields": []}
    for f in FIELDS:
        v = get(f["key"], "")
        entry = {k: f.get(k) for k in ("group", "key", "label", "hint", "choices") if f.get(k) is not None}
        entry["secret"] = bool(f.get("secret"))
        entry["required"] = bool(f.get("required"))
        if f.get("secret"):
            entry["is_set"] = bool(v)
            entry["value"] = ""
        else:
            entry["is_set"] = bool(v)
            entry["value"] = v
        out["fields"].append(entry)
    return out
