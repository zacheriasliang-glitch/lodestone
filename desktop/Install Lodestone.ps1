# Per-user Windows x86_64 install. No Python, Git, administrator or API key required.
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
