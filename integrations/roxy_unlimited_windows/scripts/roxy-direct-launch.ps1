# ============================================================
#  RoxyBrowser - Direct Core Launcher (launcher bypass)
#
#  Starts Roxy's patched Chromium directly, mirroring the official
#  genChromeLaunchCLIArgs() recipe, so a direct window behaves like
#  an official one (fingerprint + proxy from lumi.conf, extensions,
#  workbench tab, window layout) WITHOUT the server account quota.
#
#  Examples:
#    pwsh -File roxy-direct-launch.ps1 -DryRun
#    pwsh -File roxy-direct-launch.ps1 -DirId <DIR_ID> -Headless
#    pwsh -File roxy-direct-launch.ps1 -Workbench -WindowSize 1600,900
#    pwsh -File roxy-direct-launch.ps1 -Extensions "C:\ext\a","C:\ext\b"
#    pwsh -File roxy-direct-launch.ps1 -NoShow        # 不自动还原窗口
#
#  默认启动后会自动把窗口还原并拉到前台（见 MANUAL 坑 6）。
# ============================================================
[CmdletBinding()]
param(
  [string[]] $DirId,
  [int]      $Limit = 0,               # 0 = launch every resolved profile
  [int]      $DebugPortBase = 9300,
  [string]   $StartUrl = 'about:blank',
  [switch]   $Headless,
  [switch]   $Workbench,               # open the local 工作台 tab, like the official launcher
  [int]      $AppPort = 45535,
  [string[]] $Extensions,              # paths -> --load-extension=a,b,c
  [string]   $WindowSize,              # "1600,900"
  [string]   $WindowPosition,          # "0,0"
  [switch]   $Maximized,
  [switch]   $NoGpu,
  [string[]] $ExtraArgs,               # raw passthrough (official startupParam equivalent)
  [switch]   $NoShow,                  # 启动后不要自动把窗口还原/拉到前台
  [switch]   $DryRun
)

$ErrorActionPreference = 'Stop'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path

# 显示用脱敏：把 C:\Users\<用户名>\... 换成 %USERPROFILE%\...
function Redact($p) {
  if (-not $p) { return $p }
  foreach ($e in 'LOCALAPPDATA', 'APPDATA', 'ProgramData', 'USERPROFILE') {
    $base = [Environment]::GetEnvironmentVariable($e)
    if ($base -and $p.StartsWith($base, [StringComparison]::OrdinalIgnoreCase)) { return "%$e%" + $p.Substring($base.Length) }
  }
  return $p
}

# ---- 路径自动发现：复用 Node 解析器，不重复实现 ----
$P = (node "$Here\paths-cli.mjs" --json) | ConvertFrom-Json
if (-not $P.ok) {
  node "$Here\paths-cli.mjs"
  throw "环境不满足，无法启动（可用 --data-dir 或 ROXY_HOME 指定数据目录）"
}
$CacheDir = $P.browserCacheDir
$BinDir   = $P.coreBinDir

function Get-CoreExe { return $P.coreExe }

# ---- base args: mirrors gp[] from browser-manager/constants.ts ----
$BaseArgs = @(
  '--disable-background-mode'
  '--disable-popup-blocking'
  '--no-first-run'
  '--no-default-browser-check'
  '--use-mock-keychain'
  '--no-sandbox'
  '--disable-setuid-sandbox'
  '--password-store=basic'
  '--disable-backgrounding-occluded-windows'
)

$exe = Get-CoreExe
Write-Host "[core] $(Redact $exe)" -ForegroundColor Cyan

# ---- resolve profiles ----
$profiles =
  if ($DirId) { $DirId | ForEach-Object { [pscustomobject]@{ Id = $_; Path = (Join-Path $CacheDir $_) } } }
  else {
    Get-ChildItem $CacheDir -Directory |
      Where-Object { Test-Path (Join-Path $_.FullName 'lumi.conf') } |
      ForEach-Object { [pscustomobject]@{ Id = $_.Name; Path = $_.FullName } }
  }
$profiles = @($profiles | Where-Object { Test-Path $_.Path })
if (-not $profiles) { throw "no profiles resolved (looked in $CacheDir)" }
if ($Limit -gt 0 -and $profiles.Count -gt $Limit) { $profiles = $profiles[0..($Limit - 1)] }

$extPaths = @()
if ($Extensions) { $extPaths = $Extensions | Where-Object { Test-Path $_ } }

Write-Host "[plan] launching $($profiles.Count) instance(s)$(if($extPaths.Count){" with $($extPaths.Count) extension(s)"})" -ForegroundColor Cyan

# ---- launch ----
$i = 0
foreach ($p in $profiles) {
  $a = [System.Collections.Generic.List[string]]::new()
  $a.AddRange([string[]]$BaseArgs)
  $a.Add("--user-data-dir=$($p.Path)")
  $a.Add("--remote-debugging-port=$($DebugPortBase + $i)")

  if ($Workbench) {
    $a.Add("http://127.0.0.1:$AppPort/dashboard.html?id=$($p.Id)&workspaceType=0")
  }
  if ($extPaths.Count) { $a.Add("--load-extension=$($extPaths -join ',')") }
  if ($ExtraArgs)      { $a.AddRange([string[]]$ExtraArgs) }

  if ($Maximized) { $a.Add('--start-maximized') }
  elseif ($WindowSize) {
    $a.Add("--window-size=$WindowSize")
    if ($WindowPosition) { $a.Add("--window-position=$WindowPosition") }
  }
  if ($NoGpu) { $a.Add('--disable-gpu') }
  if ($Headless) { $a.Add('--headless=new') }
  $a.Add($StartUrl)

  Write-Host ("[{0}] {1}  ->  debug port {2}" -f $i, $p.Id, ($DebugPortBase + $i)) -ForegroundColor Green
  if ($DryRun) {
    Write-Host ("      {0} {1}" -f (Redact $exe), (($a -join ' ') -replace [regex]::Escape($CacheDir), (Redact $CacheDir))) -ForegroundColor DarkGray
  } else {
    Start-Process -FilePath $exe -ArgumentList $a -WorkingDirectory (Split-Path $exe -Parent) | Out-Null

    # 直启的窗口常常以最小化/隐藏态起来（详见 MANUAL 坑 6）。
    # 官方启动器会把它还原，我们没人还原 —— 所以这里补上。
    if (-not $NoShow -and -not $Headless) {
      $port = $DebugPortBase + $i
      $ready = $false
      for ($k = 0; $k -lt 60; $k++) {
        Start-Sleep -Milliseconds 500
        try { $null = Invoke-WebRequest "http://127.0.0.1:$port/json/version" -TimeoutSec 2 -UseBasicParsing; $ready = $true; break } catch {}
      }
      if ($ready) {
        # 单独起进程调用，避免 roxy-open.ps1 里的 exit 把本脚本一起结束
        pwsh -NoProfile -File "$Here\roxy-open.ps1" -Port $port 2>&1 |
          Select-String '\[after\]' | ForEach-Object { Write-Host ("      " + $_.Line.Trim()) -ForegroundColor DarkGray }
      } else {
        Write-Host "      (调试端口 $port 未就绪，跳过窗口还原；可稍后手动跑 roxy-open.ps1)" -ForegroundColor Yellow
      }
    }
    Start-Sleep -Milliseconds 400
  }
  $i++
}

if ($DryRun) { Write-Host "[dry-run] nothing was started" -ForegroundColor Yellow }
else { Write-Host "[done] $i core process(es) started" -ForegroundColor Cyan }
