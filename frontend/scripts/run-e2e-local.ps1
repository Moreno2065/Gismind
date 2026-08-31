[CmdletBinding()]
param(
    [string]$FrontendEnvPath = (Join-Path $PSScriptRoot '..\.env.local'),
    [string]$BackendEnvPath = (Join-Path $PSScriptRoot '..\..\backend\.env'),
    [string]$BackendPython = (Join-Path $PSScriptRoot '..\..\backend\.venv\Scripts\python.exe'),
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PlaywrightArgs
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Import-DotEnvFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "E2E configuration file is missing: $Path"
    }

    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*(?:#|$)') {
            continue
        }
        if ($line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
            throw "Invalid dotenv line in ${Path}: $line"
        }

        $name = $matches[1]
        $value = $matches[2]
        if ($value.Length -ge 2 -and (
            ($value.StartsWith('"') -and $value.EndsWith('"')) -or
            ($value.StartsWith("'") -and $value.EndsWith("'"))
        )) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        Set-Item -Path "Env:$name" -Value $value
    }
}

function Require-EnvironmentValue {
    param([Parameter(Mandatory = $true)][string]$Name)

    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "Required E2E environment variable is empty: $Name"
    }
}

function Assert-PortFree {
    param([Parameter(Mandatory = $true)][int]$Port)

    $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    if ($listener) {
        $owners = ($listener | Select-Object -ExpandProperty OwningProcess -Unique) -join ', '
        throw "E2E port $Port is already in use by PID(s): $owners"
    }
}

function Wait-HttpReady {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process,
        [Parameter(Mandatory = $true)][string]$Name,
        [int]$TimeoutSeconds = 120
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($Process.HasExited) {
            throw "$Name exited before becoming ready (exit code $($Process.ExitCode))"
        }
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
                return
            }
        }
        catch {
            Start-Sleep -Milliseconds 250
        }
    }
    throw "$Name did not become ready at $Uri within $TimeoutSeconds seconds"
}

function Stop-OwnedProcess {
    param(
        [System.Diagnostics.Process]$Process,
        [Parameter(Mandatory = $true)][string]$Name
    )

    if ($null -eq $Process -or $Process.HasExited) {
        return
    }
    Stop-Process -Id $Process.Id -ErrorAction SilentlyContinue
    if (-not $Process.WaitForExit(5000)) {
        Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
        [void]$Process.WaitForExit(5000)
    }
    Write-Host "Stopped owned $Name process PID $($Process.Id)"
}

function Write-LogTail {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        Get-Content -LiteralPath $Path -Tail 80
    }
}

Import-DotEnvFile -Path $FrontendEnvPath
Import-DotEnvFile -Path $BackendEnvPath

foreach ($name in @('VITE_AMAP_KEY', 'VITE_AMAP_SECURITY_CODE', 'AMAP_KEY')) {
    Require-EnvironmentValue -Name $name
}

if (-not (Test-Path -LiteralPath $BackendPython -PathType Leaf)) {
    throw "Playwright FastAPI interpreter is missing: $BackendPython"
}

$env:GISMIND_BACKEND_PYTHON = $BackendPython
$frontendRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$backendRoot = (Resolve-Path (Join-Path $frontendRoot '..\backend')).Path
$viteEntry = (Resolve-Path (Join-Path $frontendRoot 'node_modules\vite\bin\vite.js')).Path
$nodeCommand = (Get-Command node -ErrorAction Stop).Source
$e2ePort = if ($env:GISMIND_E2E_PORT) { [int]$env:GISMIND_E2E_PORT } else { 18000 }
$vitePort = if ($env:GISMIND_E2E_VITE_PORT) { [int]$env:GISMIND_E2E_VITE_PORT } else { 15173 }

Assert-PortFree -Port $e2ePort
Assert-PortFree -Port $vitePort

$env:GISMIND_E2E_PORT = [string]$e2ePort
$env:GISMIND_E2E_VITE_PORT = [string]$vitePort
$env:GISMIND_TEST_REDIS_URL = if ($env:GISMIND_TEST_REDIS_URL) { $env:GISMIND_TEST_REDIS_URL } else { 'redis://localhost:6379/15' }
$env:GISMIND_E2E_HOST = '127.0.0.1'
$env:GISMIND_E2E_UPLOAD_TTL_S = '5'
$env:GISMIND_VITE_API_TARGET = "http://127.0.0.1:$e2ePort"
$env:GISMIND_E2E_EXTERNAL_SERVERS = '1'
$env:APP_ENV = 'dev'

$logRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("gismind-e2e-" + [Guid]::NewGuid().ToString('N'))
[void](New-Item -ItemType Directory -Path $logRoot)
$backendStdout = Join-Path $logRoot 'backend.stdout.log'
$backendStderr = Join-Path $logRoot 'backend.stderr.log'
$viteStdout = Join-Path $logRoot 'vite.stdout.log'
$viteStderr = Join-Path $logRoot 'vite.stderr.log'
$backendProcess = $null
$viteProcess = $null
$playwrightExitCode = 1

try {
    $backendProcess = Start-Process -FilePath $BackendPython `
        -ArgumentList @('scripts/e2e_awaiting_server.py') `
        -WorkingDirectory $backendRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $backendStdout `
        -RedirectStandardError $backendStderr `
        -PassThru
    Wait-HttpReady -Uri "http://127.0.0.1:$e2ePort/api/health" -Process $backendProcess -Name 'FastAPI'

    $viteProcess = Start-Process -FilePath $nodeCommand `
        -ArgumentList @($viteEntry, '--host', '127.0.0.1', '--port', [string]$vitePort, '--strictPort') `
        -WorkingDirectory $frontendRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $viteStdout `
        -RedirectStandardError $viteStderr `
        -PassThru
    Wait-HttpReady -Uri "http://127.0.0.1:$vitePort/" -Process $viteProcess -Name 'Vite'

    Push-Location $frontendRoot
    try {
        npm run test:e2e -- @PlaywrightArgs
        $playwrightExitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
}
finally {
    Stop-OwnedProcess -Process $viteProcess -Name 'Vite'
    Stop-OwnedProcess -Process $backendProcess -Name 'FastAPI'
    if ($playwrightExitCode -ne 0) {
        Write-Host '--- Vite stderr (tail) ---'
        Write-LogTail -Path $viteStderr
        Write-Host '--- FastAPI stderr (tail) ---'
        Write-LogTail -Path $backendStderr
    }
    $resolvedLogRoot = [System.IO.Path]::GetFullPath($logRoot)
    $resolvedTempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
    if ($resolvedLogRoot.StartsWith($resolvedTempRoot, [StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path -Leaf $resolvedLogRoot).StartsWith('gismind-e2e-')) {
        Remove-Item -LiteralPath $resolvedLogRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

exit $playwrightExitCode
