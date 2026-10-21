# Launches a dedicated Brave profile with CDP enabled for weavy-proxy.
# Your main browser profile is untouched; sign in to app.weavy.ai once in this window.
$ErrorActionPreference = 'Stop'
$brave = 'C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe'
$profileDir = Join-Path $PSScriptRoot '.browser-profile'
$cdp = 'http://127.0.0.1:9222'

# Already running?
try {
  $v = Invoke-RestMethod -Uri "$cdp/json/version" -TimeoutSec 2
  Write-Host "CDP already up: $($v.Browser)"
  exit 0
} catch { }

New-Item -ItemType Directory -Force -Path $profileDir | Out-Null
Start-Process -FilePath $brave -ArgumentList @(
  "--user-data-dir=$profileDir",
  '--remote-debugging-port=9222',
  '--no-first-run',
  '--no-default-browser-check',
  'https://app.weavy.ai/flows'
)

$deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $deadline) {
  try {
    $v = Invoke-RestMethod -Uri "$cdp/json/version" -TimeoutSec 2
    Write-Host "CDP ready: $($v.Browser)"
    Write-Host "Profile: $profileDir"
    Write-Host 'Sign in to app.weavy.ai in this window if not already signed in.'
    exit 0
  } catch { Start-Sleep -Milliseconds 800 }
}
Write-Host 'CDP did not come up within 30s. Is Brave already running with this profile? Close it and retry.'
exit 1
