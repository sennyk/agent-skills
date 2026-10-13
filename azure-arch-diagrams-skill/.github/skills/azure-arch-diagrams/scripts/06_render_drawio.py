#!/usr/bin/env python3
"""Step 6 - Render data/graph.json into editable draw.io files with the built-in Azure icons.

Outputs (in output_dir):
  network-topology.drawio   page "Overview" (all VNets + peerings) and one page per environment:
                            VNet > subnet containers, resources placed in their subnet, NSG/UDR badges,
                            public IPs on top, PaaS behind private endpoints on the right, private DNS below.
  application-view.drawio   one page per application: environment lanes (dev | qa | prod) > resource
                            groups > tier rows (Ingress, Compute, Integration, Data, Security, Monitoring),
                            dependency edges, shared/external dependencies, and the pipelines that deploy it.

Every shape carries metadata (resource id, type, env, IaC status) - see Edit > Edit Data in draw.io.
Resources not found in IaC get a dashed red border.

Usage: python 06_render_drawio.py [--view network|app|both] [--app NAME] [--env dev]
"""
import argparse
import html
import os
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import load_config, data_dir, read_json, short_type  # noqa: E402

ICON = 48
CW, CH = 170, 105          # grid cell for one icon + label
PAD, HEAD = 20, 46
IMG = "img/lib/azure2/"

ENV_COLORS = {"dev": ("#E8F5E9", "#2E7D32"), "qa": ("#FFF8E1", "#B7791F"), "prod": ("#FDECEA", "#C62828"),
              "shared": ("#E3F2FD", "#1565C0")}
EDGE_STYLES = {
    "peering": ("endArrow=none;startArrow=none;dashed=1;strokeColor=#0078D4;strokeWidth=2;", "peering"),
    "private_link": ("dashed=1;endArrow=open;strokeColor=#7A3E9D;", "private link"),
    "vnet_integration": ("dashed=1;endArrow=open;strokeColor=#107C10;", "VNet integration"),
    "hosted_on": ("dashed=1;dashPattern=1 3;endArrow=none;strokeColor=#888888;", ""),
    "routes_to": ("endArrow=block;strokeColor=#0078D4;strokeWidth=2;", ""),
    "uses": ("endArrow=open;strokeColor=#333333;", ""),
    "logs_to": ("dashed=1;endArrow=open;strokeColor=#9E9E9E;", "telemetry"),
    "public_ip": ("endArrow=none;strokeColor=#D83B01;", ""),
    "udr": ("dashed=1;endArrow=block;strokeColor=#D83B01;", "UDR"),
    "service_endpoint": ("dashed=1;dashPattern=1 4;endArrow=open;strokeColor=#008575;", "service endpoint"),
    "dns_link": ("dashed=1;endArrow=none;strokeColor=#BDBDBD;", ""),
    "database": ("endArrow=none;strokeColor=#888888;", ""),
    "deploys": ("dashed=1;endArrow=block;strokeColor=#5C2D91;", ""),
}
EDGE_BASE = "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;fontSize=10;labelBackgroundColor=#FFFFFF;"


class Page:
    def __init__(self, name):
        self.name = name
        self.root = ET.Element("root")
        ET.SubElement(self.root, "mxCell", id="0")
        ET.SubElement(self.root, "mxCell", id="1", parent="0")
        self.n = 0
        self.cell_of = {}     # graph node id -> cell id
        self.maxx = self.maxy = 0

    def nid(self):
        self.n += 1
        return f"c{self.n}"

    def _geom(self, cell, x, y, w, h, parent):
        ET.SubElement(cell, "mxGeometry", x=str(int(x)), y=str(int(y)), width=str(int(w)),
                      height=str(int(h)), attrib={"as": "geometry"})
        if parent == "1":
            self.maxx, self.maxy = max(self.maxx, x + w), max(self.maxy, y + h)

    def box(self, label, x, y, w, h, style, parent="1", meta=None):
        cid = self.nid()
        if meta:
            obj = ET.SubElement(self.root, "object", id=cid, label=label, **{k: str(v) for k, v in meta.items()})
            cell = ET.SubElement(obj, "mxCell", style=style, vertex="1", parent=parent)
        else:
            cell = ET.SubElement(self.root, "mxCell", id=cid, value=label, style=style, vertex="1", parent=parent)
        self._geom(cell, x, y, w, h, parent)
        return cid

    def icon(self, node, x, y, parent="1", extra=""):
        name = node.get("name") or ""
        short = name if len(name) <= 28 else name[:26] + "…"
        sub = node.get("info") or short_type(node.get("type"))
        label = f"{html.escape(short)}<br><font style=\"font-size:9px\" color=\"#666666\">{html.escape(str(sub)[:40])}</font>{extra}"
        iac = (node.get("iac") or {}).get("status", "")
        style = (f"image;aspect=fixed;html=1;points=[];align=center;verticalLabelPosition=bottom;"
                 f"verticalAlign=top;fontSize=11;image={IMG}{node['icon']};")
        if iac == "unmanaged":
            style += "imageBorder=#D32F2F;dashed=1;"
        deployed = "; ".join(sorted({f"{d.get('pipeline')} [{d.get('stage')}]" for d in node.get("deployed_by") or []}))
        tip = (f"{name}\n{node.get('type')}\nRG: {node.get('rg')}\nenv: {node.get('env')} ({node.get('env_source')})"
               f"\napp: {node.get('app')}\nIaC: {iac or 'n/a'}"
               + (f" {', '.join((node.get('iac') or {}).get('files', [])[:3])}" if iac == "managed" else "")
               + (f"\ndeployed by: {deployed}" if deployed else ""))
        meta = {"tooltip": tip, "resourceId": node["id"], "resourceType": node.get("type"), "resourceGroup": node.get("rg"),
                "environment": node.get("env"), "iac": iac or "n/a"}
        cid = self.box(label, x + (CW - ICON) / 2, y, ICON, ICON, style, parent, meta)
        self.cell_of[node["id"]] = cid
        return cid

    def badge(self, icon_path, x, y, parent, tip):
        return self.box("", x, y, 22, 22, f"image;aspect=fixed;html=1;points=[];image={IMG}{icon_path};",
                        parent, {"tooltip": tip})

    def edge(self, src_cell, dst_cell, rel, label=None, straight=False):
        if not src_cell or not dst_cell or src_cell == dst_cell:
            return
        style, default_label = EDGE_STYLES.get(rel, EDGE_STYLES["uses"])
        if straight:
            style = "edgeStyle=none;" + style
        cell = ET.SubElement(self.root, "mxCell", id=self.nid(), value=html.escape(label if label is not None else default_label),
                             style=(EDGE_BASE.replace("edgeStyle=orthogonalEdgeStyle;", "") if straight else EDGE_BASE) + style, edge="1", parent="1", source=src_cell, target=dst_cell)
        ET.SubElement(cell, "mxGeometry", relative="1", attrib={"as": "geometry"})

    def text(self, label, x, y, w=600, h=30, size=18, bold=True):
        return self.box(label, x, y, w, h, f"text;html=1;align=left;verticalAlign=middle;fontSize={size};"
                        f"fontStyle={1 if bold else 0};")

    def xml(self):
        diag = ET.Element("diagram", id=self.name.replace(" ", "_")[:40], name=self.name[:60])
        model = ET.SubElement(diag, "mxGraphModel", dx="1400", dy="900", grid="1", gridSize="10", guides="1",
                              tooltips="1", connect="1", arrows="1", fold="1", page="1", pageScale="1",
                              pageWidth=str(int(max(1169, self.maxx + 80))),
                              pageHeight=str(int(max(827, self.maxy + 80))), math="0", shadow="0")
        model.append(self.root)
        return diag


def container_style(fill, stroke, dashed=False, size=13):
    return (f"rounded=1;arcSize=2;whiteSpace=wrap;html=1;container=1;collapsible=0;recursiveResize=0;"
            f"fillColor={fill};strokeColor={stroke};verticalAlign=top;align=left;spacingLeft=34;spacingTop=4;"
            f"fontStyle=1;fontSize={size};" + ("dashed=1;" if dashed else ""))


def legend(page, x, y, icon_map):
    items = [("routes_to", "traffic"), ("private_link", "private endpoint"), ("vnet_integration", "VNet integration"),
             ("peering", "VNet peering"), ("udr", "UDR to firewall/NVA"), ("service_endpoint", "service endpoint"),
             ("uses", "app dependency"), ("logs_to", "telemetry"), ("deploys", "deployed by pipeline")]
    h = 30 + 22 * len(items) + 30
    lid = page.box("Legend", x, y, 230, h, container_style("#FFFFFF", "#9E9E9E", size=11))
    for i, (rel, text) in enumerate(items):
        yy = 34 + i * 22
        a = page.box("", 12, yy, 2, 2, "point;", lid)
        b = page.box("", 70, yy, 2, 2, "point;", lid)
        page.edge(a, b, rel, "")
        page.box(html.escape(text), 80, yy - 9, 140, 18, "text;html=1;fontSize=10;align=left;", lid)
    page.box("<font color=\"#D32F2F\">dashed red border = not found in IaC</font>", 10, h - 28, 215, 20,
             "text;html=1;fontSize=10;align=left;", lid)


# ======================================================================= network view
def render_network(graph, cfg, icon_map, env_filter=None):
    nodes = {n["id"]: n for n in graph["nodes"]}
    hide = set(cfg["network_view"]["hide_types"])
    vnets = [n for n in nodes.values() if n["type"] == "microsoft.network/virtualnetworks"]
    subnets_of = defaultdict(list)
    for n in nodes.values():
        if n["type"].endswith("/subnets"):
            subnets_of[n["vnet"]].append(n)
    in_subnet = defaultdict(list)
    for n in nodes.values():
        if n["type"] in hide or n["type"] == "microsoft.network/networkinterfaces":
            continue
        if n.get("subnet") and n["subnet"] in nodes:
            in_subnet[n["subnet"]].append(n)
        elif n.get("integration_subnet") and n["integration_subnet"] in nodes:
            in_subnet[n["integration_subnet"]].append(dict(n, _integrated=True))
    edges = graph["edges"]
    pages = []

    # ---------------- overview
    ov = Page("Overview - all VNets")
    ov.text("Network overview - all environments", 40, 10)
    order = cfg["env"]["order"] + ["shared"]
    envs_present = sorted({v["env"] for v in vnets}, key=lambda e: order.index(e) if e in order else 99)
    # shared/hub VNets in a left column, environment lanes stacked on the right so peerings fan out
    y_next = 60
    x_env = 40 + (CW + 2 * PAD + 220 if "shared" in envs_present else 0)
    for env in ["shared"] + [e for e in envs_present if e != "shared"]:
        if env not in envs_present:
            continue
        evn = [v for v in vnets if v["env"] == env]
        fill, stroke = ENV_COLORS.get(env, ("#F5F5F5", "#757575"))
        cols = min(len(evn), 4)
        rows = -(-len(evn) // 4)
        w, h = cols * CW + 2 * PAD, HEAD + rows * (CH + 10) + PAD
        lx, ly = (40, 60) if env == "shared" else (x_env, y_next)
        lane = ov.box(env.upper(), lx, ly, w, h, container_style(fill, stroke))
        for i, v in enumerate(evn):
            ov.icon(dict(v, info=f"{v.get('info', '')}  ({len(subnets_of[v['id']])} subnets)"),
                    PAD + (i % 4) * CW, HEAD + (i // 4) * (CH + 10), lane)
        if env != "shared":
            y_next += h + 40
    x = ov.maxx + 80
    for e in edges:
        if e["rel"] == "peering":
            ov.edge(ov.cell_of.get(e["src"]), ov.cell_of.get(e["dst"]), "peering", e.get("label") or "", straight=True)
    legend(ov, x, 60, icon_map)
    pages.append(ov)

    # ---------------- per environment
    for env in envs_present:
        if env_filter and env != env_filter:
            continue
        if env == "shared" and len(envs_present) > 1:
            continue  # shared VNets are drawn on every environment page they connect to
        page_vnets = [v for v in vnets if v["env"] == env]
        peered = {e["dst"] if e["src"] in {v["id"] for v in page_vnets} else e["src"]
                  for e in edges if e["rel"] == "peering" and
                  (e["src"] in {v["id"] for v in page_vnets} or e["dst"] in {v["id"] for v in page_vnets})}
        page_vnets = [v for v in vnets if v["env"] == "shared" and v["id"] in peered] + page_vnets
        if not page_vnets:
            continue
        pg = Page(f"Network - {env}")
        pg.text(f"Network topology - {env.upper()}", 40, 10)
        top_y = 150  # leave a row for public IPs
        x = 40
        vnet_boxes = {}
        for v in page_vnets:
            subs = sorted(subnets_of[v["id"]], key=lambda s: s.get("info") or "")
            cols = max([min(len(in_subnet[s["id"]]), 5) for s in subs] + [2])
            w = max(PAD * 2 + cols * CW, 420)
            heights = [HEAD + max(1, -(-len(in_subnet[s["id"]]) // 5)) * CH + 10 if in_subnet[s["id"]] else 62
                       for s in subs]
            h = HEAD + sum(heights) + 12 * len(subs) + PAD
            fill, stroke = ("#EAF4FC", "#0078D4")
            label = (f"{html.escape(v['name'])}<br><font style=\"font-size:10px;font-weight:normal\">"
                     f"{html.escape(v.get('info') or '')} · RG {html.escape(v['rg'])}</font>")
            vid = pg.box(label, x, top_y, w + 2 * PAD, h, container_style(fill, stroke, v.get("external", False)),
                         meta={"tooltip": v["id"], "resourceId": v["id"]})
            pg.badge(icon_map["microsoft.network/virtualnetworks"], 6, 6, vid, "Virtual network")
            pg.cell_of[v["id"]] = vid
            vnet_boxes[v["id"]] = vid
            sy = HEAD + 8
            for s, sh in zip(subs, heights):
                extras = []
                if s.get("delegation"):
                    extras.append(f"delegated: {s['delegation']}")
                if s.get("service_endpoints"):
                    extras.append("SE: " + ", ".join(x_.split(".")[-1] for x_ in s["service_endpoints"]))
                slabel = (f"{html.escape(s['name'])}  <font style=\"font-weight:normal\">{html.escape(s.get('info') or '')}</font>"
                          + (f"<br><font style=\"font-size:9px;font-weight:normal\" color=\"#555555\">{html.escape(' · '.join(extras))}</font>" if extras else ""))
                sid = pg.box(slabel, PAD, sy, w, sh, container_style("#FFFFFF", "#6C757D", True, 11), vid,
                             meta={"tooltip": s["id"], "resourceId": s["id"]})
                pg.badge(icon_map["_subnet"], 6, 6, sid, "Subnet")
                pg.cell_of[s["id"]] = sid
                bx = w - 30
                for key, ic, tipname in (("nsg", "microsoft.network/networksecuritygroups", "NSG"),
                                         ("route_table", "microsoft.network/routetables", "Route table")):
                    if s.get(key):
                        ref = nodes.get(s[key], {"name": s[key].split("/")[-1]})
                        pg.badge(icon_map[ic], bx, 6, sid, f"{tipname}: {ref['name']}")
                        pg.box(f"<font style=\"font-size:9px\">{html.escape(ref['name'][:24])}</font>", bx - 130, 8, 125, 18,
                               "text;html=1;align=right;fontSize=9;", sid)
                        bx -= 160
                for i, r in enumerate(sorted(in_subnet[s["id"]], key=lambda r: (r["type"], r["name"]))):
                    extra = ""
                    if r.get("_integrated"):
                        extra = "<br><font style=\"font-size:9px\" color=\"#107C10\">VNet integration</font>"
                    elif r["type"] == "microsoft.network/privateendpoints" and r.get("pe_target") in nodes:
                        extra = f"<br><font style=\"font-size:9px\" color=\"#7A3E9D\">→ {html.escape(nodes[r['pe_target']]['name'][:26])}</font>"
                    pg.icon(r, PAD + (i % 5) * CW, HEAD + (i // 5) * CH, sid, extra)
                sy += sh + 12
            x += w + 2 * PAD + 160
        right_x = x
        bottom = pg.maxy + 80

        on_page = set(pg.cell_of)
        # public IPs above their owner
        pips = [(nodes[e["src"]], e["dst"]) for e in edges
                if e["rel"] == "public_ip" and e["dst"] in on_page and e["src"] in nodes]
        for i, (pip, owner) in enumerate(pips):
            pg.icon(pip, 40 + i * CW, 40)
            pg.edge(pg.cell_of[pip["id"]], pg.cell_of[owner], "public_ip")

        # PaaS / external targets on the right: private link + service endpoint targets
        targets = []
        for e in edges:
            if e["rel"] in ("private_link", "service_endpoint", "udr") and e["src"] in on_page and e["dst"] not in pg.cell_of:
                if e["dst"] not in [t["id"] for t in targets] and e["dst"] in nodes:
                    targets.append(nodes[e["dst"]])
        if targets:
            col_h = len(targets) * CH
            cont_id = pg.box("PaaS behind private / service endpoints", right_x, top_y, CW + 2 * PAD,
                             col_h + HEAD + PAD, container_style("#FAFAFA", "#9E9E9E", True, 11))
            for i, t in enumerate(targets):
                pg.icon(t, PAD, HEAD + i * CH, cont_id)
            right_x += CW + 2 * PAD + 80
        for e in edges:
            if e["rel"] in ("private_link", "service_endpoint", "udr", "vnet_integration", "peering", "routes_to"):
                if e["rel"] == "vnet_integration":
                    continue  # shown by placing the app inside its integration subnet
                pg.edge(pg.cell_of.get(e["src"]), pg.cell_of.get(e["dst"]), e["rel"],
                        (e.get("label") or None) if e["rel"] != "udr" else f"UDR {e.get('label', '')}")

        # private DNS zones linked to VNets on this page
        zones = []
        for e in edges:
            if e["rel"] == "dns_link" and e["dst"] in vnet_boxes and e["src"] in nodes and e["src"] not in [z["id"] for z in zones]:
                zones.append(nodes[e["src"]])
        if zones:
            zc = pg.box("Private DNS zones", 40, bottom, min(len(zones), 6) * CW + 2 * PAD,
                        HEAD + -(-len(zones) // 6) * CH + PAD, container_style("#FAFAFA", "#BDBDBD", True, 11))
            for i, z in enumerate(zones):
                pg.icon(dict(z, info=""), PAD + (i % 6) * CW, HEAD + (i // 6) * CH, zc)
            for e in edges:
                if e["rel"] == "dns_link":
                    pg.edge(pg.cell_of.get(e["src"]), pg.cell_of.get(e["dst"]), "dns_link")
        legend(pg, right_x, top_y, icon_map)
        pages.append(pg)
    return pages


# ======================================================================= application view
def render_apps(graph, cfg, icon_map, app_filter=None):
    nodes = {n["id"]: n for n in graph["nodes"]}
    hide = set(cfg["app_view"]["hide_types"]) | {"microsoft.network/virtualnetworks/subnets",
                                                 "microsoft.network/virtualnetworks"}
    rg_deploys = graph.get("rg_deployments") or {}
    order = cfg["env"]["order"] + ["shared"]
    tier_order = ["Ingress", "Compute", "Integration", "Data", "Security & identity", "Networking", "Monitoring", "Other"]

    by_app = defaultdict(list)
    for n in nodes.values():
        if n["type"] in hide or n.get("external"):
            continue
        by_app[n.get("app_view_app") or n["app"]].append(n)

    pages = []
    for app in sorted(by_app):
        if app_filter and app != app_filter:
            continue
        members = by_app[app]
        if not any(m["tier"] in ("Compute", "Data", "Integration") for m in members):
            continue  # pure networking / monitoring groups are covered by the network view
        member_ids = {m["id"] for m in members}
        pg = Page(f"App - {app}"[:60])
        pg.text(f"Application view - {app}", 40, 10)
        x0, y0 = 40, 190
        x = x0
        env_lanes = defaultdict(list)
        for m in members:
            env_lanes[m.get("app_view_env") or m["env"]].append(m)
        rg_cells = {}
        for env in sorted(env_lanes, key=lambda e: order.index(e) if e in order else 99):
            fill, stroke = ENV_COLORS.get(env, ("#F5F5F5", "#757575"))
            by_rg = defaultdict(list)
            for m in env_lanes[env]:
                by_rg[m["rg"]].append(m)
            # measure
            rg_layout = []
            lane_w = 0
            for rg in sorted(by_rg):
                rows = []
                for tier in tier_order:
                    items = sorted([m for m in by_rg[rg] if m["tier"] == tier], key=lambda m: (m["type"], m["name"]))
                    for k in range(0, len(items), 4):
                        rows.append((tier if k == 0 else "", items[k:k + 4]))
                w = 120 + max(len(r[1]) for r in rows) * CW + PAD
                h = HEAD + len(rows) * CH + 10
                rg_layout.append((rg, rows, w, h))
                lane_w = max(lane_w, w)
            lane_h = HEAD + sum(h + 20 for _, _, _, h in rg_layout) + PAD
            lane = pg.box(env.upper(), x, y0, lane_w + 2 * PAD, lane_h, container_style(fill, stroke, size=14))
            ry = HEAD
            for rg, rows, w, h in rg_layout:
                rid = pg.box(html.escape(rg), PAD, ry, lane_w, h, container_style("#FFFFFF", "#9E9E9E", True, 11), lane,
                             meta={"tooltip": f"Resource group {rg}"})
                pg.badge(icon_map["_resourcegroup"], 6, 6, rid, "Resource group")
                rg_cells[(rg.lower(), env)] = rid
                for ri, (tier, items) in enumerate(rows):
                    if tier:
                        pg.box(f"<font color=\"#757575\">{html.escape(tier)}</font>", 8, HEAD + ri * CH + 14, 105, 20,
                               "text;html=1;fontSize=10;align=left;fontStyle=2;", rid)
                    for ci, m in enumerate(items):
                        extra = ""
                        if m.get("integration_subnet"):
                            sn = nodes.get(m["integration_subnet"], {})
                            vn = nodes.get(sn.get("vnet"), {})
                            extra = (f"<br><font style=\"font-size:9px\" color=\"#107C10\">VNet: "
                                     f"{html.escape(str(vn.get('name', ''))[:18])}/{html.escape(str(sn.get('name', ''))[:16])}</font>")
                        pg.icon(m, 110 + ci * CW, HEAD + ri * CH, rid, extra)
                ry += h + 20
            x += lane_w + 2 * PAD + 120

        # shared / external dependencies
        ext = []
        for e in graph["edges"]:
            if e["rel"] in ("dns_link", "service_endpoint", "vnet_integration"):
                continue
            a_in, b_in = e["src"] in member_ids, e["dst"] in member_ids
            other = e["dst"] if a_in and not b_in else e["src"] if b_in and not a_in else None
            if other and other in nodes and nodes[other]["type"] not in hide and other not in [n["id"] for n in ext]:
                ext.append(nodes[other])
        if ext:
            box = pg.box("Shared / other applications", x, y0, CW + 2 * PAD, HEAD + len(ext) * CH + PAD,
                         container_style("#FAFAFA", "#9E9E9E", True, 11))
            for i, n in enumerate(ext):
                pg.icon(dict(n, info=f"{n['app']} · {n['env']}"), PAD, HEAD + i * CH, box)
            x += CW + 2 * PAD + 80
        for e in graph["edges"]:
            if e["rel"] in ("dns_link", "service_endpoint", "vnet_integration", "peering", "udr"):
                continue
            pg.edge(pg.cell_of.get(e["src"]), pg.cell_of.get(e["dst"]), e["rel"], e.get("label") or None)

        # pipelines that deploy this app's resource groups / resources
        pipes = {}
        for (rg_l, env), cell in rg_cells.items():
            for d in rg_deploys.get(rg_l, []):
                pipes.setdefault((d["pipeline"], d.get("kind")), []).append((cell, d.get("stage") or ""))
        for m in members:
            for d in m.get("deployed_by") or []:
                key = (d["pipeline"], d.get("kind"))
                rg_cell = rg_cells.get((m["rg"].lower(), m.get("app_view_env") or m["env"]))
                if m["id"] in pg.cell_of and rg_cell not in [c for c, _ in pipes.get(key, [])]:
                    pipes.setdefault(key, []).append((pg.cell_of[m["id"]], d.get("stage") or ""))
        for i, ((pname, kind), targets) in enumerate(sorted(pipes.items())):
            node = {"id": f"pipeline:{pname}", "name": os.path.basename(pname) if kind == "yaml" else pname, "type": "Azure DevOps pipeline",
                    "info": {"yaml": "YAML pipeline", "classic-release": "Classic release", "classic-build": "Classic build"}.get(kind, kind),
                    "icon": icon_map["_pipeline"], "rg": "", "env": "", "deployed_by": []}
            pc = pg.icon(node, x0 + i * (CW + 30), 60)
            seen = set()
            for cell, stage in targets:
                if (cell, stage) not in seen:
                    seen.add((cell, stage))
                    pg.edge(pc, cell, "deploys", stage)
        legend(pg, x, y0 if not ext else y0 + HEAD + len(ext) * CH + PAD + 40, icon_map)
        pages.append(pg)
    return pages


def write_file(pages, path):
    mx = ET.Element("mxfile", host="azure-arch-diagrams", type="device")
    for p in pages:
        mx.append(p.xml())
    ET.ElementTree(mx).write(path, encoding="utf-8", xml_declaration=False)
    print(f"[ok] wrote {path} ({len(pages)} pages)", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    ap.add_argument("--view", choices=["network", "app", "both"], default="both")
    ap.add_argument("--app")
    ap.add_argument("--env")
    a = ap.parse_args()
    cfg = load_config(a.config)
    graph = read_json(os.path.join(data_dir(cfg), "graph.json"))
    if not graph:
        sys.exit("Run 05_build_graph.py first.")
    icon_map = read_json(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                      "references", "icon_map.json"))
    out = cfg["output_dir"]
    if a.view in ("network", "both"):
        write_file(render_network(graph, cfg, icon_map, a.env), os.path.join(out, "network-topology.drawio"))
    if a.view in ("app", "both"):
        pages = render_apps(graph, cfg, icon_map, a.app)
        if pages:
            write_file(pages, os.path.join(out, "application-view.drawio"))
        else:
            print("[warn] no application pages produced (check app-map.json / --app)", file=sys.stderr)


if __name__ == "__main__":
    main()
