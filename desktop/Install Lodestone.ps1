# Per-user Windows x86_64 install. Long-path prerequisite may need administrator approval.
[CmdletBinding()]
param(
    [string]$Root = (Join-Path $env:LOCALAPPDATA 'Lodestone Preview'),
    [string]$App = (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Lodestone Preview.cmd'),
    [string]$Cache = '',
    [switch]$NoOpen
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -ne 'AMD64' -or [Environment]::OSVersion.Version.Build -lt 20348) {
    throw 'This download requires Windows 11 x86_64 or Windows Server 2022 or later.'
}
$Root = [IO.Path]::GetFullPath($Root)
if (-not $Cache) { $Cache = Join-Path $Root 'downloads' }
$Cache = [IO.Path]::GetFullPath($Cache)
# BEGIN WINDOWS PATH PREFLIGHT
$paths=@{root=$Root;bootstrap_root=$Root;cache=$Cache;payload=$PSScriptRoot;app=$App}
$budgetPath=Join-Path $paths.payload 'windows-path-budget.json'; $lockPath=Join-Path $paths.payload 'dependencies.windows-x86_64.lock.json'
$budget=Get-Content -LiteralPath $budgetPath -Raw | ConvertFrom-Json
$lockHash=(Get-FileHash -LiteralPath $lockPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($budget.schema -ne 'lodestone-windows-path-budget/v1' -or $budget.lock_sha256 -ne $lockHash) { throw 'Windows path inventory does not match this download dependency lock.' }
$enabled=$false; $key=$null
try { $key=[Microsoft.Win32.Registry]::LocalMachine.OpenSubKey('SYSTEM\CurrentControlSet\Control\FileSystem'); if ($null -ne $key -and $key.GetValueNames() -contains 'LongPathsEnabled') { $enabled=$key.GetValueKind('LongPathsEnabled').ToString() -eq 'DWord' -and $key.GetValue('LongPathsEnabled') -eq 1 } } catch { $enabled=$false } finally { if ($null -ne $key) { $key.Close() } }
function Get-BudgetBase([string]$value) {
    if ($value -notmatch '^[A-Za-z]:[\\/]' -or $value.Substring(2).Contains(':')) { throw 'Use absolute local-drive setup paths.' }
    foreach ($part in ($value.Substring(3) -split '[\\/]')) { if ($part -eq '..') { throw 'Setup paths cannot contain parent traversal.' }; if ($part.Length -gt 255) { throw 'LODESTONE_WINDOWS_PATH_TOO_LONG: A path component exceeds 255 UTF-16 units.' } }
    return [IO.Path]::GetFullPath($value).TrimEnd('\')
}
$rows=@()
foreach ($scope in @('root','bootstrap_root','cache','payload')) {
    $base=Get-BudgetBase $paths[$scope]; $entry=$budget.scopes.$scope
    foreach ($kind in @('file','directory')) {
        $count=$entry.('max_'+$kind+'_relative_utf16'); $template=$entry.('longest_'+$kind+'_template')
        if (($count -isnot [int] -and $count -isnot [long]) -or $count -lt 0 -or $count -ge 32767 -or $template -isnot [string]) { throw 'Invalid Windows path inventory bound.' }
        $rendered=[regex]::Replace($template.Replace('/','\').TrimStart('\'),'\{([a-zA-Z0-9_]+)\}',{ param($m) $width=$budget.token_widths.($m.Groups[1].Value); if (($width -isnot [int] -and $width -isnot [long]) -or $width -le 0 -or $width -ge 32767) { throw 'Invalid Windows path inventory token.' }; return ('x' * $width) })
        $sample=if ($rendered) { 1+$rendered.Length } else { 0 }; if ($sample -gt $count) { throw 'Windows path inventory understates its example.' }
        $limit=if ($scope -eq 'root' -and $enabled) { 32766 } elseif ($kind -eq 'file') { 259 } else { 247 }
        $rows+=@{scope=$scope;kind=$kind;required=($base.Length+$count);limit=$limit;remaining=($limit-$base.Length-$count);maximum_base=($limit-$count);member=$template}
    }
}
$appPath=Get-BudgetBase $paths.app; $appName=[IO.Path]::GetFileName($appPath)
if ($appName.Length -gt 245) { throw 'LODESTONE_WINDOWS_PATH_TOO_LONG: Choose a launcher filename of at most 245 UTF-16 units.' }
$rows+=@{scope='app';kind='file';required=($appPath.Length+10);limit=259;remaining=(249-$appPath.Length);maximum_base=249;member=('.'+$appName+'.xxxxxxxx')}
$appParent=[IO.Path]::GetDirectoryName($appPath).TrimEnd('\'); $rows+=@{scope='app';kind='directory';required=$appParent.Length;limit=247;remaining=(247-$appParent.Length);maximum_base=247;member=''}
foreach ($row in $rows) { Write-Host ($row.scope+' '+$row.kind+': '+$row.required+' UTF-16 units; '+$row.remaining+' remaining; deepest member: '+$row.member) }
$failed=@($rows | Where-Object { $_.remaining -lt 0 })
if ($failed.Count) {
    foreach ($row in $failed) { Write-Host ($row.scope+' '+$row.kind+': choose a base path no longer than '+$row.maximum_base+' UTF-16 units.') }
    throw 'LODESTONE_WINDOWS_PATH_TOO_LONG: Setup stopped before downloads or installation changes. Choose shorter paths with --root, --cache or --app, or extract into a shorter directory. For managed root paths, explicitly run Enable Windows Long Paths.cmd and approve its administrator request, then restart setup. Host bootstrap, cache, extraction and launcher limits still apply. The installer never changes this setting.'
}
Write-Host ('LODESTONE_WINDOWS_PATHS_ACCEPTED: LongPathsEnabled='+$enabled)
# END WINDOWS PATH PREFLIGHT
function Assert-ManagedPath([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Managed path contains a junction or symbolic link: $current"
            }
        }
        $parent = [IO.Path]::GetDirectoryName($current)
        if ($parent -eq $current) { break }
        $current = $parent
    }
}
Assert-ManagedPath $Root
Assert-ManagedPath $Cache
$bootstrapParent = Join-Path $Root 'deps\bootstrap'
New-Item -ItemType Directory -Force -Path $Cache,$bootstrapParent | Out-Null
$pythonHash = 'be95dc535d2de36ea895e6535da09bb970fdcaa82d6301a92f8d97809478f8e4'
$pythonUrl = 'https://github.com/astral-sh/python-build-standalone/releases/download/20251120/cpython-3.12.12%2B20251120-x86_64-pc-windows-msvc-install_only.tar.gz'
$archive = Join-Path $Cache $pythonHash
$partial = Join-Path $Cache ('python.' + [Guid]::NewGuid().ToString('N'))
$stage = Join-Path $bootstrapParent ('python.' + [Guid]::NewGuid().ToString('N'))
try {
    if (-not (Test-Path -LiteralPath $archive)) {
        & "$env:SystemRoot\System32\curl.exe" --fail --location --retry 3 --proto '=https' --proto-redir '=https' --tlsv1.2 $pythonUrl --output $partial
        if ($LASTEXITCODE -ne 0) { throw 'Python download failed.' }
        if ((Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash.ToLowerInvariant() -ne $pythonHash) { throw 'Python checksum mismatch.' }
        Move-Item -LiteralPath $partial -Destination $archive
    }
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $pythonHash) { throw 'Cached Python checksum mismatch.' }
    New-Item -ItemType Directory -Path $stage | Out-Null
    & "$env:SystemRoot\System32\tar.exe" -xzf $archive -C $stage
    if ($LASTEXITCODE -ne 0) { throw 'Verified Python archive extraction failed.' }
    $python = Join-Path $stage 'python\python.exe'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    & $python -I -B (Join-Path $PSScriptRoot 'install.py') --root $Root install --payload $PSScriptRoot --app $App --cache $Cache
    if ($LASTEXITCODE -ne 0) { throw 'Lodestone installation failed; retained records and logs remain in the application folder.' }
    Write-Output "Installed: $App"
    if (-not $NoOpen) { Start-Process -FilePath $App }
} finally {
    if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Force }
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
}
