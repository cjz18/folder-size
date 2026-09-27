@echo off
cd /d "%~dp0"
echo 正在关闭正在运行的程序，以便覆盖旧文件...
taskkill /F /IM foldersize.exe >nul 2>&1
taskkill /F /IM FolderSizeMenu.exe >nul 2>&1
py -3 -m PyInstaller --noconfirm --windowed --onedir --name foldersize app.py
if errorlevel 1 (
  echo.
  echo 打包失败。如果上面写着「拒绝访问」，请先关掉正在打开的占用窗口，再双击本文件。
  pause
  exit /b 1
)
if exist "%~dp0dist\foldersize\foldersize.exe" (
  echo 已生成 dist\foldersize\foldersize.exe
  py -3 "%~dp0app.py" --install
) else (
  echo 打包失败，没有生成 dist\foldersize\foldersize.exe
  pause
  exit /b 1
)
