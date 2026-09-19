@echo off
cd /d "%~dp0"
where pyw >nul 2>&1 && (
  start "" /D "%~dp0" pyw -3 app.py %*
  exit /b 0
)
where pythonw >nul 2>&1 && (
  start "" /D "%~dp0" pythonw app.py %*
  exit /b 0
)
where python >nul 2>&1 && (
  start "" /D "%~dp0" python app.py %*
  exit /b 0
)
echo 未找到 Python 3，请先安装后再双击打开。
pause
