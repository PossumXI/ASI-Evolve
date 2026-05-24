param(
  [switch] $Once,
  [int] $IntervalSeconds = 900,
  [int] $TimeoutSeconds = 25,
  [int] $PostHealWaitSeconds = 180,
  [int] $AutopilotLimitSeconds = 540,
  [int] $ProcessLimitSeconds = 120,
  [int] $LogRetention = 40,
  [switch] $DisableNotify
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = "python"
$StateRoot = Join-Path $Root ".arobi-evolve"
$LogRoot = Join-Path $StateRoot "logs"
$StatusRoot = Join-Path $StateRoot "status"
$HeartbeatPath = Join-Path $StatusRoot "bridge-heartbeat.json"
$MutexName = "Global\ArobiAsiEvolveGuardedBridge"

function Invoke-LoggedProcess {
  param(
    [Parameter(Mandatory = $true)]
    [string] $FilePath,
    [Parameter(Mandatory = $true)]
    [string[]] $ArgumentList,
    [Parameter(Mandatory = $true)]
    [string] $WorkingDirectory,
    [Parameter(Mandatory = $true)]
    [string] $LogPath,
    [Parameter(Mandatory = $true)]
    [int] $LimitSeconds
  )

  $safeName = ($ArgumentList -join "_") -replace '[^A-Za-z0-9_.-]', '_'
  if ($safeName.Length -gt 80) {
    $safeName = $safeName.Substring(0, 80)
  }
  $stdoutPath = Join-Path $LogRoot "$safeName.stdout.tmp"
  $stderrPath = Join-Path $LogRoot "$safeName.stderr.tmp"
  Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue

  "Running: $FilePath $($ArgumentList -join ' ')" | Add-Content -LiteralPath $LogPath -Encoding UTF8
  $process = Start-Process `
    -FilePath $FilePath `
    -ArgumentList $ArgumentList `
    -WorkingDirectory $WorkingDirectory `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutPath `
    -RedirectStandardError $stderrPath `
    -PassThru

  if (-not $process.WaitForExit($LimitSeconds * 1000)) {
    "Timed out after $LimitSeconds seconds; stopping PID $($process.Id)." | Add-Content -LiteralPath $LogPath -Encoding UTF8
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    if (Test-Path -LiteralPath $stdoutPath) {
      Get-Content -LiteralPath $stdoutPath -Tail 200 | Add-Content -LiteralPath $LogPath -Encoding UTF8
    }
    if (Test-Path -LiteralPath $stderrPath) {
      Get-Content -LiteralPath $stderrPath -Tail 200 | Add-Content -LiteralPath $LogPath -Encoding UTF8
    }
    Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    return 124
  }

  $process.WaitForExit()
  $process.Refresh()
  if (Test-Path -LiteralPath $stdoutPath) {
    Get-Content -LiteralPath $stdoutPath -Tail 400 | Add-Content -LiteralPath $LogPath -Encoding UTF8
  }
  if (Test-Path -LiteralPath $stderrPath) {
    Get-Content -LiteralPath $stderrPath -Tail 400 | Add-Content -LiteralPath $LogPath -Encoding UTF8
  }
  Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
  if ($null -eq $process.ExitCode) {
    return 0
  }
  return [int]$process.ExitCode
}

function Invoke-ArobiEvolvePass {
  New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
  New-Item -ItemType Directory -Force -Path $StatusRoot | Out-Null
  Get-ChildItem -Path $LogRoot -Filter "bridge-run-*.log" -File -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip $LogRetention |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }

  $mutex = [System.Threading.Mutex]::new($false, $MutexName)
  $lockTaken = $false
  $stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
  $logPath = Join-Path $LogRoot "bridge-run-$stamp.log"
  $startedAt = (Get-Date).ToUniversalTime().ToString("o")

  try {
    $lockTaken = $mutex.WaitOne([TimeSpan]::FromSeconds(1))
    if (-not $lockTaken) {
      @{
        version = 1
        status = "skipped_overlap"
        startedAt = $startedAt
        finishedAt = (Get-Date).ToUniversalTime().ToString("o")
        root = $Root
        logPath = $logPath
        intervalSeconds = $IntervalSeconds
      } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $HeartbeatPath -Encoding UTF8
      return $true
    }

    "Arobi ASI-Evolve bridge pass started $startedAt" | Set-Content -LiteralPath $logPath -Encoding UTF8
    "Root: $Root" | Add-Content -LiteralPath $logPath -Encoding UTF8
    "TimeoutSeconds: $TimeoutSeconds" | Add-Content -LiteralPath $logPath -Encoding UTF8
    "PostHealWaitSeconds: $PostHealWaitSeconds" | Add-Content -LiteralPath $logPath -Encoding UTF8
    "AutopilotLimitSeconds: $AutopilotLimitSeconds" | Add-Content -LiteralPath $logPath -Encoding UTF8
    "ProcessLimitSeconds: $ProcessLimitSeconds" | Add-Content -LiteralPath $logPath -Encoding UTF8
    @{
      version = 1
      status = "running"
      startedAt = $startedAt
      finishedAt = $null
      root = $Root
      logPath = $logPath
      intervalSeconds = $IntervalSeconds
      timeoutSeconds = $TimeoutSeconds
      postHealWaitSeconds = $PostHealWaitSeconds
      autopilotLimitSeconds = $AutopilotLimitSeconds
      processLimitSeconds = $ProcessLimitSeconds
      notifyEnabled = -not $DisableNotify
    } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $HeartbeatPath -Encoding UTF8

    $autopilotArgs = @(
      "-m", "arobi_integrations", "autopilot",
      "--write-report",
      "--heal",
      "--timeout", "$TimeoutSeconds",
      "--post-heal-wait", "$PostHealWaitSeconds"
    )
    if (-not $DisableNotify) {
      $autopilotArgs += "--notify"
    }

    $pythonExe = (Get-Command $Python -ErrorAction Stop).Source
    $autopilotExit = Invoke-LoggedProcess `
      -FilePath $pythonExe `
      -ArgumentList $autopilotArgs `
      -WorkingDirectory $Root `
      -LogPath $logPath `
      -LimitSeconds $AutopilotLimitSeconds
    "Autopilot exit: $autopilotExit" | Add-Content -LiteralPath $logPath -Encoding UTF8

    $processExit = Invoke-LoggedProcess `
      -FilePath $pythonExe `
      -ArgumentList @("-m", "arobi_integrations", "process") `
      -WorkingDirectory $Root `
      -LogPath $logPath `
      -LimitSeconds $ProcessLimitSeconds
    "Process exit: $processExit" | Add-Content -LiteralPath $logPath -Encoding UTF8

    $finishedAt = (Get-Date).ToUniversalTime().ToString("o")
    $ok = ($autopilotExit -eq 0 -and $processExit -eq 0)
    @{
      version = 1
      status = if ($ok) { "ok" } else { "failed" }
      startedAt = $startedAt
      finishedAt = $finishedAt
      root = $Root
      logPath = $logPath
      autopilotExit = $autopilotExit
      processExit = $processExit
      intervalSeconds = $IntervalSeconds
      timeoutSeconds = $TimeoutSeconds
      postHealWaitSeconds = $PostHealWaitSeconds
      autopilotLimitSeconds = $AutopilotLimitSeconds
      processLimitSeconds = $ProcessLimitSeconds
      notifyEnabled = -not $DisableNotify
    } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $HeartbeatPath -Encoding UTF8

    return $ok
  } catch {
    $finishedAt = (Get-Date).ToUniversalTime().ToString("o")
    "Bridge pass failed: $($_.Exception.Message)" | Add-Content -LiteralPath $logPath -Encoding UTF8
    @{
      version = 1
      status = "failed"
      startedAt = $startedAt
      finishedAt = $finishedAt
      root = $Root
      logPath = $logPath
      error = $_.Exception.Message
      intervalSeconds = $IntervalSeconds
      timeoutSeconds = $TimeoutSeconds
      postHealWaitSeconds = $PostHealWaitSeconds
      autopilotLimitSeconds = $AutopilotLimitSeconds
      processLimitSeconds = $ProcessLimitSeconds
      notifyEnabled = -not $DisableNotify
    } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $HeartbeatPath -Encoding UTF8
    return $false
  } finally {
    if ($lockTaken) {
      $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
  }
}

if ($Once) {
  $ok = Invoke-ArobiEvolvePass
  if ($ok) {
    exit 0
  }
  exit 1
}

while ($true) {
  Invoke-ArobiEvolvePass | Out-Null
  Start-Sleep -Seconds $IntervalSeconds
}
