# Build PTZ-Control release artifacts for GitHub v3.4+
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$Version = "3.28"
Write-Host "Building PTZ-Control v$Version"

# Sync version.json
@{"version" = $Version} | ConvertTo-Json | Set-Content -Path "version.json" -Encoding UTF8

Write-Host "PyInstaller: main app..."
py -3.12 -m PyInstaller PTZ-Control.spec --noconfirm
if ($LASTEXITCODE -ne 0) { throw "PTZ-Control build failed" }

Write-Host "PyInstaller: updater..."
py -3.12 -m PyInstaller PTZ-Control-Updater.spec --noconfirm
if ($LASTEXITCODE -ne 0) { throw "Updater build failed" }

Copy-Item version.json dist\version.json -Force
Copy-Item release\update.json dist\update.json -Force

$depsDir = Join-Path $Root "installer\deps"
if (-not (Test-Path (Join-Path $depsDir "MicrosoftEdgeWebview2Setup.exe"))) {
    Write-Host "Downloading installer dependencies..."
    & (Join-Path $Root "installer\download-deps.ps1")
}

$iscc = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($iscc) {
    Write-Host "Inno Setup: $iscc"
    & $iscc (Join-Path $Root "installer\PTZ-Control.iss")
    if ($LASTEXITCODE -ne 0) { throw "Installer compile failed" }
} else {
    Write-Warning "Inno Setup not found - skipping PTZ-Control-Setup.exe"
}

$exe = Join-Path $Root "dist\PTZ-Control.exe"
$hash = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLower()
Write-Host "PTZ-Control.exe SHA256: $hash"

$manifest = @{
    version       = $Version
    download_url  = "https://github.com/Danieldsavn/PTZ-Control/releases/download/v$Version/PTZ-Control.exe"
    sha256        = $hash
    release_notes = "Revert command worker queue; CUT and stream controls run direct like v3.22 (fixes 3.27 slowness and failed cuts)."
} | ConvertTo-Json -Depth 3
$manifest | Set-Content -Path "release\update.json" -Encoding UTF8
Write-Host "Wrote release\update.json"
Write-Host "Upload update.json to GitHub release (if tag exists): gh release upload v$Version dist\update.json --clobber"
Write-Host "Artifacts in dist:"
Get-ChildItem dist -Filter "*.exe" | ForEach-Object {
    $mb = [math]::Round($_.Length / 1MB, 1)
    Write-Host "  $($_.Name) ($mb MB)"
}
