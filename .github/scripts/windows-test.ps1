# Shared by the Windows smoke tests in .github/workflows/build.yml: start the app and check that its server answers
# AND its window opens. (The server starts before the window, so a window that fails to load, e.g.
# pywebview's .NET DLLs being blocked, shows an "Unhandled exception in script" box instead.)
function Test-AppOpens([string]$exe, [string]$data) {
  $p = Start-Process -PassThru $exe
  try {
    $up = $false
    foreach ($i in 1..60) {
      try { Invoke-RestMethod http://127.0.0.1:8090/healthz | Out-Null; $up = $true; break } catch { Start-Sleep 1 }
    }
    if (-not $up) { Get-Content "$data\home-finance.log" -ErrorAction SilentlyContinue; throw "The app's server didn't start" }
    $title = ""
    foreach ($i in 1..30) {
      $p.Refresh()
      if ($p.HasExited) { throw "The app quit (exit code $($p.ExitCode))" }
      $title = $p.MainWindowTitle
      if ($title -eq "Home Finance") { "Window open: $title"; return }
      if ($title -like "*exception*") { break }
      Start-Sleep 1
    }
    Get-Content "$data\home-finance.log" -ErrorAction SilentlyContinue
    throw "The app's window didn't open (window title: '$title')"
  } finally {
    Stop-Process $p -Force -ErrorAction SilentlyContinue
  }
}
