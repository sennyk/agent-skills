#!/usr/bin/env python3
"""Step 3 - Extract resources declared in Bicep and ARM JSON templates.

* .bicep files are compiled with `az bicep build --stdout` (modules are inlined as nested deployments).
* ARM templates are detected by their $schema (deploymentTemplate.json).
* Parameter files (*.parameters.json, *.bicepparam) are attached to their template and tagged with an
  environment detected from the file name (dev / qa / prod ...), so resource names can be resolved
  per environment, e.g. "[format('app-{0}-{1}', parameters('appName'), parameters('env'))]".

The output (data/iac.json) is used to mark each real resource as IaC-managed (and by which file) or
unmanaged (drift / created by hand).

Usage: python 03_extract_iac.py [--path repo1 --path repo2] [--no-bicep-build]
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import az, load_config, data_dir, write_json, detect_env  # noqa: E402

SKIP_DIRS = {".git", "node_modules", "bin", "obj", ".terraform", "arch-diagrams", ".venv", "venv"}


def walk(paths, exts):
    for root_path in paths:
        for root, dirs, files in os.walk(root_path):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                if f.lower().endswith(exts):
                    yield os.path.join(root, f)


def load_json_file(p):
    try:
        with open(p, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None


# ------------------------------------------------------------- tiny ARM expression evaluator
class Unresolved(Exception):
    pass


def split_args(s):
    args, depth, cur, quote = [], 0, "", False
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "'":
            if quote and i + 1 < len(s) and s[i + 1] == "'":
                cur += "''"
                i += 2
                continue
            quote = not quote
        if not quote:
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            elif ch == "," and depth == 0:
                args.append(cur.strip())
                cur = ""
                i += 1
                continue
        cur += ch
        i += 1
    if cur.strip():
        args.append(cur.strip())
    return args


def evaluate(expr, params, variables, depth=0):
    if depth > 20:
        raise Unresolved(expr)
    expr = expr.strip()
    if expr.startswith("'") and expr.endswith("'"):
        return expr[1:-1].replace("''", "'")
    if re.fullmatch(r"-?\d+", expr):
        return int(expr)
    m = re.fullmatch(r"([A-Za-z_][\w]*)\((.*)\)((?:\.[\w]+|\[\d+\])*)", expr, re.S)
    if not m:
        raise Unresolved(expr)
    fn, inner, accessor = m.group(1).lower(), m.group(2), m.group(3)
    args = split_args(inner) if inner.strip() else []
    ev = lambda x: evaluate(x, params, variables, depth + 1)  # noqa: E731
    if fn == "parameters":
        key = ev(args[0])
        if key not in params:
            raise Unresolved(expr)
        val = params[key]
    elif fn == "variables":
        key = ev(args[0])
        if key not in variables:
            raise Unresolved(expr)
        val = resolve_value(variables[key], params, variables, depth + 1)
    elif fn == "format":
        fmt = ev(args[0])
        vals = [ev(x) for x in args[1:]]
        val = re.sub(r"\{(\d+)(:[^}]*)?\}", lambda mm: str(vals[int(mm.group(1))]), fmt)
    elif fn == "concat":
        val = "".join(str(ev(x)) for x in args)
    elif fn == "tolower":
        val = str(ev(args[0])).lower()
    elif fn == "toupper":
        val = str(ev(args[0])).upper()
    elif fn == "replace":
        val = str(ev(args[0])).replace(str(ev(args[1])), str(ev(args[2])))
    elif fn == "string":
        val = str(ev(args[0]))
    elif fn == "take":
        val = ev(args[0])[: int(ev(args[1]))]
    elif fn == "substring":
        s = str(ev(args[0]))
        start = int(ev(args[1]))
        val = s[start: start + int(ev(args[2]))] if len(args) > 2 else s[start:]
    elif fn == "trim":
        val = str(ev(args[0])).strip()
    else:
        # uniqueString(), resourceGroup().location, etc. can't be resolved offline
        raise Unresolved(expr)
    for acc in re.findall(r"\.([\w]+)|\[(\d+)\]", accessor or ""):
        val = val[acc[0]] if acc[0] else val[int(acc[1])]
    return val


def resolve_value(v, params, variables, depth=0):
    if isinstance(v, str) and v.startswith("[") and v.endswith("]") and not v.startswith("[["):
        return evaluate(v[1:-1], params, variables, depth)
    return v


def resolve_name(name, params, variables):
    try:
        val = resolve_value(name, params, variables)
        return str(val) if not isinstance(val, (dict, list)) else None
    except (Unresolved, KeyError, IndexError, ValueError, TypeError):
        return None


# ------------------------------------------------------------- template flattening
def flatten(template, file, chain, out, parent_scopes, bindings=None):
    """parent_scopes: list of {pdefs, vars, bindings} from the root template down to the caller.
    bindings: the `parameters` the calling deployment passes into this (nested) template."""
    scope = {"pdefs": template.get("parameters") or {}, "vars": template.get("variables") or {},
             "bindings": bindings}
    scopes = parent_scopes + [scope]

    def add(r, rtype):
        out.append({"type": rtype.lower(), "name_expr": r.get("name"), "file": file,
                    "module_chain": chain, "scopes": scopes})
        for child in r.get("resources") or []:   # nested child resources (ARM style)
            ctype = child.get("type") or ""
            add(child, ctype if "/" in ctype else f"{rtype}/{ctype}")

    resources = template.get("resources") or []
    if isinstance(resources, dict):  # languageVersion 2.0 (symbolic names)
        resources = list(resources.values())
    for r in resources:
        rtype = r.get("type") or ""
        if rtype.lower() == "microsoft.resources/deployments":
            props = r.get("properties") or {}
            inner = props.get("template") or {}
            if inner:
                flatten(inner, file, chain + [str(r.get("name"))], out, scopes,
                        props.get("parameters") or {})
            continue
        add(r, rtype)
    return out


def param_defaults(pdefs):
    out = {}
    for k, v in (pdefs or {}).items():
        if isinstance(v, dict) and "defaultValue" in v:
            dv = v["defaultValue"]
            if not (isinstance(dv, str) and dv.startswith("[")):
                out[k] = dv
    return out


def resolve_in_scopes(name_expr, scopes, root_values):
    params, variables = dict(param_defaults(scopes[0]["pdefs"]), **root_values), scopes[0]["vars"]
    for sc in scopes[1:]:
        newp = param_defaults(sc["pdefs"])
        for k, b in (sc.get("bindings") or {}).items():
            if isinstance(b, dict) and "value" in b:
                try:
                    newp[k] = resolve_value(b["value"], params, variables)
                except (Unresolved, KeyError, IndexError, ValueError, TypeError):
                    pass
        params, variables = newp, sc["vars"]
    return resolve_name(name_expr, params, variables)


def param_values_from_file(p):
    if p.lower().endswith(".bicepparam"):
        res = az(["bicep", "build-params", "--file", p, "--stdout"], check=False)
        if not res:
            return None
        pj = res.get("parametersJson") if isinstance(res, dict) else None
        data = json.loads(pj) if isinstance(pj, str) else res
    else:
        data = load_json_file(p)
    if not data:
        return None
    params = data.get("parameters") or {}
    return {k: (v.get("value") if isinstance(v, dict) else v) for k, v in params.items()
            if not (isinstance(v, dict) and "reference" in v)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    ap.add_argument("--path", action="append")
    ap.add_argument("--no-bicep-build", action="store_true")
    a = ap.parse_args()
    cfg = load_config(a.config)
    paths = a.path or cfg["iac_paths"]

    templates, param_files, errors = [], [], []
    for p in walk(paths, (".bicep",)):
        if a.no_bicep_build:
            continue
        try:
            out = az(["bicep", "build", "--file", p, "--stdout"], parse_json=False)
            templates.append((p, json.loads(out)))
        except Exception as e:  # noqa: BLE001
            errors.append({"file": p, "error": str(e)[:400]})
    for p in walk(paths, (".json",)):
        d = load_json_file(p)
        if not isinstance(d, dict):
            continue
        schema = str(d.get("$schema", "")).lower()
        if "deploymenttemplate" in schema:
            # skip ARM files that are just compiled copies of a .bicep next to them
            if not os.path.exists(os.path.splitext(p)[0] + ".bicep"):
                templates.append((p, d))
        elif "deploymentparameters" in schema:
            param_files.append(p)
    param_files += list(walk(paths, (".bicepparam",)))

    # attach parameter files to templates (same folder, name prefix or `using` statement)
    def stem(x):
        b = os.path.basename(x).lower()
        for suf in (".parameters.json", ".params.json", ".bicepparam", ".json", ".bicep"):
            if b.endswith(suf):
                b = b[: -len(suf)]
        return b

    pfile_map = {}
    for pf in param_files:
        target = None
        if pf.lower().endswith(".bicepparam"):
            with open(pf, encoding="utf-8") as f:
                m = re.search(r"using\s+'([^']+)'", f.read())
            if m:
                target = os.path.normcase(os.path.normpath(os.path.join(os.path.dirname(pf), m.group(1))))
        cands = []
        for tp, _ in templates:
            if target and os.path.normcase(os.path.normpath(tp)) == target:
                cands = [tp]
                break
            if os.path.dirname(tp) == os.path.dirname(pf) and stem(pf).startswith(stem(tp)):
                cands.append(tp)
        if not cands:
            same_dir = [tp for tp, _ in templates if os.path.dirname(tp) == os.path.dirname(pf)]
            cands = same_dir if len(same_dir) == 1 else []
        for tp in cands[:1]:
            pfile_map.setdefault(tp, []).append(pf)

    resources = []
    for tp, tmpl in templates:
        flat = flatten(tmpl, tp, [], [], [])
        psets = [("defaults", "", {})]
        for pf in pfile_map.get(tp, []):
            vals = param_values_from_file(pf)
            if vals is not None:
                env, _ = detect_env("", os.path.basename(pf), {}, cfg)
                psets.append((env, pf, vals))
        for r in flat:
            names = {}
            for env, pf, vals in psets:
                n = resolve_in_scopes(r["name_expr"], r["scopes"], vals)
                if n:
                    names[env if env != "defaults" else "any"] = n
            resources.append({
                "type": r["type"], "name_expr": r["name_expr"], "resolved_names": names,
                "file": r["file"], "module_chain": r["module_chain"],
                "parameter_files": pfile_map.get(tp, []),
            })

    write_json(os.path.join(data_dir(cfg), "iac.json"),
               {"templates": [t for t, _ in templates], "parameter_files": param_files,
                "resources": resources, "errors": errors})
    print(json.dumps({"templates": len(templates), "parameter_files": len(param_files),
                      "declared_resources": len(resources),
                      "with_resolved_names": sum(1 for r in resources if r["resolved_names"]),
                      "build_errors": len(errors)}, indent=2))


if __name__ == "__main__":
    main()
