#!/usr/bin/env python3
"""
actual_sync.py - Automated Bank Synchronization & Category Rule Dispatcher for Actual Budget
Connects to Actual Budget via API, queries SimpleFin across all active bank feeds,
auto-categorizes new transactions using the model ruleset, and commits the ledger.
Dispatches Discord alerts to #finances only if MFA re-auth is needed or new transactions are imported.
"""

import sys
import argparse
import datetime
from pathlib import Path

# Add tools dir to path
TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from actual import Actual
from actual.exceptions import ActualError
try:
    from tools.apply_model_categorization import run_categorization
except ImportError:
    from apply_model_categorization import run_categorization

try:
    from tools.outbox import queue_outbox_message
except ImportError:
    from outbox import queue_outbox_message

import os
import nas_config
ACTUAL_URL = os.environ.get("ACTUAL_SERVER_URL", f"http://{nas_config.HOST_2_IP}:5006")
ACTUAL_PASSWORD = "zerobudget"
BUDGET_FILE = "My Finances"
FINANCES_CHANNEL = "finances"

def sync_actual_budget(test: bool = False, quiet: bool = False, notify: bool = False):
    """
    Executes automated bank sync.
    Returns: (success: bool, report: str, metadata: dict)
    """
    try:
        from actual.queries import get_accounts, get_transactions
        
        with Actual(base_url=ACTUAL_URL, password=ACTUAL_PASSWORD, file=BUDGET_FILE, timeout=120.0) as actual:
            accounts = get_accounts(actual.session)
            imported_txs = []
            mfa_accounts = []
            sync_errors = []

            for acct in accounts:
                sync_method = acct.account_sync_source
                account_id = acct.account_id
                if not (account_id and sync_method):
                    continue

                try:
                    status = actual.bank_sync_status(sync_method.lower())
                    if not status.data.configured:
                        continue

                    all_transactions = get_transactions(actual.session, account=acct)
                    if all_transactions:
                        default_start_date = all_transactions[0].get_date()
                    else:
                        default_start_date = datetime.date.today() - datetime.timedelta(days=90)

                    txs = actual._run_bank_sync_account(acct, default_start_date, is_first_sync=False)
                    imported_txs.extend(txs)
                except Exception as acct_err:
                    err_str = str(acct_err).lower()
                    if any(term in err_str for term in ("mfa", "reauth", "re-auth", "challenge", "login", "otp", "2fa", "credential")):
                        mfa_accounts.append((acct.name, str(acct_err)))
                    else:
                        sync_errors.append((acct.name, str(acct_err)))

            # If accounts need MFA re-auth, dispatch notice to #finances
            if mfa_accounts:
                mfa_list = "\n".join([f"- **{name}**: `{err}`" for name, err in mfa_accounts])
                mfa_msg = (
                    f"⚠️ **Actual Budget MFA Re-auth Required**\n"
                    f"The following bank connection(s) require re-authentication:\n{mfa_list}\n\n"
                    f"Update credentials in SimpleFin or via [`https://actual.brock.ventures`](<https://actual.brock.ventures>)."
                )
                try:
                    queue_outbox_message(channel=FINANCES_CHANNEL, content=mfa_msg)
                except Exception as e:
                    print(f"Failed to dispatch MFA outbox alert: {e}", file=sys.stderr)

            new_count = len(imported_txs)
            if new_count > 0:
                actual.run_rules(imported_txs)
                if not test:
                    actual.commit()
                    # Run model-based categorization on committed transactions
                    try:
                        run_categorization()
                    except Exception as cat_err:
                        print(f"Warning during model categorization: {cat_err}", file=sys.stderr)

                # Format detailed transaction summary for #finances
                tx_lines = []
                for t in imported_txs[:12]:
                    acct_name = t.account.name if t.account else "Account"
                    payee_name = (t.payee.name if t.payee else (t.imported_description or "Unknown Payee")).strip()
                    amt_val = (t.amount or 0) / 100.0
                    amt_str = f"${abs(amt_val):,.2f}"
                    sign = "+" if amt_val > 0 else "-"
                    tx_lines.append(f"- **{acct_name}**: {payee_name} (`{sign}{amt_str}`)")

                if len(imported_txs) > 12:
                    tx_lines.append(f"- *...and {len(imported_txs) - 12} more.*")

                tx_detail = "\n".join(tx_lines)
                report = f"✅ **Actual Budget Bank Sync**: Imported & categorized {new_count} new transaction(s)."
                if not quiet:
                    print(report)

                # Dispatch notification to #finances only if explicitly requested (silent sidecar by default)
                if notify:
                    finances_content = (
                        f"💳 **Actual Budget Bank Sync**\n"
                        f"Imported and categorized **{new_count}** new transaction(s):\n"
                        f"{tx_detail}"
                    )
                    try:
                        queue_outbox_message(channel=FINANCES_CHANNEL, content=finances_content)
                    except Exception as outbox_err:
                        print(f"Failed to dispatch outbox notification: {outbox_err}", file=sys.stderr)

                return True, report, {
                    "new_transactions": new_count,
                    "mfa_accounts": len(mfa_accounts),
                    "errors": len(sync_errors)
                }
            else:
                report = "Bank sync nominal (0 new transactions)."
                if not quiet:
                    print("(nominal - 0 new transactions)")
                return True, report, {
                    "new_transactions": 0,
                    "mfa_accounts": len(mfa_accounts),
                    "errors": len(sync_errors)
                }

    except Exception as e:
        error_msg = f"⚠️ **Actual Budget Sync Error**: {e}"
        print(error_msg, file=sys.stderr)
        
        try:
            queue_outbox_message(
                channel=FINANCES_CHANNEL,
                content=f"🚨 **Actual Budget Bank Sync Alert**\nFailed to sync financial feeds: `{e}`\nCheck SimpleFin MFA status or token credentials at [`https://actual.brock.ventures`](<https://actual.brock.ventures>)."
            )
        except Exception as alert_err:
            print(f"Failed to dispatch outbox alert: {alert_err}", file=sys.stderr)
            
        return False, error_msg, {"error": str(e)}

def main():
    parser = argparse.ArgumentParser(description="Actual Budget automated bank sync CLI")
    parser.add_argument("--test", "-t", action="store_true", help="Run in test mode")
    parser.add_argument("--quiet", "-q", action="store_true", help="Silent when nominal")
    parser.add_argument("--notify", "-n", action="store_true", help="Dispatch transaction summary to #finances")
    args = parser.parse_args()
    
    ok, rep, _ = sync_actual_budget(test=args.test, quiet=args.quiet, notify=args.notify)
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
