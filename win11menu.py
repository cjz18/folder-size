"""Windows 11 top-level context menu, in addition to the HKCU shell verbs."""

from __future__ import annotations

import os
import shutil
import subprocess
import zlib
import struct

PACKAGE_NAME = "MB.FolderSize"
CLASS_ID = "7C2E5B1A-4F63-4A0E-9C31-8B6D2F0A91E4"
CSC = r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"


def _root() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _png(size: int, rgb: tuple[int, int, int]) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _write_assets(folder: str) -> None:
    assets = os.path.join(folder, "Assets")
    os.makedirs(assets, exist_ok=True)
    blue = (30, 110, 180)
    for name, size in (("Square44x44Logo.png", 44), ("Square150x150Logo.png", 150), ("StoreLogo.png", 50)):
        with open(os.path.join(assets, name), "wb") as handle:
            handle.write(_png(size, blue))


def _manifest(folder: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<Package
  xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10"
  xmlns:uap="http://schemas.microsoft.com/appx/manifest/uap/windows10"
  xmlns:uap10="http://schemas.microsoft.com/appx/manifest/uap/windows10/10"
  xmlns:rescap="http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities"
  xmlns:com="http://schemas.microsoft.com/appx/manifest/com/windows10"
  xmlns:desktop4="http://schemas.microsoft.com/appx/manifest/desktop/windows10/4"
  xmlns:desktop5="http://schemas.microsoft.com/appx/manifest/desktop/windows10/5"
  IgnorableNamespaces="uap uap10 rescap com desktop4 desktop5">
  <Identity Name="{PACKAGE_NAME}" Publisher="CN=FolderSize" Version="1.0.0.0" />
  <Properties>
    <DisplayName>查看占用空间</DisplayName>
    <PublisherDisplayName>FolderSize</PublisherDisplayName>
    <Logo>Assets\\StoreLogo.png</Logo>
    <uap10:AllowExternalContent>true</uap10:AllowExternalContent>
  </Properties>
  <Resources>
    <Resource Language="zh-CN" />
  </Resources>
  <Dependencies>
    <TargetDeviceFamily Name="Windows.Desktop" MinVersion="10.0.22000.0" MaxVersionTested="10.0.26100.0" />
  </Dependencies>
  <Capabilities>
    <rescap:Capability Name="unvirtualizedResources" />
    <rescap:Capability Name="runFullTrust" />
  </Capabilities>
  <Applications>
    <Application Id="FolderSize" Executable="FolderSizeMenu.exe"
      uap10:TrustLevel="mediumIL" uap10:RuntimeBehavior="win32App">
      <uap:VisualElements AppListEntry="none" DisplayName="查看占用空间"
        Description="查看占用空间" BackgroundColor="transparent"
        Square150x150Logo="Assets\\Square150x150Logo.png"
        Square44x44Logo="Assets\\Square44x44Logo.png" />
      <Extensions>
        <com:Extension Category="windows.comServer">
          <com:ComServer>
            <com:ExeServer Executable="FolderSizeMenu.exe" Arguments="-Embedding" DisplayName="查看占用空间">
              <com:Class Id="{CLASS_ID}" DisplayName="查看占用空间" />
            </com:ExeServer>
          </com:ComServer>
        </com:Extension>
        <desktop4:Extension Category="windows.fileExplorerContextMenus">
          <desktop4:FileExplorerContextMenus>
            <desktop5:ItemType Type="*">
              <desktop5:Verb Id="FolderSizeFile" Clsid="{CLASS_ID}" />
            </desktop5:ItemType>
            <desktop5:ItemType Type="Directory">
              <desktop5:Verb Id="FolderSizeDir" Clsid="{CLASS_ID}" />
            </desktop5:ItemType>
          </desktop4:FileExplorerContextMenus>
        </desktop4:Extension>
      </Extensions>
    </Application>
  </Applications>
</Package>
"""


def build_menu_exe(folder: str) -> str:
    os.makedirs(folder, exist_ok=True)
    subprocess.run(
        ["taskkill", "/F", "/IM", "FolderSizeMenu.exe"],
        capture_output=True,
        text=True,
    )
    source = os.path.join(_root(), "shellmenu", "FolderSizeMenu.cs")
    output = os.path.join(folder, "FolderSizeMenu.exe")
    if not os.path.isfile(CSC):
        raise FileNotFoundError(CSC)
    compiled = subprocess.run(
        [
            CSC,
            "/nologo",
            "/target:winexe",
            "/r:System.dll",
            "/r:System.Windows.Forms.dll",
            f"/out:{output}",
            source,
        ],
        capture_output=True,
        text=True,
    )
    if compiled.returncode != 0:
        raise RuntimeError(compiled.stdout + compiled.stderr)
    return output


def install_top_menu(folder: str) -> str:
    """Register the Windows 11 menu beside the existing HKCU verbs. Returns a status line."""
    build_menu_exe(folder)
    _write_assets(folder)
    manifest = os.path.join(folder, "AppxManifest.xml")
    with open(manifest, "w", encoding="utf-8") as handle:
        handle.write(_manifest(folder))
    remove_top_menu()
    script = (
        "Add-AppxPackage -ForceApplicationShutdown -Register "
        f"-ExternalLocation '{folder}' '{manifest}'"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        useful = [line.strip() for line in detail if "HRESULT" in line or "失败" in line]
        tail = useful[0] if useful else (detail[0] if detail else "注册失败")
        return "Windows 11 一级菜单未装上（注册表菜单仍可用）：" + tail
    return "Windows 11 一级菜单已安装，可直接右键。"


def remove_top_menu() -> None:
    script = (
        f"Get-AppxPackage -Name '{PACKAGE_NAME}' | Remove-AppxPackage"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
    )


def package_folder(dist_dir: str) -> None:
    """Copy the menu helper into an onedir build folder when that exe exists."""
    exe = os.path.join(dist_dir, "foldersize.exe")
    if os.path.isfile(exe):
        menu = build_menu_exe(dist_dir)
        if os.path.dirname(menu) != dist_dir:
            shutil.copy2(menu, os.path.join(dist_dir, "FolderSizeMenu.exe"))
