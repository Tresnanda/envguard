param(
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
$AppName = "envguard"
$RepoSlug = "Tresnanda/envguard"
$RepoUrl = "https://github.com/$RepoSlug"
$RepoSpec = "git+https://github.com/Tresnanda/envguard.git"
$MinimumPythonMajor = 3
$MinimumPythonMinor = 9

function Confirm-Step($Prompt, $DefaultYes = $true) {
    if ($Yes) { return $true }
    $suffix = if ($DefaultYes) { "[Y/n]" } else { "[y/N]" }
    $answer = Read-Host "$Prompt $suffix"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $DefaultYes }
    return @("y", "yes") -contains $answer.ToLowerInvariant()
}

function Offer-StarRepo {
    if ($Yes) {
        Write-Host "Star it here: $RepoUrl"
        return
    }
    if (-not (Confirm-Step "If $AppName helps you, star the GitHub repo now?" $true)) {
        Write-Host "Star it here: $RepoUrl"
        return
    }
    if (Get-Command gh -ErrorAction SilentlyContinue) {
        try {
            & gh auth status *> $null
            if ($LASTEXITCODE -eq 0) {
                & gh repo star $RepoSlug *> $null
                if ($LASTEXITCODE -eq 0) {
                    Write-Host "[ok] Starred $RepoUrl"
                    return
                }
            }
        } catch {}
    }
    if ($env:GITHUB_TOKEN) {
        try {
            Invoke-RestMethod `
                -Method Put `
                -Uri "https://api.github.com/user/starred/$RepoSlug" `
                -Headers @{
                    "Accept" = "application/vnd.github+json"
                    "Authorization" = "Bearer $env:GITHUB_TOKEN"
                    "X-GitHub-Api-Version" = "2022-11-28"
                } *> $null
            Write-Host "[ok] Starred $RepoUrl"
            return
        } catch {}
    }
    Write-Host "Couldn't auto-star from this terminal."
    Write-Host "Star it here: $RepoUrl"
}

function Read-Choice($Prompt, $Default) {
    if ($Yes) { return $Default }
    $answer = Read-Host "$Prompt [$Default]"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
    return $answer
}

function Invoke-PythonCandidate($Candidate, [string[]]$Arguments) {
    $exe = $Candidate[0]
    $allArgs = @()
    if ($Candidate.Count -gt 1) {
        $allArgs += $Candidate[1..($Candidate.Count - 1)]
    }
    $allArgs += $Arguments
    & $exe @allArgs
}

function Test-PythonVersion($Candidate) {
    try {
        Invoke-PythonCandidate $Candidate @("-c", "import sys; raise SystemExit(0 if sys.version_info >= ($MinimumPythonMajor, $MinimumPythonMinor) else 1)") *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Resolve-PythonExecutable($Candidate) {
    $output = Invoke-PythonCandidate $Candidate @("-c", "import sys; print(sys.executable)")
    return ($output | Select-Object -First 1).Trim()
}

function Invoke-Pipx([string[]]$Arguments) {
    $exe = $script:PipxCommand[0]
    $allArgs = @()
    if ($script:PipxCommand.Count -gt 1) {
        $allArgs += $script:PipxCommand[1..($script:PipxCommand.Count - 1)]
    }
    $allArgs += $Arguments
    & $exe @allArgs
}

function Get-DataHome {
    if ($env:LOCALAPPDATA) { return $env:LOCALAPPDATA }
    if ($env:XDG_DATA_HOME) { return $env:XDG_DATA_HOME }
    return (Join-Path $HOME ".local/share")
}

function Get-PipxBinDir {
    if ($env:PIPX_BIN_DIR) { return $env:PIPX_BIN_DIR }
    return (Join-Path $HOME ".local/bin")
}

function Initialize-PipxBootstrap {
    $venvDir = Join-Path (Join-Path (Get-DataHome) $AppName) "pipx-bootstrap"
    Write-Host "pipx was not found; installing a private pipx helper..."
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $venvDir) *> $null
    & $Python @("-m", "venv", $venvDir)
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create a Python virtual environment for pipx. Install pipx manually, then rerun this installer."
    }
    $venvPython = Join-Path $venvDir "Scripts/python.exe"
    if (-not (Test-Path $venvPython)) {
        $venvPython = Join-Path $venvDir "bin/python"
    }
    & $venvPython @("-m", "pip", "install", "--upgrade", "pip", "pipx")
    $pipxExe = Join-Path $venvDir "Scripts/pipx.exe"
    if (-not (Test-Path $pipxExe)) {
        $pipxExe = Join-Path $venvDir "bin/pipx"
    }
    $script:PipxCommand = @($pipxExe)
}

function Find-Python {
    $candidates = @(
        @("py", "-3.13"),
        @("py", "-3.12"),
        @("py", "-3.11"),
        @("py", "-3.10"),
        @("py", "-3.9"),
        @("python3.13"),
        @("python3.12"),
        @("python3.11"),
        @("python3.10"),
        @("python3.9"),
        @("python3"),
        @("python")
    )
    foreach ($candidate in $candidates) {
        if ((Get-Command $candidate[0] -ErrorAction SilentlyContinue) -and (Test-PythonVersion $candidate)) {
            return Resolve-PythonExecutable $candidate
        }
    }
    throw "Python 3.9 or newer is required. Install it from https://www.python.org/downloads/ and rerun this installer."
}

function Read-SecretText($Prompt) {
    $secure = Read-Host $Prompt -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

function Save-UserSecret($Name, $Value) {
    [Environment]::SetEnvironmentVariable($Name, $Value, "User")
    Set-Item -Path "Env:$Name" -Value $Value
    Write-Host "[ok] Saved $Name as a user environment variable"
    Write-Host "Open a new terminal before using it in another session."
}

function Set-SupabaseToken {
    if ($Yes) { return }
    if ($env:SUPABASE_ACCESS_TOKEN) {
        Write-Host "Supabase: SUPABASE_ACCESS_TOKEN already set"
        return
    }
    Write-Host ""
    Write-Host "Supabase token was not found."
    Write-Host "Choose Supabase token setup:"
    Write-Host "1) Paste SUPABASE_ACCESS_TOKEN now"
    Write-Host "2) Show command to set it later"
    Write-Host "3) Skip Supabase token setup"
    $choice = Read-Choice "Choice" "1"
    switch ($choice) {
        "1" {
            $token = Read-SecretText "Enter SUPABASE_ACCESS_TOKEN"
            if (-not [string]::IsNullOrWhiteSpace($token)) {
                Save-UserSecret "SUPABASE_ACCESS_TOKEN" $token
            } else {
                Write-Host "[info] Empty token skipped"
            }
        }
        "2" {
            Write-Host "Run this later:"
            Write-Host '  [Environment]::SetEnvironmentVariable("SUPABASE_ACCESS_TOKEN", "your-token", "User")'
        }
        default {
            Write-Host "[info] Skipped Supabase token setup"
        }
    }
}

Write-Host "Install envguard"
Write-Host "This checks Python, installs with pipx, and can set up Supabase access."
$Python = Find-Python
Write-Host "[ok] Python: $(& $Python --version 2>&1)"

$script:PipxCommand = @()
if (Get-Command pipx -ErrorAction SilentlyContinue) {
    $script:PipxCommand = @("pipx")
    Write-Host "[ok] pipx found"
} else {
    $script:PipxCommand = @($Python, "-m", "pipx")
    try {
        Invoke-Pipx @("--version") *> $null
        Write-Host "[ok] pipx found"
    } catch {
        if (Confirm-Step "Install pipx with this Python?" $true) {
            Initialize-PipxBootstrap
        } else {
            throw "Install pipx and rerun this installer."
        }
    }
}

if (Get-Command supabase -ErrorAction SilentlyContinue) {
    Write-Host "[ok] Supabase CLI found"
} else {
    Write-Host "[info] Supabase CLI not found; envguard can still scan local .env files"
}
Set-SupabaseToken

Write-Host "Installing $AppName from GitHub..."
Invoke-Pipx @("install", "--python", $Python, "--force", $RepoSpec)

if (Get-Command $AppName -ErrorAction SilentlyContinue) {
    & $AppName --help *> $null
    Write-Host "[ok] $AppName installed"
} else {
    Write-Host "[warn] $AppName installed, but pipx bin dir may not be on PATH."
    Write-Host "Run: `$env:Path = `"$(Get-PipxBinDir);`$env:Path`""
}

Offer-StarRepo
Write-Host "Run envguard in your terminal to start the guided audit."
