#!/usr/bin/env python3
"""Settle paid-but-uncredited purchases.

Webhook backstop: runs from a timer (and by hand) so a customer who paid is credited even
if the provider's callback never reached us — e.g. it was blocked at Cloudflare's edge or
the app was down. Uses the same idempotent settle path as the webhook, so running it twice
(or alongside a late webhook) cannot double-credit.

    python3 reconcile.py            # settle anything unpaid-but-paid
    python3 reconcile.py --status   # just report, credit nothing
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server  # noqa: E402  (importing it loads the .env files, same as the service)
import billing  # noqa: E402


def main() -> int:
    dry = "--status" in sys.argv
    if not billing.PAYMENTS_ENABLED:
        print("payments are turned off — nothing to reconcile")
        return 0
    prov = billing.provider()
    if not hasattr(prov, "check_invoice"):
        print("provider %s cannot be reconciled" % prov.name)
        return 0
    rows = billing.pending_purchases(prov.name)
    checked = credited = failed = 0
    for row in rows:
        ref = row.get("provider_ref")
        if not ref:
            continue
        checked += 1
        try:
            status, paid = prov.check_invoice(ref)
        except Exception as exc:
            failed += 1
            print("  ! %s: %s" % (row["order_id"], str(exc)[:140]))
            continue
        if not paid:
            print("  . %s status=%s (unpaid)" % (row["order_id"], status or "?"))
            continue
        if dry:
            print("  + %s PAID (%s) — would credit %d cents"
                  % (row["order_id"], status, row["amount"]))
            continue
        done = billing.settle_purchase(row["order_id"], provider_ref=ref, status="paid")
        if done:
            credited += 1
            print("  + %s credited %d cents" % (row["order_id"], done[1]))
    print("reconcile: %d checked, %d credited, %d lookup errors" % (checked, credited, failed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
