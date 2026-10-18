# Landing deployment

The initial landing page uses one Consumption container (0.25 vCPU / 0.5 GiB), zero to one replicas, default networking, public HTTPS ingress and a dedicated Log Analytics workspace with 30-day retention and a 0.1-GB/day ingestion cap. That cap applies to logs, not application decisions. No SQL, broker, model, storage, paid networking or trading workload is provisioned. The always-on trading profile remains a future milestone.

## Local check

```powershell
docker compose up --build -d
python scripts/smoke.py --url http://127.0.0.1:8080 --build development
docker compose down
```

Open the local address to review desktop and mobile layouts. The page has working source/roadmap links and internal navigation and no form or external fonts. Optional Google Analytics is disabled by default. `/healthz` identifies the application, landing mode and source build.

## Google Analytics

Create a GA4 property and web stream for the public site, then supply its measurement ID through `GOOGLE_ANALYTICS_ID`. This is a public browser identifier, not an API credential; keep the actual value in environment configuration rather than source or image layers. Blank disables all analytics scripts and Google CSP permissions; an invalid nonblank ID prevents startup. No Python analytics package, Google credentials, paid infrastructure or backend event transport is required.

For local Compose, use the ignored `.env` or existing process environment (the process value takes precedence). Leave it empty for routine development. For hosted deployment, set the GitHub **demo environment variable** `GOOGLE_ANALYTICS_ID`; the manually triggered deployment passes it to Bicep's `googleAnalyticsId` parameter and the landing container. Local Bicep deployment uses the same parameter in an ignored parameter file. Removing the value and redeploying disables tracking. Changing hosted configuration requires owner approval; merging this support alone does not enable tracking or deploy Azure changes.

The local loader initializes the Google tag once per document. Its config sends the default page view with query strings/fragments removed from page and referrer URLs, and disables Google signals and ad personalization. It does not add user IDs, custom trading events or backend/private-route instrumentation. GA4 may use analytics cookies and its web-stream Enhanced Measurement settings control additional automatic events; disable Enhanced Measurement in the property if only page views are wanted. Do not enable Ads-linked features without reviewing collection and CSP requirements. The [Google config reference](https://developers.google.com/analytics/devguides/collection/ga4/reference/config) and [CSP guide](https://developers.google.com/tag-platform/security/guides/csp) describe these settings. The landing CSP permits the documented non-Ads endpoints without unsafe inline/eval execution; health and error routes retain the original restrictive policy.

CI verifies disabled and configured HTTP behavior using a synthetic ID, without contacting Google. After an approved deployment, verify the immutable image/ready revision, `/healthz`, page/assets/navigation and matching tag ID through the custom domain, then confirm `page_view` in GA4 Realtime or DebugView using an actual browser. A successful HTTP smoke check alone does not prove Google received an event. Browser blockers can prevent collection without affecting the page.

## Delivery

`ci.yml` validates lint/format, offline replay contracts/graders/archive fixtures and baseline coverage, the locked landing-container build, actual HTTP behavior, Bicep compilation and a secret scan. Replay checks run in a network-disabled container; only a bounded sanitized summary is uploaded with seven-day retention. Full traces and private mind data are excluded from CI artifacts. It requires no deployment credentials. `publish.yml` publishes the tested image to GHCR on main and can be triggered manually. Only the deliberately public application image may be made public; do not change other packages. The allowlisted Docker context omits local configuration and repository internals.

`deploy.yml` is a separate, explicitly triggered delivery workflow. It accepts a source commit and resolves its GHCR digest; it deploys only that immutable image and verifies the ready revision's image and public health/build response. Configure a GitHub `demo` environment with OIDC and these secrets: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP`, `AZURE_LOCATION`, `AZURE_CONTAINER_APP`, `AZURE_CONTAINER_ENVIRONMENT`, `AZURE_LOG_WORKSPACE`. Scope the deployment identity's Contributor role to the dedicated resource group, not the subscription. Its federation subject is the repository's `demo` environment; restrict that environment's deployment branches to `main` at GitHub as well as checking the ref in the workflow. Never give PR checks cloud credentials.

Create the dedicated resource group once using private configuration and an approved region. Keep actual names/IDs and hostname in ignored files or secrets. Copy `main.bicepparam.example` to an ignored parameter file for local Bicep deployment. Do not put secret values in shell history, source or container layers. Deploy with `az deployment group create` using the private resource group and parameters; write outputs only into ignored local deployment state. Validate provisioning state, active revision, immutable image digest, health build and the actual page after every release. Standard workflow logs must not expose private configuration.

## Custom domain

After the app exists, read its ingress FQDN and `customDomainVerificationId` from deployment outputs. For the owner's subdomain, provide a CNAME pointing directly to that FQDN and a TXT record named `asuid.` plus the subdomain label with the verification ID. The owner creates DNS. Use DNS-only routing (no proxy or intermediate CNAME) for managed-certificate validation/renewal. If existing CAA records restrict issuance, allow DigiCert.

Once DNS resolves, use `scripts/bind-domain.ps1` with an ignored configuration file. It checks the CNAME/TXT, adds the hostname and requests/binds the free managed certificate, then checks HTTPS health. If the local resolver still caches an absent record, pass `-DnsServer` with a trusted resolver address for the DNS prechecks; the final HTTPS request still uses normal system resolution and certificate validation. Keep the app enabled and public ingress reachable during issuance and renewal. Recheck the page, assets and health through the custom hostname; default-host success alone does not verify the custom domain. Domain binding is represented by the optional Bicep domain module after validation, so future app deployments preserve it.

## Teardown and later milestones

Review the dedicated group's contents using the private `AZURE_RESOURCE_GROUP`, then remove that group only when teardown is requested. Remove its DNS records, GHCR releases and deployment federation separately as appropriate; never target another project's resources. Database/feed/model resources are not part of this bootstrap. M4 adds the approved Microsoft Foundry resource/project, model deployments and managed-identity access described in the [architecture](../docs/architecture.md#microsoft-foundry-model-access); Agent Framework orchestration stays in Container Apps. Before trading, implement SQL ownership/reconciliation and the always-on two-container profile. Move the public record to Static Web Apps and sanitized Blob snapshots at M8.
