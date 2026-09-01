#!/usr/bin/env python3
"""Create, populate, and format Google Spreadsheet with Master Friends & Family Dataset using Sheets API v4."""

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

def push_to_google_sheets(sheet_id=DEFAULT_SHEET_ID):
    token = get_access_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # 1. Read CSV data
    with open(CSV_PATH, "r", encoding="utf-8") as f:
        csv_rows = list(csv.reader(f))

    num_rows = len(csv_rows)
    num_cols = len(csv_rows[0]) if num_rows > 0 else 9

    # 2. Get spreadsheet metadata to find sheetId and tab title
    meta_req = urllib.request.Request(
        f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}",
        headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(meta_req) as resp:
        meta = json.loads(resp.read().decode())

    first_sheet = meta["sheets"][0]
    tab_sheet_id = first_sheet["properties"]["sheetId"]
    tab_title = first_sheet["properties"]["title"]

    # 3. Update values within the exact range without wiping outside columns
    escaped_update_range = urllib.parse.quote(f"{tab_title}!A1:I{num_rows}")
    update_vals_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{escaped_update_range}?valueInputOption=USER_ENTERED"
    vals_body = json.dumps({"values": csv_rows}).encode("utf-8")
    u_req = urllib.request.Request(update_vals_url, data=vals_body, headers=headers, method="PUT")
    with urllib.request.urlopen(u_req) as resp:
        pass

    # 4. Construct batchUpdate requests for formatting
    requests = []

    # Update Spreadsheet tab properties (freeze header row + first column)
    requests.append({
        "updateSheetProperties": {
            "properties": {
                "sheetId": tab_sheet_id,
                "title": tab_title,
                "gridProperties": {
                    "frozenRowCount": 1,
                    "frozenColumnCount": 1,
                    "hideGridlines": False
                }
            },
            "fields": "title,gridProperties.frozenRowCount,gridProperties.frozenColumnCount,gridProperties.hideGridlines"
        }
    })

    # Clear existing banded ranges if any
    for banded in first_sheet.get("bandedRanges", []):
        requests.append({
            "deleteBanding": {
                "bandedRangeId": banded["bandedRangeId"]
            }
        })

    # Add Banding (Zebra striping)
    requests.append({
        "addBanding": {
            "bandedRange": {
                "range": {
                    "sheetId": tab_sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": num_rows,
                    "startColumnIndex": 0,
                    "endColumnIndex": num_cols
                },
                "rowProperties": {
                    "headerColor": {"red": 0.11, "green": 0.21, "blue": 0.34, "alpha": 1.0},
                    "firstBandColor": {"red": 1.0, "green": 1.0, "blue": 1.0, "alpha": 1.0},
                    "secondBandColor": {"red": 0.96, "green": 0.97, "blue": 0.99, "alpha": 1.0}
                }
            }
        }
    })

    # Header Row Styling: Navy, Bold, White Text, Middle Align
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": tab_sheet_id,
                "startRowIndex": 0,
                "endRowIndex": 1,
                "startColumnIndex": 0,
                "endColumnIndex": num_cols
            },
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": {"red": 0.11, "green": 0.21, "blue": 0.34},
                    "horizontalAlignment": "CENTER",
                    "verticalAlignment": "MIDDLE",
                    "wrapStrategy": "CLIP",
                    "textFormat": {
                        "fontFamily": "Roboto",
                        "fontSize": 10,
                        "bold": True,
                        "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}
                    }
                }
            },
            "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,wrapStrategy,textFormat)"
        }
    })

    # Data Rows Base Font & Vertical Alignment
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": tab_sheet_id,
                "startRowIndex": 1,
                "endRowIndex": num_rows,
                "startColumnIndex": 0,
                "endColumnIndex": num_cols
            },
            "cell": {
                "userEnteredFormat": {
                    "verticalAlignment": "MIDDLE",
                    "textFormat": {
                        "fontFamily": "Roboto",
                        "fontSize": 10
                    }
                }
            },
            "fields": "userEnteredFormat(verticalAlignment,textFormat)"
        }
    })

    # Col 0: Name (Left, Bold)
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": tab_sheet_id,
                "startRowIndex": 1,
                "endRowIndex": num_rows,
                "startColumnIndex": 0,
                "endColumnIndex": 1
            },
            "cell": {
                "userEnteredFormat": {
                    "horizontalAlignment": "LEFT",
                    "textFormat": {
                        "fontFamily": "Roboto",
                        "fontSize": 10,
                        "bold": True,
                        "foregroundColor": {"red": 0.08, "green": 0.15, "blue": 0.25}
                    }
                }
            },
            "fields": "userEnteredFormat(horizontalAlignment,textFormat)"
        }
    })

    # Col 1: Core (Center)
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": tab_sheet_id,
                "startRowIndex": 1,
                "endRowIndex": num_rows,
                "startColumnIndex": 1,
                "endColumnIndex": 2
            },
            "cell": {
                "userEnteredFormat": {
                    "horizontalAlignment": "CENTER"
                }
            },
            "fields": "userEnteredFormat.horizontalAlignment"
        }
    })

    # Col 2: Out of Town (Center)
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": tab_sheet_id,
                "startRowIndex": 1,
                "endRowIndex": num_rows,
                "startColumnIndex": 2,
                "endColumnIndex": 3
            },
            "cell": {
                "userEnteredFormat": {
                    "horizontalAlignment": "CENTER"
                }
            },
            "fields": "userEnteredFormat.horizontalAlignment"
        }
    })

    # Col 3: Birthday (Center)
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": tab_sheet_id,
                "startRowIndex": 1,
                "endRowIndex": num_rows,
                "startColumnIndex": 3,
                "endColumnIndex": 4
            },
            "cell": {
                "userEnteredFormat": {
                    "horizontalAlignment": "CENTER"
                }
            },
            "fields": "userEnteredFormat.horizontalAlignment"
        }
    })

    # Col 4: Last Seen (Center, Date Format)
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": tab_sheet_id,
                "startRowIndex": 1,
                "endRowIndex": num_rows,
                "startColumnIndex": 4,
                "endColumnIndex": 5
            },
            "cell": {
                "userEnteredFormat": {
                    "horizontalAlignment": "CENTER",
                    "numberFormat": {
                        "type": "DATE",
                        "pattern": "yyyy-mm-dd"
                    }
                }
            },
            "fields": "userEnteredFormat(horizontalAlignment,numberFormat)"
        }
    })

    # Col 5: Phone (Center)
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": tab_sheet_id,
                "startRowIndex": 1,
                "endRowIndex": num_rows,
                "startColumnIndex": 5,
                "endColumnIndex": 6
            },
            "cell": {
                "userEnteredFormat": {
                    "horizontalAlignment": "CENTER"
                }
            },
            "fields": "userEnteredFormat.horizontalAlignment"
        }
    })

    # Col 6: Email (Left)
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": tab_sheet_id,
                "startRowIndex": 1,
                "endRowIndex": num_rows,
                "startColumnIndex": 6,
                "endColumnIndex": 7
            },
            "cell": {
                "userEnteredFormat": {
                    "horizontalAlignment": "LEFT"
                }
            },
            "fields": "userEnteredFormat.horizontalAlignment"
        }
    })

    # Col 7: Physical Address (Left, Wrap)
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": tab_sheet_id,
                "startRowIndex": 1,
                "endRowIndex": num_rows,
                "startColumnIndex": 7,
                "endColumnIndex": 8
            },
            "cell": {
                "userEnteredFormat": {
                    "horizontalAlignment": "LEFT",
                    "wrapStrategy": "WRAP"
                }
            },
            "fields": "userEnteredFormat(horizontalAlignment,wrapStrategy)"
        }
    })

    # Col 8: Notes & Connections (Left, Wrap)
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": tab_sheet_id,
                "startRowIndex": 1,
                "endRowIndex": num_rows,
                "startColumnIndex": 8,
                "endColumnIndex": 9
            },
            "cell": {
                "userEnteredFormat": {
                    "horizontalAlignment": "LEFT",
                    "wrapStrategy": "WRAP"
                }
            },
            "fields": "userEnteredFormat(horizontalAlignment,wrapStrategy)"
        }
    })

    # Set Column Widths
    col_widths = [
        (0, 1, 200),  # Name
        (1, 2, 80),   # Core
        (2, 3, 110),  # Out of Town
        (3, 4, 100),  # Birthday
        (4, 5, 110),  # Last Seen
        (5, 6, 140),  # Phone Number
        (6, 7, 230),  # Email Address
        (7, 8, 300),  # Physical Address
        (8, 9, 380),  # Notes & Connections
    ]
    for start_c, end_c, px in col_widths:
        requests.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": tab_sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": start_c,
                    "endIndex": end_c
                },
                "properties": {
                    "pixelSize": px
                },
                "fields": "pixelSize"
            }
        })

    # Set Header Row Height
    requests.append({
        "updateDimensionProperties": {
            "range": {
                "sheetId": tab_sheet_id,
                "dimension": "ROWS",
                "startIndex": 0,
                "endIndex": 1
            },
            "properties": {
                "pixelSize": 38
            },
            "fields": "pixelSize"
        }
    })

    # Set Grid Borders
    thin_border = {
        "style": "SOLID",
        "color": {"red": 0.85, "green": 0.88, "blue": 0.92}
    }
    requests.append({
        "updateBorders": {
            "range": {
                "sheetId": tab_sheet_id,
                "startRowIndex": 0,
                "endRowIndex": num_rows,
                "startColumnIndex": 0,
                "endColumnIndex": num_cols
            },
            "top": thin_border,
            "bottom": thin_border,
            "left": thin_border,
            "right": thin_border,
            "innerHorizontal": thin_border,
            "innerVertical": thin_border
        }
    })

    # Clear existing filter first if any, then set Basic Filter
    requests.append({
        "clearBasicFilter": {
            "sheetId": tab_sheet_id
        }
    })
    requests.append({
        "setBasicFilter": {
            "filter": {
                "range": {
                    "sheetId": tab_sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": num_rows,
                    "startColumnIndex": 0,
                    "endColumnIndex": num_cols
                }
            }
        }
    })

    # 5. Execute batchUpdate
    batch_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}:batchUpdate"
    batch_body = json.dumps({"requests": requests}).encode("utf-8")
    b_req = urllib.request.Request(batch_url, data=batch_body, headers=headers, method="POST")
    with urllib.request.urlopen(b_req) as resp:
        res = json.loads(resp.read().decode())

    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    print(f"✅ Google Spreadsheet formatted & synchronized! ID: {sheet_id}")
    print(f"🔗 URL: {sheet_url}")
    return sheet_url

if __name__ == "__main__":
    try:
        url = push_to_google_sheets()
        print(f"SUCCESS_URL={url}")
    except Exception as e:
        print("Error:", e)
