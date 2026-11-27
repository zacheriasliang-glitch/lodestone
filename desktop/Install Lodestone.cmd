@echo off
setlocal DisableDelayedExpansion
rem Per-user Windows setup. Only the hash-verified Python archive is executed.
rem No PowerShell execution policy or administrator setting is changed.
chcp 65001 >nul
set "LODESTONE_SETUP_ROOT=%LOCALAPPDATA%\Lodestone Preview"
set "LODESTONE_SETUP_APP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Lodestone Preview.cmd"
set "LODESTONE_SETUP_CACHE="
set "LODESTONE_SETUP_PAYLOAD=%~dp0"
set "LODESTONE_SETUP_NO_OPEN=0"
set "LODESTONE_SETUP_PATHS_VALIDATED=0"

:arguments
if "%~1"=="" goto arguments_done
if /i "%~1"=="--help" goto usage
if /i "%~1"=="--root" goto argument_root
if /i "%~1"=="--app" goto argument_app
if /i "%~1"=="--cache" goto argument_cache
if /i "%~1"=="--no-open" goto argument_no_open
echo Unknown option. Use --root PATH --app PATH --cache PATH or --no-open.
goto failed

:argument_root
if "%~2"=="" goto missing_argument
set "LODESTONE_SETUP_ROOT=%~2"
shift
shift
goto arguments

:argument_app
if "%~2"=="" goto missing_argument
set "LODESTONE_SETUP_APP=%~2"
shift
shift
goto arguments

:argument_cache
if "%~2"=="" goto missing_argument
set "LODESTONE_SETUP_CACHE=%~2"
shift
shift
goto arguments

:argument_no_open
set "LODESTONE_SETUP_NO_OPEN=1"
shift
goto arguments

:missing_argument
echo The selected option requires an absolute path.
goto failed

:arguments_done
if not defined LODESTONE_SETUP_CACHE set "LODESTONE_SETUP_CACHE=%LODESTONE_SETUP_ROOT%\downloads"
set "LODESTONE_SETUP_HASH=be95dc535d2de36ea895e6535da09bb970fdcaa82d6301a92f8d97809478f8e4"
set "LODESTONE_SETUP_URL=https://github.com/astral-sh/python-build-standalone/releases/download/20251120/cpython-3.12.12%%2B20251120-x86_64-pc-windows-msvc-install_only.tar.gz"
set "LODESTONE_SETUP_ARCHIVE=%LODESTONE_SETUP_CACHE%\%LODESTONE_SETUP_HASH%"
set "LODESTONE_SETUP_STAGE=%LODESTONE_SETUP_ROOT%\deps\bootstrap\python-%RANDOM%-%RANDOM%-%RANDOM%"
set "LODESTONE_SETUP_PARTIAL=%LODESTONE_SETUP_CACHE%\python-%RANDOM%-%RANDOM%-%RANDOM%.partial"

rem PowerShell is used for visible OS/path checks, not to load another script.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -Command ^
 "$ErrorActionPreference='Stop'; if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -ne 'AMD64' -or [Environment]::OSVersion.Version.Build -lt 20348) { throw 'This download requires Windows 11 x86_64 or Windows Server 2022 or later.' }"
if errorlevel 1 goto failed
call :check_path_budget
if errorlevel 1 goto failed
call :check_paths
if errorlevel 1 goto failed
set "LODESTONE_SETUP_PATHS_VALIDATED=1"
rem Remove trailing directory separators before passing paths to Python's native
rem Windows argument parser. The payload path deliberately retains its separator.
:trim_root
if not "%LODESTONE_SETUP_ROOT:~-1%"=="\" goto trim_cache
set "LODESTONE_SETUP_ROOT=%LODESTONE_SETUP_ROOT:~0,-1%"
goto trim_root
:trim_cache
if not "%LODESTONE_SETUP_CACHE:~-1%"=="\" goto paths_normalized
set "LODESTONE_SETUP_CACHE=%LODESTONE_SETUP_CACHE:~0,-1%"
goto trim_cache
:paths_normalized

rem Refuse unrelated existing directories before creating bootstrap files.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -Command ^
 "$ErrorActionPreference='Stop'; $root=[IO.Path]::GetFullPath($env:LODESTONE_SETUP_ROOT); $marker=Join-Path $root 'DESKTOP.json';" ^
 "if (Test-Path -LiteralPath $marker) { $owned=Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json; if ($owned.schema -ne 'lodestone-desktop-owned/v1' -or [IO.Path]::GetFullPath($owned.root) -ne $root) { throw 'Directory belongs to another installation.' } }" ^
 "elseif (Test-Path -LiteralPath $root) { foreach ($item in Get-ChildItem -LiteralPath $root -Force) { if ($item.Name -notin @('downloads','deps','.installation.lock','.lifecycle.lock')) { throw 'Choose an empty application directory; unrelated existing data will not be adopted.' } } }"
if errorlevel 1 goto failed
if not exist "%LODESTONE_SETUP_CACHE%" mkdir "%LODESTONE_SETUP_CACHE%"
if errorlevel 1 goto failed
if not exist "%LODESTONE_SETUP_ROOT%\deps\bootstrap" mkdir "%LODESTONE_SETUP_ROOT%\deps\bootstrap"
if errorlevel 1 goto failed
call :check_paths
if errorlevel 1 goto failed

if exist "%LODESTONE_SETUP_ARCHIVE%" goto verify_archive
if exist "%LODESTONE_SETUP_PARTIAL%" goto staging_conflict
echo Downloading the pinned application Python runtime...
"%SystemRoot%\System32\curl.exe" --fail --location --retry 3 --proto "=https" --proto-redir "=https" --tlsv1.2 "%LODESTONE_SETUP_URL%" --output "%LODESTONE_SETUP_PARTIAL%"
if errorlevel 1 goto failed
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -Command ^
 "$ErrorActionPreference='Stop'; if ((Get-FileHash -LiteralPath $env:LODESTONE_SETUP_PARTIAL -Algorithm SHA256).Hash -ne $env:LODESTONE_SETUP_HASH) { throw 'Python checksum mismatch; partial download retained.' }; [IO.File]::Move($env:LODESTONE_SETUP_PARTIAL,$env:LODESTONE_SETUP_ARCHIVE)"
if errorlevel 1 goto failed

:verify_archive
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -Command ^
 "$ErrorActionPreference='Stop'; if ((Get-FileHash -LiteralPath $env:LODESTONE_SETUP_ARCHIVE -Algorithm SHA256).Hash -ne $env:LODESTONE_SETUP_HASH) { throw 'Cached Python checksum mismatch; original cache file retained.' }"
if errorlevel 1 goto failed
call :check_paths
if errorlevel 1 goto failed
if exist "%LODESTONE_SETUP_STAGE%" goto staging_conflict
mkdir "%LODESTONE_SETUP_STAGE%"
if errorlevel 1 goto failed
"%SystemRoot%\System32\tar.exe" -xzf "%LODESTONE_SETUP_ARCHIVE%" -C "%LODESTONE_SETUP_STAGE%"
if errorlevel 1 goto failed
if not exist "%LODESTONE_SETUP_STAGE%\python\python.exe" goto extraction_failed
set "PYTHONDONTWRITEBYTECODE=1"
"%LODESTONE_SETUP_STAGE%\python\python.exe" -I -B "%LODESTONE_SETUP_PAYLOAD%install.py" --root "%LODESTONE_SETUP_ROOT%" install --payload "%LODESTONE_SETUP_PAYLOAD%." --app "%LODESTONE_SETUP_APP%" --cache "%LODESTONE_SETUP_CACHE%"
if errorlevel 1 goto failed

rem Successful setup has persistent interpreters in ACTIVE.json. Only this new
rem temporary bootstrap is removed; failed stages are preserved for inspection.
rmdir /s /q "%LODESTONE_SETUP_STAGE%"
if errorlevel 1 goto cleanup_failed
echo Installed: "%LODESTONE_SETUP_APP%"
if "%LODESTONE_SETUP_NO_OPEN%"=="1" exit /b 0
start "Lodestone Preview" "%LODESTONE_SETUP_APP%"
exit /b 0

:check_path_budget
rem BEGIN WINDOWS PATH PREFLIGHT
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -Command ^
 "$ErrorActionPreference='Stop'; $paths=@{root=$env:LODESTONE_SETUP_ROOT;bootstrap_root=$env:LODESTONE_SETUP_ROOT;cache=$env:LODESTONE_SETUP_CACHE;payload=$env:LODESTONE_SETUP_PAYLOAD;app=$env:LODESTONE_SETUP_APP};" ^
 "$budgetPath=Join-Path $paths.payload 'windows-path-budget.json'; $lockPath=Join-Path $paths.payload 'dependencies.windows-x86_64.lock.json';" ^
 "$budget=Get-Content -LiteralPath $budgetPath -Raw | ConvertFrom-Json;" ^
 "$lockHash=(Get-FileHash -LiteralPath $lockPath -Algorithm SHA256).Hash.ToLowerInvariant();" ^
 "if ($budget.schema -ne 'lodestone-windows-path-budget/v1' -or $budget.lock_sha256 -ne $lockHash) { throw 'Windows path inventory does not match this download dependency lock.' };" ^
 "$enabled=$false; $key=$null;" ^
 "try { $key=[Microsoft.Win32.Registry]::LocalMachine.OpenSubKey('SYSTEM\CurrentControlSet\Control\FileSystem'); if ($null -ne $key -and $key.GetValueNames() -contains 'LongPathsEnabled') { $enabled=$key.GetValueKind('LongPathsEnabled').ToString() -eq 'DWord' -and $key.GetValue('LongPathsEnabled') -eq 1 } } catch { $enabled=$false } finally { if ($null -ne $key) { $key.Close() } };" ^
 "function Get-BudgetBase([string]$value) {" ^
 "if ($value -notmatch '^[A-Za-z]:[\\/]' -or $value.Substring(2).Contains(':')) { throw 'Use absolute local-drive setup paths.' };" ^
 "foreach ($part in ($value.Substring(3) -split '[\\/]')) { if ($part -eq '..') { throw 'Setup paths cannot contain parent traversal.' }; if ($part.Length -gt 255) { throw 'LODESTONE_WINDOWS_PATH_TOO_LONG: A path component exceeds 255 UTF-16 units.' } };" ^
 "return [IO.Path]::GetFullPath($value).TrimEnd('\');" ^
 "};" ^
 "$rows=@();" ^
 "foreach ($scope in @('root','bootstrap_root','cache','payload')) {" ^
 "$base=Get-BudgetBase $paths[$scope]; $entry=$budget.scopes.$scope;" ^
 "foreach ($kind in @('file','directory')) {" ^
 "$count=$entry.('max_'+$kind+'_relative_utf16'); $template=$entry.('longest_'+$kind+'_template');" ^
 "if (($count -isnot [int] -and $count -isnot [long]) -or $count -lt 0 -or $count -ge 32767 -or $template -isnot [string]) { throw 'Invalid Windows path inventory bound.' };" ^
 "$rendered=[regex]::Replace($template.Replace('/','\').TrimStart('\'),'\{([a-zA-Z0-9_]+)\}',{ param($m) $width=$budget.token_widths.($m.Groups[1].Value); if (($width -isnot [int] -and $width -isnot [long]) -or $width -le 0 -or $width -ge 32767) { throw 'Invalid Windows path inventory token.' }; return ('x' * $width) });" ^
 "$sample=if ($rendered) { 1+$rendered.Length } else { 0 }; if ($sample -gt $count) { throw 'Windows path inventory understates its example.' };" ^
 "$limit=if ($scope -eq 'root' -and $enabled) { 32766 } elseif ($kind -eq 'file') { 259 } else { 247 };" ^
 "$rows+=@{scope=$scope;kind=$kind;required=($base.Length+$count);limit=$limit;remaining=($limit-$base.Length-$count);maximum_base=($limit-$count);member=$template};" ^
 "};" ^
 "};" ^
 "$appPath=Get-BudgetBase $paths.app; $appName=[IO.Path]::GetFileName($appPath);" ^
 "if ($appName.Length -gt 245) { throw 'LODESTONE_WINDOWS_PATH_TOO_LONG: Choose a launcher filename of at most 245 UTF-16 units.' };" ^
 "$rows+=@{scope='app';kind='file';required=($appPath.Length+10);limit=259;remaining=(249-$appPath.Length);maximum_base=249;member=('.'+$appName+'.xxxxxxxx')};" ^
 "$appParent=[IO.Path]::GetDirectoryName($appPath).TrimEnd('\'); $rows+=@{scope='app';kind='directory';required=$appParent.Length;limit=247;remaining=(247-$appParent.Length);maximum_base=247;member=''};" ^
 "foreach ($row in $rows) { Write-Host ($row.scope+' '+$row.kind+': '+$row.required+' UTF-16 units; '+$row.remaining+' remaining; deepest member: '+$row.member) };" ^
 "$failed=@($rows | Where-Object { $_.remaining -lt 0 });" ^
 "if ($failed.Count) {" ^
 "foreach ($row in $failed) { Write-Host ($row.scope+' '+$row.kind+': choose a base path no longer than '+$row.maximum_base+' UTF-16 units.') };" ^
 "throw 'LODESTONE_WINDOWS_PATH_TOO_LONG: Setup stopped before downloads or installation changes. Choose shorter paths with --root, --cache or --app, or extract into a shorter directory. For managed root paths, explicitly run Enable Windows Long Paths.cmd and approve its administrator request, then restart setup. Host bootstrap, cache, extraction and launcher limits still apply. The installer never changes this setting.';" ^
 "};" ^
 "Write-Host ('LODESTONE_WINDOWS_PATHS_ACCEPTED: LongPathsEnabled='+$enabled);"
rem END WINDOWS PATH PREFLIGHT
exit /b %errorlevel%

:check_paths
rem Values are read from the environment, never interpolated into PowerShell code.
rem Batch cannot safely carry every legal Windows filename. Refuse those cases
rem before any filesystem mutation; spaces and Unicode are supported.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -Command ^
 "$ErrorActionPreference='Stop'; $forbidden=[char[]](34,37,33,38,124,60,62,94,40,41,13,10);" ^
 "foreach ($name in @('LODESTONE_SETUP_ROOT','LODESTONE_SETUP_APP','LODESTONE_SETUP_CACHE','LODESTONE_SETUP_PAYLOAD','LODESTONE_SETUP_STAGE','LODESTONE_SETUP_PARTIAL','LODESTONE_SETUP_ARCHIVE')) {" ^
 "  $path=[Environment]::GetEnvironmentVariable($name);" ^
 "  if (-not $path -or $path.IndexOfAny($forbidden) -ge 0) { throw ('Use paths without command-shell metacharacters: '+$name) };" ^
 "  if ($path -notmatch '^[A-Za-z]:[\\/]' -or $path.Substring(2).Contains(':')) { throw ('Use an absolute local-drive path: '+$name) };" ^
 "  foreach ($part in ($path.Substring(3) -split '[\\/]')) {" ^
 "    if ($part -eq '.' -or $part -eq '..' -or ($part -and ($part.EndsWith('.') -or $part.EndsWith(' ')))) { throw ('Unsupported path component: '+$name) };" ^
 "    if ($part -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)') { throw ('Reserved device path: '+$name) }" ^
 "  };" ^
 "  $current=[IO.Path]::GetFullPath($path);" ^
 "  while ($current) {" ^
 "    $item=Get-Item -LiteralPath $current -Force -ErrorAction SilentlyContinue;" ^
 "    if ($null -ne $item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw ('Managed path contains a junction or symbolic link: '+$name) };" ^
 "    $current=[IO.Path]::GetDirectoryName($current)" ^
 "  }" ^
 "};" ^
 "if ([IO.Path]::GetFullPath($env:LODESTONE_SETUP_ROOT).TrimEnd('\').Length -le 2) { throw 'Use a dedicated application directory, not a drive root.' }; if ([IO.Path]::GetExtension($env:LODESTONE_SETUP_APP) -ne '.cmd') { throw 'The installed launcher path must end with .cmd.' }"
exit /b %errorlevel%

:usage
echo Install Lodestone.cmd [--root PATH] [--app PATH] [--cache PATH] [--no-open]
echo Paths must be absolute local-drive paths without command-shell metacharacters.
echo Spaces and Unicode are supported. An administrator may be needed only for the optional long-path prerequisite.
exit /b 0

:staging_conflict
echo A staging path already exists. It has been preserved; rerun setup to use a new stage.
goto failed

:extraction_failed
echo The verified archive did not produce its expected Python executable.
goto failed

:cleanup_failed
echo Installation completed, but its temporary bootstrap could not be removed.
echo The installed launcher and user records remain available.
exit /b 2

:failed
echo Lodestone setup failed. Download partials and setup staging data were retained.
if "%LODESTONE_SETUP_PATHS_VALIDATED%"=="1" echo Application data: "%LODESTONE_SETUP_ROOT%"
if "%LODESTONE_SETUP_NO_OPEN%"=="0" pause
exit /b 2
