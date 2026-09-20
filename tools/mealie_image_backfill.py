#!/usr/bin/env python3
"""
Mealie Image Backfill Tool
Author: Zero
Description: Scans Mealie for recipes missing photography, queries official
sources (Green Chef recipe cards/PDFs, Serious Eats, NYT Cooking) via SerpAPI,
extracts high-res hero images, and uploads them to Mealie via REST API.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import requests
import fitz  # pymupdf

WORKSPACE = "/workspace"
SECRETS_FILE = "/secrets/env.json"

def load_config():
    if not os.path.exists(SECRETS_FILE):
        raise RuntimeError(f"Secrets file not found: {SECRETS_FILE}")
    with open(SECRETS_FILE) as f:
        env = json.load(f)
    serpapi_key = env.get("SERPAPI_API_KEY")
    mealie_url = env.get("MEALIE_BASE_URL", "http://127.0.0.1:9090").rstrip("/")
    mealie_token = env.get("MEALIE_API_TOKEN")
    if not serpapi_key or not mealie_token:
        raise RuntimeError("SERPAPI_API_KEY or MEALIE_API_TOKEN missing in secrets")
    return serpapi_key, mealie_url, mealie_token

def get_missing_image_recipes():
    try:
        import subprocess
        py_code = """
import sqlite3, json
con = sqlite3.connect('/app/data/mealie.db')
cur = con.execute('SELECT id, slug, name, org_url FROM recipes WHERE image IS NULL OR image = ""')
print(json.dumps([{'id': r[0], 'slug': r[1], 'name': r[2], 'org_url': r[3]} for r in cur.fetchall()]))
"""
        cmd = ["ssh", "-i", "/secrets/id_ed25519", "-p", os.environ.get("NAS_SSH_PORT", "22"), "-o", "StrictHostKeyChecking=no", "user@127.0.0.1", "docker exec -i mealie python -"]
        res = subprocess.run(cmd, input=py_code, capture_output=True, text=True, timeout=10)
        if res.returncode == 0 and res.stdout.strip():
            return json.loads(res.stdout)
    except Exception as e:
        print(f"[ImageBackfill] DB query failed: {e}", file=sys.stderr)
    return []

def search_recipe_image(query, serpapi_key):
    params = {
        "engine": "google_images",
        "q": query,
        "api_key": serpapi_key,
        "num": 5
    }
    url = "https://serpapi.com/search.json?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Zero/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            return data.get("images_results", [])
    except Exception as e:
        print(f"[ImageBackfill] SerpAPI search failed for '{query}': {e}", file=sys.stderr)
        return []

def extract_hero_from_pdf(pdf_url):
    try:
        req = urllib.request.Request(pdf_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        data = urllib.request.urlopen(req, timeout=15).read()
        doc = fitz.open(stream=data, filetype="pdf")
        if len(doc) > 0:
            imgs = doc[0].get_images()
            if imgs:
                hero = doc.extract_image(imgs[0][0])
                return hero["image"], hero["ext"]
    except Exception as e:
        print(f"[ImageBackfill] PDF extraction failed from {pdf_url}: {e}", file=sys.stderr)
    return None, None

def download_image_bytes(img_url):
    try:
        req = urllib.request.Request(img_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            content_type = resp.headers.get("Content-Type", "")
            ext = "jpg"
            if "png" in content_type:
                ext = "png"
            elif "webp" in content_type:
                ext = "webp"
            return resp.read(), ext
    except Exception as e:
        print(f"[ImageBackfill] Direct image download failed from {img_url}: {e}", file=sys.stderr)
        return None, None

def upload_image_to_mealie(slug, img_bytes, ext, mealie_url, mealie_token):
    files = {"image": (f"recipe.{ext}", img_bytes, f"image/{ext}")}
    data = {"extension": ext}
    headers = {"Authorization": f"Bearer {mealie_token}"}
    try:
        r = requests.put(f"{mealie_url}/api/recipes/{slug}/image", headers=headers, files=files, data=data, timeout=15)
        return r.status_code == 200
    except Exception as e:
        print(f"[ImageBackfill] Mealie upload failed for {slug}: {e}", file=sys.stderr)
        return False

def backfill_all():
    serpapi_key, mealie_url, mealie_token = load_config()
    missing = get_missing_image_recipes()
    print(f"[ImageBackfill] Found {len(missing)} recipe(s) missing photography.")
    if not missing:
        print("[ImageBackfill] All recipes already have photography!")
        return

    success_count = 0
    fail_count = 0

    for idx, r in enumerate(missing, 1):
        slug = r["slug"]
        name = r["name"]
        print(f"\n[{idx}/{len(missing)}] Processing: {name} ({slug})...")

        clean_name = re.sub(r"\b(gluten-free|recipe|quick)\b", "", name, flags=re.IGNORECASE)
        clean_name = re.sub(r"\s+", " ", clean_name).strip()
        search_query = f'"Green Chef" "{clean_name.replace("Green Chef ", "")}"'

        results = search_recipe_image(search_query, serpapi_key)
        if not results:
            # Relax query without quotes
            relaxed_query = f"Green Chef {clean_name.replace('Green Chef ', '')} recipe"
            print(f"  → Retrying with relaxed query: {relaxed_query}")
            results = search_recipe_image(relaxed_query, serpapi_key)

        img_bytes = None
        img_ext = "jpg"

        # 1. Prefer Green Chef PDF nutrition card hero image
        for item in results:
            link = item.get("link", "")
            if "greenchef.com" in link and "nutritionCard" in link:
                print(f"  → Found official Green Chef PDF card: {link}")
                img_bytes, img_ext = extract_hero_from_pdf(link)
                if img_bytes:
                    break

        # 2. Fallback to Green Chef / HelloFresh web images
        if not img_bytes and results:
            for item in results:
                src = item.get("source", "").lower()
                orig = item.get("original", "")
                thumb = item.get("thumbnail", "")
                link = item.get("link", "").lower()
                if "greenchef" in src or "green chef" in src or "greenchef" in link or "hellofresh" in link:
                    target_url = orig if orig and orig.startswith("http") else thumb
                    if target_url:
                        print(f"  → Downloading Green Chef web image from: {target_url[:70]}...")
                        img_bytes, img_ext = download_image_bytes(target_url)
                        if img_bytes:
                            break

        # 3. Fallback to top high-res recipe photo from food sites
        if not img_bytes and results:
            for item in results:
                orig = item.get("original", "")
                if orig and orig.startswith("http") and not orig.startswith("x-raw"):
                    print(f"  → Downloading high-res recipe photo from: {orig[:70]}...")
                    img_bytes, img_ext = download_image_bytes(orig)
                    if img_bytes:
                        break

        # 4. Final fallback to Google thumbnail
        if not img_bytes and results:
            target_url = results[0].get("thumbnail")
            if target_url:
                print(f"  → Fallback downloading thumbnail: {target_url[:70]}...")
                img_bytes, img_ext = download_image_bytes(target_url)

        if img_bytes:
            ok = upload_image_to_mealie(slug, img_bytes, img_ext, mealie_url, mealie_token)
            if ok:
                print(f"  ✅ Successfully uploaded image for [{slug}] ({len(img_bytes)} bytes)")
                success_count += 1
            else:
                print(f"  ❌ Failed uploading image for [{slug}] to Mealie API")
                fail_count += 1
        else:
            print(f"  ⚠️ No suitable image found for [{slug}]")
            fail_count += 1

        time.sleep(1)

    print(f"\n[ImageBackfill] Complete! Success: {success_count}, Failed/Skipped: {fail_count}")
    try:
        sys.path.insert(0, WORKSPACE)
        from tools.meal_planner_proposal import refresh_recipes_cache
        refresh_recipes_cache(force=True)
        print("[ImageBackfill] Refreshed local Mealie recipe cache.")
    except Exception as e:
        print(f"[ImageBackfill] Cache refresh notice: {e}")

if __name__ == "__main__":
    backfill_all()
