$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    Write-Error "가상환경이 없습니다. 먼저 setup_topik_lab.ps1을 실행하세요."
}

Set-Location $Root
& $Python -m streamlit run topik_question_lab\app.py
