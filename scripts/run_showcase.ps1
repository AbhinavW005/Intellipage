$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$env:PYTHONPATH = "$ProjectRoot\src"

Set-Location $ProjectRoot
python run_intellipage.py --no-elevate snapshot
python run_intellipage.py --no-elevate demo --yes

$previousSafetyMode = $env:INTELLIPAGE_SAFETY_MODE
try {
    $env:INTELLIPAGE_SAFETY_MODE = "auto"
    python run_intellipage.py --no-elevate train-dqn --steps 40
}
finally {
    $env:INTELLIPAGE_SAFETY_MODE = $previousSafetyMode
}
