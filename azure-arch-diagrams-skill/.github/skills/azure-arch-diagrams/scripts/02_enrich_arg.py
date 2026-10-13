#!/usr/bin/env python3
"""Step 2 - Enrich the CSV inventory with Azure Resource Graph (az graph query).

Pulls full properties (needed for network relationships) for every resource in the subscription
(scoped to the CSV's resource groups by default), plus resource groups, and matches them back to
CSV rows by resource id, or by name + resource group when the CSV has no ids.

Optional --scan-app-settings: reads Web/Function App settings and connection strings ONLY to find
which other resources an app talks to (SQL server, Key Vault, Service Bus, storage, ...). Values are
matched in memory and discarded; only the matched resource ids are saved.

Usage:
  python 02_enrich_arg.py [--subscription <id>] [--all-rgs] [--scan-app-settings]
  python 02_enrich_arg.py --from-file arg_export.json   # offline: JSON array of Resources rows
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import az, load_config, data_dir, write_json, read_json  # noqa: E402

KQL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kql")


def graph_query(kql, subscription):
    rows, skip = [], None
    while True:
        args = ["graph", "query", "-q", kql, "--first", "1000"]
        if subscription:
            args += ["--subscriptions", subscription]
        if skip:
            args += ["--skip-token", skip]
        res = az(args)
        rows += res.get("data", [])
        skip = res.get("skip_token") or res.get("skipToken")
        if not skip:
            return rows


def load_kql(name, rg_filter):
    with open(os.path.join(KQL_DIR, name), encoding="utf-8") as f:
        kql = f.read()
    return kql.replace("{{RG_FILTER}}", rg_filter)


def rg_filter_clause(rgs):
    if not rgs:
        return ""
    quoted = ", ".join("'" + r.lower().replace("'", "''") + "'" for r in sorted(rgs))
    return f"| where tolower(resourceGroup) in~ ({quoted})"


def scan_app_settings(resources):
    """Return {site_id: [target_resource_id, ...]} by matching setting values to known endpoints."""
    by_host = {}
    for r in resources:
        n = (r.get("name") or "").lower()
        t = (r.get("type") or "").lower()
        p = r.get("properties") or {}
        hosts = set()
        if t == "microsoft.sql/servers":
            hosts.add(f"{n}.database.windows.net")
        elif t == "microsoft.keyvault/vaults":
            hosts.add(f"{n}.vault.azure.net")
        elif t == "microsoft.storage/storageaccounts":
            hosts |= {f"{n}.blob.core.windows.net", f"{n}.queue.core.windows.net",
                      f"{n}.table.core.windows.net", f"{n}.file.core.windows.net", f"accountname={n}"}
        elif t in ("microsoft.servicebus/namespaces", "microsoft.eventhub/namespaces"):
            hosts.add(f"{n}.servicebus.windows.net")
        elif t == "microsoft.documentdb/databaseaccounts":
            hosts.add(f"{n}.documents.azure.com")
        elif t == "microsoft.cache/redis":
            hosts.add(f"{n}.redis.cache.windows.net")
        elif t in ("microsoft.dbforpostgresql/flexibleservers",):
            hosts.add(f"{n}.postgres.database.azure.com")
        elif t in ("microsoft.dbformysql/flexibleservers",):
            hosts.add(f"{n}.mysql.database.azure.com")
        elif t == "microsoft.web/sites":
            for h in p.get("hostNames") or []:
                hosts.add(h.lower())
        elif t == "microsoft.insights/components":
            if p.get("InstrumentationKey"):
                hosts.add(p["InstrumentationKey"].lower())
        elif t == "microsoft.cognitiveservices/accounts":
            hosts.add(f"{n}.openai.azure.com")
            hosts.add(f"{n}.cognitiveservices.azure.com")
        elif t == "microsoft.search/searchservices":
            hosts.add(f"{n}.search.windows.net")
        elif t == "microsoft.signalrservice/signalr":
            hosts.add(f"{n}.service.signalr.net")
        elif t == "microsoft.apimanagement/service":
            hosts.add(f"{n}.azure-api.net")
        for h in hosts:
            by_host[h] = r["id"].lower()

    found = {}
    sites = [r for r in resources if (r.get("type") or "").lower() == "microsoft.web/sites"]
    for s in sites:
        rg, name = s["resourceGroup"], s["name"]
        values = []
        st = az(["webapp", "config", "appsettings", "list", "-g", rg, "-n", name], check=False) or []
        values += [str(x.get("value") or "") for x in st]
        cs = az(["webapp", "config", "connection-string", "list", "-g", rg, "-n", name], check=False) or []
        values += [str((x.get("value") or {}).get("value") if isinstance(x.get("value"), dict) else x.get("value") or "")
                   for x in cs]
        targets = set()
        for v in values:
            low = v.lower()
            for h, rid in by_host.items():
                if h in low and rid != s["id"].lower():
                    targets.add(rid)
        del values  # never persisted
        if targets:
            found[s["id"].lower()] = sorted(targets)
        print(f"[info] app settings scanned: {name} -> {len(targets)} dependencies", file=sys.stderr)
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    ap.add_argument("--subscription")
    ap.add_argument("--all-rgs", action="store_true", help="do not limit to resource groups found in the CSV")
    ap.add_argument("--scan-app-settings", action="store_true")
    ap.add_argument("--from-file", help="offline: JSON array (or {data:[...]}) of Resources rows")
    a = ap.parse_args()
    cfg = load_config(a.config)
    dd = data_dir(cfg)
    inv = read_json(os.path.join(dd, "inventory.json"), {"resources": []})
    csv_rows = inv["resources"]
    csv_rgs = {r["resourceGroup"].lower() for r in csv_rows if r.get("resourceGroup")}
    scope_rgs = set() if (a.all_rgs or not cfg.get("scope_to_csv_resource_groups", True)) else csv_rgs

    sub = a.subscription or cfg.get("subscription_id")
    if a.from_file:
        raw = read_json(a.from_file)
        resources = raw.get("data", raw) if isinstance(raw, dict) else raw
        if scope_rgs:
            resources = [r for r in resources if (r.get("resourceGroup") or "").lower() in scope_rgs]
        groups = []
    else:
        if not sub:
            acct = az(["account", "show"])
            sub = acct["id"]
            print(f"[info] using current az subscription {acct['name']} ({sub})", file=sys.stderr)
        flt = rg_filter_clause(scope_rgs)
        resources = graph_query(load_kql("resources.kql", flt), sub)
        groups = graph_query(load_kql("resource_groups.kql", flt), sub)
        print(f"[info] Resource Graph returned {len(resources)} resources in {len(groups)} resource groups",
              file=sys.stderr)

    # ---- match CSV rows to ARG rows
    by_id = {r["id"].lower(): r for r in resources}
    by_name_rg = {}
    for r in resources:
        by_name_rg.setdefault((r["name"].lower(), (r.get("resourceGroup") or "").lower()), []).append(r)
    matched, csv_only = set(), []
    for c in csv_rows:
        hit = None
        if c.get("id"):
            hit = by_id.get(c["id"].lower())
        if not hit:
            cands = by_name_rg.get((c["name"].lower(), c.get("resourceGroup", "").lower()), [])
            if len(cands) > 1 and c.get("type"):
                friendly = re.sub(r"[^a-z]", "", c["type"].lower())
                cands = [x for x in cands if friendly and friendly[:6] in re.sub(r"[^a-z]", "", x["type"].lower())] or cands
            hit = cands[0] if cands else None
        if hit:
            matched.add(hit["id"].lower())
        else:
            csv_only.append(c)
    for r in resources:
        r["in_csv"] = r["id"].lower() in matched

    deps = scan_app_settings(resources) if (a.scan_app_settings or cfg.get("scan_app_settings")) else {}

    out = {
        "subscription": sub,
        "scope_resource_groups": sorted(scope_rgs) or "all",
        "resources": resources,
        "resource_groups": groups,
        "csv_not_found_in_azure": csv_only,
        "app_dependencies": deps,
    }
    write_json(os.path.join(dd, "arg.json"), out)
    print(json.dumps({
        "arg_resources": len(resources),
        "matched_csv_rows": len(matched),
        "csv_rows_not_found_in_azure": len(csv_only),
        "arg_resources_not_in_csv": sum(1 for r in resources if not r["in_csv"]),
        "apps_with_discovered_dependencies": len(deps),
    }, indent=2))


if __name__ == "__main__":
    main()
