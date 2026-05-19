# Download installer prerequisites into installer/deps/
$ErrorActionPreference = "Stop"
$deps = Join-Path $PSScriptRoot "deps"
New-Item -ItemType Directory -Force -Path $deps | Out-Null

$files = @(
    @{
        Name = "MicrosoftEdgeWebview2Setup.exe"
        Url  = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
    },
    @{
        Name = "vc_redist.x64.exe"
        Url  = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
    },
    @{
        Name = "ffmpeg.exe"
        Url  = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
        Zip  = $true
    }
)

foreach ($f in $files) {
    $dest = Join-Path $deps $f.Name
    if ($f.Zip) {
        $zipPath = Join-Path $deps "ffmpeg.zip"
        if (-not (Test-Path $dest)) {
            Write-Host "Downloading $($f.Name) (from zip)..."
            Invoke-WebRequest -Uri $f.Url -OutFile $zipPath -UseBasicParsing
            Expand-Archive -Path $zipPath -DestinationPath (Join-Path $deps "_ffmpeg_extract") -Force
            $bin = Get-ChildItem -Path (Join-Path $deps "_ffmpeg_extract") -Recurse -Filter "ffmpeg.exe" | Select-Object -First 1
            if (-not $bin) { throw "ffmpeg.exe not found in archive" }
            Copy-Item $bin.FullName $dest -Force
            Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
            Remove-Item (Join-Path $deps "_ffmpeg_extract") -Recurse -Force -ErrorAction SilentlyContinue
            Write-Host "  -> $dest"
        } else {
            Write-Host "Already present: $dest"
        }
        continue
    }
    if (Test-Path $dest) {
        Write-Host "Already present: $dest"
        continue
    }
    Write-Host "Downloading $($f.Name)..."
    Invoke-WebRequest -Uri $f.Url -OutFile $dest -UseBasicParsing
    Write-Host "  -> $dest"
}

Write-Host "Done. Dependency files are in $deps"
