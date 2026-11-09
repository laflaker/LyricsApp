import os
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(ROOT, "build")
DIST = os.path.join(ROOT, "dist")
PAYLOAD = os.path.join(BUILD, "payload.zip")
ICON = os.path.join(ROOT, "assets", "LyricsApp.ico")
VERSION = "1.0.1"

APP_FILES = [
    "start.py",
    "lyrics_utils.py",
    "requirements.txt",
    "README.md",
]
APP_DIRS = ["assets"]


def payload_files():
    for name in APP_FILES:
        path = os.path.join(ROOT, name)
        if not os.path.isfile(path):
            raise SystemExit(f"missing app file: {path}")
        yield path, name
    for folder in APP_DIRS:
        base = os.path.join(ROOT, folder)
        if not os.path.isdir(base):
            continue
        for current, _dirs, files in os.walk(base):
            for name in files:
                path = os.path.join(current, name)
                rel = os.path.relpath(path, ROOT).replace("\\", "/")
                yield path, rel


def make_payload():
    os.makedirs(BUILD, exist_ok=True)
    tmp = PAYLOAD + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path, rel in payload_files():
            archive.write(path, rel)
    os.replace(tmp, PAYLOAD)
    print(f"payload: {PAYLOAD} ({os.path.getsize(PAYLOAD) / 1024:.0f} KB)")


def version_file():
    path = os.path.join(BUILD, "version_info.txt")
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=(1, 0, 1, 0),
    prodvers=(1, 0, 1, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', 'LyricsApp'),
         StringStruct('FileDescription', 'LyricsApp Setup'),
         StringStruct('FileVersion', '{VERSION}'),
         StringStruct('InternalName', 'LyricsApp-Setup'),
         StringStruct('OriginalFilename', 'LyricsApp-Setup.exe'),
         StringStruct('ProductName', 'LyricsApp'),
         StringStruct('ProductVersion', '{VERSION}')])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def build():
    make_payload()
    vinfo = version_file()

    pyinstaller = shutil.which("pyinstaller")
    if not pyinstaller:
        candidate = os.path.join(os.path.dirname(sys.executable), "Scripts",
                                 "pyinstaller.exe")
        if os.path.isfile(candidate):
            pyinstaller = candidate
    if not pyinstaller:
        raise SystemExit("pyinstaller not found: pip install pyinstaller")

    work = os.path.join(BUILD, "pyinstaller")
    cmd = [
        pyinstaller,
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name", "LyricsApp-Setup",
        "--icon", ICON,
        "--version-file", vinfo,
        "--add-data", f"{PAYLOAD};payload",
        "--distpath", DIST,
        "--workpath", work,
        "--specpath", BUILD,
        os.path.join(ROOT, "installer.py"),
    ]
    print(" ".join(f'"{p}"' if " " in p else p for p in cmd))
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        raise SystemExit(f"pyinstaller failed with code {result.returncode}")

    exe = os.path.join(DIST, "LyricsApp-Setup.exe")
    print(f"\nbuilt: {exe} ({os.path.getsize(exe) / (1024 * 1024):.1f} MB)")


if __name__ == "__main__":
    build()
