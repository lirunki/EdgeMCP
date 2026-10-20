# Run in the actual restricted PowerShell session. Does NOT change policy,
# connect to SSH, load .NET types, or run any received/untrusted commands.
# A small, known-content temporary file is created and removed for the I/O test.
# Do not use a different/unrestricted host or ExecutionPolicy Bypass to run this.

Write-Output 'EdgeMCP client capability check (no network connections)'
try { Write-Output ('PowerShell version: ' + $PSVersionTable.PSVersion) } catch { Write-Output 'PowerShell version: BLOCKED' }
try { Write-Output ('Language mode: ' + $ExecutionContext.SessionState.LanguageMode) } catch { Write-Output 'Language mode: BLOCKED' }

foreach ($name in @(
    'Get-Command', 'ConvertTo-Json', 'ConvertFrom-Json', 'ForEach-Object',
    'Start-Process', 'Start-Job', 'Receive-Job', 'Wait-Job', 'Stop-Job', 'Remove-Job',
    'Get-Content', 'Set-Content', 'Add-Content', 'New-Item', 'Remove-Item',
    'Join-Path', 'Start-Sleep', 'Out-String'
)) {
    try {
        $null = Get-Command -Name $name -CommandType Cmdlet -ErrorAction Stop
        Write-Output ($name + ': AVAILABLE (not necessarily authorized)')
    } catch { Write-Output ($name + ': UNAVAILABLE OR BLOCKED') }
}

try {
    $ssh = Get-Command -Name ssh.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1
    # -V prints version information and exits; no destination is supplied.
    $version = & $ssh.Source -V 2>&1
    if ($LASTEXITCODE -eq 0) { Write-Output ('ssh.exe -V: OK ' + $version) }
    else { Write-Output 'ssh.exe -V: FAILED' }
} catch { Write-Output 'ssh.exe -V: UNAVAILABLE OR BLOCKED' }

try {
    $result = & { 'known-constant' }
    if ($result -eq 'known-constant') { Write-Output 'Local script block: OK' }
    else { Write-Output 'Local script block: FAILED' }
} catch { Write-Output 'Local script block: BLOCKED' }

try {
    $value = '{"kind":"probe","text":"hello","id":1}' | ConvertFrom-Json -ErrorAction Stop
    $roundtrip = $value | ConvertTo-Json -Compress -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
    if ($roundtrip.kind -eq 'probe' -and $roundtrip.text -eq 'hello' -and $roundtrip.id -eq 1) {
        Write-Output 'JSON roundtrip: OK'
    } else { Write-Output 'JSON roundtrip: FAILED' }
} catch { Write-Output 'JSON roundtrip: BLOCKED' }

$created = $false
$tempPath = $null
try {
    $tempPath = Join-Path $env:TEMP ('edgemcp-capabilities-' + $PID + '.tmp') -ErrorAction Stop
    # Do not overwrite any pre-existing file. No -Force.
    $null = New-Item -Path $tempPath -ItemType File -ErrorAction Stop
    $created = $true
    Set-Content -LiteralPath $tempPath -Value 'first' -Encoding UTF8 -ErrorAction Stop
    Add-Content -LiteralPath $tempPath -Value 'second' -Encoding UTF8 -ErrorAction Stop
    $lines = @(Get-Content -LiteralPath $tempPath -ErrorAction Stop)
    if ($lines.Count -eq 2 -and $lines[0] -eq 'first' -and $lines[1] -eq 'second') {
        Write-Output 'Temporary-file read/write/append: OK'
    } else { Write-Output 'Temporary-file read/write/append: FAILED' }
} catch { Write-Output 'Temporary-file read/write/append: BLOCKED OR UNAVAILABLE' }
finally {
    if ($created) {
        try { Remove-Item -LiteralPath $tempPath -ErrorAction Stop }
        catch { Write-Output 'Temporary-file cleanup: FAILED (check TEMP for edgemcp-capabilities-*.tmp)' }
    }
}

Write-Output 'Check complete. No duplex transport tested, no jobs/processes left running.'
