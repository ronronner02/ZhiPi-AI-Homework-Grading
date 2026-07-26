@echo off
rem 智批π Demo 一键启动（Windows）
rem 自动探测可用 Python（避开 Microsoft Store 的 python 占位命令）→ 安装依赖 → 启动服务 → 打开浏览器
chcp 65001 >nul
cd /d "%~dp0"

set PY=
py -3 -c "import sys" >nul 2>nul && set PY=py -3
if not defined PY (
  python -c "import sys" >nul 2>nul && set PY=python
)
if not defined PY (
  echo [错误] 未找到可用的 Python（3.10+）。请从 https://www.python.org 安装后重试。
  pause & exit /b 1
)

echo [智批π] 使用解释器：%PY%
%PY% -m pip install -r requirements.txt -q
if errorlevel 1 (
  echo [错误] 依赖安装失败，请检查网络或手动执行：%PY% -m pip install -r requirements.txt
  pause & exit /b 1
)

echo [智批π] 启动服务：http://127.0.0.1:8010 （Ctrl+C 停止）
start "" http://127.0.0.1:8010
%PY% -m uvicorn app:app --port 8010
