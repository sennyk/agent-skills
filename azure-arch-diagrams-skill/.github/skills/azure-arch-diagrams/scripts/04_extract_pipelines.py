#!/usr/bin/env python3
"""Step 4 - Extract deployment targets from Azure DevOps pipelines.

YAML pipelines   : parsed from the repo (stages, deployment jobs, environment, tasks, templates).
Classic releases : `az pipelines release definition list/show` (environments -> deploy phases -> tasks).
Classic builds   : `az pipelines build definition list/show` (designer pipelines that deploy).
Service conns    : `az devops service-endpoint list` (name -> subscription).

For every task that deploys something we record: pipeline, stage/environment, service connection,
resource group, target resource names (web app, function app, ...), template file and the
environment (dev/qa/prod) detected from the stage name or resource group.

Usage:
  python 04_extract_pipelines.py --org https://dev.azure.com/ORG --project PROJ [--path repo] [--no-classic]
Requires: pip install pyyaml ; az extension add --name azure-devops ; az devops login (or az login)
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import az, load_config, data_dir, write_json, detect_env  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required: pip install pyyaml")

SKIP_DIRS = {".git", "node_modules", "bin", "obj", "arch-diagrams", ".venv", "venv"}

# task input keys (case-insensitive) that point at deployment targets
CONN_KEYS = {"azuresubscription", "connectedservicename", "connectedservicenamearm",
             "azureresourcemanagerconnection", "azuresubscriptionendpoint", "deploymentscope_connection"}
RG_KEYS = {"resourcegroupname", "resourcegroup", "webappresourcegroupname", "functionappresourcegroupname",
           "resourcegroupnameforcontainer"}
TARGET_KEYS = {"webappname", "appname", "functionappname", "containerappname", "kubernetescluster",
               "slotname_webappname", "apimanagementservicename", "storageaccountname", "staticwebappname",
               "sqlserver", "serverName".lower(), "databasename", "datafactoryname", "vmname", "keyvaultname",
               "aksclustername", "registry", "azurecontainerregistry"}
FILE_KEYS = {"csmfile", "templatelocation_csmfile", "templatefile", "templatepath", "deploymentpackage",
             "package", "scriptpath", "dacpacfile", "packageforlinux"}
DEPLOY_HINT = re.compile(r"deploy|arm|bicep|webapp|functionapp|azurecli|azurepowershell|sql|kubernetes|"
                         r"containerapp|appservice|staticweb|apim|helm|docker", re.I)
CLI_RG = re.compile(r"(?:--resource-group|-g)\s+[\"']?([\w\-\.\$\(\)]+)")
CLI_TPL = re.compile(r"(?:--template-file|-f|-TemplateFile)\s+[\"']?([^\s\"']+\.(?:bicep|json))", re.I)
CLI_NAME = re.compile(r"az\s+(webapp|functionapp|containerapp|staticwebapp|aks|sql\s+db|keyvault|storage\s+account)"
                      r"[^\n]*?(?:--name|-n)\s+[\"']?([\w\-\.\$\(\)]+)")
VAR = re.compile(r"\$\(([\w\.\-]+)\)|\$\{\{\s*(?:variables|parameters)\.([\w\-]+)\s*\}\}")


def subst(text, variables):
    if not isinstance(text, str):
        return text
    for _ in range(3):
        new = VAR.sub(lambda m: str(variables.get(m.group(1) or m.group(2), m.group(0))), text)
        if new == text:
            break
        text = new
    return text


def vars_from_yaml(node):
    out = {}
    v = node.get("variables") if isinstance(node, dict) else None
    if isinstance(v, dict):
        out.update({k: str(x) for k, x in v.items() if not isinstance(x, (dict, list))})
    elif isinstance(v, list):
        for item in v:
            if isinstance(item, dict) and "name" in item:
                out[item["name"]] = str(item.get("value", ""))
            elif isinstance(item, dict) and "group" in item:
                out.update(GROUPS.get(str(item["group"]), {}))
    params = node.get("parameters") if isinstance(node, dict) else None
    if isinstance(params, list):
        for p in params:
            if isinstance(p, dict) and "name" in p and "default" in p and not isinstance(p["default"], (dict, list)):
                out.setdefault(p["name"], str(p["default"]))
    return out


def var_groups(node):
    v = node.get("variables") if isinstance(node, dict) else None
    if isinstance(v, list):
        return [i["group"] for i in v if isinstance(i, dict) and "group" in i]
    return []


def analyse_task(task_name, inputs, script_text, ctx, variables):
    low = {str(k).lower(): subst(v, variables) for k, v in (inputs or {}).items()}
    rec = {"task": task_name, "service_connection": None, "resource_group": None,
           "targets": [], "files": []}
    for k, v in low.items():
        if not isinstance(v, str) or not v:
            continue
        if k in CONN_KEYS:
            rec["service_connection"] = v
        elif k in RG_KEYS:
            rec["resource_group"] = v
        elif k in TARGET_KEYS:
            rec["targets"].append(v)
        elif k in FILE_KEYS:
            rec["files"].append(v)
    script = subst(script_text or "", variables)
    if script:
        m = CLI_RG.findall(script)
        if m and not rec["resource_group"]:
            rec["resource_group"] = m[0]
        rec["files"] += CLI_TPL.findall(script)
        rec["targets"] += [n for _, n in CLI_NAME.findall(script)]
    if not (rec["resource_group"] or rec["targets"] or rec["files"]) and not DEPLOY_HINT.search(task_name or ""):
        return None
    if not (rec["service_connection"] or rec["resource_group"] or rec["targets"]):
        return None
    env_text = " ".join(filter(None, [ctx.get("environment"), ctx.get("stage"), rec["resource_group"]]))
    env, src = detect_env(rec["resource_group"] or "", env_text, {}, CFG)
    if src == "default":
        env = None  # unknown - let the graph builder decide from the resource group
    rec.update({k: ctx.get(k) for k in ("pipeline", "kind", "stage", "job", "environment", "source")})
    rec["env"] = env
    # "sql-orders.database.windows.net" / "https://app-x.azurewebsites.net" -> resource name
    clean = []
    for t in rec["targets"]:
        t = re.sub(r"^https?://", "", t.strip()).split("/")[0]
        if re.search(r"\.(windows\.net|azurewebsites\.net|azure\.com|azurecr\.io|azure-api\.net)$", t, re.I):
            t = t.split(".")[0]
        clean.append(t)
    rec["targets"] = sorted(set(clean))
    rec["files"] = sorted(set(rec["files"]))
    return rec


# ------------------------------------------------------------------ YAML
def find_yaml_pipelines(paths):
    for root_path in paths:
        for root, dirs, files in os.walk(root_path):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                if f.lower().endswith((".yml", ".yaml")):
                    yield os.path.join(root, f)


def load_yaml(p):
    try:
        with open(p, encoding="utf-8-sig") as f:
            return yaml.safe_load(f)
    except Exception:  # noqa: BLE001
        return None


def walk_yaml(node, ctx, variables, base_dir, out, seen):
    if isinstance(node, list):
        for n in node:
            walk_yaml(n, ctx, variables, base_dir, out, seen)
        return
    if not isinstance(node, dict):
        return
    variables = dict(variables, **vars_from_yaml(node))
    ctx = dict(ctx)
    if "stage" in node:
        ctx["stage"] = subst(str(node.get("displayName") or node["stage"]), variables)
    if "deployment" in node or "job" in node:
        ctx["job"] = subst(str(node.get("displayName") or node.get("deployment") or node.get("job")), variables)
    env = node.get("environment")
    if env:
        ctx["environment"] = subst(env if isinstance(env, str) else env.get("name", ""), variables)
    if "task" in node or "script" in node or "pwsh" in node or "powershell" in node or "bash" in node:
        inputs = node.get("inputs") or {}
        script = " ".join(str(inputs.get(k, "")) for k in ("inlineScript", "Inline", "script", "ScriptArguments",
                                                           "scriptArguments", "arguments"))
        for k in ("script", "pwsh", "powershell", "bash"):
            if isinstance(node.get(k), str):
                script += "\n" + node[k]
        rec = analyse_task(str(node.get("task") or node.get("displayName") or "script"), inputs, script, ctx, variables)
        if rec:
            out.append(rec)
    tpl = node.get("template")
    if isinstance(tpl, str) and "@" not in tpl:
        tpath = os.path.normpath(os.path.join(base_dir, subst(tpl, variables)))
        if os.path.exists(tpath) and tpath not in seen:
            tvars = dict(variables)
            for k, v in (node.get("parameters") or {}).items() if isinstance(node.get("parameters"), dict) else []:
                if not isinstance(v, (dict, list)):
                    tvars[k] = subst(str(v), variables)
            tdoc = load_yaml(tpath)
            if tdoc is not None:
                walk_yaml(tdoc, ctx, tvars, os.path.dirname(tpath), out, seen | {tpath})
    for k, v in node.items():
        if k in ("stages", "jobs", "steps", "extends", "preDeploy", "deploy", "routeTraffic", "postRouteTraffic",
                 "on", "success", "failure", "strategy", "runOnce", "rolling", "canary") or \
                (isinstance(k, str) and k.startswith("${{")):
            walk_yaml(v, ctx, variables, base_dir, out, seen)


def extract_yaml(paths):
    out, files = [], []
    candidates = list(find_yaml_pipelines(paths))
    referenced = set()  # files used as templates by other pipelines are walked from their caller only
    for p in candidates:
        with open(p, encoding="utf-8-sig", errors="ignore") as f:
            for m in re.finditer(r"template:\s*['\"]?([^\s'\"@#]+\.ya?ml)", f.read()):
                referenced.add(os.path.normcase(os.path.normpath(os.path.join(os.path.dirname(p), m.group(1)))))
    for p in candidates:
        if os.path.normcase(os.path.normpath(p)) in referenced:
            continue
        doc = load_yaml(p)
        if not isinstance(doc, dict) or not any(k in doc for k in ("stages", "jobs", "steps", "extends")):
            continue
        files.append(p)
        ctx = {"pipeline": os.path.relpath(p), "kind": "yaml", "source": p}
        walk_yaml(doc, ctx, dict(CFG.get("variable_overrides") or {}), os.path.dirname(p), out,
                  {os.path.normpath(p)})
    return out, files


# ------------------------------------------------------------------ classic
def classic_release(org, project):
    out, defs = [], []
    lst = az(["pipelines", "release", "definition", "list", "--org", org, "--project", project], check=False) or []
    for d in lst:
        full = az(["pipelines", "release", "definition", "show", "--id", str(d["id"]),
                   "--org", org, "--project", project], check=False)
        if not full:
            continue
        defs.append({"id": full["id"], "name": full["name"],
                     "artifacts": [a.get("alias") for a in full.get("artifacts") or []]})
        out += parse_release_definition(full)
    return out, defs


def parse_release_definition(full):
    out = []
    dvars = dict(CFG.get("variable_overrides") or {})
    for gid in full.get("variableGroups") or []:
        dvars.update(GROUPS.get(str(gid), {}))
    dvars.update({k: (v or {}).get("value", "") for k, v in (full.get("variables") or {}).items()
                  if (v or {}).get("value") is not None})
    for env in full.get("environments") or []:
        evars = dict(dvars)
        for gid in env.get("variableGroups") or []:
            evars.update(GROUPS.get(str(gid), {}))
        evars.update({k: (v or {}).get("value", "") for k, v in (env.get("variables") or {}).items()
                      if (v or {}).get("value") is not None})
        for phase in env.get("deployPhases") or []:
            for t in phase.get("workflowTasks") or []:
                if t.get("enabled") is False:
                    continue
                inputs = t.get("inputs") or {}
                script = " ".join(str(inputs.get(k, "")) for k in ("inlineScript", "Inline", "script",
                                                                   "ScriptArguments", "scriptArguments", "arguments"))
                ctx = {"pipeline": full["name"], "kind": "classic-release", "stage": env.get("name"),
                       "job": phase.get("name"), "environment": env.get("name"),
                       "source": f"release-definition:{full.get('id')}"}
                rec = analyse_task(t.get("name") or t.get("refName") or "task", inputs, script, ctx, evars)
                if rec:
                    out.append(rec)
    return out


def classic_build(org, project):
    out = []
    lst = az(["pipelines", "build", "definition", "list", "--org", org, "--project", project], check=False) or []
    for d in lst:
        full = az(["pipelines", "build", "definition", "show", "--id", str(d["id"]),
                   "--org", org, "--project", project], check=False)
        if not full or (full.get("process") or {}).get("type") != 1:  # 1 = designer, 2 = YAML
            continue
        bvars = dict(CFG.get("variable_overrides") or {})
        bvars.update({k: (v or {}).get("value", "") for k, v in (full.get("variables") or {}).items()
                      if (v or {}).get("value") is not None})
        for phase in (full.get("process") or {}).get("phases") or []:
            for s in phase.get("steps") or []:
                if s.get("enabled") is False:
                    continue
                inputs = s.get("inputs") or {}
                script = " ".join(str(inputs.get(k, "")) for k in ("inlineScript", "Inline", "script"))
                ctx = {"pipeline": full["name"], "kind": "classic-build", "stage": phase.get("name"),
                       "job": phase.get("name"), "source": f"build-definition:{full['id']}"}
                rec = analyse_task(s.get("displayName") or "task", inputs, script, ctx, bvars)
                if rec:
                    out.append(rec)
    return out


def variable_groups(org, project):
    """Non-secret variable group values (secrets come back as null and are skipped)."""
    res = az(["pipelines", "variable-group", "list", "--org", org, "--project", project], check=False) or []
    out = {}
    for gr in res:
        vals = {k: str(v.get("value")) for k, v in (gr.get("variables") or {}).items()
                if isinstance(v, dict) and not v.get("isSecret") and v.get("value") is not None}
        out[str(gr.get("id"))] = vals
        out[str(gr.get("name"))] = vals
    return out


def service_connections(org, project):
    res = az(["devops", "service-endpoint", "list", "--org", org, "--project", project], check=False) or []
    out = {}
    for e in res:
        data = e.get("data") or {}
        out[e.get("name")] = {"id": e.get("id"), "type": e.get("type"),
                              "subscription_id": data.get("subscriptionId"),
                              "subscription_name": data.get("subscriptionName"),
                              "scope_resource_group": (e.get("authorization") or {}).get("parameters", {}).get("scope")}
    return out


def main():
    global CFG  # noqa: PLW0603
    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    ap.add_argument("--path", action="append")
    ap.add_argument("--org")
    ap.add_argument("--project")
    ap.add_argument("--no-classic", action="store_true")
    ap.add_argument("--release-json", action="append", help="offline: a saved release definition JSON")
    a = ap.parse_args()
    CFG = load_config(a.config)
    org = a.org or CFG["ado"].get("organization")
    project = a.project or CFG["ado"].get("project")

    if not a.no_classic and org and project:
        GROUPS.update(variable_groups(org, project))
    deployments, yaml_files = extract_yaml(a.path or CFG["pipeline_paths"])
    release_defs, conns = [], {}
    for rj in a.release_json or []:
        with open(rj, encoding="utf-8") as f:
            deployments += parse_release_definition(json.load(f))
    if not a.no_classic and org and project:
        rel, release_defs = classic_release(org, project)
        deployments += rel
        deployments += classic_build(org, project)
        conns = service_connections(org, project)
    elif not a.no_classic:
        print("[warn] no --org/--project (or ado section in config): skipping classic pipelines", file=sys.stderr)

    write_json(os.path.join(data_dir(CFG), "pipelines.json"),
               {"yaml_files": yaml_files, "release_definitions": release_defs,
                "service_connections": conns, "deployments": deployments})
    by_kind = {}
    for d in deployments:
        by_kind[d["kind"]] = by_kind.get(d["kind"], 0) + 1
    print(json.dumps({"yaml_files": len(yaml_files), "release_definitions": len(release_defs),
                      "deployment_tasks": by_kind,
                      "tasks_without_resource_group": sum(1 for d in deployments if not d["resource_group"]),
                      "unresolved_variables": sorted({m for d in deployments
                                                      for v in [d["resource_group"] or ""] + d["targets"]
                                                      for m in re.findall(r"\$\([\w\.\-]+\)", v)})},
                     indent=2))


CFG = None
GROUPS = {}
if __name__ == "__main__":
    main()
