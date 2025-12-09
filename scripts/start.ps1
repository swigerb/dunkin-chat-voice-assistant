Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptRoot "..")

Push-Location $repoRoot
try {
    & "$repoRoot/scripts/load_python_env.ps1"

    Write-Host ""
    Write-Host "Restoring frontend npm packages"
    Write-Host ""
    Push-Location "$repoRoot/app/frontend"
    try {
        npm install
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to restore frontend npm packages"
        }

        Write-Host ""
        Write-Host "Building frontend"
        Write-Host ""
        npm run build
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to build frontend"
        }
    }
    finally {
        Pop-Location
    }

    Write-Host ""
    Write-Host "Starting backend"
    Write-Host ""
    Push-Location "$repoRoot/app/backend"
    try {
        $venvPythonPath = Join-Path $repoRoot ".venv/scripts/python.exe"
        if ($IsLinux -or $IsMacOS) {
            $venvPythonPath = Join-Path $repoRoot ".venv/bin/python"
        }
        Start-Process -FilePath $venvPythonPath -ArgumentList "-m app" -Wait -NoNewWindow
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to start backend"
        }
    }
    finally {
        Pop-Location
    }
}
finally {
    Pop-Location
}
