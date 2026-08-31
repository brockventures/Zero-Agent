#!/usr/bin/env python3
"""Format and style Google Spreadsheet with multi-tab layout, banding, validation, and sizing."""

import csv
import json
import os
import urllib.request
import urllib.parse

SECRETS_PATH = os.environ.get("GOOGLE_OAUTH_PATH", "/secrets/google_oauth.json")
if not os.path.exists(SECRETS_PATH) and os.path.exists("/workspace/config/google_oauth.json"):
    SECRETS_PATH = "/workspace/config/google_oauth.json"

CSV_PATH = os.environ.get("FRIENDS_CSV_PATH", "/workspace/data/friends_and_family_master.csv")
DEFAULT_SHEET_ID = os.environ.get("FRIENDS_SHEET_ID", "1ZAhET8stLzHTR3tsRWZVB9UAgIIEWH1Gs_9Nw3KQBv8")

def load_credentials(path):
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        creds = {}
        for line in content.splitlines():
            line = line.strip().rstrip(",")
            if ":" in line:
                k, v = line.split(":", 1)
                creds[k.strip().strip("\"").strip("'")] = v.strip().strip("\"").strip("'")
        return creds

def get_access_token():
    creds = load_credentials(SECRETS_PATH)
    data = urllib.parse.urlencode({
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": creds["refresh_token"],
        "grant_type": "refresh_token"
    }).encode("utf-8")

    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())["access_token"]

def format_spreadsheet(sheet_id=DEFAULT_SHEET_ID):
    tok = get_access_token()
    headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

    # 1. Fetch current spreadsheet metadata
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as resp:
        meta = json.loads(resp.read().decode())

    existing_sheets = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta.get("sheets", [])}
    print("Existing sheet tabs:", existing_sheets)

    # 2. Prepare datasets
    with open(CSV_PATH, "r", encoding="utf-8") as f:
        master_rows = list(csv.reader(f))

    MONTH_NAMES = {
        1: "01 - Jan ❄️", 2: "02 - Feb ❄️", 3: "03 - Mar 🌱", 4: "04 - Apr 🌱",
        5: "05 - May 🌱", 6: "06 - Jun ☀️", 7: "07 - Jul ☀️", 8: "08 - Aug ☀️",
        9: "09 - Sep 🍂", 10: "10 - Oct 🍂", 11: "11 - Nov 🍂", 12: "12 - Dec ❄️"
    }

    bday_entries = []
    for r in master_rows[1:]:
        name, circle, rel, bday, phone, email, addr, handles, notes = r
        if bday:
            parts = bday.strip().split("-")
            try:
                if len(parts) == 3:
                    m, d = int(parts[1]), int(parts[2])
                elif len(parts) == 2:
                    m, d = int(parts[0]), int(parts[1])
                else:
                    m, d = 99, 99
                bday_entries.append((m, d, MONTH_NAMES.get(m, f"Month {m}"), f"{d:02d}", name, circle, rel, notes))
            except Exception:
                bday_entries.append((99, 99, "Other", bday, name, circle, rel, notes))

    bday_entries.sort(key=lambda x: (x[0], x[1], x[4]))
    bday_rows = [["Month", "Day", "Name", "Circle / Group", "Relationship", "Notes & Connections"]]
    for entry in bday_entries:
        bday_rows.append([entry[2], entry[3], entry[4], entry[5], entry[6], entry[7]])

    card_rows = [["Name / Household", "Physical Address", "Circle / Group", "Relationship", "Notes & Connections"]]
    addr_entries = [r for r in master_rows[1:] if r[6].strip()]
    addr_entries.sort(key=lambda x: (x[1], x[0]))
    for r in addr_entries:
        name, circle, rel, bday, phone, email, addr, handles, notes = r
        card_rows.append([name, addr, circle, rel, notes])

    # 3. Structural requests (add tabs / rename existing)
    batch_reqs = []
    
    # Update spreadsheet title
    batch_reqs.append({
        "updateSpreadsheetProperties": {
            "properties": {"title": "Private Network: Friends, Family & Contacts Master"},
            "fields": "title"
        }
    })

    # Master sheet ID
    first_tab_title = list(existing_sheets.keys())[0]
    master_tab_id = existing_sheets[first_tab_title]

    # Rename first tab to Master Directory
    batch_reqs.append({
        "updateSheetProperties": {
            "properties": {
                "sheetId": master_tab_id,
                "title": "Master Directory",
                "gridProperties": {
                    "frozenRowCount": 1,
                    "frozenColumnCount": 1
                }
            },
            "fields": "title,gridProperties.frozenRowCount,gridProperties.frozenColumnCount"
        }
    })

    bday_tab_id = existing_sheets.get("Birthday Calendar", 1001)
    if "Birthday Calendar" not in existing_sheets:
        batch_reqs.append({
            "addSheet": {
                "properties": {
                    "sheetId": 1001,
                    "title": "Birthday Calendar",
                    "gridProperties": {"frozenRowCount": 1}
                }
            }
        })
        bday_tab_id = 1001

    cards_tab_id = existing_sheets.get("Holiday Cards & Addresses", 1002)
    if "Holiday Cards & Addresses" not in existing_sheets:
        batch_reqs.append({
            "addSheet": {
                "properties": {
                    "sheetId": 1002,
                    "title": "Holiday Cards & Addresses",
                    "gridProperties": {"frozenRowCount": 1}
                }
            }
        })
        cards_tab_id = 1002

    # Execute structural setup
    b_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}:batchUpdate"
    b_req = urllib.request.Request(b_url, data=json.dumps({"requests": batch_reqs}).encode("utf-8"), headers=headers)
    with urllib.request.urlopen(b_req) as b_resp:
        print("✅ Structural setup complete:", b_resp.status)

    # 4. Populate values across all tabs
    val_data = [
        {"range": "Master Directory!A1:I" + str(len(master_rows)), "values": master_rows},
        {"range": "Birthday Calendar!A1:F" + str(len(bday_rows)), "values": bday_rows},
        {"range": "Holiday Cards & Addresses!A1:E" + str(len(card_rows)), "values": card_rows}
    ]
    v_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values:batchUpdate"
    v_body = {"valueInputOption": "USER_ENTERED", "data": val_data}
    v_req = urllib.request.Request(v_url, data=json.dumps(v_body).encode("utf-8"), headers=headers)
    with urllib.request.urlopen(v_req) as v_resp:
        print("✅ Data populated across 3 tabs:", v_resp.status)

    # 5. Styling, Banding, Column Sizing, and Text Formatting
    style_reqs = []

    # Clear existing banded ranges first for idempotency
    for s in meta.get("sheets", []):
        for br in s.get("bandedRanges", []):
            style_reqs.append({"deleteBanding": {"bandedRangeId": br["bandedRangeId"]}})

    # --- TAB 1: Master Directory Styling ---

    # Header format (Navy #1B2A4A)
    style_reqs.append({
        "repeatCell": {
            "range": {"sheetId": master_tab_id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 9},
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": {"red": 0.106, "green": 0.165, "blue": 0.290},
                    "textFormat": {"foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}, "bold": True, "fontSize": 10},
                    "horizontalAlignment": "CENTER",
                    "verticalAlignment": "MIDDLE",
                    "wrapStrategy": "WRAP"
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy)"
        }
    })

    # Data cells formatting (vertical middle, Inter/Roboto, wrap for address & notes)
    style_reqs.append({
        "repeatCell": {
            "range": {"sheetId": master_tab_id, "startRowIndex": 1, "endRowIndex": len(master_rows), "startColumnIndex": 0, "endColumnIndex": 9},
            "cell": {
                "userEnteredFormat": {
                    "verticalAlignment": "MIDDLE",
                    "textFormat": {"fontSize": 10}
                }
            },
            "fields": "userEnteredFormat(verticalAlignment,textFormat)"
        }
    })

    # Birthday centered
    style_reqs.append({
        "repeatCell": {
            "range": {"sheetId": master_tab_id, "startRowIndex": 1, "endRowIndex": len(master_rows), "startColumnIndex": 3, "endColumnIndex": 4},
            "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
            "fields": "userEnteredFormat.horizontalAlignment"
        }
    })

    # Wrap text for Address (Col 6) and Notes (Col 8)
    for col_idx in [6, 8]:
        style_reqs.append({
            "repeatCell": {
                "range": {"sheetId": master_tab_id, "startRowIndex": 1, "endRowIndex": len(master_rows), "startColumnIndex": col_idx, "endColumnIndex": col_idx + 1},
                "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP"}},
                "fields": "userEnteredFormat.wrapStrategy"
            }
        })

    # Column widths for Master Directory
    master_col_widths = [190, 175, 160, 110, 140, 220, 290, 180, 360]
    for i, w in enumerate(master_col_widths):
        style_reqs.append({
            "updateDimensionProperties": {
                "range": {"sheetId": master_tab_id, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": w},
                "fields": "pixelSize"
            }
        })

    # Banding for Master Directory
    style_reqs.append({
        "addBanding": {
            "bandedRange": {
                "bandedRangeId": 1,
                "range": {"sheetId": master_tab_id, "startRowIndex": 0, "endRowIndex": len(master_rows), "startColumnIndex": 0, "endColumnIndex": 9},
                "rowProperties": {
                    "headerColor": {"red": 0.106, "green": 0.165, "blue": 0.290},
                    "firstBandColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                    "secondBandColor": {"red": 0.965, "green": 0.973, "blue": 0.980}
                }
            }
        }
    })

    # --- TAB 2: Birthday Calendar Styling ---
    # Header format (Emerald Teal #134E48)
    style_reqs.append({
        "repeatCell": {
            "range": {"sheetId": bday_tab_id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 6},
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": {"red": 0.075, "green": 0.306, "blue": 0.282},
                    "textFormat": {"foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}, "bold": True, "fontSize": 10},
                    "horizontalAlignment": "CENTER",
                    "verticalAlignment": "MIDDLE"
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
        }
    })

    # Month & Day centered
    style_reqs.append({
        "repeatCell": {
            "range": {"sheetId": bday_tab_id, "startRowIndex": 1, "endRowIndex": len(bday_rows), "startColumnIndex": 0, "endColumnIndex": 2},
            "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE", "textFormat": {"fontSize": 10}}},
            "fields": "userEnteredFormat(horizontalAlignment,verticalAlignment,textFormat)"
        }
    })

    # Wrap Notes
    style_reqs.append({
        "repeatCell": {
            "range": {"sheetId": bday_tab_id, "startRowIndex": 1, "endRowIndex": len(bday_rows), "startColumnIndex": 2, "endColumnIndex": 6},
            "cell": {"userEnteredFormat": {"verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP", "textFormat": {"fontSize": 10}}},
            "fields": "userEnteredFormat(verticalAlignment,wrapStrategy,textFormat)"
        }
    })

    # Column widths for Birthday Calendar
    bday_col_widths = [140, 75, 190, 175, 160, 360]
    for i, w in enumerate(bday_col_widths):
        style_reqs.append({
            "updateDimensionProperties": {
                "range": {"sheetId": bday_tab_id, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": w},
                "fields": "pixelSize"
            }
        })

    # Banding for Birthday Calendar
    style_reqs.append({
        "addBanding": {
            "bandedRange": {
                "bandedRangeId": 2,
                "range": {"sheetId": bday_tab_id, "startRowIndex": 0, "endRowIndex": len(bday_rows), "startColumnIndex": 0, "endColumnIndex": 6},
                "rowProperties": {
                    "headerColor": {"red": 0.075, "green": 0.306, "blue": 0.282},
                    "firstBandColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                    "secondBandColor": {"red": 0.941, "green": 0.992, "blue": 0.957}
                }
            }
        }
    })

    # --- TAB 3: Holiday Cards & Addresses Styling ---
    # Header format (Royal Plum / Burgundy #581C87)
    style_reqs.append({
        "repeatCell": {
            "range": {"sheetId": cards_tab_id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 5},
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": {"red": 0.345, "green": 0.110, "blue": 0.529},
                    "textFormat": {"foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}, "bold": True, "fontSize": 10},
                    "horizontalAlignment": "CENTER",
                    "verticalAlignment": "MIDDLE"
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
        }
    })

    # Data cells formatting (wrap address & notes)
    style_reqs.append({
        "repeatCell": {
            "range": {"sheetId": cards_tab_id, "startRowIndex": 1, "endRowIndex": len(card_rows), "startColumnIndex": 0, "endColumnIndex": 5},
            "cell": {"userEnteredFormat": {"verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP", "textFormat": {"fontSize": 10}}},
            "fields": "userEnteredFormat(verticalAlignment,wrapStrategy,textFormat)"
        }
    })

    # Column widths for Holiday Cards
    card_col_widths = [200, 320, 180, 160, 320]
    for i, w in enumerate(card_col_widths):
        style_reqs.append({
            "updateDimensionProperties": {
                "range": {"sheetId": cards_tab_id, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": w},
                "fields": "pixelSize"
            }
        })

    # Banding for Holiday Cards
    style_reqs.append({
        "addBanding": {
            "bandedRange": {
                "bandedRangeId": 3,
                "range": {"sheetId": cards_tab_id, "startRowIndex": 0, "endRowIndex": len(card_rows), "startColumnIndex": 0, "endColumnIndex": 5},
                "rowProperties": {
                    "headerColor": {"red": 0.345, "green": 0.110, "blue": 0.529},
                    "firstBandColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                    "secondBandColor": {"red": 0.980, "green": 0.961, "blue": 1.0}
                }
            }
        }
    })

    # --- Data Validation (Dropdown for Circle / Group on Master Directory) ---
    circles = [
        "Immediate Family", "Extended Family (Brock)", "In-Laws (Allen)",
        "Extended Family (Allen/Nolan/Olsen)", "Extended Family", "High School (NNHS)",
        "Northwestern & Slivka", "Stanford MSE", "Longtime Friends",
        "Bay Area Friends", "Friends", "Friend Circle", "Neighbors (Redwood City)",
        "Neighbors & School Parents", "Caregivers & Nannies"
    ]
    style_reqs.append({
        "setDataValidation": {
            "range": {"sheetId": master_tab_id, "startRowIndex": 1, "endRowIndex": len(master_rows), "startColumnIndex": 1, "endColumnIndex": 2},
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [{"userEnteredValue": c} for c in circles]
                },
                "inputMessage": "Select Circle / Group",
                "strict": False,
                "showCustomUi": True
            }
        }
    })

    # Execute styling requests
    s_req = urllib.request.Request(b_url, data=json.dumps({"requests": style_reqs}).encode("utf-8"), headers=headers)
    with urllib.request.urlopen(s_req) as s_resp:
        print("✅ Applied all formatting, banding, column sizing, and validation rules:", s_resp.status)

    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    print(f"🎉 Fully Styled Spreadsheet Live at: {sheet_url}")
    return sheet_url

if __name__ == "__main__":
    try:
        url = format_spreadsheet()
        print(f"SUCCESS_URL={url}")
    except Exception as e:
        print("Error formatting spreadsheet:", e)
