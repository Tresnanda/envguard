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

function Find-Python {
    foreach ($candidate in @("py", "python3", "python")) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd) { return $candidate }
    }
    throw "Python 3 is required."
}

Write-Host "envguard installer"
$Python = Find-Python
Write-Host "[ok] Python: $(& $Python --version 2>&1)"

try {
    & $Python -m pipx --version *> $null
    Write-Host "[ok] pipx found"
} catch {
    Write-Host "[warn] pipx not found"
    if (Confirm-Step "Install pipx with this Python?" $true) {
        & $Python -m pip install --user pipx
        & $Python -m pipx ensurepath *> $null
    } else {
        throw "Install pipx and rerun this installer."
    }
}

Write-Host "Environment checks:"
if (Get-Command supabase -ErrorAction SilentlyContinue) {
    Write-Host "[ok] Supabase CLI found"
} else {
    Write-Host "[info] Supabase CLI not found"
}
if ($env:SUPABASE_ACCESS_TOKEN) {
    Write-Host "[ok] SUPABASE_ACCESS_TOKEN is set"
} else {
    Write-Host "[info] SUPABASE_ACCESS_TOKEN is not set"
}

Write-Host "Installing $AppName from GitHub..."
& $Python -m pipx install --force $RepoSpec

if (Get-Command $AppName -ErrorAction SilentlyContinue) {
    & $AppName --help *> $null
    Write-Host "[ok] $AppName installed"
} else {
    Write-Host "[warn] $AppName installed, but pipx bin dir may not be on PATH."
    Write-Host "Run: python -m pipx ensurepath"
}

if (Confirm-Step "Run $AppName wizard now?" $true) {
    & $AppName wizard
}
