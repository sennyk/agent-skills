# Azure Architecture Diagrams (GitHub Copilot agent skill)

Generates **editable draw.io diagrams with official Azure icons** from what already exists:

* your **Azure inventory CSV**, enriched with **Azure Resource Graph** (`az graph query`)
* **Bicep and ARM templates** in your repos
* **Azure DevOps pipelines**: YAML files in the repo and **classic release/build pipelines** (via `az pipelines`)

Two views are produced:

|File|What it shows|
|-|-|
|`arch-diagrams/network-topology.drawio`|Overview of all VNets and peerings, plus one page per environment (dev / qa / prod): VNet → subnets → the resources inside each subnet. Also NSG and route-table badges, UDRs to the firewall, public IPs, private endpoints → PaaS services, service endpoints and private DNS zones.|
|`arch-diagrams/application-view.drawio`|One page per application with DEV \| QA \| PROD lanes → resource groups → tiers (Ingress, Compute, Integration, Data, Security, Networking, Monitoring). Shows dependencies, shared services, and the pipelines (and stages) that deploy each environment.|

Every shape carries metadata (resource id, type, resource group, environment, IaC status), visible as a tooltip or in *Edit → Edit Data*. Resources **not found in Bicep/ARM** get a **dashed red border**, so drift is easy to spot.

The skill is **read-only**: it never creates, changes or deletes anything in Azure or Azure DevOps.

\---

## Contents

1. [Install](#install)
2. [Prerequisites](#prerequisites)
3. [Quick start with Copilot](#quick-start-with-copilot)
4. [Running the scripts manually](#running-the-scripts-manually)
5. [Configuration](#configuration)
6. [How environments and applications are detected](#how-environments-and-applications-are-detected)
7. [Viewing and editing the diagrams](#viewing-and-editing-the-diagrams)
8. [Folder structure](#folder-structure)
9. [Updating the skill](#updating-the-skill)
10. [Security notes](#security-notes)
11. [Limitations and troubleshooting](#limitations-and-troubleshooting)

\---

## Install

Copy the folder into your repository so the path is:

```
<repo-root>/.github/skills/azure-arch-diagrams/SKILL.md
```

VS Code Copilot (agent mode) discovers skills in `.github/skills/` automatically. To use the skill in every workspace, place it in `\~/.copilot/skills/azure-arch-diagrams/` instead.

## Prerequisites

|Tool|Command|
|-|-|
|Python 3.9+ and PyYAML|`python -m pip install pyyaml`|
|Azure CLI, logged in|`az login` then `az account set -s <subscription-id>`|
|Resource Graph extension|`az extension add --name resource-graph`|
|Azure DevOps extension|`az extension add --name azure-devops`, then `az devops login` (or set `AZURE\_DEVOPS\_EXT\_PAT`)|
|Bicep CLI (only if you have `.bicep` files)|`az bicep install`|
|draw.io viewer|VS Code extension **Draw.io Integration** (`hediet.vscode-drawio`), draw.io desktop, or app.diagrams.net|

**Permissions**

* **Azure:** *Reader* on the subscription.
* **Azure DevOps:** read access to Build, Release, Service connections and Variable groups.

## Quick start with Copilot

1. Put your inventory export in the repo, e.g. `azure-inventory.csv`. The Azure portal *All resources → Export to CSV* format works as-is.
2. Open Copilot Chat in **agent mode** and ask, for example:

   * *"Generate the network topology and application view diagrams from azure-inventory.csv"*
   * *"Draw the application view for the orders app, prod only"*
   * *"Refresh the Azure architecture diagrams"*
3. Copilot will:

   * check the prerequisites
   * create `arch-diagrams.config.json` and ask for any values it can't find: the Azure DevOps org/project, and whether to scan app settings
   * run the six steps
   * show you the **detected app and environment per resource group** and ask you to confirm or correct it
   * render the `.drawio` files

## Running the scripts manually

Run from the repository root (use `python3` on macOS/Linux):

```bash
S=.github/skills/azure-arch-diagrams/scripts
cp .github/skills/azure-arch-diagrams/templates/arch-diagrams.config.json .   # first time only, then edit

python $S/01\_import\_inventory.py                  # CSV  -> data/inventory.json
python $S/02\_enrich\_arg.py                        # az graph query -> data/arg.json
python $S/03\_extract\_iac.py                       # Bicep/ARM -> data/iac.json
python $S/04\_extract\_pipelines.py --org https://dev.azure.com/<org> --project <project>
python $S/05\_build\_graph.py                       # -> data/graph.json + data/app-map.json (review this!)
python $S/06\_render\_drawio.py                     # -> \*.drawio
```

Useful options:

|Script|Option|Purpose|
|-|-|-|
|`02\_enrich\_arg.py`|`--all-rgs`|Include every resource group in the subscription, not only those in the CSV|
||`--scan-app-settings`|Discover app → SQL / Key Vault / Storage / Service Bus dependencies (see [Security notes](#security-notes))|
||`--from-file export.json`|Use a Resource Graph Explorer JSON export instead of calling `az`|
|`03\_extract\_iac.py`|`--path <folder>`|Scan specific folders (repeatable)|
|`04\_extract\_pipelines.py`|`--no-classic`|YAML pipelines only|
||`--release-json file.json`|Parse a saved classic release definition offline|
|`06\_render\_drawio.py`|`--view network\|app\|both`|Choose which file to render|
||`--app <name>` / `--env <env>`|Render one application or one environment|

Each step caches its output in `arch-diagrams/data/`:

* After a **config change**, re-run steps 5–6 only.
* After a **new CSV export**, re-run steps 1–2 and 5–6.
* After **pipeline changes**, re-run step 4 and 5–6.

## Configuration

`arch-diagrams.config.json` lives at the repo root. Missing keys use the defaults; see `templates/arch-diagrams.config.json` for every option.

```jsonc
{
  "inventory\_csv": "azure-inventory.csv",
  "subscription\_id": "",                    // empty = current az account
  "iac\_paths": \["."],                       // where Bicep/ARM lives
  "pipeline\_paths": \["."],                  // where pipeline YAML lives
  "ado": { "organization": "https://dev.azure.com/contoso", "project": "Platform" },
  "output\_dir": "arch-diagrams",
  "scope\_to\_csv\_resource\_groups": true,
  "scan\_app\_settings": false,

  "env": {
    "tokens": { "dev": \["dev"], "qa": \["qa", "test", "uat"], "prod": \["prod", "prd"] },
    "default": "prod",                       // names without an env token
    "shared\_tokens": \["hub", "shared", "common", "core", "mgmt", "platform"]
  },
  "app": {
    "overrides": { "^rg-ord|orders": "orders", "pay": "payments" }   // regex on RG or resource name -> app
  },
  "variable\_overrides": { "rgName": "rg-orders" },                    // pipeline $(vars) that can't be resolved
  "app\_view": { "hide\_types": \["microsoft.network/networkinterfaces"] },
  "network\_view": { "hide\_types": \["microsoft.network/networkwatchers"] }
}
```

## How environments and applications are detected

**Environment**, in order of priority:

1. An `environment` or `env` tag.
2. A token in the **resource group name**: `dev`, `qa`, `prod`/`prd`.
3. A token in the **resource name**. This also catches names without separators, e.g. `stordersdev01`.
4. A shared token (`hub`, `shared`, …) → **shared**.
5. Otherwise the default, **prod**.

**Application**, in order of priority:

1. A regex in `app.overrides`.
2. An `application`, `app` or `workload` tag.
3. The resource group name minus `rg`, environment and region tokens: `rg-orders-dev` → `orders`.
4. The resource name minus common prefixes (`app-`, `func-`, `kv-`, `st`, …).

Private endpoints, NICs and public IPs follow the application of the resource they serve. Because naming is often inconsistent, **always review `arch-diagrams/data/app-map.json`** after step 5 and fix mistakes with `app.overrides`.

**IaC status:**

|Status|Meaning|Shown as|
|-|-|-|
|`managed`|A Bicep/ARM resource resolves to the same type and name, using the parameter file for that environment (`main.dev.parameters.json`, `\*.bicepparam`)|Normal icon|
|`unknown`|The type is declared, but no parameter file exists for this environment|Normal icon|
|`unmanaged`|Not found in any scanned template|Dashed red border|

## Viewing and editing the diagrams

* Open the `.drawio` files with the VS Code **Draw.io Integration** extension, draw.io desktop, or app.diagrams.net.
* Each page is a separate tab, e.g. *Overview*, *Network – prod*, *App – orders*.
* The layout is a clean grid with auto-routed connectors. Drag shapes or waypoints to tidy up.
* Export to PNG, SVG or PDF from draw.io (*File → Export as*) for wikis and documents.
* Manual edits are overwritten when you re-render. Keep a copy of hand-tuned diagrams, or render to a different `output\_dir`.

## Folder structure

```
.github/skills/azure-arch-diagrams/
├── SKILL.md                    # instructions Copilot follows (keep in sync with the scripts)
├── README.md                   # this file
├── scripts/
│   ├── common.py               # config, az helper, env/app detection, tiers
│   ├── 01\_import\_inventory.py  # CSV -> inventory.json
│   ├── 02\_enrich\_arg.py        # Resource Graph -> arg.json (+ optional app settings scan)
│   ├── 03\_extract\_iac.py       # Bicep/ARM + parameter files -> iac.json
│   ├── 04\_extract\_pipelines.py # YAML + classic release/build pipelines -> pipelines.json
│   ├── 05\_build\_graph.py       # merge -> graph.json + app-map.json
│   └── 06\_render\_drawio.py     # graph -> network-topology.drawio, application-view.drawio
├── kql/                        # Resource Graph queries (also handy in Resource Graph Explorer)
├── references/
│   ├── icon\_map.json           # resource type -> draw.io Azure icon (img/lib/azure2/...)
│   └── how-it-works.md         # property -> edge mapping, limits, troubleshooting
├── templates/
│   └── arch-diagrams.config.json
└── examples/                   # sample output generated from synthetic data
```

## Updating the skill

|Change|Where|
|-|-|
|App grouping, environment names, unresolved pipeline variables|`arch-diagrams.config.json`; the skill itself is unchanged|
|A resource type shows the generic icon|Add one line to `references/icon\_map.json`. File names are in draw.io's `img/lib/azure2/` folder.|
|New relationship, pipeline task type, layout or view|`scripts/\*.py`, then document it in `SKILL.md` and `references/how-it-works.md`|
|Copilot runs the steps incorrectly|`SKILL.md`. Only change the `description:` line if *when* the skill should trigger changes.|

Recommended workflow:

1. Work on a branch, e.g. `skill/arch-diagrams-<change>`. You can ask Copilot to make the change: *"Update the azure-arch-diagrams skill to …"*.
2. Re-run steps 5–6 against the cached data and compare the output.
3. Add a line to a `CHANGELOG.md` in this folder.
4. Merge via PR. Copilot uses the new version on the next chat; run *Developer: Reload Window* if it doesn't.

## Security notes

* Only read operations are used: `az graph query`, and `list`/`show` commands.
* `--scan-app-settings` reads Web/Function App settings and connection strings in memory. It only **matches** them against known endpoints (`\*.database.windows.net`, `\*.vault.azure.net`, storage account names, …), then discards the values. Only the matched resource ids are saved. It is off by default.
* Variable groups are read for **non-secret** values only. Secret values are never returned or stored.
* `arch-diagrams/data/\*.json` contains resource ids, names, IP ranges and network layout. Decide whether to commit it, or add `arch-diagrams/data/` to `.gitignore`.

## Limitations and troubleshooting

**Not drawn:**

* Front Door/CDN origins, APIM backends, NSG rules and diagnostic settings. These live in child resources or data-plane APIs that Resource Graph doesn't return.
* App → data dependencies, unless `--scan-app-settings` is used. Managed-identity RBAC and code-level calls are never discovered.
* Bicep/ARM names built with `uniqueString()`/`resourceGroup()`. These can't be resolved offline, so those resources appear as unmanaged.

**Common problems:**

|Symptom|Fix|
|-|-|
|`az graph query` not found|`az extension add --name resource-graph`|
|Graph query returns 0 rows|Wrong subscription: set `subscription\_id` or run `az account set`|
|Many "CSV rows not found in Azure"|The CSV is old, or its resource-group column holds ids. Re-export it.|
|`az pipelines release` fails|Install the `azure-devops` extension, run `az devops login`, and check *Release: read* permission|
|Everything is detected as prod|Add tokens such as `"qa": \["qa","test","uat"]`, or use `environment` tags|
|Wrong application grouping|Add `app.overrides` regexes, then re-run steps 5–6|

See `references/how-it-works.md` for the full mapping of Resource Graph properties to diagram edges.

