targetScope = 'resourceGroup'

param location string = resourceGroup().location
param environmentName string
param certificateName string
param customHostname string

resource environment 'Microsoft.App/managedEnvironments@2025-01-01' existing = {
  name: environmentName
}

// Deploy after DNS validation and after adding the hostname in Disabled state.
resource certificate 'Microsoft.App/managedEnvironments/managedCertificates@2025-01-01' = {
  parent: environment
  name: certificateName
  location: location
  properties: {
    subjectName: customHostname
    domainControlValidation: 'CNAME'
  }
}

output certificateId string = certificate.id
