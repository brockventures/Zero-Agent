import sys
#!/usr/bin/env python3
"""
apply_decisions.py - Apply User Categorization Decisions to Actual Budget
1. Maps Schwab Moneylink in Checking to Income (GSU Vesting Monetization).
2. Moves Costco to Groceries & Consumables.
3. Categorizes biweekly $100 Venmo to Maintenance & Landscaping Reserve (Alejandro).
4. Categorizes weekly ATM withdrawals and Nanny Zelle to Infant Childcare / Nanny.
5. Creates 'Gifts & Donations' category and maps ACLU, Roy Cloud PTO, and museums.
6. Keeps Flagstar in Mortgage Principal & Interest as recommended.
"""

from actual import Actual
from actual.queries import (
    get_accounts,
    get_transactions,
    get_categories,
    get_category_groups,
    create_category,
)

import os
for _p in ("/workspace", "/workspace/tools"):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import nas_config
ACTUAL_URL = os.environ.get("ACTUAL_SERVER_URL", f"http://{nas_config.HOST_2_IP}:5006")
ACTUAL_PASSWORD = "zerobudget"
BUDGET_FILE = "My Finances"

def main():
    print(f"Connecting to Actual Budget at {ACTUAL_URL} ('{BUDGET_FILE}')...")
    with Actual(base_url=ACTUAL_URL, password=ACTUAL_PASSWORD, file=BUDGET_FILE) as actual:
        session = actual.session
        categories = {c.name: c for c in get_categories(session)}
        
        # 1. Ensure 'Gifts & Donations' category exists under 'Living & Lifestyle'
        if "Gifts & Donations" not in categories:
            cat_donations = create_category(session, name="Gifts & Donations", group_name="Living & Lifestyle")
            categories["Gifts & Donations"] = cat_donations
            print("Created category 'Gifts & Donations' under Living & Lifestyle.")
        else:
            cat_donations = categories["Gifts & Donations"]

        # Ensure 'GSU Vesting Monetization' exists under 'Income'
        if "GSU Vesting Monetization" not in categories:
            cat_gsu = create_category(session, name="GSU Vesting Monetization", group_name="Income")
            categories["GSU Vesting Monetization"] = cat_gsu
            print("Created category 'GSU Vesting Monetization' under Income.")
        else:
            cat_gsu = categories["GSU Vesting Monetization"]
            
        cat_groceries = categories["Groceries & Consumables"]
        cat_landscaping = categories["Maintenance & Landscaping Reserve"]
        cat_nanny = categories["Infant Childcare / Nanny"]
        
        txs = get_transactions(session)
        
        counts = {
            "gsu": 0,
            "costco": 0,
            "landscaping": 0,
            "nanny": 0,
            "donations": 0,
        }
        
        for t in txs:
            if t.starting_balance_flag or t.transferred_id:
                continue
                
            pname = (t.payee.name if t.payee else (t.imported_description or "")).upper()
            notes = (t.notes or "").upper()
            search = f"{pname} {notes}"
            
            # 1. Schwab Moneylink -> GSU Vesting Monetization
            if "SCHWAB BROKERAGE MONEYLINK" in search:
                t.category_id = cat_gsu.id
                counts["gsu"] += 1
                continue
                
            # 2. Costco -> Groceries & Consumables
            if "COSTCO" in search:
                t.category_id = cat_groceries.id
                counts["costco"] += 1
                continue
                
            # 3. Biweekly $100 Venmo -> Alejandro Landscaping
            if "VENMO" in search and t.amount == -10000:
                t.category_id = cat_landscaping.id
                counts["landscaping"] += 1
                continue
                
            # 4. Weekly ATM withdrawals ($1,000 or $2,000) or Nanny Zelle -> Infant Childcare / Nanny
            if "ATM WITHDRAWAL" in search and t.amount in (-100000, -200000):
                t.category_id = cat_nanny.id
                counts["nanny"] += 1
                continue
            if "ZELLE" in search and ("RAFAELA" in search or "4159666297" in search):
                t.category_id = cat_nanny.id
                counts["nanny"] += 1
                continue
                
            # 5. Donations -> Gifts & Donations
            if any(k in search for k in ["ACLU", "ROY CLOUD PTO", "ZEFFY", "CUSTOMSPOR* ROY CLOUD", "CHILDDISCOVERYMUS", "BADISCMUSEUM"]):
                t.category_id = cat_donations.id
                counts["donations"] += 1
                continue

        print(f"Applied Decision Updates:")
        print(f"  • GSU Vesting Monetization: {counts['gsu']} txs")
        print(f"  • Costco to Groceries: {counts['costco']} txs")
        print(f"  • Alejandro Landscaping ($100 Venmo): {counts['landscaping']} txs")
        print(f"  • Nanny Cash/ATM & Zelle: {counts['nanny']} txs")
        print(f"  • Gifts & Donations (ACLU, PTO, Museums): {counts['donations']} txs")
        
        print("Committing to Actual Budget server...")
        actual.commit()
        print("Commit and sync completed successfully!")

if __name__ == "__main__":
    main()
