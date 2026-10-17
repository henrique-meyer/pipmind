param(
    [Parameter(Mandatory)][string]$ConfigFile
)
$ErrorActionPreference = 'Stop'
$config = Get-Content -LiteralPath $ConfigFile -Raw | ConvertFrom-Json
foreach ($field in 'resourceGroup','location','appName','environmentName','workspaceName','customHostname','certificateName') {
    if (-not $config.$field) { throw "Missing private configuration field: $field" }
}
$app = az containerapp show -g $config.resourceGroup -n $config.appName -o json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Cannot read the target app' }
$target = $app.properties.configuration.ingress.fqdn
$cname = @(Resolve-DnsName -Name $config.customHostname -Type CNAME -DnsOnly)
if (-not ($cname | Where-Object { $_.NameHost.TrimEnd('.') -eq $target })) { throw 'CNAME must point directly to the Container App' }
$txt = @(Resolve-DnsName -Name "asuid.$($config.customHostname)" -Type TXT -DnsOnly)
if (-not ($txt | Where-Object { ($_.Strings -join '') -eq $app.properties.customDomainVerificationId })) { throw 'Ownership TXT does not match' }
$repoRoot = Split-Path -Parent $PSScriptRoot
$privateDirectory = Join-Path $repoRoot '.azure'
New-Item -ItemType Directory -Path $privateDirectory -Force | Out-Null
$parameterFile = Join-Path $privateDirectory 'domain-parameters.local.json'
$existingDomains = @($app.properties.configuration.ingress.customDomains | Where-Object { $_ -and $_.name -ne $config.customHostname })
$values = @{
    location = $config.location
    appName = $config.appName
    environmentName = $config.environmentName
    workspaceName = $config.workspaceName
    containerImage = $app.properties.template.containers[0].image
    minimumReplicas = $app.properties.template.scale.minReplicas
    customDomains = @($existingDomains) + @(@{ name = $config.customHostname; bindingType = 'Disabled' })
}
function Invoke-AppDeployment {
    $parameters = @{}
    foreach ($key in $values.Keys) { $parameters[$key] = @{ value = $values[$key] } }
    @{ parameters = $parameters } | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $parameterFile
    az deployment group create -g $config.resourceGroup -n landing-domain --template-file (Join-Path $repoRoot 'infra/main.bicep') --parameters "@$parameterFile" --only-show-errors -o none
    if ($LASTEXITCODE -ne 0) { throw 'App domain deployment failed' }
}
$bound = @($app.properties.configuration.ingress.customDomains | Where-Object { $_.name -eq $config.customHostname -and $_.bindingType -eq 'SniEnabled' })
if (-not $bound) {
    Invoke-AppDeployment
    az deployment group create -g $config.resourceGroup -n landing-certificate --template-file (Join-Path $repoRoot 'infra/domain.bicep') --parameters "location=$($config.location)" "environmentName=$($config.environmentName)" "certificateName=$($config.certificateName)" "customHostname=$($config.customHostname)" --only-show-errors -o none
    if ($LASTEXITCODE -ne 0) { throw 'Managed certificate deployment failed' }
    $certificateId = az deployment group show -g $config.resourceGroup -n landing-certificate --query properties.outputs.certificateId.value -o tsv
    if ($LASTEXITCODE -ne 0 -or -not $certificateId) { throw 'Certificate ID unavailable' }
    $values.customDomains = @($existingDomains) + @(@{ name = $config.customHostname; bindingType = 'SniEnabled'; certificateId = $certificateId })
    Invoke-AppDeployment
}
$health = Invoke-RestMethod -Uri "https://$($config.customHostname)/healthz" -TimeoutSec 30
if ($health.application -ne 'pipmind' -or $health.mode -ne 'landing' -or $health.status -ne 'ok') { throw 'Custom hostname health check failed' }
Write-Output 'Custom hostname HTTPS health verified. Run the full HTTP/browser checks before completion.'
