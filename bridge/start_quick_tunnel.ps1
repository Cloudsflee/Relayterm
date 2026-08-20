param(
    [string]$Token = $env:RELAYTERM_TOKEN,
    [int]$Port = 18765
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
$cloudflaredCommand = Get-Command cloudflared -ErrorAction SilentlyContinue
if (-not $cloudflaredCommand) {
    $knownCloudflared = "D:\02_dev-tools\auto_test_chrome\cloudflared.exe"
    if (Test-Path -LiteralPath $knownCloudflared) {
        $cloudflaredPath = $knownCloudflared
    }
    else {
        throw "cloudflared was not found"
    }
}
else {
    $cloudflaredPath = $cloudflaredCommand.Source
}
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCommand) {
    throw "python was not found on PATH"
}
$pythonPath = $pythonCommand.Source
& $pythonPath -c "import winpty, websocket, qrcode, PIL" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing RelayTerm dependencies..."
    & $pythonPath -m pip install -r (Join-Path $projectRoot "bridge\requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        throw "bridge dependency installation failed"
    }
}
if ([string]::IsNullOrWhiteSpace($Token)) {
    $Token = (& $pythonPath -c "from pc.config import TokenStore; print(TokenStore().load_or_create())").Trim()
}
$env:RELAYTERM_TOKEN = $Token
$env:RELAYTERM_HOST = "127.0.0.1"
$env:RELAYTERM_PORT = "$Port"
$env:RELAYTERM_PROFILE_PATH = (& $pythonPath -c "from pc.config import profile_store; print(profile_store().path)").Trim()

Write-Host "Starting RelayTerm bridge on http://127.0.0.1:$Port"
$bridge = Start-Process -FilePath $pythonPath -ArgumentList "bridge\relay_bridge.py" -PassThru -WindowStyle Hidden
try {
    Start-Sleep -Milliseconds 800
    Write-Host "Starting Cloudflare Quick Tunnel (HTTP/2)."
    Write-Host "The temporary HTTPS address is printed below. The bearer token remains in DPAPI storage."
    & $cloudflaredPath tunnel --protocol http2 --url "http://127.0.0.1:$Port" --no-autoupdate
}
finally {
    if ($bridge -and -not $bridge.HasExited) {
        Stop-Process -Id $bridge.Id -Force -ErrorAction SilentlyContinue
    }
}
} finally {
    Pop-Location
}
