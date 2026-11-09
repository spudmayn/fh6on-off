# Builds release\FH6EngineToggle-<version>.zip from fh6_car_audio_mem.py.
param([string]$Version = "1.0.0")
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
[Environment]::CurrentDirectory = $PSScriptRoot

$work = Join-Path $PSScriptRoot "_release"
if (Test-Path $work) { Remove-Item -Recurse -Force $work }

py -3.8 -m PyInstaller --noconfirm --onedir --console --name FH6EngineToggle `
    --collect-all vgamepad --exclude-module tkinter `
    --distpath "$work\dist" --workpath "$work\build" --specpath $work `
    fh6_car_audio_mem.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$app = "$work\dist\FH6EngineToggle"
Copy-Item "release_files\*" $app
& "$app\FH6EngineToggle.exe" --restore-controller | Out-Null
if ($LASTEXITCODE -ne 0) { throw "The built exe did not start" }

New-Item -ItemType Directory -Force release | Out-Null
$zip = "release\FH6EngineToggle-$Version.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path $app -DestinationPath $zip
"Built $zip ({0:N1} MB)" -f ((Get-Item $zip).Length / 1MB)
