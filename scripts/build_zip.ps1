<#
.SYNOPSIS
    Builds a Linux-compatible AWS Lambda deployment.zip for manual upload in the AWS Console.

.DESCRIPTION
    Exports locked runtime dependencies from uv (excluding boto3 and its Lambda-provided
    transitive dependencies), installs Linux wheels for the target Python runtime and CPU
    architecture into a staging folder, copies src/spotify_add_song_button, and packages
    everything into deployment.zip with POSIX paths and Unix permissions.

.EXAMPLE
    .\scripts\build_zip.ps1
    .\scripts\build_zip.ps1 -Architecture arm64
#>

[CmdletBinding()]
param(
    [ValidateSet("x86_64", "arm64")]
    [string]$Architecture = "x86_64",

    [string]$PythonVersion = "3.14",

    [string]$OutputZip = "deployment.zip"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BuildDir = Join-Path $ProjectRoot "build\lambda_package"
$ReqFile = Join-Path $ProjectRoot "build\lambda_requirements.txt"
$ZipPath = if ([System.IO.Path]::IsPathRooted($OutputZip)) {
    $OutputZip
} else {
    Join-Path $ProjectRoot $OutputZip
}

$Platform = if ($Architecture -eq "arm64") {
    "aarch64-manylinux_2_28"
} else {
    "x86_64-manylinux_2_28"
}

# Packages already bundled in the AWS Lambda Python runtime.
$ExcludedPackages = @(
    "boto3",
    "botocore",
    "jmespath",
    "python-dateutil",
    "s3transfer",
    "six",
    "urllib3"
)

Write-Host "Building AWS Lambda bundle ($Architecture, Python $PythonVersion, $Platform)..."

if (Test-Path $BuildDir) {
    Remove-Item -Recurse -Force $BuildDir
}
if (Test-Path $ZipPath) {
    Remove-Item -Force $ZipPath
}
New-Item -ItemType Directory -Path $BuildDir -Force | Out-Null

Push-Location $ProjectRoot
try {
    Write-Host "Exporting locked runtime dependencies (excluding AWS-provided boto3 stack)..."
    $ExportLines = uv export --frozen --no-dev --no-emit-project --no-hashes --no-header
    if ($LASTEXITCODE -ne 0) {
        throw "uv export failed with exit code $LASTEXITCODE"
    }

    $FilteredLines = foreach ($Line in $ExportLines) {
        $Trimmed = $Line.Trim()
        if ([string]::IsNullOrWhiteSpace($Trimmed) -or $Trimmed.StartsWith("#")) {
            continue
        }
        $PkgName = ($Trimmed -split '[=<>!~;\[\s]', 2)[0].ToLowerInvariant()
        if ($ExcludedPackages -contains $PkgName) {
            continue
        }
        $Trimmed
    }

    [System.IO.File]::WriteAllLines(
        $ReqFile,
        [string[]]$FilteredLines,
        (New-Object System.Text.UTF8Encoding($false))
    )

    Write-Host "Installing Linux wheels into $BuildDir..."
    uv pip install `
        --no-deps `
        --requirements $ReqFile `
        --target $BuildDir `
        --python-platform $Platform `
        --python-version $PythonVersion `
        --only-binary :all:
    if ($LASTEXITCODE -ne 0) {
        throw "uv pip install failed with exit code $LASTEXITCODE"
    }

    Write-Host "Copying src/spotify_add_song_button into package root..."
    Copy-Item `
        -Path (Join-Path $ProjectRoot "src\spotify_add_song_button") `
        -Destination (Join-Path $BuildDir "spotify_add_song_button") `
        -Recurse `
        -Force

    Write-Host "Creating $ZipPath with POSIX paths and Unix permissions..."
    $ZipScriptPath = Join-Path $ProjectRoot "build\_zip_package.py"
    $ZipScript = @'
import os
import sys
import zipfile
from pathlib import Path

src_dir = Path(sys.argv[1])
zip_path = Path(sys.argv[2])

with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for filename in sorted(files):
            if filename.endswith((".pyc", ".pyo")):
                continue
            full_path = Path(root) / filename
            rel_path = full_path.relative_to(src_dir).as_posix()
            info = zipfile.ZipInfo(rel_path)
            info.compress_type = zipfile.ZIP_DEFLATED
            # rw-r--r-- (0644) for regular files so AWS Lambda can always read/import them
            info.external_attr = 0o100644 << 16
            zf.writestr(info, full_path.read_bytes())
'@
    [System.IO.File]::WriteAllText(
        $ZipScriptPath,
        $ZipScript,
        (New-Object System.Text.UTF8Encoding($false))
    )

    uv run python $ZipScriptPath $BuildDir $ZipPath
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create deployment zip (exit code $LASTEXITCODE)"
    }

    $ZipSizeMB = [Math]::Round((Get-Item $ZipPath).Length / 1MB, 2)
    Write-Host "Done! Created $ZipPath ($ZipSizeMB MB)."
    Write-Host "Lambda Handler: spotify_add_song_button.main.handler"
}
finally {
    Pop-Location
}
