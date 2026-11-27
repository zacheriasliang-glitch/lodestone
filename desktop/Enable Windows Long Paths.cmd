@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
rem Optional Windows setting. The installer never calls this helper.
rem Only LongPathsEnabled is changed; no execution policy is changed.
set "LODESTONE_LONG_PATH_EXPLICIT=0"
set "LODESTONE_LONG_PATH_HELPER_SELF=%~f0"
if /i "%~1"=="--help" goto usage
if /i "%~1"=="--yes" set "LODESTONE_LONG_PATH_EXPLICIT=1"
if not "%~1"=="" if /i not "%~1"=="--yes" goto usage_error
if not "%~2"=="" goto usage_error

"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command ^
 "$ErrorActionPreference='Stop'; $keyPath='SYSTEM\CurrentControlSet\Control\FileSystem'; $valueName='LongPathsEnabled';" ^
 "function Read-Policy { $key=[Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($keyPath); if ($null -eq $key) { throw 'The existing Windows FileSystem registry key could not be read.' }; try { $exists=$key.GetValueNames() -contains $valueName; $kind=$null; $value=$null; if ($exists) { $kind=$key.GetValueKind($valueName).ToString(); $value=$key.GetValue($valueName,$null,[Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames); if ($kind -eq 'DWord') { $value=([long]$value -band 4294967295) } }; return [pscustomobject]@{exists=$exists;kind=$kind;value=$value} } finally { $key.Close() } };" ^
 "function Write-Record($file,$value) { $bytes=([Text.UTF8Encoding]::new($false)).GetBytes(($value | ConvertTo-Json -Depth 6)); $stream=[IO.FileStream]::new($file,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::Read,4096,[IO.FileOptions]::WriteThrough); try { $stream.Write($bytes,0,$bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() } };" ^
 "$previous=Read-Policy; if ($previous.exists -and $previous.kind -ne 'DWord') { throw 'LongPathsEnabled has an unexpected registry type. It was left unchanged; ask your administrator to inspect it.' };" ^
 "$display=if ($previous.exists) { 'REG_DWORD '+$previous.value } else { 'absent' }; Write-Host ('Current LongPathsEnabled: '+$display); if ($previous.exists -and $previous.value -eq 1) { Write-Host 'Long paths are already enabled. Start the installer again.'; exit 0 };" ^
 "Write-Host 'This optional helper sets only HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled to DWORD 1. It affects applications that support long paths. The previous value and a restore command are saved first. Administrator approval is required; managed-host policy still applies.';" ^
 "if ($env:LODESTONE_LONG_PATH_EXPLICIT -ne '1' -and (Read-Host 'Type ENABLE to request this change, or press Enter to cancel') -cne 'ENABLE') { Write-Host 'Cancelled; no setting was changed.'; exit 2 };" ^
 "$directory=Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Lodestone Windows Setup\receipts'; $current=$directory; while ($current) { if (Test-Path -LiteralPath $current) { $item=Get-Item -LiteralPath $current -Force; if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'The receipt directory contains a junction or symbolic link; no setting was changed.' } }; $current=[IO.Path]::GetDirectoryName($current) }; [IO.Directory]::CreateDirectory($directory) | Out-Null;" ^
 "$id=[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')+'-'+[Guid]::NewGuid().ToString('N'); $receipt=Join-Path $directory ($id+'.json'); $resultFile=Join-Path $directory ($id+'-result.json');" ^
 "$restore=if ($previous.exists) { 'reg.exe add HKLM\SYSTEM\CurrentControlSet\Control\FileSystem /v LongPathsEnabled /t REG_DWORD /d '+$previous.value+' /f' } else { 'reg.exe delete HKLM\SYSTEM\CurrentControlSet\Control\FileSystem /v LongPathsEnabled /f' };" ^
 "$record=[ordered]@{schema='lodestone-windows-long-path-change/v1';created_at=[DateTime]::UtcNow.ToString('o');key='HKLM\SYSTEM\CurrentControlSet\Control\FileSystem';name=$valueName;previous_exists=$previous.exists;previous_kind=$previous.kind;previous_value=$previous.value;requested_value=1;restore_command=$restore;helper_sha256=(Get-FileHash -LiteralPath $env:LODESTONE_LONG_PATH_HELPER_SELF -Algorithm SHA256).Hash.ToLowerInvariant()}; Write-Record $receipt $record; Write-Host ('LODESTONE_LONG_PATH_RECEIPT='+$receipt); Write-Host ('To restore this exact previous value, use an administrator terminal: '+$restore);" ^
 "$code=1; $failure=$null; $after=$null; try { $reg=Join-Path ([Environment]::SystemDirectory) 'reg.exe'; $arguments=@('add','HKLM\SYSTEM\CurrentControlSet\Control\FileSystem','/v','LongPathsEnabled','/t','REG_DWORD','/d','1','/f'); $identity=[Security.Principal.WindowsIdentity]::GetCurrent(); $administrator=([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator); if ($administrator) { & $reg @arguments; $code=$LASTEXITCODE } else { $process=Start-Process -FilePath $reg -ArgumentList $arguments -Verb RunAs -Wait -PassThru; $code=$process.ExitCode }; $after=Read-Policy; if ($code -eq 0 -and (-not $after.exists -or $after.kind -ne 'DWord' -or $after.value -ne 1)) { $code=1; $failure='The requested value could not be confirmed.' } } catch { $code=1; $failure=$_.Exception.Message };" ^
 "$state=if ($code -eq 0) { 'APPLIED' } else { 'FAILED' }; Write-Record $resultFile ([ordered]@{schema='lodestone-windows-long-path-result/v1';receipt=$receipt;state=$state;exit_code=$code;observed=$after;error=$failure}); Write-Host ('LODESTONE_LONG_PATH_RESULT='+$resultFile);" ^
 "if ($code -ne 0) { Write-Host ('The request did not complete: '+$failure); Write-Host ('The previous value and restore command remain in '+$receipt); exit $code }; Write-Host 'Long paths are enabled. Close this window and start the installer again; existing applications may need to be restarted.'; exit 0"
exit /b %errorlevel%

:usage
echo Enable Windows Long Paths.cmd [--yes]
echo With no option, type ENABLE to confirm. --yes is an explicit request for the same change.
echo Only LongPathsEnabled is set to 1. Administrator approval may be requested.
echo Previous-value receipts and restore commands are saved in LocalAppData\Lodestone Windows Setup\receipts.
exit /b 0

:usage_error
echo Use Enable Windows Long Paths.cmd [--yes]. No setting was changed.
exit /b 2
