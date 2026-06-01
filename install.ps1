param(
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
$AppName = "envguard"
$RepoSpec = "git+https://github.com/Tresnanda/envguard.git"

function Confirm-Step($Prompt, $DefaultYes = $true) {
    if ($Yes) { return $true }
    $suffix = if ($DefaultYes) { "[Y/n]" } else { "[y/N]" }
    $answer = Read-Host "$Prompt $suffix"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $DefaultYes }
    return @("y", "yes") -contains $answer.ToLowerInvariant()
}

function Read-Choice($Prompt, $Default) {
    if ($Yes) { return $Default }
    $answer = Read-Host "$Prompt [$Default]"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
    return $answer
}

function Find-Python {
    foreach ($candidate in @("py", "python3", "python")) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd) { return $candidate }
    }
    throw "Python 3 is required."
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

try {
    & $Python -m pipx --version *> $null
    Write-Host "[ok] pipx found"
} catch {
    if (Confirm-Step "Install pipx with this Python?" $true) {
        & $Python -m pip install --user pipx
        & $Python -m pipx ensurepath *> $null
    } else {
        throw "Install pipx and rerun this installer."
    }
}

if (Get-Command supabase -ErrorAction SilentlyContinue) {
    Write-Host "[ok] Supabase CLI found"
} else {
    Write-Host "[info] Supabase CLI not found; envguard can still scan local .env files"
}
Set-SupabaseToken

Write-Host "Installing $AppName from GitHub..."
& $Python -m pipx install --force $RepoSpec

if (Get-Command $AppName -ErrorAction SilentlyContinue) {
    & $AppName --help *> $null
    Write-Host "[ok] $AppName installed"
} else {
    Write-Host "[warn] $AppName installed, but pipx bin dir may not be on PATH."
    Write-Host "Run: python -m pipx ensurepath"
}

Write-Host "Run envguard in your terminal to start the guided audit."
