$ErrorActionPreference = 'Stop'
$BriocheRoot = Split-Path $PSScriptRoot -Parent
& "$BriocheRoot\.venv\Scripts\python.exe" -m brioche @args
exit $LASTEXITCODE
