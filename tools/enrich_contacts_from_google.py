#!/usr/bin/env python3
"""Enrich Master Friends & Family Dataset from Google Contacts Takeout VCFs."""

import os
import glob
import csv
import re
import json

CSV_PATH = os.environ.get("FRIENDS_CSV_PATH", "/workspace/data/friends_and_family_master.csv")
VCF_SEARCH_PATH = os.environ.get("VCF_SEARCH_PATH", "/workspace/data/takeout_extracted/Takeout/Contacts/**/*.vcf")

def parse_vcf(path):
    contacts = []
    current = {}
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if line == "BEGIN:VCARD":
                current = {}
            elif line == "END:VCARD":
                contacts.append(current)
                current = {}
            elif current is not None:
                if line.startswith("FN:"):
                    current["fn"] = line[3:]
                elif line.startswith("TEL") and ":" in line:
                    current.setdefault("tels", []).append(line.split(":", 1)[1])
                elif line.startswith("EMAIL") and ":" in line:
                    current.setdefault("emails", []).append(line.split(":", 1)[1])
                elif line.startswith("BDAY:"):
                    current["bday"] = line[5:]
                elif line.startswith("ADR") and ":" in line:
                    current.setdefault("adrs", []).append(line.split(":", 1)[1])
                elif line.startswith("NOTE:"):
                    current["note"] = line[5:]
    return contacts

def clean_phone(p):
    p = p.replace("\xa0", " ").strip()
    return p

def run_enrichment():
    vcf_files = glob.glob(VCF_SEARCH_PATH, recursive=True)
    all_contacts = {}
    for vf in vcf_files:
        for c in parse_vcf(vf):
            fn = c.get("fn", "").strip()
            if fn:
                existing = all_contacts.setdefault(fn.lower(), {"fn": fn, "tels": set(), "emails": set(), "bday": None, "adrs": set(), "notes": set()})
                for t in c.get("tels", []): existing["tels"].add(t)
                for e in c.get("emails", []): existing["emails"].add(e)
                if c.get("bday") and not existing["bday"]: existing["bday"] = c["bday"]
                for a in c.get("adrs", []): existing["adrs"].add(a)
                if c.get("note"): existing["notes"].add(c["note"])

    print(f"Loaded {len(all_contacts)} unique contacts from Google Contacts VCFs.")

    with open(CSV_PATH, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        master = list(reader)

    enriched_items = []
    for row in master:
        name = row["Name"]
        clean_name = re.sub(r"\(.*?\)", "", name).strip().lower()
        clean_name = clean_name.replace("née allen", "").replace("'", "").strip()

        cdata = all_contacts.get(clean_name)
        if not cdata:
            tokens = [t for t in clean_name.split() if len(t) > 2]
            for k, v in all_contacts.items():
                if all(t in k for t in tokens):
                    cdata = v
                    break

        if cdata:
            changes = {}
            if not row["Phone Number"] and cdata["tels"]:
                phones = [clean_phone(p) for p in cdata["tels"] if p.strip()]
                if phones:
                    row["Phone Number"] = phones[0]
                    changes["Phone Number"] = phones[0]

            if not row["Email Address"] and cdata["emails"]:
                emails = [e.strip() for e in cdata["emails"] if "@" in e]
                if emails:
                    row["Email Address"] = emails[0]
                    changes["Email Address"] = emails[0]

            if not row["Birthday"] and cdata["bday"]:
                b_val = str(cdata["bday"]).strip()
                if b_val.startswith("--"):
                    b_val = b_val[2:]
                row["Birthday"] = b_val
                changes["Birthday"] = b_val

            if changes:
                enriched_items.append((name, cdata["fn"], changes))

    print(f"Enriched {len(enriched_items)} contacts in master dataset.")
    for name, cfn, ch in enriched_items:
        print(f"  • {name} (from '{cfn}'): {ch}")

    # Write back enriched CSV
    with open(CSV_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(master)

    print(f"✅ Successfully wrote enriched data to {CSV_PATH}")
    return enriched_items

if __name__ == "__main__":
    run_enrichment()
