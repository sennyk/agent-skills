#!/usr/bin/env python3
"""Step 5 - Merge inventory + Resource Graph + IaC + pipelines into one graph (data/graph.json).

Nodes: every resource, plus subnets (taken from VNet properties).
Each node gets env (dev/qa/prod/shared), app, tier, icon, IaC status and the pipelines that deploy it.
Edges (rel): peering, private_link, vnet_integration, hosted_on, logs_to, routes_to, public_ip,
             udr, service_endpoint, dns_link, database, uses (from app settings scan).
Placement: resources that live inside a subnet get node["subnet"] (VMs via NIC, PEs, App GW, ...).

Also writes data/app-map.json: resource group -> detected app / env, for the user to review.
Correct mistakes with "app.overrides" / "env" settings in arch-diagrams.config.json and re-run.

Usage: python 05_build_graph.py
"""
import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (SKILL_DIR, load_config, data_dir, write_json, read_json, detect_env, detect_app,  # noqa: E402
                    type_key, tier_of, rid_lower, parent_id)


def g(d, *path, default=None):
    for p in path:
        if isinstance(d, dict):
            d = d.get(p)
        elif isinstance(d, list) and isinstance(p, int) and len(d) > p:
            d = d[p]
        else:
            return default
    return d if d is not None else default


def resource_id_of(sub_id):
    """Trim a sub-resource id (…/applicationGateways/x/frontendIPConfigurations/y) to the resource id."""
    parts = rid_lower(sub_id).split("/")
    try:
        i = parts.index("providers")
    except ValueError:
        return rid_lower(sub_id)
    return "/".join(parts[: i + 4])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    a = ap.parse_args()
    cfg = load_config(a.config)
    dd = data_dir(cfg)
    icon_map = read_json(os.path.join(SKILL_DIR, "references", "icon_map.json"))
    arg = read_json(os.path.join(dd, "arg.json"))
    iac = read_json(os.path.join(dd, "iac.json"))
    pipes = read_json(os.path.join(dd, "pipelines.json"), {"deployments": []})

    if not arg:
        inv = read_json(os.path.join(dd, "inventory.json"))
        if not inv:
            sys.exit("Run 01_import_inventory.py (and ideally 02_enrich_arg.py) first.")
        print("[warn] arg.json missing - building from CSV only (no relationships)", file=sys.stderr)
        resources = [dict(r, id=r["id"] or f"/csv/{r['resourceGroup']}/{r['name']}", properties={}, in_csv=True)
                     for r in inv["resources"]]
        arg = {"resources": resources, "app_dependencies": {}}

    nodes, edges = {}, []

    def add_edge(src, dst, rel, label=""):
        src, dst = rid_lower(src), rid_lower(dst)
        if src and dst and src != dst:
            edges.append({"src": src, "dst": dst, "rel": rel, "label": label})

    # ---------------------------------------------------------------- nodes
    for r in arg["resources"]:
        rid = rid_lower(r["id"])
        rtype = (r.get("type") or "").lower()
        tkey = type_key(rtype, r.get("kind"))
        tags = r.get("tags") or {}
        env, env_src = detect_env(r.get("resourceGroup"), r.get("name"), tags, cfg)
        app, app_src = detect_app(r.get("resourceGroup"), r.get("name"), tags, cfg)
        nodes[rid] = {
            "id": rid, "name": r.get("name"), "type": rtype, "kind": r.get("kind"),
            "icon": icon_map.get(tkey) or icon_map.get(rtype) or icon_map["_default"],
            "rg": r.get("resourceGroup") or "", "location": r.get("location"),
            "env": env, "env_source": env_src, "app": app, "app_source": app_src,
            "tier": tier_of(rtype), "in_csv": r.get("in_csv", True), "subnet": None,
            "iac": None, "deployed_by": [], "info": "",
        }
    props = {rid_lower(r["id"]): (r.get("properties") or {}) for r in arg["resources"]}

    # subnets from VNets
    for rid, p in list(props.items()):
        n = nodes[rid]
        if n["type"] != "microsoft.network/virtualnetworks":
            continue
        n["info"] = ", ".join(g(p, "addressSpace", "addressPrefixes", default=[]))
        for s in p.get("subnets") or []:
            sid = rid_lower(s.get("id"))
            sp = s.get("properties") or {}
            prefix = sp.get("addressPrefix") or ", ".join(sp.get("addressPrefixes") or [])
            deleg = [g(d, "properties", "serviceName", default="") for d in sp.get("delegations") or []]
            nodes[sid] = {
                "id": sid, "name": s.get("name"), "type": "microsoft.network/virtualnetworks/subnets",
                "icon": icon_map["_subnet"], "rg": n["rg"], "location": n["location"],
                "env": n["env"], "env_source": n["env_source"], "app": n["app"], "app_source": n["app_source"],
                "tier": "Networking", "in_csv": True, "subnet": None, "vnet": rid, "iac": None,
                "deployed_by": [], "info": prefix,
                "nsg": rid_lower(g(sp, "networkSecurityGroup", "id", default="")),
                "route_table": rid_lower(g(sp, "routeTable", "id", default="")),
                "delegation": ", ".join(x.split("/")[-1] for x in deleg if x),
                "service_endpoints": [x.get("service") for x in sp.get("serviceEndpoints") or []],
            }
        for pr in p.get("virtualNetworkPeerings") or []:
            remote = rid_lower(g(pr, "properties", "remoteVirtualNetwork", "id", default=""))
            state = g(pr, "properties", "peeringState", default="")
            if remote:
                a_, b_ = sorted([rid, remote])
                add_edge(a_, b_, "peering", state if state and state != "Connected" else "")
                if remote not in nodes:
                    nodes[remote] = {"id": remote, "name": remote.split("/")[-1], "type": "microsoft.network/virtualnetworks",
                                     "icon": icon_map["microsoft.network/virtualnetworks"],
                                     "rg": remote.split("/resourcegroups/")[-1].split("/")[0], "location": "",
                                     "env": "shared", "env_source": "external", "app": "external",
                                     "app_source": "external", "tier": "Networking", "in_csv": False,
                                     "subnet": None, "iac": None, "deployed_by": [],
                                     "info": "outside scope (other RG/subscription)", "external": True}

    # private IP -> resource (for UDR next hop and App GW backends)
    ip_owner = {}
    nic_subnet = {}
    for rid, p in props.items():
        t = nodes[rid]["type"]
        for ipc in p.get("ipConfigurations") or []:
            ip = g(ipc, "properties", "privateIPAddress")
            if ip:
                ip_owner[ip] = rid
            sn = g(ipc, "properties", "subnet", "id")
            if t == "microsoft.network/networkinterfaces" and sn:
                nic_subnet[rid] = rid_lower(sn)

    host_owner = {}
    for rid, p in props.items():
        if nodes[rid]["type"] == "microsoft.web/sites":
            for h in (p.get("hostNames") or []) + [p.get("defaultHostName") or ""]:
                if h:
                    host_owner[h.lower()] = rid

    # ---------------------------------------------------------------- relationships
    for rid, p in props.items():
        n = nodes[rid]
        t = n["type"]

        def place(subnet_id):
            if subnet_id and not n["subnet"]:
                n["subnet"] = rid_lower(subnet_id)

        if t == "microsoft.network/networkinterfaces":
            place(nic_subnet.get(rid))
            vm = rid_lower(g(p, "virtualMachine", "id", default=""))
            if vm in nodes:
                n["owner"] = vm
                place_vm = nodes[vm]
                if not place_vm["subnet"]:
                    place_vm["subnet"] = nic_subnet.get(rid)
            pe = rid_lower(g(p, "privateEndpoint", "id", default=""))
            if pe:
                n["owner"] = pe
            for ipc in p.get("ipConfigurations") or []:
                pip = g(ipc, "properties", "publicIPAddress", "id")
                if pip:
                    add_edge(pip, vm or rid, "public_ip")
        elif t == "microsoft.network/privateendpoints":
            place(g(p, "subnet", "id"))
            for c in (p.get("privateLinkServiceConnections") or []) + (p.get("manualPrivateLinkServiceConnections") or []):
                target = g(c, "properties", "privateLinkServiceId")
                groups = ",".join(g(c, "properties", "groupIds", default=[]) or [])
                if target:
                    add_edge(rid, target, "private_link", groups)
                    n["pe_target"] = rid_lower(target)
        elif t == "microsoft.web/sites":
            if p.get("serverFarmId"):
                add_edge(rid, p["serverFarmId"], "hosted_on")
            vsn = p.get("virtualNetworkSubnetId") or g(p, "siteConfig", "vnetName")
            if vsn and str(vsn).startswith("/subscriptions"):
                add_edge(rid, vsn, "vnet_integration", "outbound")
                n["integration_subnet"] = rid_lower(vsn)
            if g(p, "hostingEnvironmentProfile", "id"):
                add_edge(rid, g(p, "hostingEnvironmentProfile", "id"), "hosted_on")
        elif t == "microsoft.network/applicationgateways":
            for ipc in p.get("gatewayIPConfigurations") or []:
                place(g(ipc, "properties", "subnet", "id"))
            for fe in p.get("frontendIPConfigurations") or []:
                pip = g(fe, "properties", "publicIPAddress", "id")
                if pip:
                    add_edge(pip, rid, "public_ip")
            for pool in p.get("backendAddressPools") or []:
                for b in g(pool, "properties", "backendAddresses", default=[]) or []:
                    target = host_owner.get((b.get("fqdn") or "").lower()) or ip_owner.get(b.get("ipAddress") or "")
                    if target:
                        target = nodes.get(target, {}).get("owner", target)
                        add_edge(rid, target, "routes_to", pool.get("name", ""))
            if g(p, "firewallPolicy", "id"):
                add_edge(rid, g(p, "firewallPolicy", "id"), "uses", "WAF")
        elif t in ("microsoft.network/azurefirewalls", "microsoft.network/bastionhosts",
                   "microsoft.network/virtualnetworkgateways"):
            for ipc in p.get("ipConfigurations") or []:
                place(g(ipc, "properties", "subnet", "id"))
                pip = g(ipc, "properties", "publicIPAddress", "id")
                if pip:
                    add_edge(pip, rid, "public_ip")
        elif t == "microsoft.network/loadbalancers":
            for fe in p.get("frontendIPConfigurations") or []:
                place(g(fe, "properties", "subnet", "id"))
                pip = g(fe, "properties", "publicIPAddress", "id")
                if pip:
                    add_edge(pip, rid, "public_ip")
        elif t == "microsoft.containerservice/managedclusters":
            for pool in p.get("agentPoolProfiles") or []:
                place(pool.get("vnetSubnetID"))
        elif t == "microsoft.apimanagement/service":
            place(g(p, "virtualNetworkConfiguration", "subnetResourceId"))
        elif t == "microsoft.app/managedenvironments":
            place(g(p, "vnetConfiguration", "infrastructureSubnetId"))
        elif t == "microsoft.app/containerapps":
            env_id = p.get("managedEnvironmentId") or p.get("environmentId")
            if env_id:
                add_edge(rid, env_id, "hosted_on")
        elif t in ("microsoft.sql/managedinstances", "microsoft.cache/redis"):
            place(p.get("subnetId"))
        elif t == "microsoft.web/hostingenvironments":
            place(g(p, "virtualNetwork", "id"))
        elif t == "microsoft.compute/virtualmachinescalesets":
            for nc in g(p, "virtualMachineProfile", "networkProfile", "networkInterfaceConfigurations", default=[]) or []:
                for ipc in g(nc, "properties", "ipConfigurations", default=[]) or []:
                    place(g(ipc, "properties", "subnet", "id"))
        elif t == "microsoft.insights/components":
            ws = p.get("WorkspaceResourceId") or p.get("workspaceResourceId")
            if ws:
                add_edge(rid, ws, "logs_to")
        elif t == "microsoft.sql/servers/databases" or t == "microsoft.sql/servers/elasticpools":
            add_edge(parent_id(rid), rid, "database")
        elif t == "microsoft.network/privatednszones/virtualnetworklinks":
            vnet = g(p, "virtualNetwork", "id")
            if vnet:
                add_edge(parent_id(rid), vnet, "dns_link")
        elif t == "microsoft.network/publicipaddresses":
            ipc = g(p, "ipConfiguration", "id")
            if ipc:
                owner = resource_id_of(ipc)
                owner = nodes.get(owner, {}).get("owner", owner) if owner in nodes else owner
                add_edge(rid, owner, "public_ip")
            n["info"] = p.get("ipAddress") or ""
        # service endpoints / VNet rules (storage, key vault, sql, cosmos, ...)
        for rule in (g(p, "networkAcls", "virtualNetworkRules", default=[]) or []) + \
                    (g(p, "virtualNetworkRules", default=[]) or []):
            sn = rule.get("id") or rule.get("virtualNetworkResourceId")
            if sn:
                add_edge(sn, rid, "service_endpoint")

    # UDR next hops -> firewall / NVA
    for sid, sn in [(k, v) for k, v in nodes.items() if v["type"].endswith("/subnets")]:
        rt = sn.get("route_table")
        if rt and rt in props:
            for route in props[rt].get("routes") or []:
                rp = route.get("properties") or {}
                if rp.get("nextHopType") == "VirtualAppliance" and rp.get("nextHopIpAddress") in ip_owner:
                    hop = ip_owner[rp["nextHopIpAddress"]]
                    hop = nodes.get(hop, {}).get("owner", hop)
                    add_edge(sid, hop, "udr", rp.get("addressPrefix", ""))

    for site, targets in (arg.get("app_dependencies") or {}).items():
        for t in targets:
            add_edge(site, t, "uses")

    # PEs / NICs / public IPs belong to the app of what they serve (for the app view)
    for n in nodes.values():
        link = n.get("pe_target") or n.get("owner")
        if link and link in nodes:
            n["app_view_app"] = nodes[link]["app"]
            n["app_view_env"] = nodes[link]["env"]
    for e in edges:
        if e["rel"] == "public_ip" and e["src"] in nodes and e["dst"] in nodes:
            nodes[e["src"]]["app_view_app"] = nodes[e["dst"]]["app"]
            nodes[e["src"]]["app_view_env"] = nodes[e["dst"]]["env"]

    # ---------------------------------------------------------------- IaC status
    if iac and iac.get("resources"):
        declared = defaultdict(list)
        for r in iac["resources"]:
            for nm in (r.get("resolved_names") or {}).values():
                declared[(r["type"], nm.lower().split("/")[-1])].append(r["file"])
        declared_types = defaultdict(list)
        covered_envs = set()
        for r in iac["resources"]:
            declared_types[r["type"]].append(r)
            covered_envs |= set((r.get("resolved_names") or {}).keys()) - {"any"}
        for n in nodes.values():
            if n["type"].endswith("/subnets") or n.get("external"):
                continue
            files = declared.get((n["type"], (n["name"] or "").lower().split("/")[-1]))
            if files:
                n["iac"] = {"status": "managed", "files": sorted(set(files))}
            elif n["type"] in declared_types and n["env"] not in covered_envs:
                # template declares this type but has no parameter file for this env -> can't tell
                n["iac"] = {"status": "unknown", "files": sorted({r["file"] for r in declared_types[n["type"]]})}
            else:
                n["iac"] = {"status": "unmanaged", "files": []}

    # ---------------------------------------------------------------- pipelines
    rg_deploys = defaultdict(list)
    for d in pipes.get("deployments", []):
        entry = {k: d.get(k) for k in ("pipeline", "kind", "stage", "environment", "task")}
        if d.get("resource_group") and "$(" not in d["resource_group"]:
            rg_deploys[d["resource_group"].lower()].append(entry)
        for tname in d.get("targets") or []:
            for n in nodes.values():
                if (n["name"] or "").lower() == tname.lower():
                    n["deployed_by"].append(entry)
    for n in nodes.values():
        if not n["deployed_by"]:
            n["deployed_by"] = []

    # ---------------------------------------------------------------- app map for review
    app_map = defaultdict(lambda: {"resources": 0, "apps": defaultdict(int), "envs": defaultdict(int)})
    for n in nodes.values():
        if n["type"].endswith("/subnets") or n.get("external"):
            continue
        m = app_map[n["rg"]]
        m["resources"] += 1
        m["apps"][f"{n['app']} ({n['app_source']})"] += 1
        m["envs"][f"{n['env']} ({n['env_source']})"] += 1
    app_map_out = {rg: {"resources": v["resources"], "apps": dict(v["apps"]), "envs": dict(v["envs"]),
                        "pipelines": sorted({f"{d['pipeline']} [{d['stage']}]" for d in rg_deploys.get(rg.lower(), [])})}
                   for rg, v in sorted(app_map.items())}
    write_json(os.path.join(dd, "app-map.json"), app_map_out)

    # dedupe edges
    seen, uniq = set(), []
    for e in edges:
        k = (e["src"], e["dst"], e["rel"])
        if k not in seen and e["src"] in nodes and e["dst"] in nodes:
            seen.add(k)
            uniq.append(e)

    graph = {"nodes": list(nodes.values()), "edges": uniq, "rg_deployments": rg_deploys,
             "has_iac": bool(iac and iac.get("resources"))}
    write_json(os.path.join(dd, "graph.json"), graph)

    apps = defaultdict(set)
    for n in nodes.values():
        if not n["type"].endswith("/subnets"):
            apps[n["app"]].add(n["env"])
    rels = defaultdict(int)
    for e in uniq:
        rels[e["rel"]] += 1
    print(json.dumps({
        "nodes": len(nodes), "edges": dict(rels),
        "apps_detected": {k: sorted(v) for k, v in sorted(apps.items())},
        "env_from_default_rule": sum(1 for n in nodes.values() if n["env_source"] == "default"),
        "unmanaged_by_iac": sum(1 for n in nodes.values() if (n["iac"] or {}).get("status") == "unmanaged"),
        "review": os.path.join(dd, "app-map.json"),
    }, indent=2))


if __name__ == "__main__":
    main()
