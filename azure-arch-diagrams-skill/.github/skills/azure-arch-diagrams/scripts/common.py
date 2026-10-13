"""Shared helpers: config, az CLI calls, environment/app detection, type helpers."""
import json
import os
import re
import shutil
import subprocess
import sys

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_CONFIG = {
    "subscription_id": "",
    "inventory_csv": "inventory.csv",
    "iac_paths": ["."],
    "pipeline_paths": ["."],
    "ado": {"organization": "", "project": ""},
    "output_dir": "arch-diagrams",
    "scope_to_csv_resource_groups": True,
    "scan_app_settings": False,
    "variable_overrides": {},
    "env": {
        "order": ["dev", "qa", "prod"],
        "tokens": {
            "dev": ["dev", "develop", "development"],
            "qa": ["qa"],
            "prod": ["prod", "prd", "production"],
        },
        "default": "prod",
        "tag_keys": ["environment", "env", "Environment", "Env"],
        "shared_tokens": ["hub", "shared", "common", "connectivity", "core", "mgmt", "management", "platform"],
    },
    "app": {
        "tag_keys": ["application", "app", "workload", "Application", "App", "Workload"],
        "overrides": {},
        "strip_tokens": [
            "rg", "res", "resource", "group", "grp", "001", "01", "02",
            "we", "weu", "westeurope", "ne", "neu", "northeurope", "sc", "swc", "sdc",
            "swedencentral", "swedensouth", "eus", "eus2", "eastus", "eastus2", "wus",
            "westus", "uks", "uksouth", "ukw", "ukwest", "cus", "centralus", "frc",
            "francecentral", "gwc", "germanywestcentral", "noe", "norwayeast",
        ],
    },
    "network_view": {"hide_types": ["microsoft.network/networkwatchers"]},
    "app_view": {
        "hide_types": [
            "microsoft.network/networkinterfaces", "microsoft.compute/disks",
            "microsoft.network/networkwatchers", "microsoft.insights/actiongroups",
            "microsoft.insights/metricalerts", "microsoft.insights/scheduledqueryrules",
            "microsoft.alertsmanagement/smartdetectoralertrules",
            "microsoft.compute/virtualmachines/extensions",
            "microsoft.network/privatednszones/virtualnetworklinks",
            "microsoft.portal/dashboards", "microsoft.network/networksecuritygroups",
            "microsoft.network/routetables",
        ]
    },
}


def deep_merge(base, extra):
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path=None):
    path = path or os.environ.get("ARCH_DIAGRAM_CONFIG") or "arch-diagrams.config.json"
    cfg = DEFAULT_CONFIG
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg = deep_merge(DEFAULT_CONFIG, json.load(f))
    else:
        print(f"[info] no config at {path}, using defaults", file=sys.stderr)
    return cfg


def data_dir(cfg):
    d = os.path.join(cfg["output_dir"], "data")
    os.makedirs(d, exist_ok=True)
    return d


def write_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    print(f"[ok] wrote {path}", file=sys.stderr)


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- az CLI
def az(args, parse_json=True, check=True):
    """Run an az CLI command (works on Windows where az is az.cmd)."""
    exe = shutil.which("az") or shutil.which("az.cmd")
    if not exe:
        raise SystemExit("Azure CLI 'az' not found on PATH. Install it and run 'az login'.")
    cmd = [exe] + list(args)
    if parse_json and "-o" not in args and "--output" not in args:
        cmd += ["-o", "json"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        msg = proc.stderr.strip()
        if check:
            raise RuntimeError(f"az {' '.join(args[:4])} ... failed: {msg}")
        print(f"[warn] az {' '.join(args[:4])} failed: {msg[:300]}", file=sys.stderr)
        return None
    if not parse_json:
        return proc.stdout
    out = proc.stdout.strip()
    return json.loads(out) if out else None


# ---------------------------------------------------------------- naming helpers
def tokens(name):
    """'rg-MyApp_dev01' -> ['rg','myapp','dev','01']"""
    parts = re.split(r"[^a-z0-9]+", (name or "").lower())
    out = []
    for p in parts:
        out += re.findall(r"[a-z]+|\d+", p)
    return [t for t in out if t]


def _env_from_text(text, cfg, compact_ok):
    toks = tokens(text)
    envtok = cfg["env"]["tokens"]
    for env, words in envtok.items():
        if any(w in toks for w in words):
            return env
    if compact_ok:
        # names without separators (storage accounts, ACR): stmyappdev01, devmyappkv
        low = re.sub(r"[^a-z0-9]", "", (text or "").lower())
        low = re.sub(r"\d+$", "", low)
        for env, words in envtok.items():
            for w in words:
                if len(w) >= 3 and (low.endswith(w) or low.startswith(w)):
                    return env
    return None


def is_shared(text, cfg):
    toks = tokens(text)
    return any(t in toks for t in cfg["env"]["shared_tokens"])


def detect_env(rg, name, tags, cfg):
    """Returns (env, source). Order: env tag -> RG name -> resource name -> shared -> default."""
    tags = tags or {}
    for k in cfg["env"]["tag_keys"]:
        if k in tags and tags[k]:
            e = _env_from_text(str(tags[k]), cfg, True)
            if e:
                return e, "tag"
    e = _env_from_text(rg, cfg, False)
    if e:
        return e, "resource-group"
    if "/" not in (name or ""):  # child resources (zone/link-dev) inherit from their RG
        e = _env_from_text(name, cfg, True)
        if e:
            return e, "resource-name"
    if is_shared(rg, cfg) or is_shared(name, cfg):
        return "shared", "shared-token"
    return cfg["env"]["default"], "default"


CAF_PREFIXES = {
    "rg", "app", "func", "fa", "fn", "asp", "plan", "kv", "st", "sa", "sql", "sqldb", "db",
    "cosmos", "cosmo", "redis", "appi", "ai", "log", "law", "vnet", "snet", "nsg", "rt", "pip",
    "pe", "pep", "nic", "vm", "vmss", "agw", "appgw", "afd", "fd", "apim", "aks", "acr", "cr",
    "sb", "sbns", "evh", "evhns", "evgt", "logic", "la", "id", "mi", "uami", "bas", "afw", "fw",
    "lb", "ilb", "ng", "nat", "cae", "ca", "adf", "syn", "dbw", "srch", "oai", "cog", "psql",
    "mysql", "stapp", "swa", "web", "api", "ui", "fe", "be",
}


def detect_app(rg, name, tags, cfg):
    """Best-effort application name. Returns (app, source)."""
    tags = tags or {}
    for pattern, app in cfg["app"].get("overrides", {}).items():
        if re.search(pattern, rg or "", re.I) or re.search(pattern, name or "", re.I):
            return app, "override"
    for k in cfg["app"]["tag_keys"]:
        if tags.get(k):
            return str(tags[k]).lower(), "tag"
    drop = set(cfg["app"]["strip_tokens"])
    for words in cfg["env"]["tokens"].values():
        drop |= set(words)
    rg_toks = [t for t in tokens(rg) if t not in drop and not t.isdigit()]
    if rg_toks:
        return "-".join(rg_toks), "resource-group"
    nm = [t for t in tokens(name) if t not in drop and t not in CAF_PREFIXES and not t.isdigit()]
    if nm:
        return "-".join(nm[:2]), "resource-name"
    return (rg or "unassigned").lower(), "fallback"


# ---------------------------------------------------------------- type helpers
def type_key(rtype, kind=""):
    t = (rtype or "").lower()
    k = (kind or "").lower()
    if t == "microsoft.web/sites":
        if "workflowapp" in k:
            return t + "#workflowapp"
        if "functionapp" in k:
            return t + "#functionapp"
    if t == "microsoft.cognitiveservices/accounts" and "openai" in k:
        return t + "#openai"
    return t


TIERS = [
    ("Ingress", ["microsoft.network/applicationgateways", "microsoft.network/frontdoors", "microsoft.cdn/profiles",
                 "microsoft.network/publicipaddresses", "microsoft.apimanagement/service",
                 "microsoft.network/trafficmanagerprofiles", "microsoft.network/loadbalancers",
                 "microsoft.network/applicationgatewaywebapplicationfirewallpolicies",
                 "microsoft.network/frontdoorwebapplicationfirewallpolicies", "microsoft.web/staticsites"]),
    ("Compute", ["microsoft.web/sites", "microsoft.web/serverfarms", "microsoft.compute/virtualmachines",
                 "microsoft.compute/virtualmachinescalesets", "microsoft.containerservice/managedclusters",
                 "microsoft.app/containerapps", "microsoft.app/managedenvironments",
                 "microsoft.containerinstance/containergroups", "microsoft.batch/batchaccounts",
                 "microsoft.web/hostingenvironments", "microsoft.containerregistry/registries"]),
    ("Integration", ["microsoft.servicebus/namespaces", "microsoft.eventhub/namespaces", "microsoft.eventgrid/topics",
                     "microsoft.eventgrid/systemtopics", "microsoft.eventgrid/domains", "microsoft.logic/workflows",
                     "microsoft.relay/namespaces", "microsoft.signalrservice/signalr", "microsoft.datafactory/factories"]),
    ("Data", ["microsoft.sql/servers", "microsoft.sql/servers/databases", "microsoft.sql/servers/elasticpools",
              "microsoft.sql/managedinstances", "microsoft.documentdb/databaseaccounts",
              "microsoft.dbforpostgresql/flexibleservers", "microsoft.dbformysql/flexibleservers",
              "microsoft.storage/storageaccounts", "microsoft.cache/redis", "microsoft.search/searchservices",
              "microsoft.cognitiveservices/accounts", "microsoft.synapse/workspaces", "microsoft.databricks/workspaces"]),
    ("Security & identity", ["microsoft.keyvault/vaults", "microsoft.managedidentity/userassignedidentities"]),
    ("Networking", ["microsoft.network/privateendpoints", "microsoft.network/virtualnetworks",
                    "microsoft.network/privatednszones", "microsoft.network/networksecuritygroups"]),
    ("Monitoring", ["microsoft.insights/components", "microsoft.operationalinsights/workspaces",
                    "microsoft.insights/workbooks", "microsoft.automation/automationaccounts",
                    "microsoft.recoveryservices/vaults", "microsoft.dashboard/grafana"]),
]


def tier_of(rtype):
    t = (rtype or "").lower()
    for name, types in TIERS:
        if t in types:
            return name
    return "Other"


def short_type(rtype):
    t = (rtype or "").split("/")
    return t[-1] if t else ""


def rid_lower(x):
    return (x or "").lower().rstrip("/")


def parent_id(rid, levels=2):
    """/subscriptions/../providers/microsoft.network/virtualnetworks/v/subnets/s -> vnet id"""
    parts = rid_lower(rid).split("/")
    return "/".join(parts[:-levels]) if len(parts) > levels else ""
