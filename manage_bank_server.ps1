param(
    [ValidateSet("start", "stop", "status")]
    [string]$Action = "status"
)

$ErrorActionPreference = "Stop"
$bankPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $bankPython)) {
    throw "Project Python environment not found. Run setup_topik_lab.ps1 first."
}
Push-Location -LiteralPath $PSScriptRoot
try {
    & $bankPython -m topik_question_lab.maintenance.bank_server $Action
    $bankExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $bankExitCode
