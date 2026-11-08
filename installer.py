
import ctypes
import datetime
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import winreg
import zipfile

import requests

APP_NAME = "LyricsApp"
APP_VERSION = "1.0.1"
APP_PUBLISHER = "LyricsApp"
INSTALL_FOLDER = "LyricsApp"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\LyricsApp"

PYTHON_VERSION = "3.13.9"
PYTHON_URL = (f"https://www.python.org/ftp/python/{PYTHON_VERSION}/"
              f"python-{PYTHON_VERSION}-amd64.exe")

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008

PROBE_CODE = (
    "import sys,struct;"
    "print('PYEXE='+sys.executable);"
    "print('PYVER=%d.%d.%d'%sys.version_info[:3]);"
    "print('PYBITS=%d'%(struct.calcsize('P')*8))"
)

SETUP_LOG = os.path.join(tempfile.gettempdir(), "LyricsApp-setup.log")


def resource_path(*parts):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def default_install_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, INSTALL_FOLDER)


def install_dir():
    override = os.environ.get("LYRICSAPP_INSTALL_DIR")
    if override:
        return os.path.abspath(os.path.expandvars(override.strip('"')))
    path = default_install_dir()
    if os.path.isfile(os.path.join(path, "start.py")):
        return path
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            saved = winreg.QueryValueEx(key, "InstallLocation")[0]
        if saved and os.path.isfile(os.path.join(saved, "start.py")):
            return os.path.abspath(saved)
    except OSError:
        pass
    return path


def shortcut_dirs():
    """Desktop and Start Menu folders, overridable for tests."""
    desktop = os.environ.get("LYRICSAPP_DESKTOP_DIR")
    programs = os.environ.get("LYRICSAPP_PROGRAMS_DIR")
    if not desktop:
        desktop = _known_folder(0x10)
    if not programs:
        programs = _known_folder(0x02)
    return desktop, programs


def _known_folder(csidl):
    buf = ctypes.create_unicode_buffer(1024)
    res = ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
    return buf.value if res == 0 else ""


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def write_text(path, text, encoding="utf-8"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding=encoding, newline="") as f:
        f.write(text)


def run_hidden(cmd, timeout=120, cwd=None, env=None):
    return subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", creationflags=CREATE_NO_WINDOW)


def run_stream(cmd, emit, cwd=None, env=None, timeout=1800):
    """Run a child process and forward its combined output line by line."""
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", bufsize=1,
                            creationflags=CREATE_NO_WINDOW)
    started = time.time()
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                emit("log", line)
            if time.time() - started > timeout:
                proc.kill()
                raise RuntimeError("Команда выполнялась слишком долго")
        return proc.wait()
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass


def _probe_python(prefix):
    try:
        res = run_hidden(list(prefix) + ["-c", PROBE_CODE], timeout=25)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    data = {}
    for line in (res.stdout or "").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            data[key.strip()] = value.strip()
    exe = data.get("PYEXE", "")
    version = data.get("PYVER", "")
    bits = data.get("PYBITS", "")
    if not exe or not os.path.isfile(exe):
        return None
    if "windowsapps" in exe.lower():
        return None
    try:
        parts = tuple(int(p) for p in version.split("."))
        bits = int(bits)
    except ValueError:
        return None
    if parts < (3, 10) or bits != 64:
        return None
    return exe, parts


def _registry_pythons():
    found = []
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, r"Software\Python\PythonCore") as core:
                index = 0
                while True:
                    try:
                        name = winreg.EnumKey(core, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        with winreg.OpenKey(core, name + r"\InstallPath") as ip:
                            exe = winreg.QueryValueEx(ip, "ExecutablePath")[0]
                            if exe and os.path.isfile(exe):
                                found.append([exe])
                    except OSError:
                        pass
        except OSError:
            pass
    return found


def _common_pythons():
    bases = [os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python"),
             os.environ.get("ProgramFiles", ""),
             os.environ.get("ProgramFiles(x86)", ""),
             "C:\\"]
    found = []
    for base in bases:
        if not base or not os.path.isdir(base):
            continue
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for name in names:
            if name.lower().startswith(("python3", "python 3")):
                exe = os.path.join(base, name, "python.exe")
                if os.path.isfile(exe):
                    found.append([exe])
    return found


def find_python(emit=None, include_launcher=True):
    candidates = []
    override = os.environ.get("LYRICSAPP_PYTHON")
    if override:
        candidates.append([override.strip('"')])

    if include_launcher:
        launchers = []
        py = shutil.which("py")
        if py:
            launchers.append(py)
        for path in (os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs",
                                  "Python", "Launcher", "py.exe"),
                     os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "py.exe")):
            if os.path.isfile(path) and path not in launchers:
                launchers.append(path)
        for launcher in launchers:
            for tag in ("-3.14", "-3.13", "-3.12", "-3.11", "-3.10", "-3"):
                candidates.append([launcher, tag])

    for name in ("python", "python3"):
        exe = shutil.which(name)
        if exe:
            candidates.append([exe])

    candidates.extend(_registry_pythons())
    candidates.extend(_common_pythons())

    seen = set()
    for prefix in candidates:
        key = os.path.normcase(" ".join(prefix))
        if key in seen:
            continue
        seen.add(key)
        result = _probe_python(prefix)
        if result:
            if emit:
                emit("log", f"Найден Python {result[1][0]}.{result[1][1]}.{result[1][2]}: {result[0]}")
            return result[0]
    return None


class InstallerCore:
    def __init__(self, emit, skip_shortcuts=False, skip_registry=False):
        self.emit = emit
        self.skip_shortcuts = skip_shortcuts
        self.skip_registry = skip_registry
        self.target = install_dir()
        self.venv = os.path.join(self.target, "venv")
        self.venv_python = os.path.join(self.venv, "Scripts", "python.exe")
        self.venv_pythonw = os.path.join(self.venv, "Scripts", "pythonw.exe")

    def log(self, message):
        self.emit("log", message)

    def status(self, message):
        self.emit("status", message)

    def progress(self, value):
        self.emit("progress", value)


    def install(self):
        self.status("Подготовка...")
        self.progress(2)
        stop_running_app(self.target, self.log)
        os.makedirs(self.target, exist_ok=True)

        self.extract_payload()
        self.progress(8)

        python = find_python(self.emit)
        if not python:
            python = self.install_python()
        self.progress(35)

        self.create_venv(python)
        self.progress(45)

        self.install_requirements()
        self.progress(88)

        self.create_launchers()
        self.copy_uninstaller()
        if not self.skip_shortcuts:
            self.create_shortcuts()
        if not self.skip_registry:
            self.register_uninstall()

        write_text(os.path.join(self.target, "version.txt"),
                   APP_VERSION + "\n", encoding="utf-8")
        self.progress(100)
        self.status("Готово")
        self.log(f"LyricsApp {APP_VERSION} установлен в {self.target}")

    def extract_payload(self):
        self.status("Распаковываю файлы LyricsApp...")
        payload = resource_path("payload", "payload.zip")
        if os.path.isfile(payload):
            self.log("Распаковываю встроенный архив...")
            with zipfile.ZipFile(payload) as archive:
                root = os.path.abspath(self.target) + os.sep
                for member in archive.infolist():
                    destination = os.path.abspath(os.path.join(self.target, member.filename))
                    if not destination.startswith(root):
                        raise RuntimeError("Некорректный путь в архиве: " + member.filename)
                    archive.extract(member, self.target)
            return

        source = os.path.dirname(os.path.abspath(__file__))
        self.log("Архив не найден, копирую файлы из папки проекта.")
        for name in ("start.py", "lyrics_utils.py", "requirements.txt", "README.md"):
            src = os.path.join(source, name)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(self.target, name))
        assets_src = os.path.join(source, "assets")
        if os.path.isdir(assets_src):
            shutil.copytree(assets_src, os.path.join(self.target, "assets"), dirs_exist_ok=True)
        if not os.path.isfile(os.path.join(self.target, "start.py")):
            raise RuntimeError("Не найдены файлы приложения (start.py).")

    def install_python(self):
        self.status(f"Python не найден. Скачиваю Python {PYTHON_VERSION}...")
        self.log(f"Python 3.10+ не найден, скачиваю {PYTHON_VERSION} с python.org")
        temp_dir = tempfile.mkdtemp(prefix="lyricsapp-python-")
        installer = os.path.join(temp_dir, f"python-{PYTHON_VERSION}-amd64.exe")
        try:
            self._download(PYTHON_URL, installer)
            signature = self._signature_status(installer)
            if signature:
                self.log(f"Подпись установщика Python: {signature}")
                if signature != "Valid":
                    raise RuntimeError("Подпись установщика Python недействительна. "
                                       "Установите Python 3.10+ вручную с python.org.")
            self.status("Устанавливаю Python...")
            self.progress(20)
            args = [installer, "/quiet", "InstallAllUsers=0", "PrependPath=1",
                    "Include_launcher=1", "Include_test=0", "Include_doc=0",
                    "Include_tcltk=0", "Include_pip=1", "Shortcuts=0",
                    "AssociateFiles=0"]
            res = run_hidden(args, timeout=1200)
            self.log(f"Установщик Python завершился с кодом {res.returncode}")
            if res.returncode not in (0, 1638, 3010):
                raise RuntimeError(f"Не удалось установить Python (код {res.returncode}).")
            python = find_python(self.emit)
            if not python:
                expected = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                        "Programs", "Python", "Python313", "python.exe")
                if os.path.isfile(expected):
                    python = expected
            if not python:
                raise RuntimeError("Python установлен, но не найден. Перезапустите установщик.")
            self.log(f"Python установлен: {python}")
            return python
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _download(self, url, destination):
        headers = {"User-Agent": f"LyricsApp-Setup/{APP_VERSION}"}
        with requests.get(url, stream=True, timeout=60, headers=headers) as response:
            response.raise_for_status()
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            with open(destination, "wb") as f:
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        percent = 10 + int(done * 22 / total)
                        self.progress(min(33, percent))
        self.log(f"Скачано {done // (1024 * 1024)} МБ")

    def _signature_status(self, path):
        script = (f"(Get-AuthenticodeSignature -LiteralPath {ps_quote(path)}).Status")
        try:
            res = run_hidden(["powershell.exe", "-NoProfile", "-NonInteractive",
                              "-Command", script], timeout=90)
        except (OSError, subprocess.SubprocessError):
            return None
        if res.returncode != 0:
            return None
        return (res.stdout or "").strip() or None

    def create_venv(self, python):
        self.status("Создаю виртуальное окружение...")
        self.log(f"Создаю venv: {self.venv}")
        if os.path.isdir(self.venv) and not os.path.isfile(self.venv_python):
            shutil.rmtree(self.venv, ignore_errors=True)
        if not os.path.isfile(self.venv_python):
            res = run_hidden([python, "-m", "venv", self.venv], timeout=600,
                             cwd=self.target)
            if res.returncode != 0:
                self.log(res.stdout or "")
                self.log(res.stderr or "")
                raise RuntimeError("Не удалось создать виртуальное окружение Python.")
        if not os.path.isfile(self.venv_python):
            raise RuntimeError("В окружении нет python.exe.")

        pip_ok = run_hidden([self.venv_python, "-m", "pip", "--version"], timeout=120)
        if pip_ok.returncode != 0:
            self.log("В окружении нет pip, устанавливаю ensurepip...")
            run_hidden([self.venv_python, "-m", "ensurepip", "--upgrade"], timeout=300)

    def install_requirements(self):
        requirements = os.path.join(self.target, "requirements.txt")
        if not os.path.isfile(requirements):
            raise RuntimeError("Не найден requirements.txt.")

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PIP_NO_INPUT"] = "1"
        env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"

        self.status("Обновляю pip...")
        self.emit("indeterminate", True)
        run_stream([self.venv_python, "-m", "pip", "install", "--upgrade", "pip"],
                   self.emit, cwd=self.target, env=env)

        self.status("Устанавливаю библиотеки Python (это может занять несколько минут)...")
        cmd = [self.venv_python, "-m", "pip", "install", "--no-input",
               "-r", requirements]
        code = run_stream(cmd, self.emit, cwd=self.target, env=env)
        if code != 0:
            self.log("Первая попытка не удалась, повторяю без кеша...")
            code = run_stream(cmd + ["--no-cache-dir"], self.emit,
                              cwd=self.target, env=env)
        self.emit("indeterminate", False)
        if code != 0:
            raise RuntimeError("Не удалось установить библиотеки Python. "
                               "Проверьте интернет и повторите установку.")

    def create_launchers(self):
        self.status("Создаю ярлыки запуска...")
        script = os.path.join(self.target, "start.py")
        vbs = os.path.join(self.target, "LyricsApp.vbs")
        body = ('Set sh = CreateObject("WScript.Shell")\r\n'
                f'sh.CurrentDirectory = "{self.target}"\r\n'
                f'sh.Run """{self.venv_pythonw}"" ""{script}""", 0, False\r\n')
        write_text(vbs, body, encoding="utf-16")

        bat = os.path.join(self.target, "LyricsApp.bat")
        write_text(bat,
                   "@echo off\r\n"
                   "start \"\" \"%~dp0venv\\Scripts\\pythonw.exe\" \"%~dp0start.py\"\r\n",
                   encoding="ascii")

    def copy_uninstaller(self):
        if not getattr(sys, "frozen", False):
            self.log("Запуск из исходников: uninstall.exe не создаётся.")
            return
        target = os.path.join(self.target, "uninstall.exe")
        if os.path.normcase(os.path.abspath(sys.executable)) == os.path.normcase(target):
            return
        try:
            shutil.copy2(sys.executable, target)
            self.log("Создан uninstall.exe")
        except OSError as exc:
            self.log(f"Не удалось создать uninstall.exe: {exc}")

    def create_shortcuts(self):
        self.status("Создаю ярлыки на рабочем столе и в меню «Пуск»...")
        desktop, programs = shortcut_dirs()
        icon = os.path.join(self.target, "assets", "LyricsApp.ico")
        vbs = os.path.join(self.target, "LyricsApp.vbs")
        uninstall = os.path.join(self.target, "uninstall.exe")

        script = f"""
$ErrorActionPreference = 'Stop'
$ws = New-Object -ComObject WScript.Shell
$desktop = {ps_quote(desktop)}
$programs = {ps_quote(programs)}
$vbs = {ps_quote(vbs)}
$icon = {ps_quote(icon + ',0')}
$work = {ps_quote(self.target)}
function New-Lnk($path, $target, $arguments, $workdir, $iconLocation, $description) {{
    $lnk = $ws.CreateShortcut($path)
    $lnk.TargetPath = $target
    $lnk.Arguments = $arguments
    $lnk.WorkingDirectory = $workdir
    $lnk.IconLocation = $iconLocation
    $lnk.Description = $description
    $lnk.Save()
}}
$items = @(
    @{{ Path = (Join-Path $desktop 'LyricsApp.lnk'); Target = $vbs; Args = ''; Desc = 'LyricsApp - текст песен на экране' }},
    @{{ Path = (Join-Path $programs 'LyricsApp.lnk'); Target = $vbs; Args = ''; Desc = 'LyricsApp - текст песен на экране' }}
)
if (Test-Path -LiteralPath {ps_quote(uninstall)}) {{
    $items += @{{ Path = (Join-Path $programs 'Удалить LyricsApp.lnk'); Target = {ps_quote(uninstall)}; Args = '--uninstall'; Desc = 'Удалить LyricsApp' }}
}}
foreach ($item in $items) {{
    New-Lnk $item.Path $item.Target $item.Args $work $icon $item.Desc
}}
"""
        path = os.path.join(tempfile.gettempdir(), "lyricsapp-shortcuts.ps1")
        write_text(path, script, encoding="utf-8-sig")
        try:
            res = run_hidden(["powershell.exe", "-NoProfile", "-NonInteractive",
                              "-ExecutionPolicy", "Bypass", "-File", path], timeout=120)
            if res.returncode != 0:
                self.log(res.stdout or "")
                self.log(res.stderr or "")
                raise RuntimeError("Не удалось создать ярлыки.")
            self.log(f"Ярлыки созданы: {desktop}, {programs}")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def register_uninstall(self):
        uninstall = os.path.join(self.target, "uninstall.exe")
        if not os.path.isfile(uninstall):
            return
        icon = os.path.join(self.target, "assets", "LyricsApp.ico")
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
                winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
                winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, APP_VERSION)
                winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, APP_PUBLISHER)
                winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, self.target)
                winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, icon)
                winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ,
                                  f'"{uninstall}" --uninstall')
                winreg.SetValueEx(key, "QuietUninstallString", 0, winreg.REG_SZ,
                                  f'"{uninstall}" --uninstall --silent')
                winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD,
                                  self._dir_size_kb())
            self.log("Запись добавлена в «Приложения и возможности».")
        except OSError as exc:
            self.log(f"Не удалось добавить запись об удалении: {exc}")

    def _dir_size_kb(self):
        total = 0
        for root, _dirs, files in os.walk(self.target):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        return max(1, total // 1024)


    def launch(self):
        vbs = os.path.join(self.target, "LyricsApp.vbs")
        if os.path.isfile(vbs):
            os.startfile(vbs)
        else:
            os.startfile(os.path.join(self.target, "start.py"))


def stop_running_app(target, log=None):
    """Close an overlay started from this folder so its files are not locked."""
    if not os.path.isdir(target):
        return
    needle = os.path.abspath(target).lower()
    script = f"""
$needle = {ps_quote(needle)}
Get-CimInstance Win32_Process | Where-Object {{
    $_.CommandLine -and
    $_.CommandLine.ToLower().Contains($needle) -and
    $_.CommandLine.ToLower().Contains('start.py')
}} | ForEach-Object {{
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}}
"""
    try:
        run_hidden(["powershell.exe", "-NoProfile", "-NonInteractive",
                    "-Command", script], timeout=60)
        time.sleep(1.0)
    except (OSError, subprocess.SubprocessError):
        if log:
            log("Не удалось проверить запущенные копии LyricsApp.")


def uninstall(emit, silent=False):
    target = install_dir()
    if not silent:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        answer = messagebox.askyesno(
            "Удаление LyricsApp",
            f"Удалить LyricsApp и все файлы из папки?\n\n{target}\n\n"
            "Настройки и кеш текстов тоже будут удалены.")
        root.destroy()
        if not answer:
            return False

    emit("status", "Закрываю LyricsApp...")
    stop_running_app(target, lambda m: emit("log", m))

    desktop, programs = shortcut_dirs()
    for path in (os.path.join(desktop, "LyricsApp.lnk"),
                 os.path.join(programs, "LyricsApp.lnk"),
                 os.path.join(programs, "Удалить LyricsApp.lnk")):
        try:
            if path and os.path.isfile(path):
                os.remove(path)
                emit("log", f"Удалён ярлык: {path}")
        except OSError as exc:
            emit("log", f"Не удалось удалить {path}: {exc}")

    startup = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                           "Start Menu", "Programs", "Startup", "LyricsApp.vbs")
    try:
        if os.path.isfile(startup):
            os.remove(startup)
            emit("log", "Удалён автозапуск.")
    except OSError:
        pass

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
        emit("log", "Запись удалена из «Приложения и возможности».")
    except OSError:
        pass

    emit("status", "Удаляю файлы...")
    try:
        os.chdir(tempfile.gettempdir())
    except OSError:
        pass
    if getattr(sys, "frozen", False) and os.path.normcase(
            os.path.dirname(os.path.abspath(sys.executable))) == os.path.normcase(target):
        command = f'cmd.exe /c cd /d "%TEMP%" & timeout /t 2 /nobreak >nul & rmdir /s /q "{target}"'
        subprocess.Popen(command, cwd=tempfile.gettempdir(),
                         creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
                         close_fds=True)
    else:
        shutil.rmtree(target, ignore_errors=True)
    emit("log", "LyricsApp удалён.")
    return True


class InstallerWindow:
    def __init__(self):
        import tkinter as tk
        from tkinter import ttk, messagebox

        self.tk = tk
        self.ttk = ttk
        self.messagebox = messagebox
        self.queue = queue.Queue()
        self.installing = False
        self.done = False
        self.core = None

        self.root = tk.Tk()
        self.root.title("Установка LyricsApp")
        self.root.geometry("660x520")
        self.root.minsize(600, 460)
        self.root.configure(bg="#14161c")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._set_icon()

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TProgressbar", troughcolor="#242833",
                        background="#89b4fa", bordercolor="#14161c",
                        lightcolor="#89b4fa", darkcolor="#89b4fa")
        self._build()
        self.root.after(120, self.poll)
        self.refresh_installed_state()

    def _set_icon(self):
        ico = resource_path("assets", "LyricsApp.ico")
        try:
            self.root.iconbitmap(default=ico)
        except Exception:
            pass

    def _build(self):
        tk, ttk = self.tk, self.ttk
        outer = tk.Frame(self.root, bg="#14161c")
        outer.pack(fill="both", expand=True, padx=24, pady=20)

        header = tk.Frame(outer, bg="#14161c")
        header.pack(fill="x")
        try:
            image = tk.PhotoImage(file=resource_path("assets", "LyricsApp.png"))
            self.logo = image.subsample(max(1, image.width() // 96))
            tk.Label(header, image=self.logo, bg="#14161c").pack(side="left", padx=(0, 16))
        except Exception:
            self.logo = None
        titles = tk.Frame(header, bg="#14161c")
        titles.pack(side="left", fill="x", expand=True)
        self.title_label = tk.Label(titles, text="LyricsApp", fg="#f5f5f7", bg="#14161c",
                                    font=("Segoe UI", 22, "bold"), anchor="w")
        self.title_label.pack(fill="x")
        tk.Label(titles, text="Текст песен прямо на экране",
                 fg="#a6adc8", bg="#14161c", font=("Segoe UI", 11), anchor="w").pack(fill="x")
        self.version_label = tk.Label(titles, text=f"Версия {APP_VERSION}",
                                      fg="#6c7086", bg="#14161c",
                                      font=("Segoe UI", 9), anchor="w")
        self.version_label.pack(fill="x", pady=(2, 0))

        self.status_label = tk.Label(outer, text="Нажмите «Установить»",
                                     fg="#cdd6f4", bg="#14161c",
                                     font=("Segoe UI", 10), anchor="w")
        self.status_label.pack(fill="x", pady=(18, 4))
        self.progress = ttk.Progressbar(outer, mode="determinate", maximum=100)
        self.progress.pack(fill="x")

        log_frame = tk.Frame(outer, bg="#14161c")
        log_frame.pack(fill="both", expand=True, pady=(14, 8))
        scroll = tk.Scrollbar(log_frame)
        scroll.pack(side="right", fill="y")
        self.log_text = tk.Text(log_frame, height=8, bg="#0f1117", fg="#a6adc8",
                                insertbackground="#a6adc8", relief="flat",
                                font=("Consolas", 9), wrap="word",
                                yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.config(command=self.log_text.yview)
        self.log_text.configure(state="disabled")

        buttons = tk.Frame(outer, bg="#14161c")
        buttons.pack(fill="x", pady=(6, 0))
        self.install_btn = self._button(buttons, "Установить", self.start_install,
                                        primary=True)
        self.install_btn.pack(side="left")
        self.uninstall_btn = self._button(buttons, "Удалить", self.ask_uninstall)
        self.uninstall_btn.pack(side="left", padx=(8, 0))
        self.close_btn = self._button(buttons, "Закрыть", self.on_close)
        self.close_btn.pack(side="right")

    def _button(self, parent, text, command, primary=False):
        tk = self.tk
        return tk.Button(parent, text=text, command=command,
                         bg="#89b4fa" if primary else "#242833",
                         fg="#11131a" if primary else "#cdd6f4",
                         activebackground="#a6c8ff" if primary else "#31364a",
                         activeforeground="#11131a" if primary else "#ffffff",
                         relief="flat", bd=0, padx=18, pady=8,
                         font=("Segoe UI", 10, "bold"), cursor="hand2",
                         state="disabled")

    def refresh_installed_state(self):
        installed = os.path.isfile(os.path.join(install_dir(), "start.py"))
        if installed:
            self.title_label.config(text="LyricsApp установлен")
            self.status_label.config(text="Можно обновить или удалить приложение.")
            self.install_btn.config(text="Обновить", state="normal",
                                    command=self.start_install)
            self.uninstall_btn.config(state="normal")
        else:
            self.install_btn.config(text="Установить", state="normal",
                                    command=self.start_install)
            self.uninstall_btn.config(state="disabled")
        self.close_btn.config(state="normal")

    def start_install(self):
        if self.installing:
            return
        self.installing = True
        self.done = False
        self.install_btn.config(state="disabled", text="Установка...")
        self.uninstall_btn.config(state="disabled")
        self.close_btn.config(state="disabled")
        self.progress.config(mode="determinate", value=0)
        self.append_log("")
        self.core = InstallerCore(self.emit)
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            self.core.install()
        except Exception as exc:
            self.emit("error", str(exc))
        else:
            self.emit("done", None)

    def emit(self, kind, payload=None):
        self.queue.put((kind, payload))

    def poll(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                self.handle(kind, payload)
        except queue.Empty:
            pass
        self.root.after(120, self.poll)

    def handle(self, kind, payload):
        if kind == "status":
            self.status_label.config(text=str(payload))
        elif kind == "log":
            self.append_log(str(payload))
        elif kind == "progress":
            self.progress.config(mode="determinate", value=float(payload))
        elif kind == "indeterminate":
            if payload:
                self.progress.config(mode="indeterminate")
                self.progress.start(12)
            else:
                self.progress.stop()
                self.progress.config(mode="determinate", value=88)
        elif kind == "done":
            self.installing = False
            self.done = True
            self.progress.config(mode="determinate", value=100)
            self.status_label.config(text="Готово! LyricsApp установлен.")
            self.install_btn.config(text="Запустить LyricsApp", state="normal",
                                    command=self.launch)
            self.uninstall_btn.config(state="normal")
            self.close_btn.config(state="normal")
            self.title_label.config(text="LyricsApp установлен")
        elif kind == "error":
            self.installing = False
            self.progress.stop()
            self.progress.config(mode="determinate", value=0)
            self.status_label.config(text="Ошибка установки")
            self.append_log("ОШИБКА: " + str(payload))
            self.install_btn.config(text="Повторить", state="normal",
                                    command=self.start_install)
            self.uninstall_btn.config(state="normal")
            self.close_btn.config(state="normal")
            self.messagebox.showerror("LyricsApp", str(payload))
        elif kind == "uninstalled":
            self.installing = False
            self.done = False
            self.progress.config(mode="determinate", value=0)
            self.status_label.config(text="LyricsApp удалён.")
            self.title_label.config(text="LyricsApp")
            self.install_btn.config(text="Установить", state="normal",
                                    command=self.start_install)
            self.uninstall_btn.config(state="disabled")
            self.close_btn.config(state="normal")
        elif kind == "uninstall_cancelled":
            self.installing = False
            self.refresh_installed_state()

    def append_log(self, text):
        if not text:
            return
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 1500:
            self.log_text.delete("1.0", "300.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def launch(self):
        try:
            InstallerCore(self.emit).launch()
            self.status_label.config(text="LyricsApp запущен.")
        except Exception as exc:
            self.messagebox.showerror("LyricsApp", f"Не удалось запустить: {exc}")

    def ask_uninstall(self):
        if not self.messagebox.askyesno("Удаление LyricsApp",
                                        "Удалить LyricsApp и все его файлы?"):
            return
        self.status_label.config(text="Удаление...")
        self.install_btn.config(state="disabled")
        self.uninstall_btn.config(state="disabled")

        def worker():
            try:
                ok = uninstall(lambda kind, value=None: self.emit(kind, value), silent=True)
                self.emit("uninstalled" if ok else "uninstall_cancelled", None)
            except Exception as exc:
                self.emit("error", str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def on_close(self):
        if self.installing:
            self.messagebox.showinfo("LyricsApp", "Дождитесь окончания установки.")
            return
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def setup_logging():
    try:
        with open(SETUP_LOG, "w", encoding="utf-8") as f:
            f.write(f"LyricsApp Setup {APP_VERSION}  {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n")
    except OSError:
        pass


def emit_to_file(kind, payload=None):
    if kind in ("log", "status", "error"):
        line = f"[{kind}] {payload}"
        print(line, flush=True)
        try:
            with open(SETUP_LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def main():
    args = [a.lower() for a in sys.argv[1:]]
    silent = "--silent" in args
    uninstall_mode = "--uninstall" in args
    skip_shortcuts = "--no-shortcuts" in args
    skip_registry = "--no-registry" in args

    setup_logging()

    if uninstall_mode:
        try:
            ok = uninstall(emit_to_file, silent=silent)
            print("UNINSTALL OK" if ok else "UNINSTALL CANCELLED", flush=True)
            return 0 if ok else 1
        except Exception as exc:
            emit_to_file("error", str(exc))
            return 1

    if silent:
        core = InstallerCore(emit_to_file, skip_shortcuts=skip_shortcuts,
                             skip_registry=skip_registry)
        try:
            core.install()
            print("INSTALL OK", flush=True)
            return 0
        except Exception as exc:
            emit_to_file("error", str(exc))
            print("INSTALL FAILED", flush=True)
            return 1

    window = InstallerWindow()
    window.core = InstallerCore(window.emit)
    window.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
