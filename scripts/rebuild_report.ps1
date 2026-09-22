param([string]$AsOf = (Get-Date -Format 'yyyy-MM-dd'))
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$runtime = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $runtime)) {
    throw 'Python environment is missing. Follow README.md setup first.'
}
& $runtime -m predictor run --as-of $AsOf
if ($LASTEXITCODE -ne 0) { throw 'Report rebuild failed; see output above.' }
