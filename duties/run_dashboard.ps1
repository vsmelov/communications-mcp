<#
.SYNOPSIS
  Дашборд дежурств (http://127.0.0.1:18210/) отдельным скрытым процессом. Singleton: если уже отвечает —
  только печатает адрес и выходит.

.EXAMPLE
  powershell -NoProfile -File C:\Users\v\PycharmProjects\claude-workspace\communications-mcp\duties\run_dashboard.ps1
  powershell -NoProfile -File ...\duties\run_dashboard.ps1 -Open      # и открыть в браузере

.NOTES
  Передний план (для отладки): из папки communications-mcp — python -m duties
  Лог фонового процесса: duties\.runtime\dashboard.log (+ dashboard.err.log)
  Остановить: Stop-Process -Id (Get-Content duties\.runtime\dashboard.json | ConvertFrom-Json).pid
#>
param(
  [int]$Port = 18210,
  [switch]$Open
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot                 # communications-mcp
$runtime = Join-Path $PSScriptRoot '.runtime'
New-Item -ItemType Directory -Force -Path $runtime | Out-Null

function Test-Dashboard([int]$p) {
  try {
    $req = [System.Net.WebRequest]::Create("http://127.0.0.1:$p/api/ping")
    $req.Proxy = $null                                   # системный прокси VPN перехватывает даже loopback
    $req.Timeout = 1500
    $resp = $req.GetResponse()
    $body = (New-Object System.IO.StreamReader($resp.GetResponseStream())).ReadToEnd()
    $resp.Close()
    return ($body -match 'claude-duties-dashboard')
  } catch {
    return $false
  }
}

foreach ($p in @($Port, 18211)) {
  if (Test-Dashboard $p) {
    Write-Output "дашборд уже запущен: http://127.0.0.1:$p/"
    if ($Open) { Start-Process "http://127.0.0.1:$p/" }
    exit 0
  }
}

$py = 'C:\Python312\pythonw.exe'                         # без окна; тот же Python, что у MCP-сервера
if (-not (Test-Path $py)) { $py = 'C:\Python312\python.exe' }
if (-not (Test-Path $py)) { $py = (Get-Command python -ErrorAction Stop).Source }

Start-Process -FilePath $py -ArgumentList @('-m', 'duties', 'dashboard', '--port', "$Port") `
  -WorkingDirectory $root -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $runtime 'dashboard.log') `
  -RedirectStandardError (Join-Path $runtime 'dashboard.err.log')

for ($i = 0; $i -lt 20; $i++) {
  Start-Sleep -Milliseconds 500
  foreach ($p in @($Port, 18211)) {
    if (Test-Dashboard $p) {
      Write-Output "дашборд запущен: http://127.0.0.1:$p/"
      if ($Open) { Start-Process "http://127.0.0.1:$p/" }
      exit 0
    }
  }
}
Write-Output "дашборд не ответил за 10 с — смотри $runtime\dashboard.err.log"
exit 1
