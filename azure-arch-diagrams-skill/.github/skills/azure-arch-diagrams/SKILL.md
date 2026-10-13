---
name: azure-arch-diagrams
description: Generate editable draw.io Azure architecture diagrams (network topology and application view, with official Azure icons) from an Azure inventory CSV, Azure Resource Graph, Bicep/ARM templates and Azure DevOps YAML and classic release pipelines. Use when asked to draw, document, diagram or visualize Azure architecture, network topology, VNets/subnets, or how an application and its dev/qa/prod environments are deployed.
---

# Azure architecture diagrams (draw.io)

Produces two editable files with the built-in draw.io Azure icons:

- `arch-diagrams/network-topology.drawio`: an overview page with all VNets and peerings, plus one page per environment. Each environment page shows VNet → subnet containers, the resources inside each subnet, NSG/route-table badges, public IPs, private endpoints → PaaS targets, UDR → firewall, service endpoints, and private DNS zones.
- `arch-diagrams/application-view.drawio`: one page per application. Each page has environment lanes (DEV | QA | PROD) → resource groups → tier rows (Ingress, Compute, Integration, Data, Security, Networking, Monitoring), dependency edges, shared dependencies, and the YAML/classic pipelines that deploy each resource group.

Shapes carry metadata (resource id, type, RG, env, IaC status) as tooltips and in *Edit Data*. Resources not found in the Bicep/ARM code get a **dashed red border** so drift is visible.

All scripts are **read-only** against Azure and Azure DevOps.

## Rules for the agent

- Run the scripts; do not hand-write draw.io XML. To change layout or grouping, change the config and re-run steps 5–6.
- Use only read-only commands (`az graph query`, `az ... show/list`). Never create, update or delete Azure or Azure DevOps resources.
- Never print, log or save secrets, app setting values or connection strings. `--scan-app-settings` matches values in memory and stores only resource ids. Ask the user before enabling it.
- Run commands from the **repository root**. Paths below are relative to it. On Windows use `python` instead of `python3`.
- When a step prints a warning or a summary with surprises (CSV rows not found, tasks without a resource group, many `default` env detections), show it to the user briefly before continuing.

## Step 0: Prerequisites and config (ask only for what is missing)

1. Check the tools:
   ```bash
   python3 --version            # 3.9+
   python3 -m pip install pyyaml
   az version                   # Azure CLI
   az extension add --name resource-graph --upgrade
   az extension add --name azure-devops --upgrade
   az bicep install             # only if .bicep files exist
   az account show              # must be logged in: az login
   ```
2. If `arch-diagrams.config.json` does not exist at the repo root, copy `.github/skills/azure-arch-diagrams/templates/arch-diagrams.config.json` there. Then ask the user only for the values you can't find:
   - path of the inventory CSV
   - folders with Bicep/ARM (`iac_paths`) and pipeline YAML (`pipeline_paths`); search the repo first
   - Azure DevOps `organization` URL and `project` (needed for classic release pipelines)
   - subscription id (otherwise the current `az account` is used)
   - whether to scan Web/Function App settings to discover app → SQL/Key Vault/Storage/Service Bus dependencies
3. Environment rule (default): a resource group or resource name containing `dev` → dev, `qa` → qa, `prod`/`prd` → prod, and **no environment token → prod**. Names containing `hub`, `shared`, `common`, `core`, `mgmt`, `platform` → shared. An `environment`/`env` tag overrides all of this. Change it under `env` in the config.

## Step 1: Import the inventory CSV
```bash
python3 .github/skills/azure-arch-diagrams/scripts/01_import_inventory.py
```
Accepts portal *Export to CSV* (NAME, TYPE, RESOURCE GROUP, …), Resource Graph Explorer downloads, or custom exports. If it can't find the name/resource-group columns, it prints the headers. Map them by adding aliases in the script's `ALIASES`.

## Step 2: Enrich with Azure Resource Graph
```bash
python3 .github/skills/azure-arch-diagrams/scripts/02_enrich_arg.py [--scan-app-settings] [--all-rgs]
```
Fetches full properties (subnets, peerings, private endpoints, VNet integration, App GW backends, route tables, …). By default it is scoped to the resource groups in the CSV; `--all-rgs` covers the whole subscription. It reports CSV rows not found in Azure (deleted or renamed) and resources missing from the CSV. Queries live in `kql/`. `network_summary.kql` and `private_endpoints.kql` are handy for manual checks in Resource Graph Explorer.

## Step 3: Extract Bicep / ARM templates
```bash
python3 .github/skills/azure-arch-diagrams/scripts/03_extract_iac.py
```
Compiles `.bicep` with `az bicep build`, reads ARM templates, and pairs them with `*.parameters.json` / `.bicepparam` files. The parameter file name gives the environment, e.g. `main.dev.parameters.json`. Resource names are resolved per environment, including `format()`/`concat()`/`parameters()`/`variables()` and module parameters. Names using `uniqueString()` or `resourceGroup()` stay unresolved, so those resources show as unmanaged. Mention this to the user if many resources are affected.

## Step 4: Extract Azure DevOps pipelines (YAML + classic)
```bash
python3 .github/skills/azure-arch-diagrams/scripts/04_extract_pipelines.py --org https://dev.azure.com/<org> --project <project>
```
- **YAML pipelines:** parsed from the repo, following `template:` references and substituting `$(var)` and `${{ parameters.x }}`.
- **Classic pipelines:** read with `az pipelines release definition list/show`, `az pipelines build definition list/show` (designer pipelines only) and `az devops service-endpoint list`.

For each deploy task it records the stage/environment, service connection, resource group, target app names and template file. `$(var)` values are resolved from pipeline variables and from the **non-secret** values of the variable groups each pipeline references (`az pipelines variable-group list`); secret values are never read.

If `az devops` asks for login, run `az devops login` or set `AZURE_DEVOPS_EXT_PAT`. Tasks whose resource group is still unresolved, e.g. `$(rgName)`, are listed in the summary. Ask the user whether to add the values to `variable_overrides` in the config, or accept them as unmapped.

## Step 5: Build the graph and review app grouping
```bash
python3 .github/skills/azure-arch-diagrams/scripts/05_build_graph.py
```
Then read `arch-diagrams/data/app-map.json`. It lists each resource group with its detected app, environment and deploying pipelines. Show it to the user as a short table and **ask them to confirm or correct it**. Naming is inconsistent, so grouping is a best guess.

Fix grouping by editing the config, then re-run step 5:
- `app.overrides`: `{ "<regex on RG or resource name>": "<app name>" }`, e.g. `{ "^rg-ord": "orders", "payments|pay-": "payments" }`
- `env.tokens` / `env.shared_tokens` / `env.default`: change environment detection

## Step 6: Render the draw.io files
```bash
python3 .github/skills/azure-arch-diagrams/scripts/06_render_drawio.py              # both views
python3 .github/skills/azure-arch-diagrams/scripts/06_render_drawio.py --view app --app orders
python3 .github/skills/azure-arch-diagrams/scripts/06_render_drawio.py --view network --env prod
```
Tell the user how to open the output: the VS Code extension **Draw.io Integration** (`hediet.vscode-drawio`), draw.io desktop, or app.diagrams.net. Layout is a clean grid. Edges are auto-routed and can be tidied by dragging.

## Re-running
Steps are independent and cached in `arch-diagrams/data/*.json`:
- Config tweaks only: re-run steps 5–6.
- New CSV export: re-run steps 1–2 and 5–6.
- Pipeline changes: re-run step 4 and 5–6.

## Reference files
- `references/icon_map.json`: Azure resource type → draw.io icon (`img/lib/azure2/...`). Add rows for unmapped types; the generic icon is used otherwise.
- `references/how-it-works.md`: which properties create which edges, known limits, and troubleshooting.
- `templates/arch-diagrams.config.json`: config template with every option.
