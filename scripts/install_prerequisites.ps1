[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Write-Host "Installing Azure CLI tools..."
if (Get-Command winget -ErrorAction SilentlyContinue) {
    winget install --id Microsoft.AzureCLI -e --accept-package-agreements --accept-source-agreements | Out-Null
} else {
    Write-Warning "winget is not available. Install the Azure CLI manually from https://aka.ms/InstallAzureCli."
}

Write-Host "Logging in to Azure..."
az login

Write-Host "Checking for Docker Desktop..."
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Warning "Docker not found. Please install Docker Desktop from https://www.docker.com/products/docker-desktop."
} else {
    Write-Host "Docker is installed."
}

Write-Host "Setup complete!"
