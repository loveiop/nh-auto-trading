#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [ ! -f .env ]; then
  cp .env.example .env
  echo '.env 파일에 앱키, 시크릿, 앱 비밀번호를 입력한 뒤 다시 실행하세요.'
  exit 1
fi
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.txt
exec .venv/bin/python -m streamlit run app.py --server.address 127.0.0.1

