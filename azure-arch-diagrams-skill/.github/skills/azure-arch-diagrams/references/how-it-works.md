# How the diagrams are derived

## Data flow
```
inventory.csv ──01──► data/inventory.json ─┐
az graph query ──02──► data/arg.json ──────┤
Bicep / ARM ─────03──► data/iac.json ──────┼──05──► data/graph.json + data/app-map.json ──06──► *.drawio
ADO YAML + classic 04► data/pipelines.json ┘
```

## Relationships (Resource Graph properties → edges)

| Edge (rel) | Source property |
|---|---|
| subnet placement | NIC `ipConfigurations[].subnet` (VM via NIC `virtualMachine`), PE `subnet`, App GW `gatewayIPConfigurations`, Firewall/Bastion/VNet GW `ipConfigurations`, internal LB `frontendIPConfigurations`, AKS `agentPoolProfiles[].vnetSubnetID`, APIM `virtualNetworkConfiguration`, Container Apps env `vnetConfiguration`, SQL MI / Redis `subnetId`, VMSS profile |
| `vnet_integration` | Web/Function App `virtualNetworkSubnetId` (app is drawn inside its integration subnet) |
| `private_link` | PE `privateLinkServiceConnections[].privateLinkServiceId` (+ groupIds as label) |
| `peering` | VNet `virtualNetworkPeerings[].remoteVirtualNetwork` (remote VNets out of scope are drawn dashed) |
| `udr` | subnet → route table → route `nextHopType=VirtualAppliance` → private IP owner (Firewall / NVA NIC) |
| `service_endpoint` | `networkAcls.virtualNetworkRules` / `virtualNetworkRules` on Storage, Key Vault, SQL, Cosmos… |
| `public_ip` | Public IP `ipConfiguration.id` (owner = resource part of the id) |
| `routes_to` | App GW `backendAddressPools[].backendAddresses` matched by FQDN to app hostnames or by IP |
| `hosted_on` | Site `serverFarmId`, `hostingEnvironmentProfile`; Container App `managedEnvironmentId` |
| `logs_to` | App Insights `WorkspaceResourceId` |
| `database` | SQL server → databases / elastic pools (parent id) |
| `dns_link` | `privateDnsZones/virtualNetworkLinks.virtualNetwork` |
| `uses` | Only with `--scan-app-settings`: app settings / connection strings mention `<name>.database.windows.net`, `<name>.vault.azure.net`, storage account names, `<ns>.servicebus.windows.net`, Cosmos, Redis, PostgreSQL/MySQL, Search, OpenAI, other app hostnames |
| `deploys` | Pipeline task resource group / target app name |

## Environment and application detection
- Env: `environment`/`env` tag → RG name token → resource name token (also catches names without separators, e.g. `storordersdev01`) → shared token (`hub`, `shared`…) → default `prod`.
- App: `app.overrides` regex → `application`/`app`/`workload` tag → RG name minus `rg`, env and region tokens → resource name minus CAF prefixes (`app-`, `func-`, `kv-`, `st`…).
- Private endpoints, NICs and public IPs follow the app/env of the resource they serve in the application view.
- Pages are made only for apps that have compute, data or integration resources. Pure network or monitoring groups appear in the network view or as "shared" dependencies.

## IaC status (red dashed border)
- `managed`: a Bicep/ARM resource of the same type resolves to the same name for some parameter file.
- `unknown`: the type is declared, but no parameter file exists for this resource's environment (no border).
- `unmanaged`: not declared in any scanned template (border). Typical causes: created in the portal, created by a pipeline script, a template in another repo, or a name built with `uniqueString()`.

## Known limits
- Front Door / CDN origins, APIM backends, NSG rules and diagnostic settings are not drawn. They live in child resources or data-plane APIs that Resource Graph doesn't return.
- App → data dependencies need `--scan-app-settings`. Managed-identity RBAC and code-level calls are not discovered.
- Layout is a deterministic grid. Large environments (>40 resources per subnet or RG) are readable but wide. Use `--app` / `--env` to render one page at a time.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `az graph query` not found | `az extension add --name resource-graph` |
| Graph query returns 0 rows | Wrong subscription: set `subscription_id` or run `az account set -s <id>` |
| Many "CSV rows not found" | The CSV is old, or the RG column holds RG ids. Re-export or check the RG column |
| `az pipelines release` fails | `az extension add --name azure-devops`; `az devops login`; needs *Release: read* permission |
| Bicep build errors | `az bicep upgrade`; modules from private registries need `az login` to the ACR |
| Everything is "prod" | Names have no env tokens. Add tokens (e.g. `"qa": ["qa","test","uat"]`) or `environment` tags |
| Wrong app grouping | Add `app.overrides` regexes, re-run steps 5–6 |
| An icon is the generic RG icon | Add the type to `references/icon_map.json` (file names in draw.io `img/lib/azure2/`) |
