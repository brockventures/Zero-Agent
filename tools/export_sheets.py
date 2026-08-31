import json
import os
import urllib.request
import urllib.parse

SECRETS_PATH = os.environ.get("GOOGLE_OAUTH_PATH", os.environ.get("GOOGLE_OAUTH_CREDENTIALS", "/secrets/google_oauth.json"))
if not os.path.exists(SECRETS_PATH) and os.path.exists("/workspace/config/google_oauth.json"):
    SECRETS_PATH = "/workspace/config/google_oauth.json"

def load_credentials(path: str) -> dict:
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

creds = load_credentials(SECRETS_PATH)

data = urllib.parse.urlencode({
    "client_id": creds["client_id"],
    "client_secret": creds["client_secret"],
    "refresh_token": creds["refresh_token"],
    "grant_type": "refresh_token"
}).encode("utf-8")

req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
with urllib.request.urlopen(req) as resp:
    tok = json.loads(resp.read().decode())["access_token"]

# Export Address book for Christmas cards
file_id = "1GK3dtz7iE1P6B1IJAuV5y4Y7qi9vpPKpY7Hbq7N3dfo"
export_url = f"https://www.googleapis.com/drive/v3/files/{file_id}/export?mimeType=text/csv"
e_req = urllib.request.Request(export_url, headers={"Authorization": f"Bearer {tok}"})
try:
    with urllib.request.urlopen(e_req) as e_resp:
        csv_data = e_resp.read().decode("utf-8")
        print("=== ADDRESS BOOK FOR CHRISTMAS CARDS ===")
        print(csv_data[:2000])
        with open("/workspace/data/christmas_card_address_book.csv", "w") as f:
            f.write(csv_data)
except Exception as e:
    print("Error exporting Christmas cards:", e)

# Export Minted Address Lists 2022
file_id2 = "16AwYqyhf-e5kdhT8zhIlX6pE7KpGihkcG6mlzCOfqQ4"
export_url2 = f"https://www.googleapis.com/drive/v3/files/{file_id2}/export?mimeType=text/csv"
e_req2 = urllib.request.Request(export_url2, headers={"Authorization": f"Bearer {tok}"})
try:
    with urllib.request.urlopen(e_req2) as e_resp2:
        csv_data2 = e_resp2.read().decode("utf-8")
        print("\n=== MINTED ADDRESS LISTS 2022 ===")
        print(csv_data2[:2000])
        with open("/workspace/data/minted_address_lists_2022.csv", "w") as f:
            f.write(csv_data2)
except Exception as e:
    print("Error exporting Minted lists:", e)
