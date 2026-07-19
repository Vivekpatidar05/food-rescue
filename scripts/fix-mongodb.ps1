# fix-mongodb.ps1 — repair and start the local MongoDB service.
#
# WHY THIS EXISTS
#   mongod crashed with "out of memory" (SIGABRT) and the service stayed down,
#   so the backend could not reach localhost:27017. This machine has ~7.3 GB of
#   RAM, but MongoDB's default WiredTiger cache is 50% of (RAM - 1 GB) ~= 3.2 GB
#   — too greedy alongside a browser and an IDE. This script caps the cache so
#   the service stops dying, then starts it.
#
# RUN AS ADMINISTRATOR:
#   Right-click PowerShell -> "Run as administrator", then:
#       cd C:\Users\HP2008ax\food-rescue
#       powershell -ExecutionPolicy Bypass -File .\scripts\fix-mongodb.ps1
#
# Safe to re-run: it backs up mongod.cfg and skips the edit if already applied.

$ErrorActionPreference = "Stop"

$CacheSizeGB = 1          # WiredTiger cache cap, in GB
$Cfg = "C:\Program Files\MongoDB\Server\8.0\bin\mongod.cfg"

# --- must be elevated -------------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host "ERROR: this script must run as administrator." -ForegroundColor Red
    Write-Host "Close this window, right-click PowerShell -> 'Run as administrator', and re-run."
    exit 1
}

if (-not (Test-Path $Cfg)) {
    Write-Host "ERROR: mongod.cfg not found at $Cfg" -ForegroundColor Red
    Write-Host "Adjust the `$Cfg path in this script to match your MongoDB install."
    exit 1
}

# --- 1. cap the WiredTiger cache -------------------------------------------
$config = Get-Content $Cfg -Raw

if ($config -match "cacheSizeGB") {
    Write-Host "[1/3] Cache cap already present in mongod.cfg — leaving it alone." -ForegroundColor Yellow
}
else {
    $backup = "$Cfg.backup-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    Copy-Item $Cfg $backup
    Write-Host "[1/3] Backed up config to: $backup" -ForegroundColor Gray

    # Insert the engineConfig block under the existing `storage:` section.
    $patched = $config -replace "(?m)^storage:\s*\r?\n", @"
storage:
  wiredTiger:
    engineConfig:
      cacheSizeGB: $CacheSizeGB

"@
    if ($patched -eq $config) {
        Write-Host "ERROR: could not find a 'storage:' section to patch." -ForegroundColor Red
        Write-Host "Add this to $Cfg by hand:"
        Write-Host "  storage:"
        Write-Host "    wiredTiger:"
        Write-Host "      engineConfig:"
        Write-Host "        cacheSizeGB: $CacheSizeGB"
        exit 1
    }
    Set-Content -Path $Cfg -Value $patched -Encoding utf8
    Write-Host "[1/3] Capped WiredTiger cache at $CacheSizeGB GB." -ForegroundColor Green
}

# --- 2. start the service ---------------------------------------------------
$svc = Get-Service MongoDB -ErrorAction SilentlyContinue
if ($null -eq $svc) {
    Write-Host "ERROR: no 'MongoDB' service is installed on this machine." -ForegroundColor Red
    exit 1
}

if ($svc.Status -eq "Running") {
    Write-Host "[2/3] MongoDB service already running — restarting to apply the cap." -ForegroundColor Gray
    Restart-Service MongoDB
}
else {
    Write-Host "[2/3] Starting the MongoDB service..." -ForegroundColor Gray
    Start-Service MongoDB
}

# --- 3. verify it is actually listening -------------------------------------
$up = $false
foreach ($attempt in 1..15) {
    Start-Sleep -Seconds 1
    $probe = Test-NetConnection -ComputerName localhost -Port 27017 -InformationLevel Quiet -WarningAction SilentlyContinue
    if ($probe) { $up = $true; break }
}

if ($up) {
    Write-Host "[3/3] MongoDB is UP and listening on localhost:27017." -ForegroundColor Green
    Write-Host ""
    Write-Host "You can now start the backend:" -ForegroundColor Cyan
    Write-Host "    cd C:\Users\HP2008ax\food-rescue\backend"
    Write-Host "    .\.venv\Scripts\Activate.ps1"
    Write-Host "    python app.py"
}
else {
    Write-Host "[3/3] Service started but nothing is listening on 27017." -ForegroundColor Red
    Write-Host "Check the tail of the log for the reason:"
    Write-Host '    Get-Content "C:\Program Files\MongoDB\Server\8.0\log\mongod.log" -Tail 30'
    exit 1
}
