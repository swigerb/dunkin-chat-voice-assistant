<#
.SYNOPSIS
    Deploy Dunkin Voice Chat Assistant to Azure Local (AKS Arc)

.DESCRIPTION
    Provisions Key Vault, ACR, builds/pushes container image, and deploys to AKS Arc.
    Reads configuration from .env.template-based .env file.

.PARAMETER SkipBuild
    Skip Docker build and push

.PARAMETER SkipKeyVault
    Skip Key Vault creation and secret storage

.PARAMETER SkipDeploy
    Skip Kubernetes deployment

.EXAMPLE
    .\scripts\deploy-edge.ps1
    .\scripts\deploy-edge.ps1 -SkipBuild -SkipKeyVault
#>
[CmdletBinding()]
param(
    [switch]$SkipBuild,
    [switch]$SkipKeyVault,
    [switch]$SkipDeploy
)

$ErrorActionPreference = 'Stop'
$RootDir = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $RootDir '.env'

# --------------------------------------------------------------------------- #
# Load .env file
# --------------------------------------------------------------------------- #
if (-not (Test-Path $EnvFile)) {
    Write-Error "ERROR: .env file not found. Copy .env.template to .env and fill in values."
    exit 1
}

Get-Content $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith('#')) {
        $parts = $line -split '=', 2
        if ($parts.Count -eq 2) {
            [System.Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim(), 'Process')
        }
    }
}

# Validate required variables
$RequiredVars = @(
    'AZURE_SUBSCRIPTION_ID', 'AZURE_RESOURCE_GROUP', 'AZURE_LOCATION',
    'AZURE_KEYVAULT_NAME', 'AZURE_ACR_NAME',
    'AZURE_OPENAI_EASTUS2_ENDPOINT', 'AZURE_OPENAI_EASTUS2_API_KEY'
)
foreach ($var in $RequiredVars) {
    $val = [System.Environment]::GetEnvironmentVariable($var, 'Process')
    if ([string]::IsNullOrWhiteSpace($val)) {
        Write-Error "ERROR: Required variable $var is not set in .env"
        exit 1
    }
}

# Helper to read env var with default
function Get-EnvOrDefault($Name, $Default) {
    $val = [System.Environment]::GetEnvironmentVariable($Name, 'Process')
    if ([string]::IsNullOrWhiteSpace($val)) { return $Default } else { return $val }
}

$SubscriptionId   = $env:AZURE_SUBSCRIPTION_ID
$ResourceGroup    = $env:AZURE_RESOURCE_GROUP
$Location         = $env:AZURE_LOCATION
$KeyVaultName     = $env:AZURE_KEYVAULT_NAME
$KeyVaultRg       = Get-EnvOrDefault 'AZURE_KEYVAULT_RG' $ResourceGroup
$KeyVaultSku      = Get-EnvOrDefault 'AZURE_KEYVAULT_SKU' 'standard'
$AcrName          = $env:AZURE_ACR_NAME
$AcrRg            = Get-EnvOrDefault 'AZURE_ACR_RG' $ResourceGroup
$AcrSku           = Get-EnvOrDefault 'AZURE_ACR_SKU' 'Standard'
$ImageName        = Get-EnvOrDefault 'DOCKER_IMAGE_NAME' 'dunkin-voice-assistant'
$ImageTag         = Get-EnvOrDefault 'DOCKER_IMAGE_TAG' 'latest'
$K8sNamespace     = Get-EnvOrDefault 'K8S_NAMESPACE' 'dunkin-voice'

Write-Host "============================================================"
Write-Host "Dunkin Voice Chat Assistant - Azure Local Edge Deployment"
Write-Host "============================================================"
Write-Host "Subscription:   $SubscriptionId"
Write-Host "Resource Group: $ResourceGroup"
Write-Host "Location:       $Location"
Write-Host "Key Vault:      $KeyVaultName"
Write-Host "ACR:            $AcrName"
Write-Host "Image:          ${ImageName}:${ImageTag}"
Write-Host "K8s Namespace:  $K8sNamespace"
Write-Host "============================================================"

az account set --subscription $SubscriptionId

# --------------------------------------------------------------------------- #
# Step 1: Ensure Resource Group
# --------------------------------------------------------------------------- #
Write-Host "`n>>> Step 1: Ensuring resource group '$ResourceGroup' exists..."
az group create --name $ResourceGroup --location $Location --output none 2>$null

# --------------------------------------------------------------------------- #
# Step 2: Key Vault
# --------------------------------------------------------------------------- #
if (-not $SkipKeyVault) {
    Write-Host "`n>>> Step 2: Creating Key Vault '$KeyVaultName'..."
    az keyvault create `
        --name $KeyVaultName `
        --resource-group $KeyVaultRg `
        --location $Location `
        --sku $KeyVaultSku `
        --enable-rbac-authorization true `
        --output none 2>$null

    Write-Host "   Storing secrets in Key Vault..."
    az keyvault secret set `
        --vault-name $KeyVaultName `
        --name "azure-openai-endpoint" `
        --value $env:AZURE_OPENAI_EASTUS2_ENDPOINT `
        --output none

    az keyvault secret set `
        --vault-name $KeyVaultName `
        --name "azure-openai-api-key" `
        --value $env:AZURE_OPENAI_EASTUS2_API_KEY `
        --output none

    Write-Host "   ✓ Secrets stored in Key Vault."
} else {
    Write-Host "`n>>> Step 2: Skipping Key Vault setup (--SkipKeyVault)"
}

# --------------------------------------------------------------------------- #
# Step 3: ACR + Docker Build
# --------------------------------------------------------------------------- #
$FullImage = "${AcrName}.azurecr.io/${ImageName}:${ImageTag}"

if (-not $SkipBuild) {
    Write-Host "`n>>> Step 3: Ensuring ACR '$AcrName' exists..."
    az acr create --name $AcrName --resource-group $AcrRg --sku $AcrSku --output none 2>$null

    Write-Host "   Building image in ACR (cloud build — linux/amd64)..."
    az acr build `
        --registry $AcrName `
        --image "${ImageName}:${ImageTag}" `
        --platform linux/amd64 `
        --file "$RootDir/app/Dockerfile" `
        "$RootDir/app"
    Write-Host "   ✓ Image built and pushed: $FullImage"
} else {
    Write-Host "`n>>> Step 3: Skipping build (-SkipBuild)"
}

# --------------------------------------------------------------------------- #
# Step 4: Deploy to K8s
# --------------------------------------------------------------------------- #
if (-not $SkipDeploy) {
    Write-Host "`n>>> Step 4: Deploying to Kubernetes..."

    kubectl create namespace $K8sNamespace --dry-run=client -o yaml | kubectl apply -f -
    kubectl apply -f "$RootDir\k8s\configmap.yaml"

    # Template substitution for SecretProviderClass and Deployment
    $spcContent = (Get-Content "$RootDir\k8s\secret-provider-class.yaml" -Raw)
    $spcContent = $spcContent -replace '\$\{AKS_MANAGED_IDENTITY_CLIENT_ID\}', (Get-EnvOrDefault 'AKS_MANAGED_IDENTITY_CLIENT_ID' '')
    $spcContent = $spcContent -replace '\$\{AZURE_KEYVAULT_NAME\}', $KeyVaultName
    $spcContent = $spcContent -replace '\$\{AZURE_TENANT_ID\}', (Get-EnvOrDefault 'AZURE_TENANT_ID' '')
    $spcContent | kubectl apply -f -

    $depContent = (Get-Content "$RootDir\k8s\deployment.yaml" -Raw)
    $depContent = $depContent -replace '\$\{AZURE_ACR_NAME\}', $AcrName
    $depContent = $depContent -replace '\$\{DOCKER_IMAGE_NAME\}', $ImageName
    $depContent = $depContent -replace '\$\{DOCKER_IMAGE_TAG\}', $ImageTag
    $depContent | kubectl apply -f -

    Write-Host "`n   Waiting for deployment rollout..."
    kubectl rollout status deployment/dunkin-voice-assistant -n $K8sNamespace --timeout=120s

    Write-Host "`n   ✓ Deployment complete!"
    kubectl get svc dunkin-voice-service -n $K8sNamespace -o wide
} else {
    Write-Host "`n>>> Step 4: Skipping deploy (-SkipDeploy)"
}

Write-Host "`n============================================================"
Write-Host "Done! Next steps:"
Write-Host "  1. Install Key Vault CSI driver on your AKS Arc cluster if not already installed."
Write-Host "  2. Grant the AKS managed identity 'Key Vault Secrets User' role on the Key Vault."
Write-Host "  3. Access the app at the LoadBalancer IP shown above."
Write-Host "============================================================"
