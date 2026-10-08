Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$logDirectory = Join-Path $projectRoot "logs\notifications"
$logPath = Join-Path $logDirectory "notification-worker.log"

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null

function Write-NotificationLog {
    param (
        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    $Message | Out-File -FilePath $logPath -Append -Encoding utf8
}

$exitCode = 1
$locationWasPushed = $false

try {
    Push-Location -LiteralPath $projectRoot
    $locationWasPushed = $true

    Write-NotificationLog "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')] START"

    $pythonExecutable = $null
    $pythonArguments = @()
    $virtualEnvironmentCandidates = @(
        (Join-Path $projectRoot ".venv\Scripts\python.exe"),
        (Join-Path $projectRoot "venv\Scripts\python.exe")
    )

    foreach ($candidate in $virtualEnvironmentCandidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $pythonExecutable = $candidate
            break
        }
    }

    if ($null -eq $pythonExecutable) {
        $pyLauncher = Get-Command "py" -CommandType Application -ErrorAction SilentlyContinue

        if ($null -ne $pyLauncher) {
            $pythonExecutable = $pyLauncher.Source
            $pythonArguments = @("-3")
        }
        else {
            $pythonCommand = Get-Command "python" -CommandType Application -ErrorAction SilentlyContinue

            if ($null -eq $pythonCommand) {
                throw "No compatible Python interpreter was found."
            }

            $pythonExecutable = $pythonCommand.Source
        }
    }

    $pythonArguments += @(
        "-m",
        "flask",
        "--app",
        "app",
        "process-medication-notifications"
    )

    & $pythonExecutable @pythonArguments 2>&1 |
        Out-File -FilePath $logPath -Append -Encoding utf8
    $exitCode = $LASTEXITCODE
}
catch {
    Write-NotificationLog "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')] ERROR wrapper failure ($($_.Exception.GetType().Name))"
    $exitCode = 1
}
finally {
    Write-NotificationLog "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')] EXIT $exitCode"

    if ($locationWasPushed) {
        Pop-Location
    }
}

exit $exitCode
