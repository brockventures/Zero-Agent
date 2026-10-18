#!/usr/bin/env python3
"""
apply_model_categorization.py - Bulk Categorize Actual Budget Transactions
Applies deterministic regex rules derived from the canonical Google Sheet
'Personal Financial Model' to categorize 2024-2026 historical transactions in Actual Budget.
"""

import re
import sys
from collections import Counter
from actual import Actual
from actual.queries import (
    get_accounts,
    get_transactions,
    get_categories,
    get_category_groups,
)

import nas_config
ACTUAL_URL = os.environ.get("ACTUAL_SERVER_URL", f"http://{nas_config.HOST_2_IP}:5006")
ACTUAL_PASSWORD = "zerobudget"
BUDGET_FILE = "My Finances"

RULES = [
    # 1. Income & Payroll
    (r"GOOGLE.*PAYROLL", "Income"),
    (r"PAYPRO.*PAYROLL", "Income"),
    (r"BAMBOOHR.*PAYROLL", "Income"),
    (r"INTEREST PAYMENT", "Income"),
    (r"DIVIDEND", "Income"),
    (r"8879 FL2023.*DEPOSIT", "Income"),
    (r"REMOTE ONLINE DEPOSIT", "Income"),
    
    # 2. Housing & Property
    (r"FLAGSTAR", "Mortgage Principal & Interest"),
    (r"BAMBOO.*INSUR", "Homeowners Insurance (Bamboo)"),
    (r"HOME DEPOT|HOMEDEPOT\.COM", "Maintenance & Landscaping Reserve"),
    (r"ACE HARDWARE", "Maintenance & Landscaping Reserve"),
    
    # 3. Family & Childcare
    (r"PENINSULA GYMNAS", "Kids Activities & Enrichment"),
    (r"BAYSHORE ELITE", "Kids Activities & Enrichment"),
    (r"REDWOOD CITY PARKS|PCC SACC|BAY AREA KIDS CAMP", "Summer Camps & After Care"),
    (r"KINGS SWIM", "Kids Activities & Enrichment"),
    (r"ROY CLOUD SCHOOL", "Kids Activities & Enrichment"),
    (r"SAGO MINI|SAGO SAGO", "Kids Activities & Enrichment"),
    (r"ACADEMY OF AMERICAN DANCE", "Kids Activities & Enrichment"),
    (r"FAMLY", "Infant Childcare / Nanny"),
    (r"CARE\.COM|CCI\*CARE", "Infant Childcare / Nanny"),
    (r"ALASKA AIR|UNITED AIRLINES|DELTA AIR|SOUTHWEST AIR|AIRBNB|VRBO|MARRIOTT|HILTON|HYATT", "Family Travel & Vacations"),
    
    # 4. Groceries & Consumables
    (r"SAFEWAY", "Groceries & Consumables"),
    (r"WHOLEFDS|WHOLE FOODS|AMAZON TIPS|PRIME NOW", "Groceries & Consumables"),
    (r"KEY MARKET", "Groceries & Consumables"),
    (r"TRADER JOE", "Groceries & Consumables"),
    (r"LUCKY\b|LUCKY #", "Groceries & Consumables"),
    
    # Medical & Healthcare
    (r"CVS/PHARMACY|WALGREENS", "Personal Care & Medical Out-of-Pocket"),
    (r"SUTTER HEALTH|STANFORD HEALTH|KAISER|PAMF|PALO ALTO MEDICAL|PROGYNY", "Personal Care & Medical Out-of-Pocket"),
    (r"SEQUOIA HOSPITAL", "Personal Care & Medical Out-of-Pocket"),
    
    # 5. Dining Out & Delivery
    (r"DOORDASH|DD \*", "Dining Out & Delivery"),
    (r"UBER.*EATS|GRUBHUB", "Dining Out & Delivery"),
    (r"STARBUCKS", "Dining Out & Delivery"),
    (r"PHILZ COFFEE", "Dining Out & Delivery"),
    (r"BOBER TEA", "Dining Out & Delivery"),
    (r"PROPER FOOD", "Dining Out & Delivery"),
    (r"EMERALD HILLS CAFE", "Dining Out & Delivery"),
    (r"LOU.?S CAFE", "Dining Out & Delivery"),
    (r"CAPELOS BARBECUE", "Dining Out & Delivery"),
    (r"ESPOSTOS DELICATEZZ", "Dining Out & Delivery"),
    (r"CHIPOTLE", "Dining Out & Delivery"),
    (r"HARRIS BBQ", "Dining Out & Delivery"),
    (r"IN N OUT|SHAKE SHACK", "Dining Out & Delivery"),
    (r"MCDONALD|BURGER KING|WENDY", "Dining Out & Delivery"),
    (r"MILAGROS", "Dining Out & Delivery"),
    (r"CAFFE STELLATO", "Dining Out & Delivery"),
    (r"STACKS.*MENLO PARK|BIG BROTHERS BURGERS|DOMINO|SANDWICH SPOT|GROOVY GOOSE", "Dining Out & Delivery"),
    (r"ORACLE PARK F&B", "Dining Out & Delivery"),
    (r"CAFE|RESTAURANT|PIZZERIA|TAQUERIA|BAKERY|BISTRO|DELI\b|GRILL\b", "Dining Out & Delivery"),
    
    # 6. Shopping, Retail & Amazon
    (r"AMAZON|AMZN", "Shopping, Retail & Amazon"),
    (r"TARGET", "Shopping, Retail & Amazon"),
    (r"COSTCO", "Shopping, Retail & Amazon"),
    (r"ZAPPOS", "Shopping, Retail & Amazon"),
    (r"EYEBUYDIRECT", "Shopping, Retail & Amazon"),
    (r"BEST BUY", "Shopping, Retail & Amazon"),
    (r"IKEA", "Shopping, Retail & Amazon"),
    (r"CLOUDFREE", "Shopping, Retail & Amazon"),
    (r"MICHAELS STORES|ALIEXPRESS", "Shopping, Retail & Amazon"),
    
    # 7. Transportation
    (r"KMF\b|KMFUSA", "Auto Financing (Kia EV9)"),
    (r"PROGRESSIVE", "Auto Insurance (Progressive)"),
    (r"TESLA SUPERCHARG", "EV Charging & Fuel"),
    (r"ELECTRIFY AMERICA|EVGO|CHARGEPOINT", "EV Charging & Fuel"),
    (r"SAV ON GASOLINE|JEFFERSON GAS|SHELL OIL|CHEVRON|ARCO\b|76 -|VALERO", "EV Charging & Fuel"),
    (r"REDWOOD CITY PARKING|PARKMOBILE|ACE PARKING", "Vehicle Registration & Maintenance"),
    
    # 8. Utilities & Subscriptions
    (r"PGANDE|PG&E|PACIFIC GAS", "PG&E (Electric & Gas)"),
    (r"REDWOOD CITY WATER|CITY OF REDWOOD CITY", "Water & Municipal Utilities (Redwood City)"),
    (r"ATT\b|AT&T", "Home Fiber Internet (AT&T)"),
    (r"GOOGLE \*FI\b|GOOGLE FI", "Mobile Wireless (Google Fi)"),
    (r"YOUTUBEPREMIUM|YOUTUBE PREMIUM", "Digital Subscriptions & Cloud"),
    (r"DISNEY\b|DISNEYPLUS", "Digital Subscriptions & Cloud"),
    (r"DROPOUT", "Digital Subscriptions & Cloud"),
    (r"PATREON", "Digital Subscriptions & Cloud"),
    (r"HOME ASSISTANT CLOUD|NABU CASA", "Digital Subscriptions & Cloud"),
    (r"NETFLIX|SPOTIFY|APPLE\.COM/BILL|CLAUDE\.AI|OPENAI", "Digital Subscriptions & Cloud"),
    (r"GITHUB|AWS|CLOUDFLARE|HEROKU", "Digital Subscriptions & Cloud"),
    (r"NYTIMES|NEW YORK TIMES|WASHINGTON POST", "Digital Subscriptions & Cloud"),
    (r"GOOGLE \*GOOGLE NEST|GOOGLE \*ONE|GOOGLE ONE", "Digital Subscriptions & Cloud"),
    (r"NINTENDO|STEAM PURCHASE|VALVE", "Digital Subscriptions & Cloud"),
]

def run_categorization():
    print(f"Connecting to Actual Budget at {ACTUAL_URL} ('{BUDGET_FILE}')...")
    with Actual(base_url=ACTUAL_URL, password=ACTUAL_PASSWORD, file=BUDGET_FILE) as actual:
        session = actual.session
        accounts = {a.id: a for a in get_accounts(session)}
        categories = {c.name: c for c in get_categories(session)}
        
        # Verify all target categories exist
        missing_cats = set()
        for _, cat_name in RULES:
            if cat_name not in categories:
                missing_cats.add(cat_name)
        if missing_cats:
            print(f"Warning: Categories missing in Actual: {missing_cats}", file=sys.stderr)
            sys.exit(1)
            
        a6860 = next(a for a in accounts.values() if "6860" in a.name)
        a5646 = next(a for a in accounts.values() if "5646" in a.name)
        
        txs = get_transactions(session)
        
        # 1. Link Transfers between Checking (6860) and Card (5646)
        autopay_6860 = [t for t in txs if t.acct == a6860.id and ("CHASE CREDIT CRD AUTOPAY" in (t.notes or "").upper() or "CHASE CREDIT CRD" in (t.payee.name if t.payee else "").upper())]
        payments_5646 = [t for t in txs if t.acct == a5646.id and ("AUTOMATIC PAYMENT" in (t.payee.name if t.payee else "").upper() or "AUTOMATIC PAYMENT" in (t.notes or "").upper())]
        
        linked_transfers = 0
        used_5646 = set()
        for t1 in autopay_6860:
            target_amt = -t1.amount
            candidates = [t2 for t2 in payments_5646 if t2.id not in used_5646 and t2.amount == target_amt and abs(t2.date - t1.date) <= 3]
            if candidates:
                c = candidates[0]
                used_5646.add(c.id)
                t1.transferred_id = c.id
                c.transferred_id = t1.id
                t1.category_id = None
                c.category_id = None
                linked_transfers += 1
        print(f"Linked {linked_transfers} credit card autopay transfer pairs.")
        
        # 2. Apply Rule-Based Categorization
        categorized_counts = Counter()
        total_modified = 0
        
        for t in txs:
            if t.starting_balance_flag:
                continue
            if t.transferred_id:
                continue
            # Only categorize on-budget accounts
            acct = accounts.get(t.acct)
            if not acct or acct.offbudget:
                continue
                
            pname = (t.payee.name if t.payee else (t.imported_description or "")).upper()
            notes = (t.notes or "").upper()
            search_text = f"{pname} {notes}"
            
            # Check rules
            for pat, cat_name in RULES:
                if re.search(pat, search_text):
                    target_cat = categories[cat_name]
                    if t.category_id != target_cat.id:
                        t.category_id = target_cat.id
                        categorized_counts[cat_name] += 1
                        total_modified += 1
                    break
                    
        # 3. Correlate Tip-Paired Amazon Whole Foods Delivery Orders
        cat_groceries = categories.get("Groceries & Consumables")
        cat_shopping = categories.get("Shopping, Retail & Amazon")
        if cat_groceries and cat_shopping:
            tip_txs = [t for t in txs if "AMAZON TIPS" in ((t.payee.name if t.payee else "") + " " + (t.notes or "")).upper()]
            matched_deliveries = 0
            for tip in tip_txs:
                try:
                    t_date = int(str(tip.date).replace("-", ""))
                except Exception:
                    continue
                candidates = [
                    t for t in txs 
                    if t.id != tip.id 
                    and t.category_id == cat_shopping.id
                    and any(k in ((t.payee.name if t.payee else "") + " " + (t.notes or "")).upper() for k in ["AMAZON", "AMZN"])
                ]
                for cand in candidates:
                    try:
                        c_date = int(str(cand.date).replace("-", ""))
                        if abs(c_date - t_date) <= 1:
                            cand.category_id = cat_groceries.id
                            matched_deliveries += 1
                            total_modified += 1
                    except Exception:
                        pass
            if matched_deliveries:
                print(f"Auto-paired {matched_deliveries} Amazon Whole Foods delivery charge(s) to Groceries & Consumables.")

        print(f"Categorized {total_modified} transactions across {len(categorized_counts)} categories.")
        for cat_name, cnt in categorized_counts.most_common():
            print(f"  • {cat_name:42}: {cnt:4} txs")
            
        print("Committing changes to Actual Budget server...")
        actual.commit()
        print("Commit and sync completed successfully!")

if __name__ == "__main__":
    run_categorization()
