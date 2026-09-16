# ============================================================
#  RoxyBrowser - Open / Show Window
#
#  解决一个坑：直接 spawn RoxyChrome 时，主窗口 (Chrome_WidgetWin_1)
#  已经创建、标题和尺寸都对，但 style 里没有 WS_VISIBLE，从外面调
#  ShowWindow() 也拉不起来。原因是 RoxyChrome 先建隐藏窗口，等
#  CDP 客户端 attach 后才真正 Show()。官方启动器一上来就 puppeteer
#  attach，所以从来不会看到这个现象。
#
#  本脚本：找到运行中的实例 -> 用 CDP 让它显示 -> 拉到前台。
#
#  Examples:
#    pwsh -File roxy-open.ps1 -List
#    pwsh -File roxy-open.ps1
#    pwsh -File roxy-open.ps1 -DirId <DIR_ID> -Url https://example.com
#    pwsh -File roxy-open.ps1 -Port 62040 -WindowSize 1600,900
# ============================================================
[CmdletBinding()]
param(
  [string] $DirId,
  [int]    $Port = 0,
  [string] $Url,
  [string] $WindowSize,        # "1600,900"
  [string] $WindowPosition,    # "80,80"
  [switch] $List,              # 只列出运行中的实例
  [switch] $NoForeground
)

$ErrorActionPreference = 'Stop'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path

function Redact($p) {
  if (-not $p) { return $p }
  foreach ($e in 'LOCALAPPDATA', 'APPDATA', 'ProgramData', 'USERPROFILE') {
    $base = [Environment]::GetEnvironmentVariable($e)
    if ($base -and $p.StartsWith($base, [StringComparison]::OrdinalIgnoreCase)) { return "%$e%" + $p.Substring($base.Length) }
  }
  return $p
}

# ---- Win32 ----
if (-not ('RoxyWin' -as [type])) {
  Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public class RoxyWin {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc f, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr p, EnumProc f, IntPtr l);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern int GetWindowThreadProcessId(IntPtr h, out int pid);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool AttachThreadInput(int a, int b, bool f);
  [DllImport("kernel32.dll")] public static extern int GetCurrentThreadId();

  static string Cls(IntPtr h) { var s = new StringBuilder(128); GetClassNameW(h, s, 128); return s.ToString(); }

  // 主浏览器窗口 = class Chrome_WidgetWin_1
  public static IntPtr FindBrowserWindow(int pid) {
    IntPtr found = IntPtr.Zero;
    EnumWindows(delegate(IntPtr h, IntPtr l) {
      int p; GetWindowThreadProcessId(h, out p);
      if (p == pid && Cls(h) == "Chrome_WidgetWin_1") { found = h; return false; }
      return true;
    }, IntPtr.Zero);
    return found;
  }

  public static string Describe(int pid) {
    IntPtr h = FindBrowserWindow(pid);
    if (h == IntPtr.Zero) return "hwnd=<none>";
    return string.Format("hwnd={0} visible={1} iconic={2}", h, IsWindowVisible(h), IsIconic(h));
  }

  public static bool Foreground(int pid) {
    IntPtr h = FindBrowserWindow(pid);
    if (h == IntPtr.Zero) return false;
    if (IsIconic(h)) ShowWindow(h, 9);   // SW_RESTORE
    int fg = GetForegroundWindow() == IntPtr.Zero ? 0 : GetWindowThreadProcessId(GetForegroundWindow(), out int _dummy);
    int me = GetCurrentThreadId();
    bool attached = false;
    if (fg != 0 && fg != me) attached = AttachThreadInput(fg, me, true);
    try {
      BringWindowToTop(h);
      ShowWindow(h, 5);                  // SW_SHOW
      return SetForegroundWindow(h);
    } finally { if (attached) AttachThreadInput(fg, me, false); }
  }
}
'@ -Language CSharp
}

# ---- 路径自动发现 ----
$P = (node "$Here\paths-cli.mjs" --json) | ConvertFrom-Json
if (-not $P.ok) { node "$Here\paths-cli.mjs"; throw "环境不满足（可用 --data-dir 或 ROXY_HOME 指定数据目录）" }
$CacheDir = $P.browserCacheDir

# ---- 枚举运行中的 RoxyChrome 实例 ----
$instances = @()
foreach ($proc in (Get-CimInstance Win32_Process -Filter "Name='RoxyChrome.exe'" -ErrorAction SilentlyContinue)) {
  $cmd = $proc.CommandLine
  # 只保留浏览器主进程：子进程（renderer/gpu/utility）命令行里带 --type=
  if ($cmd -match '--type=') { continue }
  $m = [regex]::Match($cmd, '--user-data-dir=(?:"([^"]+)"|(\S+))')
  if (-not $m.Success) { continue }
  $udd = if ($m.Groups[1].Success) { $m.Groups[1].Value } else { $m.Groups[2].Value }
  $id = Split-Path $udd -Leaf
  # 端口来源：命令行里写了固定端口就以它为准（此时 Chromium 不会写/刷新
  # DevToolsActivePort，profile 目录里那份是上一次运行残留的，会指错）；
  # 只有 --remote-debugging-port=0（自动分配）才读 DevToolsActivePort。
  $dp = 0
  $mp = [regex]::Match($cmd, '--remote-debugging-port=(\d+)')
  $cmdPort = if ($mp.Success) { [int]$mp.Groups[1].Value } else { -1 }
  $portFile = Join-Path $udd 'DevToolsActivePort'
  if ($cmdPort -gt 0) { $dp = $cmdPort }
  elseif (Test-Path $portFile) { $dp = [int]((Get-Content $portFile -First 1).Trim()) }
  $instances += [pscustomobject]@{ DirId = $id; Pid = $proc.ProcessId; Port = $dp; Headless = ($cmd -match '--headless') }
}

if ($List -or (-not $instances)) {
  if (-not $instances) { Write-Host "没有运行中的 RoxyChrome 实例。" -ForegroundColor Yellow }
  else {
    Write-Host "运行中的实例 ($($instances.Count))：" -ForegroundColor Cyan
    $instances | ForEach-Object {
      Write-Host ("  {0}  pid={1,-7} port={2,-6} {3}" -f $_.DirId, $_.Pid, $_.Port, ([RoxyWin]::Describe($_.Pid))) -ForegroundColor Gray
    }
  }
  if ($List) { return }
  throw "没有可操作的实例，请先用 roxy-direct-launch.ps1 启动一个。"
}

# ---- 选定目标 ----
$t = $null
if ($Port)        { $t = $instances | Where-Object Port   -eq $Port  | Select-Object -First 1 }
elseif ($DirId)   { $t = $instances | Where-Object DirId  -eq $DirId | Select-Object -First 1 }
else              { $t = $instances | Select-Object -First 1 }

if (-not $t) { Write-Host "没找到匹配的实例。" -ForegroundColor Red; $instances | Format-Table DirId,Pid,Port; exit 1 }
if (-not $t.Port) { Write-Host "实例 $($t.DirId) 没有调试端口（既无 DevToolsActivePort，命令行里也没有 --remote-debugging-port）。" -ForegroundColor Red; exit 1 }
if ($t.Headless) { Write-Host "实例 $($t.DirId) 是 --headless 启动的，本来就没有可见窗口。" -ForegroundColor Yellow }

Write-Host "[target] $($t.DirId)  pid=$($t.Pid)  port=$($t.Port)" -ForegroundColor Cyan
Write-Host "[before] $([RoxyWin]::Describe($t.Pid))" -ForegroundColor DarkGray

# ---- CDP 显示窗口 ----
$nodeArgs = @("$Here\show-window.mjs", "$($t.Port)")
if ($Url) { $nodeArgs += @('--url', $Url) }
if ($WindowSize) {
  $pos = if ($WindowPosition) { $WindowPosition } else { '80,80' }
  $nodeArgs += @('--bounds', "$pos,$WindowSize")
}
node @nodeArgs
if ($LASTEXITCODE -ne 0) { Write-Host "CDP 调用失败。" -ForegroundColor Red; exit $LASTEXITCODE }

# ---- 拉到前台 ----
if (-not $NoForeground) {
  Start-Sleep -Milliseconds 400
  $ok = [RoxyWin]::Foreground($t.Pid)
  Write-Host "[fg] SetForegroundWindow -> $ok" -ForegroundColor DarkGray
}

Write-Host "[after]  $([RoxyWin]::Describe($t.Pid))" -ForegroundColor Green
