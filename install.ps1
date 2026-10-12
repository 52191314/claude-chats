# PowerShell installer for claude-chats
$ErrorActionPreference = "Stop"

Write-Host "Installing claude-chats..." -ForegroundColor Cyan

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$localBin = Join-Path $HOME ".local\bin"

if (!(Test-Path $localBin)) {
    New-Item -ItemType Directory -Path $localBin -Force | Out-Null
}

# 1. Copy CLI files to ~/.local/bin
Copy-Item (Join-Path $scriptDir "claude_chats.py") (Join-Path $localBin "claude_chats.py") -Force
Copy-Item (Join-Path $scriptDir "claude-chats.bat") (Join-Path $localBin "claude-chats.bat") -Force
Copy-Item (Join-Path $scriptDir "claude-chats.bat") (Join-Path $localBin "claude-history.bat") -Force
Copy-Item (Join-Path $scriptDir "claude-chats.bat") (Join-Path $localBin "csess.bat") -Force

Write-Host "[+] Copied scripts to $localBin" -ForegroundColor Green

# 2. Add PowerShell profile functions
$profilePath = $PROFILE
$profileDir = Split-Path -Parent $profilePath
if (!(Test-Path $profileDir)) {
    New-Item -ItemType Directory -Path $profileDir -Force | Out-Null
}

$functionLines = @(
    "",
    "# --- claude-chats & csess (Claude Code Sessions Manager) ---",
    "function claude-chats {",
    "    `$resume_cmd = `"`$HOME\.claude_resume_cmd.ps1`"",
    "    if (Test-Path `$resume_cmd) { Remove-Item `$resume_cmd -Force }",
    "",
    "    & `"`$HOME\.local\bin\claude-chats.bat`" @args",
    "",
    "    if (Test-Path `$resume_cmd) {",
    "        `$cmd = Get-Content `$resume_cmd -Raw",
    "        Remove-Item `$resume_cmd -Force",
    "        Invoke-Expression `$cmd",
    "    }",
    "}",
    "Set-Alias -Name csess -Value claude-chats -Option AllScope -Force -ErrorAction SilentlyContinue",
    "Set-Alias -Name claude-history -Value claude-chats -Option AllScope -Force -ErrorAction SilentlyContinue"
)
$functionSnippet = $functionLines -join "`r`n"

$currentProfile = if (Test-Path $profilePath) { Get-Content $profilePath -Raw } else { "" }
if ($currentProfile -notmatch "function claude-chats") {
    if ($currentProfile -match "function csess") {
        $pattern = "(?ms)function csess\s*\{.*?^\}"
        $updated = [regex]::Replace($currentProfile, $pattern, $functionSnippet.Trim())
        Set-Content -Path $profilePath -Value $updated -Encoding UTF8
    } else {
        Add-Content -Path $profilePath -Value "`r`n$functionSnippet" -Encoding UTF8
    }
    Write-Host "[+] Configured claude-chats and csess in PowerShell profile ($profilePath)" -ForegroundColor Green
} else {
    Set-Content -Path $profilePath -Value $functionSnippet.Trim() -Encoding UTF8
    Write-Host "[+] Updated claude-chats function in PowerShell profile ($profilePath)" -ForegroundColor Green
}

Write-Host "claude-chats installed successfully! Try running 'claude-chats' or 'csess'." -ForegroundColor Cyan
