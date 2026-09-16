# ============================================================
#  roxy-newprofile.ps1  —  create AND launch offline Roxy profiles
#
#  One command: generate N fingerprint profiles locally, then start
#  each one by invoking the patched core directly. Nothing here talks
#  to Roxy's server, so maxWindowCount / useWindowCount never moves.
#
#  Examples:
#    pwsh -File _reverse\roxy-newprofile.ps1 -Count 3
#    pwsh -File _reverse\roxy-newprofile.ps1 -Count 5 -Name bot -DryRun
#    pwsh -File _reverse\roxy-newprofile.ps1 -Count 2 -Proxy "socks5://user:pass@host:1080"
#    pwsh -File _reverse\roxy-newprofile.ps1 -Count 4 -Headless -Limit 4
# ============================================================
[CmdletBinding()]
param(
  [int]    $Count      = 1,
  [string] $Name       = $null,
  [string] $Proxy      = $null,      # "" or omitted = inherit the relay of the newest stock profile
  [string] $From       = $null,      # inherit machine fields from this dirId
  [switch] $Headless,
  [int]    $Limit      = 0,          # 0 = launch every created profile
  [int]    $DebugPortBase = 9400,
  [switch] $NoLaunch,
  [switch] $DryRun
)

$ErrorActionPreference = 'Stop'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path

# ---- 路径自动发现 ----
function Redact($p) {
  if (-not $p) { return $p }
  foreach ($e in 'LOCALAPPDATA','APPDATA','ProgramData','USERPROFILE') {
    $base = [Environment]::GetEnvironmentVariable($e)
    if ($base -and $p.StartsWith($base, [StringComparison]::OrdinalIgnoreCase)) { return "%$e%" + $p.Substring($base.Length) }
  }
  return $p
}
$P = (node "$Here\paths-cli.mjs" --json) | ConvertFrom-Json
if (-not $P.ok) { node "$Here\paths-cli.mjs"; throw "环境不满足（可用 --data-dir 或 ROXY_HOME 指定）" }
Write-Host "[env] 数据目录 $(Redact $P.dataDir)" -ForegroundColor DarkGray
Write-Host "[env] 内核 v$($P.coreVersion)" -ForegroundColor DarkGray

# ---------- 1. generate ----------
$mkArgs = @("$Here\mkprofile.mjs", '--count', $Count)
if ($Name)  { $mkArgs += @('--name',  $Name) }
if ($Proxy) { $mkArgs += @('--proxy', $Proxy) }
if ($From)  { $mkArgs += @('--from',  $From) }

Write-Host "=== generating $Count offline profile(s) ===" -ForegroundColor Cyan
node @mkArgs
if ($LASTEXITCODE -ne 0) { throw "mkprofile.mjs failed ($LASTEXITCODE)" }

$created = Get-Content (Join-Path $Here 'last-created.json') -Raw | ConvertFrom-Json
$ids = @($created | ForEach-Object { $_.dirId })
if (-not $ids.Count) { throw 'no profiles were created' }

# ---------- 2. launch ----------
if ($NoLaunch) {
  Write-Host "`n[skip] -NoLaunch: profiles created but not started" -ForegroundColor Yellow
  Write-Host "       dirIds: $($ids -join ', ')"
  return
}

Write-Host "`n=== launching $($ids.Count) profile(s) ===" -ForegroundColor Cyan
$lnArgs = @{
  DirId         = $ids
  DebugPortBase = $DebugPortBase
}
if ($Limit -gt 0) { $lnArgs.Limit = $Limit }
if ($Headless)    { $lnArgs.Headless = $true }
if ($DryRun)      { $lnArgs.DryRun   = $true }

& "$Here\roxy-direct-launch.ps1" @lnArgs

if ($DryRun) { return }

# ---------- 3. health check ----------
Write-Host "`n=== waiting for cores to expose DevTools ===" -ForegroundColor Cyan
$n = 0
foreach ($id in $ids) {
  if ($Limit -gt 0 -and $n -ge $Limit) { break }
  $port = $DebugPortBase + $n
  $ok = $false
  foreach ($try in 1..12) {
    Start-Sleep -Milliseconds 1200
    try {
      $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/json/version" -TimeoutSec 3
      $b = ($r.Content | ConvertFrom-Json).Browser
      Write-Host ("  [ok]   port {0,-5} {1}  browser={2}" -f $port, $id, $b) -ForegroundColor Green
      $ok = $true; break
    } catch { }
  }
  if (-not $ok) { Write-Host ("  [warn] port {0,-5} {1}  no DevTools after ~15s" -f $port, $id) -ForegroundColor Yellow }
  $n++
}

Write-Host "`n[done] fingerprint check for one of them:" -ForegroundColor Cyan
Write-Host "       node `"$Here\cdpcheck.mjs`" $DebugPortBase"
