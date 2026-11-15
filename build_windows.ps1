param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$DependencyPath = Join-Path $ProjectRoot "work\pydeps"
$env:PYTHONPATH = "$ProjectRoot\src;$DependencyPath"

& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --console `
    --uac-admin `
    --name IntelliPage `
    --paths "$ProjectRoot\src" `
    --paths $DependencyPath `
    --add-data "$ProjectRoot\database;database" `
    --add-data "$ProjectRoot\.env.example;." `
    --hidden-import torch `
    --exclude-module torchaudio `
    --exclude-module torchvision `
    --exclude-module transformers `
    --exclude-module pytest `
    --exclude-module tensorboard `
    --collect-all mysql.connector `
    --collect-all oracledb `
    --collect-all cryptography `
    "$ProjectRoot\run_intellipage.py"

Write-Host "Built: $ProjectRoot\dist\IntelliPage.exe"


