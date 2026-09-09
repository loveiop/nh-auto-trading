@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python 3.11 이상을 설치한 뒤 다시 실행해 주세요.
  pause
  exit /b 1
)
if not exist .venv\Scripts\python.exe (
  py -3 -m venv .venv
  if errorlevel 1 goto fail
)
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto fail
if not exist .env (
  copy .env.example .env >nul
  echo .env 파일에 앱키, 시크릿, 앱 비밀번호를 입력하고 저장해 주세요.
  notepad .env
)
start "" http://localhost:8501
.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1
goto end
:fail
echo 준비 중 오류가 발생했습니다. 위 오류를 확인해 주세요.
:end
pause

