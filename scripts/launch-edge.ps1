# Launch Windows Edge with loopback-only CDP and a dedicated debugging profile.
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = $(if ($env:CDP_PORT) { [int]$env:CDP_PORT } else { 9222 }),
    [string]$EdgePath = $env:EDGE_PATH,
    [string]$ProfilePath = $env:EDGE_PROFILE_DIR,
    [switch]$PrintCommand
)

$ErrorActionPreference = 'Stop'

if (-not $EdgePath) {
    $Candidates = @()
    foreach ($Base in @(${env:ProgramFiles(x86)}, $env:ProgramFiles, $env:LOCALAPPDATA)) {
        if ($Base) {
            $Candidates += Join-Path $Base 'Microsoft\Edge\Application\msedge.exe'
        }
    }
    $EdgePath = $Candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
}
if (-not $EdgePath -or -not (Test-Path -LiteralPath $EdgePath -PathType Leaf)) {
    throw 'Microsoft Edge not found. Pass -EdgePath or set EDGE_PATH to its executable.'
}
if (-not $ProfilePath) {
    if (-not $env:LOCALAPPDATA) {
        throw 'LOCALAPPDATA is unavailable. Pass -ProfilePath for a dedicated local profile.'
    }
    $ProfilePath = Join-Path $env:LOCALAPPDATA 'EdgeMCP\debug-profile'
}
if ($ProfilePath.Contains('"') -or $EdgePath.Contains('"')) {
    throw 'Paths must not contain double quotes.'
}

# Start-Process joins arguments into a command line. Quote the profile argument
# explicitly so spaces in Windows paths are preserved.
$EdgeArgs = @(
    "--remote-debugging-port=$Port",
    '--remote-debugging-address=127.0.0.1',
    ('"--user-data-dir={0}"' -f $ProfilePath),
    '--no-first-run',
    '--no-default-browser-check',
    'about:blank'
)
if ($PrintCommand) {
    Write-Output ('"{0}" {1}' -f $EdgePath, ($EdgeArgs -join ' '))
    return
}

Start-Process -FilePath $EdgePath -ArgumentList $EdgeArgs
Write-Host "Edge launch requested. CDP: http://127.0.0.1:$Port"
Write-Host 'This profile is separate from the normal browser profile.'
Write-Host "Connect MCP with: python src/server.py --cdp 127.0.0.1:$Port"
