#!/usr/bin/env python3
"""
import_chase_history.py - Batch Import Chase Historical Activity into Actual Budget
Imports verified historical exports for Chase Checking (6860) and Freedom Unlimited (5646),
anchors starting balances to the earliest statements, and cleanly deduplicates against SimpleFin.
"""

import csv
import sys
import uuid
import datetime
from datetime import date
from decimal import Decimal
from actual import Actual
from actual.queries import (
    get_accounts,
    get_transactions,
    get_payees,
    create_payee,
    create_transaction_from_ids,
)
from actual.database import Transactions

import os
for _p in ("/workspace", "/workspace/tools"):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import nas_config
ACTUAL_URL = os.environ.get("ACTUAL_SERVER_URL", f"http://{nas_config.HOST_2_IP}:5006")
ACTUAL_PASSWORD = "zerobudget"
BUDGET_FILE = "My Finances"

FILE_6860 = "/workspace/data/chase_exports/Chase6860_Activity_20260822.CSV"
FILE_5646_2025 = "/workspace/data/chase_exports/Chase5646_Activity20250101_20251231_20260822.CSV"
FILE_5646_2026 = "/workspace/data/chase_exports/Chase5646_Activity_20261002_YTD.csv"

def run_import():
    print(f"Connecting to Actual Budget at {ACTUAL_URL} ('{BUDGET_FILE}')...")
    with Actual(base_url=ACTUAL_URL, password=ACTUAL_PASSWORD, file=BUDGET_FILE) as actual:
        session = actual.session
        accounts = {a.name: a for a in get_accounts(session)}
        
        a6860 = accounts.get("Chase Premier Checking (6860)")
        a5646 = accounts.get("Chase Freedom Unlimited (5646)")
        
        if not a6860 or not a5646:
            print("Error: Target Chase accounts not found in Actual Budget.", file=sys.stderr)
            sys.exit(1)
            
        print(f"Located Accounts: {a6860.name} ({a6860.id}), {a5646.name} ({a5646.id})")
        
        # Build payee cache
        print("Caching existing payees...")
        payees = get_payees(session)
        payee_cache = {p.name: p for p in payees if p.name}
        
        def resolve_payee(name: str):
            clean = name.strip() if name else "Unknown Payee"
            if clean in payee_cache:
                return payee_cache[clean]
            new_p = create_payee(session, clean)
            payee_cache[clean] = new_p
            return new_p

        # 1. Clean existing starting balances on both accounts
        print("Clearing prior starting balances...")
        existing_txs = get_transactions(session)
        deleted_count = 0
        for t in existing_txs:
            if t.acct in (a6860.id, a5646.id) and (t.starting_balance_flag or "starting" in (t.notes or "").lower()):
                t.delete()
                deleted_count += 1
        print(f"Removed {deleted_count} legacy starting balance entries.")

        # 2. Add New Starting Balance for Checking (6860) on 2024-08-22 ($56,829.45)
        payee_start = resolve_payee("Starting Balance")
        t_start_6860 = create_transaction_from_ids(
            session,
            date=date(2024, 8, 22),
            account_id=a6860.id,
            payee_id=payee_start.id,
            notes="Starting Balance (Chase Checking 6860 Statement Archive)",
            amount=Decimal("56829.45"),
            cleared=True
        )
        t_start_6860.starting_balance_flag = 1
        print("Created 6860 Starting Balance on 2024-08-22: $56,829.45")

        # 3. Add New Starting Balance for Freedom Unlimited (5646) on 2025-01-01 (-$13,778.48)
        t_start_5646 = create_transaction_from_ids(
            session,
            date=date(2025, 1, 1),
            account_id=a5646.id,
            payee_id=payee_start.id,
            notes="Starting Balance (Reconciled from 2025 Statement Archive)",
            amount=Decimal("-13778.48"),
            cleared=True
        )
        t_start_5646.starting_balance_flag = 1
        print("Created 5646 Starting Balance on 2025-01-01: -$13,778.48")

        # 4. Import Checking (6860) transactions (pre-2026-08-03)
        print("Importing Checking (6860) transactions...")
        imported_6860 = 0
        with open(FILE_6860, "r", errors="ignore") as f:
            for r in csv.DictReader(f):
                pdate_str = r.get("Posting Date", "").strip()
                if not pdate_str:
                    continue
                pdate = datetime.datetime.strptime(pdate_str, "%m/%d/%Y").date()
                if pdate >= date(2026, 8, 3):
                    continue  # Handled by SimpleFin live sync
                
                amt = Decimal(r["Amount"])
                payee_obj = resolve_payee(r.get("Description", ""))
                slip = r.get("Check or Slip #", "").strip()
                ttype = r.get("Type", "").strip()
                notes = f"{ttype} | Slip: {slip}".strip(" |") if (slip or ttype) else ""
                
                create_transaction_from_ids(
                    session,
                    date=pdate,
                    account_id=a6860.id,
                    payee_id=payee_obj.id,
                    notes=notes,
                    amount=amt,
                    cleared=True
                )
                imported_6860 += 1
        print(f"Staged {imported_6860} transactions for Checking (6860).")

        # 5. Import Freedom Unlimited (5646) 2025 Transactions
        print("Importing Freedom Unlimited (5646) 2025 transactions...")
        imported_5646_2025 = 0
        with open(FILE_5646_2025, "r", errors="ignore") as f:
            for r in csv.DictReader(f):
                pdate_str = r.get("Post Date", "").strip()
                if not pdate_str:
                    continue
                pdate = datetime.datetime.strptime(pdate_str, "%m/%d/%Y").date()
                amt = Decimal(r["Amount"])
                payee_obj = resolve_payee(r.get("Description", ""))
                cat = r.get("Category", "").strip()
                ttype = r.get("Type", "").strip()
                notes = f"Category: {cat} | Type: {ttype}".strip(" |") if (cat or ttype) else ""
                
                create_transaction_from_ids(
                    session,
                    date=pdate,
                    account_id=a5646.id,
                    payee_id=payee_obj.id,
                    notes=notes,
                    amount=amt,
                    cleared=True
                )
                imported_5646_2025 += 1
        print(f"Staged {imported_5646_2025} transactions for 5646 (2025).")

        # 6. Import Freedom Unlimited (5646) 2026 Transactions (pre-2026-08-03)
        print("Importing Freedom Unlimited (5646) 2026 YTD transactions (pre-2026-08-03)...")
        imported_5646_2026 = 0
        with open(FILE_5646_2026, "r", errors="ignore") as f:
            for r in csv.DictReader(f):
                pdate_str = r.get("Post Date", "").strip()
                if not pdate_str:
                    continue
                pdate = datetime.datetime.strptime(pdate_str, "%m/%d/%Y").date()
                if pdate >= date(2026, 8, 3):
                    continue  # Handled by SimpleFin live sync
                
                amt = Decimal(r["Amount"])
                payee_obj = resolve_payee(r.get("Description", ""))
                cat = r.get("Category", "").strip()
                ttype = r.get("Type", "").strip()
                notes = f"Category: {cat} | Type: {ttype}".strip(" |") if (cat or ttype) else ""
                
                create_transaction_from_ids(
                    session,
                    date=pdate,
                    account_id=a5646.id,
                    payee_id=payee_obj.id,
                    notes=notes,
                    amount=amt,
                    cleared=True
                )
                imported_5646_2026 += 1
        print(f"Staged {imported_5646_2026} transactions for 5646 (2026 YTD).")

        total_new_txs = 2 + imported_6860 + imported_5646_2025 + imported_5646_2026
        print(f"Total new entries staged: {total_new_txs}. Committing to Actual Budget server...")
        actual.commit()
        print("Commit and sync completed successfully!")

if __name__ == "__main__":
    run_import()
