param(
  [switch] $Once,
  [int] $IntervalSeconds = 900
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = "python"

function Invoke-ArobiEvolvePass {
  Push-Location $Root
  try {
    & $Python -m arobi_integrations status --write-snapshot | Out-Null
    & $Python -m arobi_integrations process | Out-Null
  } finally {
    Pop-Location
  }
}

if ($Once) {
  Invoke-ArobiEvolvePass
  exit 0
}

while ($true) {
  Invoke-ArobiEvolvePass
  Start-Sleep -Seconds $IntervalSeconds
}
