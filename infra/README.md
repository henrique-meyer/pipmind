# Landing deployment

The initial landing page uses one Consumption container (0.25 vCPU / 0.5 GiB), zero to one replicas, default networking, public HTTPS ingress and a dedicated Log Analytics workspace with 30-day retention and a 0.1-GB/day ingestion cap. That cap applies to logs, not application decisions. No SQL, broker, model, storage, paid networking or trading workload is provisioned. The always-on trading profile remains a future milestone.

## Local check

```powershell
docker compose up --build -d
python scripts/smoke.py --url http://127.0.0.1:8080 --build development
docker compose down
```

Open the local address to review desktop and mobile layouts. The page has working source/roadmap links and internal navigation, no form, no analytics and no external font/script dependency. `/healthz` identifies the application, landing mode and source build.

## Delivery

`ci.yml` validates lint/format, the locked container build, actual HTTP behavior, Bicep compilation and a secret scan. It requires no deployment credentials. `publish.yml` publishes the tested image to GHCR on main and can be triggered manually. Only the deliberately public application image may be made public; do not change other packages. The allowlisted Docker context omits local configuration and repository internals.

`deploy.yml` is a separate, explicitly triggered delivery workflow. It accepts a source commit and resolves its GHCR digest; it deploys only that immutable image and verifies the ready revision's image and public health/build response. Configure a GitHub `demo` environment with OIDC and these secrets: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP`, `AZURE_LOCATION`, `AZURE_CONTAINER_APP`, `AZURE_CONTAINER_ENVIRONMENT`, `AZURE_LOG_WORKSPACE`. Scope the deployment identity's Contributor role to the dedicated resource group, not the subscription. Its federation subject is the repository's `demo` environment; restrict that environment's deployment branches to `main` at GitHub as well as checking the ref in the workflow. Never give PR checks cloud credentials.

Create the dedicated resource group once using private configuration and an approved region. Keep actual names/IDs and hostname in ignored files or secrets. Copy `main.bicepparam.example` to an ignored parameter file for local Bicep deployment. Do not put secret values in shell history, source or container layers. Deploy with `az deployment group create` using the private resource group and parameters; write outputs only into ignored local deployment state. Validate provisioning state, active revision, immutable image digest, health build and the actual page after every release. Standard workflow logs must not expose private configuration.

## Custom domain

After the app exists, read its ingress FQDN and `customDomainVerificationId` from deployment outputs. For the owner's subdomain, provide a CNAME pointing directly to that FQDN and a TXT record named `asuid.` plus the subdomain label with the verification ID. The owner creates DNS. Use DNS-only routing (no proxy or intermediate CNAME) for managed-certificate validation/renewal. If existing CAA records restrict issuance, allow DigiCert.

Once DNS resolves, use `scripts/bind-domain.ps1` with an ignored configuration file. It checks the CNAME/TXT, adds the hostname and requests/binds the free managed certificate, then checks HTTPS health. Keep the app enabled and public ingress reachable during issuance and renewal. Recheck the page, assets and health through the custom hostname; default-host success alone does not verify the custom domain. Domain binding is represented by the optional Bicep domain module after validation, so future app deployments preserve it.

## Teardown and later milestones

Review the dedicated group's contents using the private `AZURE_RESOURCE_GROUP`, then remove that group only when teardown is requested. Remove its DNS records, GHCR releases and deployment federation separately as appropriate; never target another project's resources. Database/feed/model resources are not part of this bootstrap. M4 adds the approved Microsoft Foundry resource/project, model deployments and managed-identity access described in the [architecture](../docs/architecture.md#microsoft-foundry-model-access); Agent Framework orchestration stays in Container Apps. Before trading, implement SQL ownership/reconciliation and the always-on two-container profile. Move the public record to Static Web Apps and sanitized Blob snapshots at M8.
