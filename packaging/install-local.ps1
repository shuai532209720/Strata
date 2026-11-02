# Strata Terminal: install an archive from a file (Windows PowerShell 5.1 or newer).
#
#     powershell -ExecutionPolicy Bypass -File install-local.ps1 [-Zip <strata-control-...-windows-x86_64.zip>]
#     powershell -ExecutionPolicy Bypass -File install-local.ps1 -Rollback
#
# For an archive made by packaging/patch_release_terminal.py (a release with this checkout's commits on top), which is not in
# the signed feed, so install.ps1 cannot install it. What it does, in order (and nothing else):
#   1. checks the archive's SHA-256 against SHA256SUMS.txt beside it (or -Sha256);
#   2. unpacks it into  %LOCALAPPDATA%\Strata\terminal\<name>  (no administrator rights), and checks that it starts (strata --version);
#   3. points  %LOCALAPPDATA%\Strata\terminal\current  at it and keeps the Terminal that was there, so -Rollback can go back;
#   4. adds  %LOCALAPPDATA%\Strata\terminal\current\bin  to your user PATH if it is not there yet.
# Models, engines, presets and settings live outside the Terminal folder and are not touched.
# Optional: $env:STRATA_DATA_DIR (default %LOCALAPPDATA%\Strata), as for install.ps1.
param(
    [string]$Zip = '',
    [string]$Sha256 = '',
    [switch]$Rollback
)
& {
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
function Die([string]$msg) { [Console]::Error.WriteLine("strata install-local: $msg"); throw "strata install-local stopped" }

if ([Environment]::OSVersion.Platform -ne 'Win32NT') { Die 'this is the Windows installer.' }
if (-not $env:STRATA_DATA_DIR -and -not $env:LOCALAPPDATA) { Die 'Windows did not say where your user folder is. Set $env:STRATA_DATA_DIR and run this again.' }
$data = if ($env:STRATA_DATA_DIR) { $env:STRATA_DATA_DIR } else { Join-Path $env:LOCALAPPDATA 'Strata' }
$terminalDir = Join-Path $data 'terminal'
$current = Join-Path $terminalDir 'current'

function Target-Of([string]$link) {
    if (-not (Test-Path $link)) { return $null }
    $item = Get-Item -LiteralPath $link -Force
    if ($item.LinkType -and $item.Target) { return [string]@($item.Target)[0] }
    $txt = Join-Path $link 'current.txt'
    if (Test-Path $txt) { return (Get-Content $txt -Raw).Trim() }
    return $null
}

function Point-Current([string]$dest) {
    if (Test-Path $current) { cmd /c rmdir "$current" | Out-Null }
    if (Test-Path $current) { Remove-Item -Recurse -Force $current }
    cmd /c mklink /J "$current" "$dest" | Out-Null
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path (Join-Path $current 'bin\strata.cmd'))) {
        if (Test-Path $current) { Remove-Item -Recurse -Force $current -ErrorAction SilentlyContinue }
        New-Item -ItemType Directory -Force -Path (Join-Path $current 'bin') | Out-Null
        Set-Content -Encoding Ascii -Path (Join-Path $current 'bin\strata.cmd') -Value @('@echo off', ('"' + (Join-Path $dest 'bin\strata.cmd') + '" %*'))
        Set-Content -Encoding Ascii -Path (Join-Path $current 'current.txt') -Value $dest
    }
}

# a Strata server or window still running from a Terminal folder keeps its files open
$busy = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($terminalDir, [StringComparison]::OrdinalIgnoreCase) })
if ($busy.Count) { Die "Strata is running ($($busy[0].Path)). Stop it first (run: strata launch --stop), then run this again. Nothing was changed." }

$previousFile = Join-Path $terminalDir 'previous.txt'
if ($Rollback) {
    $back = if (Test-Path $previousFile) { (Get-Content $previousFile -Raw).Trim() } else { '' }
    if (-not $back -or -not (Test-Path (Join-Path $back 'bin\strata.cmd'))) {
        $back = Get-ChildItem $terminalDir -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '^\d+(\.\d+)+$' } |
            Sort-Object { [version]($_.Name -replace '^(\d+(\.\d+){0,3}).*$', '$1') } -Descending | Select-Object -First 1 -ExpandProperty FullName
    }
    if (-not $back) { Die "there is no earlier Terminal in $terminalDir to go back to." }
    Point-Current $back
    Write-Host "Back to $back. Run: strata launch"
    return
}

if (-not $Zip) {
    $Zip = Get-ChildItem -Path $PSScriptRoot -Filter 'strata-control-*-windows-x86_64.zip' -File | Where-Object { $_.Name -notmatch '^strata-control-[\d.]+-windows' } |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
    if (-not $Zip) { Die "no archive given and none beside this script. Run: install-local.ps1 -Zip <strata-control-...-windows-x86_64.zip>" }
}
$Zip = (Resolve-Path -LiteralPath $Zip).Path
$name = [IO.Path]::GetFileName($Zip)
if ($name -notmatch '^strata-control-(.+)-windows-x86_64\.zip$') { Die "$name does not look like a Windows Strata Terminal archive (strata-control-<version>-windows-x86_64.zip)." }
$label = $Matches[1]
Write-Host "Strata Terminal from a file: $name"

if (-not $Sha256) {
    $sums = Join-Path (Split-Path $Zip) 'SHA256SUMS.txt'
    if (-not (Test-Path $sums)) { Die "no SHA256SUMS.txt beside the archive and no -Sha256: its checksum cannot be checked." }
    $line = Get-Content $sums | Where-Object { $_ -match ('\s\*?' + [regex]::Escape($name) + '$') } | Select-Object -First 1
    if (-not $line) { Die "SHA256SUMS.txt has no line for $name." }
    $Sha256 = (-split $line)[0]
}
$got = (Get-FileHash -Algorithm SHA256 $Zip).Hash.ToLower()
if ($got -ne $Sha256.ToLower()) { Die "the archive does not match its checksum (got $got, wanted $Sha256). Nothing was installed." }
Write-Host '  Checksum ok.'

New-Item -ItemType Directory -Force -Path $terminalDir | Out-Null
$dest = Join-Path $terminalDir $label
$incoming = "$dest.incoming"
if (Test-Path $incoming) { Remove-Item -Recurse -Force $incoming }
try {
    # as install.ps1: Windows' own tar.exe (long paths), ASCII-only relative arguments from inside the terminal folder
    $tar = Get-Command (Join-Path $env:SystemRoot 'System32\tar.exe') -ErrorAction SilentlyContinue
    if ($tar) {
        $tag = 'u' + [Guid]::NewGuid().ToString('N').Substring(0, 6)
        Push-Location -LiteralPath $terminalDir
        try {
            Copy-Item -LiteralPath $Zip -Destination "$tag.zip"
            New-Item -ItemType Directory -Path $tag | Out-Null
            $tarOut = & $tar.Source -xf "$tag.zip" -C $tag 2>&1 | Out-String
            if ($LASTEXITCODE -ne 0) { throw $tarOut.Trim() }
        }
        finally {
            Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath "$tag.zip"
            Pop-Location
        }
        Move-Item -LiteralPath (Join-Path $terminalDir $tag) -Destination $incoming
    } else { Expand-Archive -LiteralPath $Zip -DestinationPath $incoming -Force }
}
catch { Remove-Item -Recurse -Force $incoming -ErrorAction SilentlyContinue; Die "the archive could not be unpacked ($($_.Exception.Message)). If a path is too long, set `$env:STRATA_DATA_DIR to a short folder such as C:\Strata. Nothing was installed." }
if (-not (Test-Path (Join-Path $incoming 'bin\strata.cmd'))) { Remove-Item -Recurse -Force $incoming; Die 'the archive does not look like a Strata Terminal (no bin\strata.cmd).' }

Write-Host 'Checking that it starts...'
$prevEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
$env:STRATA_NO_HANDOVER = '1'          # this Terminal itself, not a newer one `strata update` may have staged
$out = & (Join-Path $incoming 'bin\strata.cmd') --version 2>&1 | Out-String
$code = $LASTEXITCODE
Remove-Item Env:\STRATA_NO_HANDOVER -ErrorAction SilentlyContinue
$ErrorActionPreference = $prevEap
if ($code -ne 0) {
    [Console]::Error.WriteLine($out)
    Remove-Item -Recurse -Force $incoming
    Die 'the unpacked Terminal did not start (its output is above). Nothing was changed.'
}
Write-Host "  $($out.Trim())"

$before = Target-Of $current
if (Test-Path $dest) {
    if ($before -and (Test-Path $before) -and ((Resolve-Path $before).Path -eq (Resolve-Path $dest).Path)) { cmd /c rmdir "$current" | Out-Null; $before = $null }
    try { Remove-Item -Recurse -Force $dest }
    catch { Remove-Item -Recurse -Force $incoming -ErrorAction SilentlyContinue; Die "$dest is in use. Close every Strata window, then run this again." }
}
Move-Item $incoming $dest
if ($before -and (Test-Path (Join-Path $before 'bin\strata.cmd'))) { Set-Content -Encoding UTF8 -Path $previousFile -Value $before }
Point-Current $dest

$bin = Join-Path $current 'bin'
$user = [Environment]::GetEnvironmentVariable('Path', 'User'); if (-not $user) { $user = '' }
if (($user -split ';') -notcontains $bin) {
    [Environment]::SetEnvironmentVariable('Path', ($user.TrimEnd(';') + ';' + $bin).TrimStart(';'), 'User')
    Write-Host '  (added to your PATH: open a new terminal window if strata is not found yet)'
}
Write-Host "Installed $label. Run: strata launch"
if ($before) { Write-Host "  The Terminal it replaced stays in $before; to go back: install-local.ps1 -Rollback" }
$patches = Join-Path $dest 'PATCHES.txt'
if (Test-Path $patches) { Write-Host ''; Get-Content $patches | ForEach-Object { Write-Host "  $_" } }
}
