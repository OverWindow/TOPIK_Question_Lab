#!/usr/bin/env sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON="$ROOT/.venv/bin/python"

if [ ! -x "$PYTHON" ]; then
    echo "가상환경이 없습니다. 먼저 sh setup_topik_lab.sh 를 실행하세요." >&2
    exit 1
fi

cd "$ROOT"
exec "$PYTHON" -m streamlit run topik_question_lab/app.py
