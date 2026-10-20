# EdgeMCP ReversePS v1. Save this file and run it in a separate PowerShell window.
# Requires Windows PowerShell 5.1+, ssh.exe, and cmd.exe (native pipe wiring only).
# Commands run in THIS session's existing language mode. No policy changes,
# Add-Type, downloaded binaries, automatic elevation, or encoded commands.
# This intentionally gives the authenticated SSH server control of this worker.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Server,
    [ValidateRange(1, 65535)][int]$Port = 22,
    [string]$User,
    [string]$IdentityFile,
    [string]$KnownHostsFile,
    [string]$Label = 'PowerShell worker',
    [switch]$AcceptCommands,
    [ValidateRange(0, 86400)][int]$MaxSessionSeconds = 0
)

$ErrorActionPreference = 'Stop'
if (-not $AcceptCommands) {
    throw 'Read this script first. Add -AcceptCommands only if you authorize the SSH server/MCP callers to run commands in this session.'
}
if ($env:OS -ne 'Windows_NT') { throw 'This client currently supports Windows PowerShell only.' }
$mode = '' + $ExecutionContext.SessionState.LanguageMode
if ($mode -notin @('FullLanguage', 'ConstrainedLanguage')) { throw 'This language mode cannot run the worker.' }
if ($Label.Length -lt 1 -or $Label.Length -gt 64) { throw 'Label must contain 1-64 characters.' }
$ssh = (Get-Command ssh.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
$powerShell = Join-Path $PSHOME 'powershell.exe'
if (-not (Test-Path -LiteralPath $powerShell)) { throw 'Windows PowerShell executable not found.' }
$cmd = $env:ComSpec
if (-not $cmd -or -not (Test-Path -LiteralPath $cmd)) { throw 'cmd.exe is required for native pipe wiring.' }

function ConvertTo-ProtocolJson {
    param($Value)
    $json = $Value | ConvertTo-Json -Compress -Depth 5
    # PS5.1 native stdin is often ASCII. Escape UTF-16 code units with ordinary
    # string operations and primitive casts (allowed in ConstrainedLanguage).
    $characters = foreach ($character in $json.ToCharArray()) {
        $number = [int]$character
        if ($number -gt 127) { '\u{0:x4}' -f $number } else { '' + $character }
    }
    return ($characters -join '')
}

# Keep protocol files in the current user's local app-data area, not a shared
# working directory. Its inherited ACL must be private to this account.
$root = Join-Path $env:LOCALAPPDATA 'EdgeMCP\reverseps'
$null = New-Item -ItemType Directory -Path $root -Force
$work = Join-Path $root ('worker-' + $PID + '-' + (Get-Random))
$null = New-Item -ItemType Directory -Path $work
$incoming = Join-Path $work 'incoming.txt'
$outgoing = Join-Path $work 'reply.json'
$staging = Join-Path $work 'reply.tmp'
$stop = Join-Path $work 'stop'
$exitFile = Join-Path $work 'exit.txt'
$sshErrors = Join-Path $work 'ssh.err'
$batch = Join-Path $work 'wire.cmd'
$transport = $null
$previousPumpDir = $env:EDGEMCP_REVERSEPS_PUMP_DIR

try {
    $sshArgs = @('-T', '-p', ('' + $Port), '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                 '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3')
    if ($User) { $sshArgs += @('-l', $User) }
    if ($IdentityFile) { $sshArgs += @('-i', $IdentityFile, '-o', 'IdentitiesOnly=yes') }
    if ($KnownHostsFile) { $sshArgs += @('-o', ('UserKnownHostsFile=' + $KnownHostsFile)) }
    if ($Server.StartsWith('-')) { throw 'Server must not start with a dash.' }
    $sshArgs += $Server

    # Reject cmd metacharacters instead of attempting ambiguous shell escaping.
    # Never interpolate received commands into this batch file.
    foreach ($argument in (@($powerShell, $ssh, $cmd, $work) + $sshArgs)) {
        if ($argument -match '["%!^&|<>\r\n]' -or $argument -match '[^\x20-\x7e]') {
            throw 'Paths/SSH arguments must be printable ASCII without cmd metacharacters (command/output text can be Unicode).'
        }
    }
    $quotedArgs = ($sshArgs | ForEach-Object { '"' + $_ + '"' }) -join ' '

    # A FIXED producer only reads reply.json and emits heartbeats. It never
    # evaluates received commands. A native OS pipe avoids PS5.1's input buffering.
    $pump = @'
$ErrorActionPreference='Stop'; $d=$env:EDGEMCP_REVERSEPS_PUMP_DIR;
$r=Join-Path $d 'reply.json'; $s=Join-Path $d 'stop'; $e=Join-Path $d 'exit.txt';
while(-not (Test-Path -LiteralPath $s) -and -not (Test-Path -LiteralPath $e)) {
 if(Test-Path -LiteralPath $r) {
  $line=(Get-Content -Raw -LiteralPath $r) -replace '[\r\n]+$',''; Remove-Item -LiteralPath $r;
  Write-Output $line;
 };
 Write-Output (@{type='ping'}|ConvertTo-Json -Compress);
 Start-Sleep -Milliseconds 100;
};
'@
    $pumpLine = $pump -replace '\r?\n', ' '
    $wire = '"' + $powerShell + '" -NoProfile -NonInteractive -Command "' + $pumpLine +
            '" | ("' + $ssh + '" ' + $quotedArgs + ' > "' + $incoming + '" 2> "' + $sshErrors +
            '" & echo done > "' + $exitFile + '")'
    Set-Content -LiteralPath $batch -Encoding ASCII -Value @('@echo off', $wire)
    $env:EDGEMCP_REVERSEPS_PUMP_DIR = $work
    $transport = Start-Process -FilePath $cmd -ArgumentList @('/d', '/s', '/c', ('""' + $batch + '""')) -WindowStyle Hidden -PassThru
    $env:EDGEMCP_REVERSEPS_PUMP_DIR = $previousPumpDir

    Write-Host ('ReversePS connecting to ' + $Server + ':' + $Port + '; language mode: ' + $mode)
    Write-Host 'Only use a verified SSH host key. Ctrl+C stops the worker. Authentication uses an SSH key/agent.'
    $marker = '# EDGEMCP_REVERSEPS_' + 'BOOTSTRAP_END_V1'
    $bootstrapDone = $false
    $lastLine = 0
    $startedAt = Get-Date -UFormat %s
    $welcome = $false
    $finished = $false
    $clientId = $null
    $seenRequests = @{}
    while ($true) {
        $elapsed = (Get-Date -UFormat %s) - $startedAt
        if ($MaxSessionSeconds -and $elapsed -ge $MaxSessionSeconds) { break }
        if (-not $welcome -and $elapsed -gt 30) { throw 'Worker handshake timed out; verify host key/authentication and --reversePS.' }
        if (Test-Path -LiteralPath $incoming) {
            $raw = Get-Content -Raw -LiteralPath $incoming
            if ($raw.Length -gt 4194304) { throw 'Session receive limit reached; restart worker to continue.' }
            $lines = $raw -split "`n"
            # Process COMPLETE lines only; the final element may still be partial.
            for ($index = $lastLine; $index -lt ($lines.Count - 1); $index++) {
                $line = $lines[$index] -replace '\r$', ''
                if (-not $bootstrapDone) {
                    if ($line -eq $marker) {
                        $bootstrapDone = $true
                        $hello = @{type='hello'; version=1; accept_commands=$true; label=$Label; language_mode=$mode}
                        Set-Content -LiteralPath $staging -Encoding ASCII -Value (ConvertTo-ProtocolJson $hello)
                        Move-Item -LiteralPath $staging -Destination $outgoing -Force
                    }
                    continue # Bootstrap is DATA; never evaluate it.
                }
                if ($line.Length -gt 65536) { throw 'Protocol frame too large.' }
                $message = $line | ConvertFrom-Json
                if ($message.type -eq 'close') { $finished = $true; break }
                if ($message.type -eq 'welcome' -and -not $welcome) {
                    $clientId = $message.client_id
                    if ($message.version -ne 1 -or $clientId -notmatch '^[a-f0-9]{32}$') { throw 'Invalid worker welcome.' }
                    $welcome = $true
                    Write-Host ('Ready. MCP client_id: ' + $clientId)
                    continue
                }
                if ($message.type -ne 'command' -or -not $welcome) { throw 'Unexpected server protocol frame.' }
                $requestId = '' + $message.id
                if ($requestId -notmatch '^[a-f0-9]{32}$' -or $seenRequests[$requestId]) { throw 'Invalid or repeated command ID.' }
                if ($message.command -isnot [string] -or $message.command.Length -gt 4096) { throw 'Invalid command size.' }
                $seenRequests[$requestId] = $true
                $text = ''
                $truncated = $false
                $success = $true
                $nativeExit = $null
                $script:reversePsNativeExit = $null
                Write-Host ('Executing request ' + $requestId)
                try {
                    # Child scope, SAME runspace/language mode, no new unrestricted
                    # command host. All streams are returned as bounded plain text.
                    & {
                        param($CommandText)
                        $ErrorActionPreference = 'Stop'
                        $LASTEXITCODE = 0
                        Invoke-Expression -Command $CommandText
                        $script:reversePsNativeExit = $LASTEXITCODE
                    } $message.command *>&1 | ForEach-Object {
                        $chunk = $_ | Out-String -Width 200
                        $remaining = 8192 - $text.Length
                        if ($remaining -gt 0) {
                            if ($chunk.Length -gt $remaining) { $text += $chunk.Substring(0, $remaining); $truncated = $true }
                            else { $text += $chunk }
                        } else { $truncated = $true }
                    }
                    $nativeExit = $script:reversePsNativeExit
                    if ($null -ne $nativeExit -and $nativeExit -ne 0) { $success = $false }
                } catch {
                    $success = $false
                    $chunk = $_ | Out-String -Width 200
                    $remaining = 8192 - $text.Length
                    if ($remaining -gt 0) {
                        if ($chunk.Length -gt $remaining) { $text += $chunk.Substring(0, $remaining); $truncated = $true }
                        else { $text += $chunk }
                    } else { $truncated = $true }
                }
                # Never truncate between the two UTF-16 code units of a pair.
                if ($text.Length -gt 0) {
                    $lastCodeUnit = [int]$text[$text.Length - 1]
                    if ($lastCodeUnit -ge 55296 -and $lastCodeUnit -le 56319) {
                        $text = $text.Substring(0, $text.Length - 1)
                        $truncated = $true
                    }
                }
                $reply = @{type='result'; id=$requestId; success=$success; output=$text; truncated=$truncated; native_exit_code=$nativeExit}
                Set-Content -LiteralPath $staging -Encoding ASCII -Value (ConvertTo-ProtocolJson $reply)
                Move-Item -LiteralPath $staging -Destination $outgoing -Force
            }
            $lastLine = $lines.Count - 1
        }
        if ($finished) { break }
        if (Test-Path -LiteralPath $exitFile) {
            $detail = ''
            if (Test-Path -LiteralPath $sshErrors) { $detail = Get-Content -Raw -LiteralPath $sshErrors }
            throw ('SSH transport exited. Check authentication, host key, and server availability. ' + $detail)
        }
        Start-Sleep -Milliseconds 100
    }
} finally {
    $env:EDGEMCP_REVERSEPS_PUMP_DIR = $previousPumpDir
    # Signal the fixed producer to close stdin. Server EOF handling closes SSH.
    if ($transport) {
        Set-Content -LiteralPath $stop -Encoding ASCII -Value 'stop'
        for ($i = 0; $i -lt 50; $i++) {
            if (-not (Get-Process -Id $transport.Id -ErrorAction SilentlyContinue)) { break }
            Start-Sleep -Milliseconds 100
        }
        if (Get-Process -Id $transport.Id -ErrorAction SilentlyContinue) {
            Write-Warning ('Transport has not exited yet. Inspect PID ' + $transport.Id + ' and private directory ' + $work)
        } else { Remove-Item -LiteralPath $work -Recurse -Force }
    } else { Remove-Item -LiteralPath $work -Recurse -Force }
}
