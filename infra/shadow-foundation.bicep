targetScope = 'resourceGroup'

param location string = resourceGroup().location
param sqlLocation string = location
param identityName string
param sqlServerName string
param databaseName string
param sqlAdminName string
param sqlAdminObjectId string
param foundryName string
param projectName string
param triageDeployment string
param decisionDeployment string
param triageModelVersion string
param decisionModelVersion string
param triageSku string = 'GlobalStandard'
param decisionSku string = 'DataZoneStandard'
@minValue(1)
param triageCapacity int = 50
@minValue(1)
param decisionCapacity int = 40
@description('Explicit approved IPv4 ranges only. No all-Azure or internet-wide rule.')
@maxLength(256)
param sqlFirewallRanges array = []

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
  tags: { project: 'PipMind', workload: 'shadow' }
}

resource sql 'Microsoft.Sql/servers@2023-08-01' = {
  name: sqlServerName
  location: sqlLocation
  tags: { project: 'PipMind', workload: 'shadow' }
  properties: {
    version: '12.0'
    minimalTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
    administrators: {
      administratorType: 'ActiveDirectory'
      azureADOnlyAuthentication: true
      principalType: 'User'
      login: sqlAdminName
      sid: sqlAdminObjectId
      tenantId: tenant().tenantId
    }
  }
}
resource proxy 'Microsoft.Sql/servers/connectionPolicies@2023-08-01' = {
  parent: sql
  name: 'default'
  properties: { connectionType: 'Proxy' }
}
resource database 'Microsoft.Sql/servers/databases@2023-08-01' = {
  parent: sql
  name: databaseName
  location: sqlLocation
  sku: { name: 'S0', tier: 'Standard', capacity: 10 }
  properties: {
    collation: 'Latin1_General_100_BIN2'
    maxSizeBytes: 268435456000
    zoneRedundant: false
    requestedBackupStorageRedundancy: 'Local'
  }
}
resource firewall 'Microsoft.Sql/servers/firewallRules@2023-08-01' = [for (range, index) in sqlFirewallRanges: {
  parent: sql
  name: 'shadow-${index}'
  properties: {
    startIpAddress: range.startIpAddress
    endIpAddress: range.endIpAddress
  }
}]

resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: foundryName
  location: location
  kind: 'AIServices'
  sku: { name: 'S0' }
  tags: { project: 'PipMind', workload: 'shadow' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: foundryName
    allowProjectManagement: true
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}
resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: foundry
  name: projectName
  location: location
  identity: { type: 'SystemAssigned' }
  properties: { displayName: 'PipMind shadow', description: 'Shadow inference; no order execution or hosted agent runtime.' }
}
resource triage 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundry
  name: triageDeployment
  sku: { name: triageSku, capacity: triageCapacity }
  properties: {
    model: { format: 'OpenAI', name: 'gpt-6-luna', version: triageModelVersion }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
  dependsOn: [project]
}
resource decision 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundry
  name: decisionDeployment
  sku: { name: decisionSku, capacity: decisionCapacity }
  properties: {
    model: { format: 'OpenAI', name: 'gpt-6.1-sol', version: decisionModelVersion }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
  dependsOn: [triage]
}
resource inferenceRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: project
  name: guid(project.id, identity.id, 'foundry-inference')
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '53ca6127-db72-4b80-b1b0-d745d6d5456d')
  }
  dependsOn: [decision]
}

output identityId string = identity.id
output identityClientId string = identity.properties.clientId
output identityPrincipalId string = identity.properties.principalId
output sqlHostname string = sql.properties.fullyQualifiedDomainName
output projectEndpoint string = 'https://${foundryName}.services.ai.azure.com/api/projects/${projectName}'
