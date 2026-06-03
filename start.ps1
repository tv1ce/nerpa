# TMS - zapusk servera
# Zapusk: pravoy knopkoy -> "Zapustit s pomoschyu PowerShell"
#         ili iz terminala: powershell -File start.ps1

$ErrorActionPreference = "Stop"
$port = 8080

function Info { param($msg) Write-Host "  $msg" -ForegroundColor Cyan }
function Ok   { param($msg) Write-Host "  [OK] $msg" -ForegroundColor Green }
function Warn { param($msg) Write-Host "  [!!] $msg" -ForegroundColor Yellow }
function Fail { param($msg) Write-Host "  [X]  $msg" -ForegroundColor Red }
function Sep  { Write-Host "  ================================================" -ForegroundColor DarkGray }

Clear-Host
Sep
Write-Host "   TMS -- Sistema upravleniya postavkami" -ForegroundColor White
Sep

# 1. Proverka Python
Info "Proverka Python..."
try {
    $pyver = & python --version 2>&1
    Ok "$pyver"
} catch {
    Fail "Python ne najden. Ustanovite Python 3.10+ i dobavte v PATH."
    Read-Host "Nazhmite Enter dlya vyhoda"
    exit 1
}

# 2. Ustanovka zavisimostej
$reqFile = Join-Path $PSScriptRoot "requirements.txt"
if (Test-Path $reqFile) {
    Info "Proverka zavisimostej..."
    & python -m pip install -r $reqFile --quiet --disable-pip-version-check
    if ($LASTEXITCODE -eq 0) { Ok "Zavisimosti ustanovleny" }
    else { Warn "Ne udalos ustanovit zavisimosti" }
}

# 3. Adresa dostupa
$localUrl = "http://localhost:$port"
$networkUrls = @()
try {
    $ips = Get-NetIPAddress -AddressFamily IPv4 -Type Unicast |
           Where-Object { $_.InterfaceAlias -notmatch "Loopback" -and $_.IPAddress -notmatch "^169" } |
           Select-Object -ExpandProperty IPAddress
    foreach ($ip in $ips) { $networkUrls += "http://${ip}:${port}" }
} catch {}

Sep
Write-Host ""
Write-Host "   Lokalno : $localUrl" -ForegroundColor Green
foreach ($url in $networkUrls) {
    Write-Host "   V seti  : $url" -ForegroundColor Yellow
}
Write-Host ""
Write-Host "   Login: admin" -ForegroundColor DarkGray
Write-Host "   Dlya ostanovki nazhmite Ctrl+C" -ForegroundColor DarkGray
Write-Host ""
Sep
Write-Host ""

# 4. Otkryt brauzer cherez 2 sekundy
$job = Start-Job -ArgumentList $localUrl -ScriptBlock {
    param($url)
    Start-Sleep -Seconds 2
    Start-Process $url
}

# 5. Zapusk servera
Set-Location $PSScriptRoot
try {
    & python run.py
} finally {
    Stop-Job $job -ErrorAction SilentlyContinue
    Remove-Job $job -ErrorAction SilentlyContinue
}

Write-Host ""
Warn "Server ostanovlen."
Read-Host "Nazhmite Enter dlya vyhoda"
