# Start the Chanakya GTM application: FastAPI backend (:8010) + Next.js UI (:3000), each in its own window.
#   .\scripts\start_chanakya.ps1                 # real database data\chanakya.db (backed up first)
#   .\scripts\start_chanakya.ps1 -Db C:\path\copy.db
#   .\scripts\start_chanakya.ps1 -Simulated      # isolated temp DB + SIMULATED Clay/LinkedIn/web (test mode)
param(
  [string]$Db = "",
  [string]$Ui = "$PSScriptRoot\..\..\chanakya-ui\web",
  [switch]$Simulated
)
$ErrorActionPreference = "Stop"
$root = Resolve-Path "$PSScriptRoot\.."
if (-not (Test-Path $Ui)) { throw "UI folder not found: $Ui (clone github.com/mrigankad/Linkedinharness- to ..\chanakya-ui)" }
$env:PYTHONPATH = "$root\src"
$env:PYTHONIOENCODING = "utf-8"
if ($Simulated) {
  $tmp = Join-Path $env:TEMP ("chanakya-sim-" + (Get-Date -Format yyyyMMddHHmmss))
  New-Item -ItemType Directory -Force $tmp | Out-Null
  $env:CHANAKYA_SIMULATED_EXTERNALS = "1"
  $env:CHANAKYA_DB_PATH = "$tmp\chanakya-sim.db"
  $env:CHANAKYA_EXPORT_DIR = "$tmp\exports"
  Write-Host "SIMULATED external services, isolated DB: $env:CHANAKYA_DB_PATH"
} else {
  if ($Db) { $env:CHANAKYA_DB_PATH = (Resolve-Path $Db) } else { Remove-Item Env:CHANAKYA_DB_PATH -ErrorAction SilentlyContinue }
  $target = if ($Db) { $env:CHANAKYA_DB_PATH } else { "$root\data\chanakya.db" }
  if (Test-Path $target) {
    $bak = "$root\data\backups\chanakya_start_$(Get-Date -Format yyyyMMddHHmmss).db"
    New-Item -ItemType Directory -Force "$root\data\backups" | Out-Null
    Copy-Item $target $bak
    Write-Host "Backed up $target -> $bak"
  }
}
foreach ($p in 8010, 3000) {
  $busy = Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction SilentlyContinue
  if ($busy) { throw "Port $p is already in use by process $($busy[0].OwningProcess). Close that window (or Stop-Process -Id $($busy[0].OwningProcess)) and run again." }
}
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$root'; python -m chanakya.api --port 8010" -WindowStyle Normal
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$Ui'; if (-not (Test-Path node_modules)) { npm install }; npm run dev" -WindowStyle Normal
$ok = $false
for ($i = 0; $i -lt 40 -and -not $ok; $i++) {
  Start-Sleep -Seconds 1
  try { Invoke-RestMethod http://127.0.0.1:8010/api/health -TimeoutSec 2 | Out-Null; $ok = $true } catch {}
}
if ($ok) { Write-Host "API is up." } else { Write-Warning "API did not answer within 40 s; check its window for the error." }
Write-Host "API  http://127.0.0.1:8010/api/health"
Write-Host "UI   http://localhost:3000  (first visit creates the owner account)"
