# Strata Terminal with the GPU lease (this fork): one-line installer for Windows (PowerShell 5.1 or newer).
#
#     irm https://raw.githubusercontent.com/shuai532209720/Strata/main/packaging/install-gpulease.ps1 | iex
#
# Go back to the official Terminal:
#     $env:STRATA_GPULEASE_ROLLBACK='1'; irm https://raw.githubusercontent.com/shuai532209720/Strata/main/packaging/install-gpulease.ps1 | iex
#
# What it does, in order (and nothing else):
#   1. finds the official Terminal of release v0.2.9 (Control 0.1.55, commit c62d5e3a) under %LOCALAPPDATA%\Strata\terminal; when
#      there is none, installs it with the official installer of that release (signed feed, checksum);
#   2. downloads the files this fork changed (serve\server.py, serve\images.py, serve\gpu_lease.py and its test) from the fork at a
#      fixed commit, and checks each one's SHA-256 against the value written below;
#   3. copies the official Terminal to  terminal\0.1.55-gpulease-040afb0e, puts those files in, and checks that it starts;
#   4. points  terminal\current  at it (the official folder stays, for the rollback) and keeps your user PATH as it is.
# Models, engines, presets and run configs live outside the Terminal folder: nothing is downloaded again and nothing of them changes.
# Optional: $env:STRATA_DATA_DIR (default %LOCALAPPDATA%\Strata), as for install.ps1.
& {
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
function Die([string]$msg) { [Console]::Error.WriteLine("strata gpulease installer: $msg"); throw "strata gpulease installer stopped" }
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }
try { if ([Net.WebRequest]::DefaultWebProxy) { [Net.WebRequest]::DefaultWebProxy.Credentials = [Net.CredentialCache]::DefaultNetworkCredentials } } catch { }

$BaseRelease = 'v0.2.9'
$BaseCommit = 'c62d5e3a15b40ea51c96aae9e8d36928965f53ab'
$PatchCommit = '040afb0e3c9b705e6267644077724212970ab89a'
$Raw = "https://raw.githubusercontent.com/shuai532209720/Strata/$PatchCommit"
$Files = [ordered]@{
    'serve/server.py'         = '361ed77d6b4e12a890444ca177b8bd1de3e7e0cbd4b1bc67e537f6b54dbb0921'
    'serve/images.py'         = '6fd58991377c7d97e0424872ab8f2c86ce3e7e2633b5f2aca899df42637899df'
    'serve/gpu_lease.py'      = '96dee6baeb874529a079e33fa46ae8b3ee385526b8c99ca5d3178e8b1be5e645'
    'serve/test_gpu_lease.py' = '35c212814a226c890ac6d603bca3ce9361a8653a11c03a4d2fb5e09b140b0b51'
}
$Label = '0.1.55-gpulease-' + $PatchCommit.Substring(0, 8)

if ([Environment]::OSVersion.Platform -ne 'Win32NT') { Die 'this is the Windows installer.' }
if (-not $env:STRATA_DATA_DIR -and -not $env:LOCALAPPDATA) { Die 'Windows did not say where your user folder is. Set $env:STRATA_DATA_DIR and run this again.' }
$data = if ($env:STRATA_DATA_DIR) { $env:STRATA_DATA_DIR } else { Join-Path $env:LOCALAPPDATA 'Strata' }
$terminalDir = Join-Path $data 'terminal'
$current = Join-Path $terminalDir 'current'
$previousFile = Join-Path $terminalDir 'previous.txt'
Write-Host 'Strata Terminal with the GPU lease (shuai532209720/Strata)'

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
function Commit-Of([string]$dir) {
    $f = Join-Path $dir 'COMMIT'
    if (Test-Path $f) { return (Get-Content $f -Raw).Trim().ToLower() }
    return ''
}
function Find-Base {
    Get-ChildItem $terminalDir -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne 'current' -and (Commit-Of $_.FullName) -eq $BaseCommit -and (Test-Path (Join-Path $_.FullName 'bin\strata.cmd')) } |
        Sort-Object { if ($_.Name -match '^\d+(\.\d+)+$') { 0 } else { 1 } } | Select-Object -First 1 -ExpandProperty FullName
}

# a Strata server or window still running from a Terminal folder keeps its files open
$busy = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($terminalDir, [StringComparison]::OrdinalIgnoreCase) })
if ($busy.Count) { Die "Strata is running ($($busy[0].Path)). Stop it first (run: strata launch --stop), then run this again. Nothing was changed." }

if ($env:STRATA_GPULEASE_ROLLBACK -eq '1') {
    $back = if (Test-Path $previousFile) { (Get-Content $previousFile -Raw).Trim() } else { '' }
    if (-not $back -or -not (Test-Path (Join-Path $back 'bin\strata.cmd'))) { $back = Find-Base }
    if (-not $back) { Die "there is no official Terminal in $terminalDir to go back to." }
    Point-Current $back
    Write-Host "Back to $back. Run: strata launch"
    return
}

# 1. the official Terminal of the base release
$base = Find-Base
if (-not $base) {
    Write-Host "  No official Terminal $BaseRelease here yet: installing it with the official installer of $BaseRelease ..."
    $feed = "https://github.com/Niko1221/Strata/releases/download/$BaseRelease"
    $saved = $env:STRATA_FEED
    $env:STRATA_FEED = $feed
    try { Invoke-RestMethod -UseBasicParsing "$feed/install.ps1" | Invoke-Expression }
    finally { $env:STRATA_FEED = $saved }
    $base = Find-Base
    if (-not $base) { Die "the official installer finished, but no Terminal of commit $($BaseCommit.Substring(0, 8)) is in $terminalDir." }
}
Write-Host "  Official Terminal: $base"

# 2. this fork's files, checked
$work = Join-Path ([IO.Path]::GetTempPath()) ('strata-gpulease-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $work | Out-Null
try {
    foreach ($f in $Files.Keys) {
        $to = Join-Path $work ([IO.Path]::GetFileName($f))
        try { Invoke-WebRequest -UseBasicParsing -Uri "$Raw/$f" -OutFile $to }
        catch { Die "could not download $f ($($_.Exception.Message)). Check the internet connection; nothing was changed." }
        $got = (Get-FileHash -Algorithm SHA256 $to).Hash.ToLower()
        if ($got -ne $Files[$f]) { Die "$f does not match its checksum (got $got, wanted $($Files[$f])). Nothing was changed." }
    }
    Write-Host "  Downloaded $($Files.Count) files from commit $($PatchCommit.Substring(0, 8)); checksums ok."

    # 3. a copy of the official Terminal with them in
    $dest = Join-Path $terminalDir $Label
    $incoming = "$dest.incoming"
    if (Test-Path $incoming) { Remove-Item -Recurse -Force $incoming }
    $null = robocopy "$base" "$incoming" /E /NFL /NDL /NJH /NJS /NP /R:1 /W:1
    if ($LASTEXITCODE -ge 8) { Remove-Item -Recurse -Force $incoming -ErrorAction SilentlyContinue; Die "copying $base failed (robocopy $LASTEXITCODE)." }
    Get-ChildItem -Path (Join-Path $incoming 'serve') -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
    foreach ($f in $Files.Keys) { Copy-Item -Force (Join-Path $work ([IO.Path]::GetFileName($f))) (Join-Path $incoming ($f -replace '/', '\')) }
    Set-Content -Encoding Ascii -Path (Join-Path $incoming 'COMMIT') -Value $PatchCommit
    $notes = @(
        "This Terminal is release $BaseRelease (Control 0.1.55, commit $BaseCommit) with the GPU lease of",
        "https://github.com/shuai532209720/Strata at commit $PatchCommit (installed by packaging/install-gpulease.ps1).",
        'Changed files:')
    foreach ($f in $Files.Keys) { $notes += "  $f" }
    Set-Content -Encoding Ascii -Path (Join-Path $incoming 'PATCHES.txt') -Value $notes

    Write-Host 'Checking that it starts...'
    $prevEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    $env:STRATA_NO_HANDOVER = '1'
    $out = & (Join-Path $incoming 'bin\strata.cmd') --version 2>&1 | Out-String
    $code = $LASTEXITCODE
    Push-Location -LiteralPath $incoming
    $imp = & (Join-Path $incoming 'python\python.exe') -I -c "import sys; sys.path.insert(0, '.'); import serve.gpu_lease, serve.server; print('serve ok')" 2>&1 | Out-String
    $icode = $LASTEXITCODE
    Pop-Location
    Remove-Item Env:\STRATA_NO_HANDOVER -ErrorAction SilentlyContinue
    $ErrorActionPreference = $prevEap
    if ($code -ne 0 -or $icode -ne 0) {
        [Console]::Error.WriteLine($out + $imp)
        Remove-Item -Recurse -Force $incoming
        Die 'the new Terminal did not start (its output is above). Nothing was changed.'
    }
    Write-Host "  $($out.Trim()); $($imp.Trim())"

    # 4. switch over, keeping the official one
    $before = Target-Of $current
    if (Test-Path $dest) {
        if ($before -and (Test-Path $before) -and ((Resolve-Path $before).Path -eq (Resolve-Path $dest).Path)) { cmd /c rmdir "$current" | Out-Null; $before = $base }
        try { Remove-Item -Recurse -Force $dest }
        catch { Remove-Item -Recurse -Force $incoming -ErrorAction SilentlyContinue; Die "$dest is in use. Close every Strata window, then run this again." }
    }
    Move-Item $incoming $dest
    if (-not $before -or -not (Test-Path (Join-Path $before 'bin\strata.cmd'))) { $before = $base }
    Set-Content -Encoding UTF8 -Path $previousFile -Value $before
    Point-Current $dest

    $bin = Join-Path $current 'bin'
    $user = [Environment]::GetEnvironmentVariable('Path', 'User'); if (-not $user) { $user = '' }
    if (($user -split ';') -notcontains $bin) {
        [Environment]::SetEnvironmentVariable('Path', ($user.TrimEnd(';') + ';' + $bin).TrimStart(';'), 'User')
        $env:Path = "$env:Path;$bin"
        Write-Host '  (added to your PATH: open a new terminal window if strata is not found yet)'
    }
    Write-Host "Installed $Label. Run: strata launch"
    Write-Host "  Turn the lease on with  `"gpu_lease`": true  in the model's run config (docs/GPU_LEASE.md)."
    Write-Host "  Back to the official Terminal: `$env:STRATA_GPULEASE_ROLLBACK='1' and run this installer again."
}
finally {
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}
}
