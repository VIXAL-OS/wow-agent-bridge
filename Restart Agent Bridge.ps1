param([switch]$Check)

$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\pythonw.exe'
$taskLaunch = Join-Path $taskRoot 'Launch Agent Bridge.pyw'
$taskState = Join-Path $taskRoot 'state'

function Find-AgentBridge {
    $taskLaunchPattern = '(?i)(?:^|\s)"?' + [regex]::Escape($taskLaunch) + '"?(?=\s|$)'
    $taskVenv = Join-Path $taskRoot '.venv\Scripts\'
    @(Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
        Where-Object {
            $taskCommand = $_.CommandLine
            if (-not $taskCommand) { return $false }
            if ($taskCommand -match $taskLaunchPattern) { return $true }
            # Also recognize launches using this installation's interpreter.
            $taskOwnPython = $taskCommand.IndexOf($taskVenv, [StringComparison]::OrdinalIgnoreCase) -ge 0
            $taskRelativeScript = $taskCommand -match '(?i)\s"?(?:\.\\)?Launch Agent Bridge\.pyw"?(?=\s|$)'
            $taskModule = $taskCommand -match '(?i)\s-m\s+companion\.app(?=\s|$)'
            $taskOtherState = $taskCommand -match '(?i)--state(?:\s|=)' -and
                $taskCommand.IndexOf($taskState, [StringComparison]::OrdinalIgnoreCase) -lt 0
            $taskOwnPython -and ($taskRelativeScript -or ($taskModule -and -not $taskOtherState))
        })
}

$taskMutex = $null
$taskOwned = $false
try {
    if (-not (Test-Path -LiteralPath $taskPython -PathType Leaf) -or
        -not (Test-Path -LiteralPath $taskLaunch -PathType Leaf)) {
        throw 'Keep this restart script in the AgentBridge source folder beside Launch Agent Bridge.pyw.'
    }
    if ($Check) {
        $taskRunning = @(Find-AgentBridge)
        [pscustomobject]@{
            Launcher = $taskLaunch
            Python = $taskPython
            MatchingProcesses = @($taskRunning | ForEach-Object { $_.ProcessId })
        } | ConvertTo-Json
        exit 0
    }
    $taskHasher = [Security.Cryptography.SHA256]::Create()
    try {
        $taskHash = [BitConverter]::ToString(
            $taskHasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($taskRoot.ToLowerInvariant()))
        ).Replace('-', '').Substring(0, 16)
    } finally { $taskHasher.Dispose() }
    $taskMutex = New-Object Threading.Mutex($false, ('Local\AgentBridgeRestart-' + $taskHash))
    try { $taskOwned = $taskMutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $taskOwned = $true }
    if (-not $taskOwned) { exit 0 }

    # Send the normal close request once. Never force-kill Python, agents or WoW.
    foreach ($taskMatch in @(Find-AgentBridge)) {
        $taskProcess = Get-Process -Id $taskMatch.ProcessId -ErrorAction SilentlyContinue
        if ($taskProcess -and $taskProcess.MainWindowHandle -ne 0) {
            [void]$taskProcess.CloseMainWindow()
        }
    }
    $taskDeadline = [DateTime]::UtcNow.AddSeconds(15)
    while (@(Find-AgentBridge).Count -gt 0) {
        if ([DateTime]::UtcNow -ge $taskDeadline) {
            throw 'Agent Bridge is still open, possibly finishing a job. Let the job finish, close its window, then use this shortcut again. No second companion was started.'
        }
        Start-Sleep -Milliseconds 300
    }
    # pythonw opens the companion UI without a console window.
    Start-Process -FilePath $taskPython -ArgumentList ('"' + $taskLaunch + '"') -WorkingDirectory $taskRoot -WindowStyle Normal
    # Keep the mutex until the new process is discoverable, including rapid double-clicks.
    $taskDeadline = [DateTime]::UtcNow.AddSeconds(10)
    while (@(Find-AgentBridge).Count -eq 0) {
        if ([DateTime]::UtcNow -ge $taskDeadline) { throw 'Agent Bridge did not start. Try Launch Agent Bridge.pyw for the error details.' }
        Start-Sleep -Milliseconds 300
    }
} catch {
    if ($Check) { throw }
    Add-Type -AssemblyName System.Windows.Forms
    [void][Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Restart Agent Bridge', 'OK', 'Information')
    exit 1
} finally {
    if ($taskOwned) { $taskMutex.ReleaseMutex() }
    if ($taskMutex) { $taskMutex.Dispose() }
}
