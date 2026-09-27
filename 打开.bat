@echo off
cd /d "%~dp0"
if exist "%~dp0dist\foldersize\foldersize.exe" (
  start "" /D "%~dp0dist\foldersize" "%~dp0dist\foldersize\foldersize.exe" %*
  exit /b 0
)
if exist "%~dp0dist\foldersize.exe" (
  start "" /D "%~dp0" "%~dp0dist\foldersize.exe" %*
  exit /b 0
)
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
