#!/usr/bin/env python3
"""Step 1 - Normalise the user's inventory CSV into data/inventory.json.

Handles Azure portal "Export to CSV" (NAME, TYPE, RESOURCE GROUP, LOCATION, SUBSCRIPTION),
Resource Graph Explorer downloads (id, name, type, resourceGroup, ...) and most custom exports.
Column names are matched case-insensitively ignoring spaces/underscores.

Usage: python 01_import_inventory.py [--csv inventory.csv] [--config arch-diagrams.config.json]
"""
import argparse
import csv
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import load_config, data_dir, write_json  # noqa: E402

ALIASES = {
    "id": ["id", "resourceid", "azureresourceid", "resourceuri"],
    "name": ["name", "resourcename", "resource"],
    "type": ["type", "resourcetype", "kindtype", "servicetype"],
    "resourceGroup": ["resourcegroup", "resourcegroupname", "rg", "resourcegroupid"],
    "location": ["location", "region"],
    "subscription": ["subscription", "subscriptionid", "subscriptionname"],
    "kind": ["kind"],
    "tags": ["tags", "tag"],
    "sku": ["sku", "skuname", "pricingtier"],
}


def norm(h):
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def parse_tags(raw):
    if not raw:
        return {}
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    out = {}
    for part in re.split(r"[;,|\n]", raw):
        if ":" in part or "=" in part:
            k, v = re.split(r"[:=]", part, 1)
            out[k.strip().strip('"')] = v.strip().strip('"')
    return out


def sniff_dialect(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        return csv.excel


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv")
    ap.add_argument("--config")
    a = ap.parse_args()
    cfg = load_config(a.config)
    path = a.csv or cfg["inventory_csv"]
    if not os.path.exists(path):
        sys.exit(f"Inventory CSV not found: {path}")

    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, dialect=sniff_dialect(path))
        headers = reader.fieldnames or []
        colmap = {}
        for field, names in ALIASES.items():
            for h in headers:
                if norm(h) in names:
                    colmap[field] = h
                    break
        rows = list(reader)

    print(f"[info] {len(rows)} rows, columns detected: {colmap}", file=sys.stderr)
    missing = [k for k in ("name", "resourceGroup") if k not in colmap and "id" not in colmap]
    if missing:
        sys.exit(f"Could not find columns for {missing}. Headers were: {headers}. "
                 "Rename the columns or add aliases in 01_import_inventory.py.")

    items = []
    for r in rows:
        g = lambda k: (r.get(colmap[k]) or "").strip() if k in colmap else ""  # noqa: E731
        rid = g("id")
        rg = g("resourceGroup")
        if rg.lower().startswith("/subscriptions/"):
            rg = rg.rstrip("/").split("/")[-1]
        if rid and not rg:
            m = re.search(r"/resourcegroups/([^/]+)", rid, re.I)
            rg = m.group(1) if m else ""
        name = g("name") or (rid.rstrip("/").split("/")[-1] if rid else "")
        if not name:
            continue
        items.append({
            "id": rid,
            "name": name,
            "type": g("type"),            # may be a friendly name, e.g. "App Service"
            "resourceGroup": rg,
            "location": g("location"),
            "subscription": g("subscription"),
            "kind": g("kind"),
            "sku": g("sku"),
            "tags": parse_tags(g("tags")),
        })

    out = {"source_csv": os.path.abspath(path), "columns": colmap, "resources": items}
    write_json(os.path.join(data_dir(cfg), "inventory.json"), out)
    rgs = sorted({i["resourceGroup"] for i in items if i["resourceGroup"]})
    print(json.dumps({"resources": len(items), "resource_groups": len(rgs),
                      "has_resource_ids": bool(colmap.get("id"))}, indent=2))


if __name__ == "__main__":
    main()
