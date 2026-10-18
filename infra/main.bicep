targetScope = 'resourceGroup'

@description('Azure region, supplied by private deployment configuration.')
param location string = resourceGroup().location

@description('Container App name, supplied privately.')
param appName string

@description('Dedicated Container Apps environment name, supplied privately.')
param environmentName string

@description('Dedicated Log Analytics workspace name, supplied privately.')
param workspaceName string

@description('Public GHCR image pinned by sha256 digest; no mutable deployment tags.')
@minLength(80)
param containerImage string

@description('Use one minimum replica before deploying any trading workload. Zero is landing-only.')
@minValue(0)
@maxValue(1)
param minimumReplicas int = 0

@description('Existing bindings, supplied from private deployment state to preserve custom domains.')
param customDomains array = []

@description('Optional GA4 measurement ID for the public landing page. Empty disables analytics.')
param googleAnalyticsId string = ''

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: workspaceName
  location: location
  tags: { project: 'PipMind', workload: 'landing' }
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    workspaceCapping: { dailyQuotaGb: json('0.1') }
  }
}

resource environment 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: environmentName
  location: location
  tags: { project: 'PipMind', workload: 'landing' }
  properties: {
    zoneRedundant: false
    workloadProfiles: [
      { name: 'Consumption', workloadProfileType: 'Consumption' }
    ]
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource app 'Microsoft.App/containerApps@2025-01-01' = {
  name: appName
  location: location
  tags: { project: 'PipMind', workload: 'landing' }
  identity: { type: 'SystemAssigned' }
  properties: {
    managedEnvironmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8080
        transport: 'auto'
        allowInsecure: false
        customDomains: customDomains
        traffic: [{ latestRevision: true, weight: 100 }]
      }
    }
    template: {
      containers: [
        {
          name: 'landing'
          image: containerImage
          env: [
            { name: 'GOOGLE_ANALYTICS_ID', value: googleAnalyticsId }
          ]
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          probes: [
            { type: 'Startup', httpGet: { path: '/healthz', port: 8080 }, initialDelaySeconds: 1, periodSeconds: 2, timeoutSeconds: 2, failureThreshold: 30 }
            { type: 'Readiness', httpGet: { path: '/healthz', port: 8080 }, periodSeconds: 10, timeoutSeconds: 3, failureThreshold: 3 }
            { type: 'Liveness', httpGet: { path: '/healthz', port: 8080 }, periodSeconds: 30, timeoutSeconds: 3, failureThreshold: 3 }
          ]
        }
      ]
      scale: {
        minReplicas: minimumReplicas
        maxReplicas: 1
        rules: [
          { name: 'http', http: { metadata: { concurrentRequests: '20' } } }
        ]
      }
    }
  }
}

output defaultHostname string = app.properties.configuration.ingress.fqdn
output domainVerificationId string = app.properties.customDomainVerificationId
