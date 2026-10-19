targetScope = 'resourceGroup'

param location string = resourceGroup().location
param appName string
param environmentName string
param identityName string
param engineImage string
param gatewayImage string
@description('Non-secret runtime configuration; actual values stay in private parameters.')
param engineEnvironment array
param gatewayEnvironment array
@description('Enable the verified demo read-only profile; never enables command acceptance or dispatch.')
param brokerReadOnlyEnabled bool = false
@secure()
param appSecrets object

resource environment 'Microsoft.App/managedEnvironments@2025-01-01' existing = { name: environmentName }
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = { name: identityName }

resource app 'Microsoft.App/containerApps@2025-01-01' = {
  name: appName
  location: location
  tags: { project: 'PipMind', workload: 'shadow' }
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${identity.id}': {} } }
  properties: {
    managedEnvironmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      // items() loses the linter's taint annotation; the ARM input is secureObject.
      #disable-next-line use-secure-value-for-secure-inputs
      secrets: [for item in items(appSecrets): { name: item.key, value: item.value }]
      ingress: {
        external: false
        targetPort: 8084
        transport: 'auto'
        allowInsecure: false
        traffic: [{ latestRevision: true, weight: 100 }]
      }
    }
    template: {
      containers: [
        {
          name: 'engine'
          image: engineImage
          env: engineEnvironment
          volumeMounts: [{ volumeName: 'feed-config', mountPath: '/run/pipmind/feeds' }]
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          probes: [
            { type: 'Startup', httpGet: { path: '/healthz', port: 8084 }, periodSeconds: 5, timeoutSeconds: 3, failureThreshold: 60 }
            { type: 'Readiness', httpGet: { path: '/healthz', port: 8084 }, periodSeconds: 10, timeoutSeconds: 3, failureThreshold: 3 }
            { type: 'Liveness', httpGet: { path: '/healthz', port: 8084 }, periodSeconds: 30, timeoutSeconds: 5, failureThreshold: 5 }
          ]
        }
        {
          name: 'gateway'
          image: gatewayImage
          env: brokerReadOnlyEnabled ? concat(filter(gatewayEnvironment, item => !contains(['PIPMIND_BROKER_READS', 'PIPMIND_GATEWAY_UNCONFIGURED', 'PIPMIND_GATEWAY_READ_ONLY'], item.name)), [
            { name: 'PIPMIND_BROKER_READS', value: 'true' }
            { name: 'PIPMIND_GATEWAY_UNCONFIGURED', value: 'false' }
            { name: 'PIPMIND_GATEWAY_READ_ONLY', value: 'true' }
          ]) : gatewayEnvironment
          volumeMounts: [{ volumeName: 'gateway-config', mountPath: '/run/pipmind/gateway' }]
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          probes: [
            { type: 'Startup', httpGet: { path: '/healthz', port: 8081 }, periodSeconds: 5, timeoutSeconds: 3, failureThreshold: 30 }
            { type: 'Liveness', httpGet: { path: '/healthz', port: 8081 }, periodSeconds: 30, timeoutSeconds: 5, failureThreshold: 5 }
          ]
        }
      ]
      volumes: [
        { name: 'feed-config', storageType: 'Secret', secrets: [{ secretRef: 'feed-config', path: 'recipes.json' }] }
        { name: 'gateway-config', storageType: 'Secret', secrets: [{ secretRef: 'gateway-symbols', path: 'symbols.json' }] }
      ]
      scale: { minReplicas: 1, maxReplicas: 1 }
      terminationGracePeriodSeconds: 60
    }
  }
}
output readyRevision string = app.properties.latestReadyRevisionName
