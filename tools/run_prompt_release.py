#!/usr/bin/env python3
"""Prompt release builder.

This is the rebuilt v223-style release runner.  It is intentionally independent
from the GUI startup path so `start.py --build --offscreen ...` cannot open a Qt
window while building executables/installers.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import threading
import queue
import sys
import tempfile
import textwrap
import time
import traceback
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

APP_NAME = "Prompt"
VERSION = "1.0.0"
# Default release candidates. PyOxidizer and Briefcase stay available by
# explicit flag/env, but they are intentionally not in the default path: the
# current PyOxidizer crates.io install path can fail before it even reaches our
# config, and Briefcase needs a real BeeWare app template before it can produce
# a release-ready Windows executable. Keeping known-diagnostic builders out of
# the default run prevents slow, noisy failures from hiding the good artifacts.
DEFAULT_EXE_BACKENDS = ["pyinstaller", "pyinstaller_dir", "cx_freeze", "pyapp", "nuitka"]
OPTIONAL_EXE_BACKENDS = ["py2exe", "pyoxidizer", "briefcase"]
DEFAULT_INSTALLERS = ["nsis", "inno", "wix", "msix", "advanced_installer"]
VOLATILE_DATA_DIRS = {"generated", "linux_runtime", "logs", "reports", "workspaces", "build", "dist", "installer", "release_upload"}
DATA_DIRS = ["assets", "favicon_io", "help", "js", "doctypes", "prompts", "workflows", "fonts", "typings", "debs"]
DATA_FILES = ["config.ini", "icon.ico", "favicon.ico"]


@dataclass(frozen=True)
class InstallerToolSpec:
    maker: str
    tool_names: tuple[str, ...]
    winget_id: str = ""
    winget_name: str = ""
    install_note: str = ""
    install_kind: str = "winget"


INSTALLER_TOOL_SPECS: dict[str, InstallerToolSpec] = {
    "nsis": InstallerToolSpec(
        maker="nsis",
        tool_names=("makensis", "makensis.exe"),
        winget_id="NSIS.NSIS",
        install_note="NSIS installs makensis.exe, usually under Program Files\\NSIS.",
    ),
    "inno": InstallerToolSpec(
        maker="inno",
        tool_names=("ISCC", "ISCC.exe", "iscc"),
        winget_id="JRSoftware.InnoSetup",
        install_note="Inno Setup installs ISCC.exe, usually under Program Files (x86)\\Inno Setup 6; winget may not add it to PATH.",
    ),
    "wix": InstallerToolSpec(
        maker="wix",
        tool_names=("wix", "wix.exe"),
        winget_id="",
        install_kind="dotnet_tool",
        install_note="WiX CLI is best installed as a global .NET tool: dotnet tool install --global wix. Requires .NET SDK 6+.",
    ),
    "msix": InstallerToolSpec(
        maker="msix",
        tool_names=("makeappx", "makeappx.exe"),
        winget_id="Microsoft.WindowsSDK",
        install_note="MakeAppx.exe is shipped in the Windows SDK / Windows Kits folders, not as a Python package.",
    ),
    "advanced_installer": InstallerToolSpec(
        maker="advanced_installer",
        tool_names=("AdvancedInstaller.com", "advancedinstaller.com", "advinst.exe"),
        winget_id="Caphyon.AdvancedInstaller",
        install_note="Advanced Installer CLI is commercial-friendly; some project types/features may need a license even if the CLI is present.",
    ),
}


def _now() -> str:
    import datetime as _dt
    return _dt.datetime.now().isoformat(timespec="seconds")


def _is_windows() -> bool:
    return os.name == "nt" or platform.system().lower().startswith("win")


def md5_file(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024.0
    return f"{size}B"


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def quote_cmd(cmd: Iterable[str]) -> str:
    import shlex
    return " ".join(shlex.quote(str(part)) for part in cmd)


def normalize_backend(name: str) -> str:
    key = str(name or "").strip().lower().replace("-", "_")
    aliases = {
        "pyinstaller": "pyinstaller", "py_installer": "pyinstaller",
        "pyinstaller_dir": "pyinstaller_dir", "pyinstallerdir": "pyinstaller_dir", "pyinstaller_onedir": "pyinstaller_dir", "pyinstaller_one_dir": "pyinstaller_dir", "pyinstaller-dir": "pyinstaller_dir", "pyinstaller-onedir": "pyinstaller_dir", "pyinstaller_d": "pyinstaller_dir", "onedir": "pyinstaller_dir", "one_dir": "pyinstaller_dir",
        "nuitka": "nuitka", "nikita": "nuitka",
        "cxfreeze": "cx_freeze", "cx_freeze": "cx_freeze", "cx_freeze_exe": "cx_freeze",
        "py2exe": "py2exe",
        "pyoxidizer": "pyoxidizer",
        "pyapp": "pyapp",
        "briefcase": "briefcase", "beeware": "briefcase",
    }
    return aliases.get(key, key)


@dataclass
class Artifact:
    backend: str
    path: Path
    kind: str = "exe"
    bundle: Path | None = None
    release_ready: bool = True
    note: str = ""


@dataclass
class BackendResult:
    backend: str
    status: str
    artifact: Artifact | None = None
    message: str = ""
    elapsed: float = 0.0
    logs: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.artifact is not None and self.artifact.path.exists()




@dataclass
class InstallerInput:
    exe_backend: str
    source_exe: Path
    payload_dir: Path
    exe_rel: str
    bundle_zip: Path | None = None
    release_ready: bool = True
    note: str = ""


class ReleaseRunner:
    def __init__(self, root: Path, argv: list[str], *, dry_run: bool = False, force: bool = False) -> None:
        self.root = root.resolve()
        self.argv = list(argv)
        self.dry_run = bool(dry_run)
        self.force = bool(force)
        self.dist = self.root / "dist"
        self.logs = self.root / "logs"
        self.debug_log = self.root / "debug.log"
        self.pipeline_log = self.logs / "build_pipeline.log"
        self.tool_state_path = self.logs / "installer_tool_state.json"
        self.build_python_state_path = self.logs / "build_python_state.json"
        self.installer_tool_cooldown_hours = float(os.environ.get("PROMPT_INSTALLER_TOOL_COOLDOWN_HOURS", "24") or "24")
        root_hash = hashlib.sha1(str(self.root).encode("utf-8", "replace")).hexdigest()[:12]
        temp_base = Path(os.environ.get("PROMPT_BUILD_TEMP", "") or tempfile.gettempdir()).resolve()
        # Unique session folder prevents overlapped builds from fighting over PyInstaller/Nuitka scratch.
        self.session = f"{int(time.time())}_{os.getpid()}"
        self.temp = temp_base / f"PromptBuild_{root_hash}" / self.session
        self.frozen_entry = self.root / "frozen_prompt_entry.py"
        self.app_entry = self.root / "prompt_app.py"
        self.require_strict_exe = os.environ.get("PROMPT_REQUIRE_ALL_EXE_BACKENDS", "").lower() in {"1", "true", "yes", "on"} or os.environ.get("PROMPT_STRICT_BUILD", "").lower() in {"1", "true", "yes", "on"}
        self.require_strict_installers = os.environ.get("PROMPT_REQUIRE_ALL_INSTALLERS", "").lower() in {"1", "true", "yes", "on"} or os.environ.get("PROMPT_STRICT_BUILD", "").lower() in {"1", "true", "yes", "on"}
        self.pyinstaller_collection_mode = os.environ.get("PROMPT_PYINSTALLER_QT_COLLECTION", "lean").strip().lower() or "lean"
        self.dist.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        self.temp.mkdir(parents=True, exist_ok=True)
        if os.environ.get("PROMPT_APPEND_BUILDER_RAW_LOG", "").lower() not in {"1", "true", "yes", "on"}:
            try:
                self.pipeline_log.write_text("", encoding="utf-8")
            except OSError:
                pass
        self.log(f"RELEASE:BEGIN root={self.root} python={sys.executable} version={sys.version.split()[0]} argv={self.argv} dry_run={self.dry_run} temp={self.temp}")

    def log(self, message: str) -> None:
        line = f"[PROMPT-RELEASE] {message}"
        print(line, flush=True)
        for path in (self.pipeline_log, self.debug_log):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8", errors="replace") as fh:
                    fh.write(f"{_now()} {line}\n")
            except OSError:
                pass

    def warn(self, message: str) -> None:
        line = f"[WARN:PROMPT-RELEASE] {message}"
        print(line, file=sys.stderr, flush=True)
        for path in (self.pipeline_log, self.debug_log):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8", errors="replace") as fh:
                    fh.write(f"{_now()} {line}\n")
            except OSError:
                pass

    def fault_log_path(self) -> Path:
        return self.logs / "release_faults.log"

    def status_log_path(self) -> Path:
        return self.logs / "release_status.log"

    def _tail_text(self, path: Path | None, *, lines: int = 160, chars: int = 12000) -> str:
        if path is None:
            return ""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        tail = "\n".join(text.splitlines()[-max(1, int(lines or 1)):])
        return tail[-max(1000, int(chars or 1000)):]

    def mark_status(self, label: str, status: str, **details: object) -> None:
        pairs = " ".join(f"{key}={str(value).replace(chr(10), ' ')[:500]}" for key, value in sorted(details.items()))
        line = f"RELEASE:STATUS label={label} status={status}" + (f" {pairs}" if pairs else "")
        self.log(line)
        try:
            self.status_log_path().parent.mkdir(parents=True, exist_ok=True)
            with self.status_log_path().open("a", encoding="utf-8", errors="replace") as fh:
                fh.write(f"{_now()} {line}\n")
        except OSError:
            pass

    def fault(self, label: str, message: str, *, raw: Path | None = None, exc: BaseException | None = None, rc: int | None = None, pid: int | None = None) -> None:
        tail = self._tail_text(raw, lines=160, chars=12000) if raw else ""
        trace = ""
        if exc is not None:
            try:
                trace = "".join(traceback.format_exception(type(exc), exc, getattr(exc, "__traceback__", None)))
            except Exception:
                trace = f"{type(exc).__name__}: {exc}"
        header = f"FAULT label={label} rc={rc if rc is not None else ''} pid={pid if pid is not None else ''} raw_log={raw or ''} message={message}"
        block = header
        if trace:
            block += "\nTRACEBACK:\n" + trace.rstrip()
        if tail:
            block += "\nRAW-TAIL-BEGIN\n" + tail.rstrip() + "\nRAW-TAIL-END"
        # Print a compact grep-friendly line and store the full diagnostic block.
        self.warn(f"{label}:FAULT rc={rc if rc is not None else ''} pid={pid if pid is not None else ''} raw_log={raw or ''} message={message}")
        for path in (self.fault_log_path(), self.pipeline_log, self.debug_log):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8", errors="replace") as fh:
                    fh.write(f"{_now()} [FAULT:PROMPT-RELEASE] {block}\n")
            except OSError:
                pass

    def dist_snapshot(self, context: str, *, limit: int = 80) -> list[str]:
        """Log a grep-friendly inventory of current release artifacts.

        The build can spend a long time inside one backend before an EXE appears.
        These snapshots make it obvious whether dist is empty because the current
        backend is still running, because force-clean removed older artifacts, or
        because a backend failed after writing a temporary file elsewhere.
        """
        rows: list[str] = []
        try:
            self.dist.mkdir(parents=True, exist_ok=True)
            files = sorted([p for p in self.dist.rglob("*") if p.is_file()], key=lambda x: str(x).lower())
            total = sum(int(p.stat().st_size or 0) for p in files if p.exists())
            self.log(f"DIST:SNAPSHOT context={context} count={len(files)} total_bytes={total} total_human={human_size(total)} dist={self.dist}")
            for item in files[:max(0, int(limit or 0))]:
                try:
                    rows.append(f"{item.relative_to(self.dist)}:{item.stat().st_size}")
                    self.log(f"DIST:FILE context={context} name={item.relative_to(self.dist)} bytes={item.stat().st_size} human={human_size(item.stat().st_size)} md5={md5_file(item) if item.is_file() else ''}")
                except Exception as exc:
                    self.warn(f"DIST:FILE:ERROR context={context} path={item} {type(exc).__name__}: {exc}")
            if len(files) > limit:
                self.log(f"DIST:SNAPSHOT context={context} omitted={len(files)-limit}")
        except Exception as exc:
            self.warn(f"DIST:SNAPSHOT:ERROR context={context} {type(exc).__name__}: {exc}")
        return rows

    def temp_artifact_snapshot(self, context: str, *, limit: int = 80) -> None:
        """Log current temp build outputs, so failed builders are not invisible."""
        try:
            patterns = ("*.exe", "*.dll", "*.pyd", "*.msi", "*.msix", "*.zip")
            files: list[Path] = []
            for pattern in patterns:
                files.extend(self.temp.rglob(pattern) if self.temp.exists() else [])
            files = sorted(set(files), key=lambda x: str(x).lower())
            self.log(f"TEMP:SNAPSHOT context={context} count={len(files)} temp={self.temp}")
            for item in files[:max(0, int(limit or 0))]:
                try:
                    self.log(f"TEMP:FILE context={context} name={item.relative_to(self.temp)} bytes={item.stat().st_size} human={human_size(item.stat().st_size)}")
                except Exception as exc:
                    self.warn(f"TEMP:FILE:ERROR context={context} path={item} {type(exc).__name__}: {exc}")
            if len(files) > limit:
                self.log(f"TEMP:SNAPSHOT context={context} omitted={len(files)-limit}")
        except Exception as exc:
            self.warn(f"TEMP:SNAPSHOT:ERROR context={context} {type(exc).__name__}: {exc}")

    def log_backend_plan(self, backends: list[str]) -> None:
        self.log(f"EXE:BACKENDS requested={','.join(backends)} strict={self.require_strict_exe} dry_run={self.dry_run} force={self.force} resume={os.environ.get('PROMPT_BUILD_RESUME','')}")
        self.log(f"EXE:ENV PROMPT_EXE_BACKENDS={os.environ.get('PROMPT_EXE_BACKENDS','')} PROMPT_BUILD_PYTHON={os.environ.get('PROMPT_BUILD_PYTHON','')} PROMPT_LAUNCH_PYTHON={os.environ.get('PROMPT_LAUNCH_PYTHON','')}")

    def raw_log_path(self, label: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in label)
        return self.logs / f"{safe}.raw.log"

    def run_cmd(self, cmd: list[str], *, label: str, cwd: Path | None = None, timeout: int = 7200, env: dict[str, str] | None = None, soft: bool = True) -> tuple[int, str]:
        raw = self.raw_log_path(label)
        raw.parent.mkdir(parents=True, exist_ok=True)
        if os.environ.get("PROMPT_APPEND_BUILDER_RAW_LOG", "").lower() not in {"1", "true", "yes", "on"}:
            raw.write_text("", encoding="utf-8")
        self.mark_status(label, "starting", command=quote_cmd(cmd), cwd=str(cwd or self.root), raw_log=str(raw), timeout=timeout)
        self.log(f"{label}:COMMAND {quote_cmd(cmd)}")
        self.log(f"{label}:CWD {cwd or self.root}")
        self.log(f"{label}:RAW-LOG {raw}")
        self.dist_snapshot(f"before-command:{label}", limit=20)
        if self.dry_run:
            raw.write_text(f"DRY RUN: {quote_cmd(cmd)}\n", encoding="utf-8")
            self.mark_status(label, "dry-run", rc=0)
            self.log(f"{label}:DRY-RUN rc=0")
            return 0, ""
        started = time.monotonic()
        proc: subprocess.Popen[str] | None = None
        lines: list[str] = []
        last_line = ""
        # phase/process-owned helper thread: this reader is scoped to exactly one
        # child build process. It exists only to keep parent-side heartbeats and
        # fault logging alive when a builder goes quiet without printing a newline.
        output_queue: "queue.Queue[object]" = queue.Queue()
        sentinel = object()

        def reader_thread() -> None:
            try:
                assert proc is not None and proc.stdout is not None
                for child_line in proc.stdout:
                    output_queue.put(child_line)
            except BaseException as read_error:
                output_queue.put(read_error)
            finally:
                output_queue.put(sentinel)

        try:
            merged_env = os.environ.copy()
            merged_env.update(env or {})
            popen_kwargs = dict(
                cwd=str(cwd or self.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=merged_env,
                bufsize=1,
            )
            if os.name == "nt":
                flags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) or 0) | int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
                if flags:
                    popen_kwargs["creationflags"] = flags
                try:
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= int(getattr(subprocess, "STARTF_USESHOWWINDOW", 0) or 0)
                    startupinfo.wShowWindow = 0
                    popen_kwargs["startupinfo"] = startupinfo
                except Exception as start_info_error:
                    self.warn(f"{label}:STARTUPINFO warning {type(start_info_error).__name__}: {start_info_error}")
            else:
                popen_kwargs["start_new_session"] = True
            proc = subprocess.Popen([str(part) for part in cmd], **popen_kwargs)
            self.mark_status(label, "running", pid=getattr(proc, "pid", 0))
            thread = threading.Thread(target=reader_thread, name=f"release-reader-{label}", daemon=True)
            thread.start()
            next_heartbeat = started + 20.0
            next_snapshot = started + 60.0
            reader_done = False
            while True:
                now = time.monotonic()
                try:
                    item = output_queue.get(timeout=0.20)
                except queue.Empty:
                    item = None
                if item is sentinel:
                    reader_done = True
                elif isinstance(item, BaseException):
                    self.fault(label, f"stdout reader failed: {type(item).__name__}: {item}", raw=raw, exc=item, pid=getattr(proc, "pid", None))
                elif item:
                    text = str(item).rstrip("\r\n")
                    last_line = text
                    lines.append(text)
                    with raw.open("a", encoding="utf-8", errors="replace") as fh:
                        fh.write(text + "\n")
                    lowered = text.lower()
                    if text.strip() and ("error" in lowered or "warn" in lowered or "success" in lowered or "failed" in lowered or "traceback" in lowered or "exception" in lowered or len(lines) % 80 == 0):
                        self.log(f"{label}:OUTPUT {text}")
                poll = proc.poll()
                if poll is not None and (reader_done or output_queue.empty()):
                    break
                if now >= next_heartbeat:
                    raw_size = 0
                    try:
                        raw_size = int(raw.stat().st_size)
                    except OSError:
                        pass
                    alive = proc.poll() is None
                    self.log(f"{label}:HEARTBEAT pid={proc.pid} alive={alive} elapsed={int(now-started)}s lines={len(lines)} raw_bytes={raw_size} last={last_line[-240:]}")
                    self.mark_status(label, "running", pid=proc.pid, elapsed=int(now-started), lines=len(lines), raw_bytes=raw_size, last=last_line[-240:], alive=alive)
                    next_heartbeat = now + 20.0
                if now >= next_snapshot:
                    self.dist_snapshot(f"heartbeat:{label}:elapsed={int(now-started)}", limit=20)
                    self.temp_artifact_snapshot(f"heartbeat:{label}:elapsed={int(now-started)}", limit=30)
                    next_snapshot = now + 60.0
                if timeout and (now - started) > timeout:
                    self.fault(label, f"timeout after {timeout}s; killing process tree", raw=raw, rc=124, pid=getattr(proc, "pid", None))
                    try:
                        if os.name == "nt":
                            kill_result = subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, errors="replace", check=False, timeout=20)
                            self.warn(f"{label}:TIMEOUT-KILL taskkill rc={getattr(kill_result, 'returncode', '')} output={str(getattr(kill_result, 'stdout', '') or '')[-1000:]}")
                        else:
                            try:
                                os.killpg(int(proc.pid), signal.SIGTERM)
                            except Exception:
                                proc.terminate()
                            try:
                                proc.wait(timeout=8)
                            except Exception:
                                try:
                                    os.killpg(int(proc.pid), signal.SIGKILL)
                                except Exception:
                                    proc.kill()
                    except BaseException as kill_error:
                        self.fault(label, "timeout kill failed", raw=raw, exc=kill_error, rc=124, pid=getattr(proc, "pid", None))
                    raise subprocess.TimeoutExpired(cmd, timeout)
            rc = int(proc.wait() or 0)
            with contextlib.suppress(Exception):
                thread.join(timeout=2.0)
            elapsed = time.monotonic() - started
            self.log(f"{label}:EXIT rc={rc} elapsed={elapsed:.1f}s lines={len(lines)} last={last_line[-240:]}")
            self.mark_status(label, "exited", rc=rc, elapsed=f"{elapsed:.1f}", lines=len(lines), last=last_line[-240:])
            self.dist_snapshot(f"after-command:{label}:rc={rc}", limit=40)
            self.temp_artifact_snapshot(f"after-command:{label}:rc={rc}", limit=40)
            if rc != 0:
                self.fault(label, f"process exited non-zero rc={rc}", raw=raw, rc=rc, pid=getattr(proc, "pid", None))
            return rc, "\n".join(lines)
        except BaseException as exc:
            rc = 124 if isinstance(exc, subprocess.TimeoutExpired) else 1
            if proc is not None:
                with contextlib.suppress(Exception):
                    if proc.poll() is None:
                        if os.name == "nt":
                            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, check=False, timeout=20)
                        else:
                            with contextlib.suppress(Exception):
                                os.killpg(int(proc.pid), signal.SIGKILL)
            self.fault(label, f"{type(exc).__name__}: {exc}", raw=raw, exc=exc, rc=rc, pid=getattr(proc, "pid", None) if proc is not None else None)
            self.mark_status(label, "fault", rc=rc, exception=type(exc).__name__, message=str(exc))
            self.dist_snapshot(f"fault-command:{label}:rc={rc}", limit=40)
            self.temp_artifact_snapshot(f"fault-command:{label}:rc={rc}", limit=40)
            if not soft:
                raise
            return rc, self._tail_text(raw, lines=160, chars=12000)

    def _command_display(self, command: list[str]) -> str:
        return quote_cmd([str(part) for part in list(command or [])])

    def _hidden_run_probe(self, command: list[str], *, timeout: int = 12) -> tuple[int, str]:
        popen_kwargs = dict(
            cwd=str(self.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=max(1, int(timeout or 12)),
        )
        if os.name == "nt":
            flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
            if flags:
                popen_kwargs["creationflags"] = flags
            try:
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= int(getattr(subprocess, "STARTF_USESHOWWINDOW", 0) or 0)
                startupinfo.wShowWindow = 0
                popen_kwargs["startupinfo"] = startupinfo
            except Exception:
                pass
        try:
            completed = subprocess.run([str(part) for part in command], **popen_kwargs)
            return int(completed.returncode or 0), str(completed.stdout or "")
        except BaseException as exc:
            return 999, f"{type(exc).__name__}: {exc}"

    def probe_python_version(self, command: list[str], *, label: str = "python-probe") -> tuple[int, int, int] | None:
        probe = [*list(command or []), "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"]
        rc, out = self._hidden_run_probe(probe, timeout=10)
        text = str(out or "").strip().splitlines()[-1:] or [""]
        if rc != 0:
            self.warn(f"BUILD-PYTHON:PROBE:FAILED label={label} command={self._command_display(command)} rc={rc} output={str(out or '')[-600:]}")
            return None
        try:
            parts = [int(part) for part in text[0].strip().split(".")[:3]]
            while len(parts) < 3:
                parts.append(0)
            version = (parts[0], parts[1], parts[2])
            self.log(f"BUILD-PYTHON:PROBE:OK label={label} command={self._command_display(command)} version={version[0]}.{version[1]}.{version[2]}")
            return version
        except Exception as exc:
            self.warn(f"BUILD-PYTHON:PROBE:PARSE-FAILED label={label} command={self._command_display(command)} output={str(out or '')[-600:]} {type(exc).__name__}: {exc}")
            return None

    def python_command_candidates(self, backend: str) -> list[list[str]]:
        candidates: list[list[str]] = []
        env_keys = [f"PROMPT_{backend.upper()}_PYTHON", "PROMPT_BUILD_PYTHON", "PROMPT_PYTHON", "PYTHON313", "PYTHON312", "PYTHON311", "PYTHON_EXE"]
        seen: set[str] = set()

        def add(command: list[str]) -> None:
            cleaned = [str(part).strip().strip('"') for part in list(command or []) if str(part).strip()]
            if not cleaned:
                return
            key = "\0".join(cleaned).lower()
            if key in seen:
                return
            seen.add(key)
            candidates.append(cleaned)

        current_exe = str(sys.executable or "").strip().strip('"')
        keep_build_python = str(os.environ.get("PROMPT_KEEP_BUILD_PYTHON", "") or "").strip().lower() in {"1", "true", "yes", "on"}
        for key in env_keys:
            raw = str(os.environ.get(key, "") or "").strip().strip('"')
            if raw:
                if key == "PROMPT_BUILD_PYTHON" and current_exe and raw.lower() == current_exe.lower() and not keep_build_python:
                    self.warn(f"BUILD-PYTHON:ENV:IGNORED key={key} value={raw} reason=matches-release-runner-python; per-backend probing must remain free. Set PROMPT_KEEP_BUILD_PYTHON=1 to force it.")
                    continue
                add([raw])
        if os.name == "nt":
            local = os.environ.get("LOCALAPPDATA", "") or ""
            defaults = [r"C:\Python313\python.exe", r"C:\Python312\python.exe", r"C:\Python311\python.exe", r"C:\Python310\python.exe"]
            if local:
                defaults.extend([
                    str(Path(local) / "Programs" / "Python" / "Python313" / "python.exe"),
                    str(Path(local) / "Programs" / "Python" / "Python312" / "python.exe"),
                    str(Path(local) / "Programs" / "Python" / "Python311" / "python.exe"),
                    str(Path(local) / "Programs" / "Python" / "Python310" / "python.exe"),
                ])
            for raw in defaults:
                add([raw])
            py_launcher = shutil.which("py") or shutil.which("py.exe")
            if py_launcher:
                for ver in ("-3.13", "-3.12", "-3.11", "-3.10", "-3.14"):
                    add([py_launcher, ver])
        for exe in (shutil.which("python"), shutil.which("python3"), sys.executable):
            if exe:
                add([str(exe)])
        return candidates

    def _build_python_state(self) -> dict[str, object]:
        try:
            return json.loads(self.build_python_state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_build_python_state(self, state: dict[str, object]) -> None:
        try:
            self.build_python_state_path.parent.mkdir(parents=True, exist_ok=True)
            self.build_python_state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        except OSError as exc:
            self.warn(f"BUILD-PYTHON:STATE write failed {type(exc).__name__}: {exc}")

    def maybe_install_build_python(self, version: str = "3.13") -> None:
        if self.dry_run:
            self.log(f"BUILD-PYTHON:INSTALL:DRY-RUN version={version} winget_id=Python.Python.{version}")
            return
        if os.name != "nt":
            self.warn(f"BUILD-PYTHON:INSTALL:SKIP non-Windows host version={version}")
            return
        if str(os.environ.get("PROMPT_AUTO_INSTALL_BUILD_PYTHON", "0")).lower() not in {"1", "true", "yes", "on"}:
            self.warn("BUILD-PYTHON:INSTALL:SKIP PROMPT_AUTO_INSTALL_BUILD_PYTHON is not enabled")
            return
        state = self._build_python_state()
        now = time.time()
        cooldown_hours = float(os.environ.get("PROMPT_BUILD_PYTHON_COOLDOWN_HOURS", "24") or "24")
        key = f"Python.Python.{version}"
        last = state.get(key, {}) if isinstance(state.get(key, {}), dict) else {}
        last_at = float(last.get("attempted_at", 0) or 0)
        if last_at and (now - last_at) < cooldown_hours * 3600:
            self.warn(f"BUILD-PYTHON:INSTALL:COOLDOWN version={version} hours={cooldown_hours} last_status={last.get('status','unknown')}")
            return
        winget = shutil.which("winget") or shutil.which("winget.exe")
        if not winget:
            self.warn("BUILD-PYTHON:INSTALL:SKIP winget not found; install Python 3.13 or set PROMPT_BUILD_PYTHON")
            state[key] = {"attempted_at": now, "status": "no-winget"}
            self._write_build_python_state(state)
            return
        command = [winget, "install", "-e", "--id", key, "--accept-source-agreements", "--accept-package-agreements"]
        rc, output = self.run_cmd(command, label=f"install_build_python_{version.replace('.', '_')}", timeout=3600, soft=True)
        state[key] = {"attempted_at": now, "status": "ok" if rc == 0 else "failed", "rc": rc, "tail": output[-1000:]}
        self._write_build_python_state(state)

    def backend_python_command(self, backend: str, *, prefer_build_safe: bool = True) -> list[str]:
        allowed_safe = {(3, 10), (3, 11), (3, 12), (3, 13)}
        fallback: tuple[list[str], tuple[int, int, int]] | None = None
        for command in self.python_command_candidates(backend):
            if len(command) == 1 and (":" in command[0] or command[0].startswith("/")) and not Path(command[0]).exists():
                self.log(f"BUILD-PYTHON:CANDIDATE:MISS backend={backend} command={self._command_display(command)}")
                continue
            version = self.probe_python_version(command, label=backend)
            if version is None:
                continue
            if prefer_build_safe and (version[0], version[1]) in allowed_safe:
                self.log(f"BUILD-PYTHON:SELECT backend={backend} command={self._command_display(command)} version={version[0]}.{version[1]}.{version[2]} safe=1")
                return command
            if not prefer_build_safe and version >= (3, 10, 0):
                self.log(f"BUILD-PYTHON:SELECT backend={backend} command={self._command_display(command)} version={version[0]}.{version[1]}.{version[2]} safe=0")
                return command
            if version >= (3, 10, 0) and fallback is None:
                fallback = (command, version)
        if prefer_build_safe:
            self.maybe_install_build_python("3.13")
            for command in self.python_command_candidates(backend):
                version = self.probe_python_version(command, label=f"{backend}-after-install")
                if version is not None and (version[0], version[1]) in allowed_safe:
                    self.log(f"BUILD-PYTHON:SELECT backend={backend} command={self._command_display(command)} version={version[0]}.{version[1]}.{version[2]} safe=1 after_install=1")
                    return command
        if fallback is not None:
            command, version = fallback
            self.warn(f"BUILD-PYTHON:UNSAFE-FALLBACK backend={backend} command={self._command_display(command)} version={version[0]}.{version[1]}.{version[2]} reason=no-3.10-to-3.13-found")
            return command
        self.warn(f"BUILD-PYTHON:FALLBACK-CURRENT backend={backend} command={sys.executable}")
        return [sys.executable]

    def ensure_module_for_python(self, import_name: str, package: str, *, backend: str, python_cmd: list[str]) -> bool:
        probe = [*list(python_cmd or [sys.executable]), "-c", f"import {import_name}; print('ok')"]
        self.log(f"{backend}:MODULE:PROBE import={import_name} package={package} python={self._command_display(python_cmd)}")
        rc, out = self._hidden_run_probe(probe, timeout=20)
        if rc == 0:
            self.log(f"{backend}:MODULE import={import_name} status=ok python={self._command_display(python_cmd)}")
            return True
        self.warn(f"{backend}:MODULE import={import_name} missing rc={rc} python={self._command_display(python_cmd)} output={out[-1200:]}")
        pip_version_rc, pip_version_out = self._hidden_run_probe([*list(python_cmd or [sys.executable]), "-m", "pip", "--version"], timeout=20)
        self.log(f"{backend}:PIP:VERSION rc={pip_version_rc} python={self._command_display(python_cmd)} output={str(pip_version_out or '')[-800:]}")
        if self.dry_run:
            self.log(f"{backend}:MODULE dry-run would install package={package} python={self._command_display(python_cmd)}")
            return True
        if os.environ.get("PROMPT_DISABLE_AUTO_PIP_INSTALL", "").lower() in {"1", "true", "yes", "on"}:
            self.warn(f"{backend}:MODULE install disabled by PROMPT_DISABLE_AUTO_PIP_INSTALL=1")
            return False
        retries = int(os.environ.get("PROMPT_PIP_INSTALL_RETRIES", "2") or "2")
        timeout = int(os.environ.get("PROMPT_PIP_INSTALL_TIMEOUT_SECONDS", "7200") or "7200")
        install_commands: list[list[str]] = []
        base_python = list(python_cmd or [sys.executable])
        install_commands.append([*base_python, "-m", "pip", "install", package])
        if backend == "py2exe":
            # py2exe wheel availability changes by Python minor version.  Try the
            # normal resolver first, then a pre-release/upgrade resolver so the
            # log shows whether a newer wheel exists for Python 3.14.
            install_commands.append([*base_python, "-m", "pip", "install", "--upgrade", "--pre", package])
        for attempt in range(1, max(1, retries) + 1):
            for command_index, command in enumerate(install_commands, start=1):
                label = f"install_{backend}_{attempt}_{command_index}"
                self.log(f"{backend}:MODULE:INSTALL:COMMAND attempt={attempt}/{retries} variant={command_index}/{len(install_commands)} command={quote_cmd(command)}")
                rc, output = self.run_cmd(command, label=label, timeout=timeout)
                self.log(f"{backend}:MODULE:INSTALL:EXIT attempt={attempt}/{retries} variant={command_index}/{len(install_commands)} rc={rc} tail={str(output or '')[-1600:]}")
                if rc == 0:
                    verify_rc, verify_out = self._hidden_run_probe(probe, timeout=20)
                    if verify_rc == 0:
                        self.log(f"{backend}:MODULE post-install import={import_name} status=ok python={self._command_display(python_cmd)} attempt={attempt} variant={command_index}")
                        return True
                    self.warn(f"{backend}:MODULE post-install import failed rc={verify_rc} output={verify_out[-1200:]}")
            self.warn(f"{backend}:MODULE install attempt {attempt}/{retries} exhausted; retrying can reuse pip cache")
        self.warn(f"{backend}:MODULE unavailable after install attempts import={import_name} package={package} python={self._command_display(python_cmd)}")
        return False

    def ensure_module(self, import_name: str, package: str, *, backend: str) -> bool:
        try:
            __import__(import_name)
            self.log(f"{backend}:MODULE import={import_name} status=ok")
            return True
        except Exception as first_error:
            self.warn(f"{backend}:MODULE import={import_name} missing={type(first_error).__name__}: {first_error}")
        if self.dry_run:
            self.log(f"{backend}:MODULE dry-run would install package={package}")
            return True
        if os.environ.get("PROMPT_DISABLE_AUTO_PIP_INSTALL", "").lower() in {"1", "true", "yes", "on"}:
            return False
        retries = int(os.environ.get("PROMPT_PIP_INSTALL_RETRIES", "2") or "2")
        timeout = int(os.environ.get("PROMPT_PIP_INSTALL_TIMEOUT_SECONDS", "7200") or "7200")
        for attempt in range(1, max(1, retries) + 1):
            rc, _ = self.run_cmd([sys.executable, "-m", "pip", "install", package], label=f"install_{backend}_{attempt}", timeout=timeout)
            if rc == 0:
                try:
                    __import__(import_name)
                    return True
                except Exception as err:
                    self.warn(f"{backend}:MODULE post-install import failed: {type(err).__name__}: {err}")
            self.warn(f"{backend}:MODULE install attempt {attempt}/{retries} failed rc={rc}; retrying can reuse pip cache")
        return False

    def data_args_pyinstaller(self) -> list[str]:
        args: list[str] = []
        sep = ";" if _is_windows() else ":"
        for name in DATA_DIRS:
            if name in VOLATILE_DATA_DIRS:
                continue
            path = self.root / name
            if path.exists():
                args.extend(["--add-data", f"{path}{sep}{name}"])
        for name in DATA_FILES:
            path = self.root / name
            if path.exists():
                args.extend(["--add-data", f"{path}{sep}{name}"])
        return args

    def data_args_nuitka(self) -> list[str]:
        args: list[str] = []
        for name in DATA_DIRS:
            if name in VOLATILE_DATA_DIRS:
                continue
            path = self.root / name
            if path.exists():
                args.append(f"--include-data-dir={path}={name}")
        for name in DATA_FILES:
            path = self.root / name
            if path.exists():
                args.append(f"--include-data-files={path}={name}")
        return args

    def copy_exe(self, source: Path, target_name: str, *, backend: str, context: str = "post-backend") -> Artifact | None:
        target = self.dist / target_name
        if self.dry_run:
            self.log(f"EXE:MD5 backend={backend} name={target_name} path={target} md5=DRYRUN bytes=0 human=0B valid=0 status=dry-run context={context}")
            return Artifact(backend, target, note="dry-run")
        if not source.exists():
            self.log(f"EXE:MD5 backend={backend} name={target_name} path={target} md5=missing bytes=0 human=0B valid=0 status=missing context={context}")
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        self.log_exe_md5(backend, target, context=context)
        return Artifact(backend, target)

    def log_exe_md5(self, backend: str, path: Path, *, context: str) -> None:
        if not path.exists() or not path.is_file():
            self.log(f"EXE:MD5 backend={backend} name={path.name} path={path} md5=missing bytes=0 human=0B valid=0 status=missing context={context}")
            return
        size = path.stat().st_size
        valid = bool(size > 1024)
        digest = md5_file(path) if valid else "invalid"
        self.log(f"EXE:MD5 backend={backend} name={path.name} path={path} md5={digest} bytes={size} human={human_size(size)} valid={1 if valid else 0} status={'ok' if valid else 'invalid'} context={context}")

    def log_bundle_as_exe_input(self, backend: str, bundle: Path | None, *, context: str) -> Artifact | None:
        name = bundle.name if bundle else f"Prompt-{backend}-bundle.zip"
        path = bundle if bundle else self.dist / name
        if self.dry_run:
            self.log(f"EXE:MD5 backend={backend} name={name} path={path} md5=DRYRUN bytes=0 human=0B valid=0 status=dry-run-bundle context={context}")
            return Artifact(backend, path, kind="bundle", bundle=path, release_ready=True, note="dry-run-dependent-runtime-bundle")
        if not bundle or not bundle.exists():
            self.log(f"EXE:MD5 backend={backend} name={name} path={path} md5=missing bytes=0 human=0B valid=0 status=missing-bundle context={context}")
            return None
        size = bundle.stat().st_size
        digest = md5_file(bundle)
        self.log(f"EXE:MD5 backend={backend} name={bundle.name} path={bundle} md5={digest} bytes={size} human={human_size(size)} valid=1 status=ok-bundle context={context}")
        return Artifact(backend, bundle, kind="bundle", bundle=bundle, release_ready=True, note="dependent-runtime-bundle")

    def final_artifact_md5_rows(self, results: list[BackendResult], *, context: str = "final-build-exes") -> list[str]:
        rows: list[str] = []
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        rows.append(f"# Prompt final executable artifact MD5 manifest generated={stamp}")
        for result in results:
            artifact = result.artifact
            if artifact and artifact.path and artifact.path.exists():
                path = artifact.path
                size = path.stat().st_size
                digest = md5_file(path)
                line = (
                    f"backend={result.backend} status={result.status} kind={artifact.kind} "
                    f"name={path.name} path={path} bytes={size} human={human_size(size)} md5={digest}"
                )
                rows.append(line)
                self.log(f"EXE:FINAL_MD5 {line} context={context}")
                if artifact.bundle and artifact.bundle != path and artifact.bundle.exists():
                    bpath = artifact.bundle
                    bsize = bpath.stat().st_size
                    bdigest = md5_file(bpath)
                    bline = (
                        f"backend={result.backend} status={result.status} kind=bundle "
                        f"name={bpath.name} path={bpath} bytes={bsize} human={human_size(bsize)} md5={bdigest}"
                    )
                    rows.append(bline)
                    self.log(f"EXE:FINAL_MD5 {bline} context={context}")
            else:
                md5_value = "DRYRUN" if self.dry_run and result.status == "ok" else "missing"
                kind_value = "dry-run" if self.dry_run and result.status == "ok" else "missing"
                line = f"backend={result.backend} status={result.status} kind={kind_value} name=Prompt-{result.backend}.exe path= bytes=0 human=0B md5={md5_value} message={str(result.message or '').replace(chr(10), ' ')[:1000]}"
                rows.append(line)
                self.log(f"EXE:FINAL_MD5 {line} context={context}")
        return rows

    def write_final_md5_manifest(self, results: list[BackendResult]) -> None:
        rows = self.final_artifact_md5_rows(results)
        payload = "\n".join(rows).rstrip() + "\n"
        targets = [self.logs / "final_exe_md5s.log", self.dist / "Prompt_EXE_MD5S.log"]
        for target in targets:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(payload, encoding="utf-8")
                self.log(f"EXE:FINAL_MD5_MANIFEST path={target} rows={len(rows)-1} bytes={target.stat().st_size if target.exists() else 0}")
            except Exception as exc:
                self.warn(f"EXE:FINAL_MD5_MANIFEST:FAILED path={target} {type(exc).__name__}: {exc}")

    def python_runtime_binary_candidates(self) -> list[Path]:
        candidates: list[Path] = []
        roots: list[Path] = []
        for raw in (sys.base_prefix, sys.exec_prefix, Path(sys.executable).parent):
            try:
                path = Path(raw).resolve()
                if path not in roots:
                    roots.append(path)
            except Exception:
                pass
        patterns = ("python*.dll", "vcruntime*.dll", "msvcp*.dll", "concrt*.dll")
        for root in roots:
            for pattern in patterns:
                candidates.extend(root.glob(pattern))
            dlls = root / "DLLs"
            if dlls.exists():
                for pattern in patterns:
                    candidates.extend(dlls.glob(pattern))
        unique: list[Path] = []
        seen: set[str] = set()
        for item in candidates:
            try:
                if item.exists() and item.is_file():
                    key = str(item.resolve()).lower()
                    if key not in seen:
                        seen.add(key)
                        unique.append(item)
            except Exception:
                pass
        return unique

    def log_cx_freeze_runtime_inventory(self, build_dir: Path) -> None:
        patterns = ("python*.dll", "vcruntime*.dll", "msvcp*.dll", "concrt*.dll", "Qt6Core*.dll", "Qt6Gui*.dll", "Qt6Widgets*.dll", "Qt6WebEngine*.dll")
        for pattern in patterns:
            matches = sorted(build_dir.rglob(pattern)) if build_dir.exists() else []
            sample: list[str] = []
            for item in matches[:8]:
                try:
                    sample.append(str(item.relative_to(build_dir)))
                except Exception:
                    sample.append(str(item))
            self.log(f"CX_FREEZE:DLL pattern={pattern} count={len(matches)} sample={sample}")
        exe_count = len(list(build_dir.rglob("*.exe"))) if build_dir.exists() else 0
        dll_count = len(list(build_dir.rglob("*.dll"))) if build_dir.exists() else 0
        self.log(f"CX_FREEZE:RUNTIME-INVENTORY dir={build_dir} exe_count={exe_count} dll_count={dll_count}")

    def zip_bundle(self, source: Path, target_name: str, *, backend: str) -> Path | None:
        if self.dry_run:
            target = self.dist / target_name
            self.log(f"BUNDLE:MD5 backend={backend} name={target.name} path={target} md5=DRYRUN bytes=0 human=0B valid=0 status=dry-run")
            return target
        if not source.exists():
            return None
        target = self.dist / target_name
        if target.exists():
            target.unlink()
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            if source.is_file():
                zf.write(source, source.name)
            else:
                for item in source.rglob("*"):
                    if item.is_file():
                        zf.write(item, item.relative_to(source.parent))
        size = target.stat().st_size
        self.log(f"BUNDLE:MD5 backend={backend} name={target.name} path={target} md5={md5_file(target)} bytes={size} human={human_size(size)} valid=1 status=ok")
        return target

    def clean_generated_before_build(self) -> None:
        self.dist.mkdir(exist_ok=True)
        if self.force and not os.environ.get("PROMPT_BUILD_RESUME"):
            for item in self.dist.glob("*"):
                try:
                    if item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        item.unlink()
                    self.log(f"CLEAN:DIST removed={item}")
                except OSError as exc:
                    self.warn(f"CLEAN:DIST failed path={item} {type(exc).__name__}: {exc}")
        for name in ("generated", "reports", "workspaces"):
            path = self.root / name
            if path.exists() and name != "workflows":
                self.log(f"CLEAN:NOTE generated/runtime folder exists and will not be bundled: {path}")

    def build_pyinstaller(self) -> BackendResult:
        backend = "pyinstaller"
        started = time.monotonic()
        try:
            if not self.ensure_module("PyInstaller", "pyinstaller", backend=backend):
                return BackendResult(backend, "failed", message="PyInstaller unavailable")
            out = self.temp / backend / "dist"
            work = self.temp / backend / "work"
            spec = self.temp / backend / "spec"
            target = self.frozen_entry if self.frozen_entry.exists() else self.app_entry
            cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed", "--name", APP_NAME, "--distpath", str(out), "--workpath", str(work), "--specpath", str(spec)]
            icon = self.root / "icon.ico"
            if icon.exists():
                cmd += ["--icon", str(icon)]
            cmd += self.data_args_pyinstaller()
            cmd += ["--hidden-import", "prompt_app", "--hidden-import", "sqlalchemy", "--hidden-import", "sqlalchemy.orm"]
            for module in ("PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets", "PySide6.QtPrintSupport", "PySide6.QtWebChannel", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets"):
                cmd += ["--hidden-import", module]
            # Do not use --collect-all PySide6 by default. It collects
            # submodules, data, and binaries, which makes PyInstaller try to
            # import PySide6.scripts.deploy_lib; that optional deployment helper
            # imports project_lib and produces the warning seen in debug.log.
            # Prompt needs Qt DLLs/resources plus the explicit Qt modules above.
            cmd += ["--collect-binaries", "PySide6", "--collect-data", "PySide6"]
            for excluded in (
                "PySide6.scripts", "PySide6.scripts.deploy", "PySide6.scripts.deploy_lib",
                "pysqlite2", "MySQLdb", "psycopg2",
            ):
                cmd += ["--exclude-module", excluded]
            if self.pyinstaller_collection_mode in {"all", "collect-all", "full"}:
                self.warn("PyInstaller:COLLECTION mode=collect-all requested; PySide6.scripts.deploy_lib/project_lib warnings may return")
                cmd += ["--collect-all", "PySide6"]
            else:
                self.log("PyInstaller:COLLECTION mode=lean collect-binaries+collect-data explicit-hidden-imports excludes=PySide6.scripts.deploy_lib,pysqlite2,MySQLdb,psycopg2")
            cmd.append(str(target))
            rc, _ = self.run_cmd(cmd, label="PyInstaller_build", timeout=7200)
            if rc != 0:
                return BackendResult(backend, "failed", message=f"PyInstaller rc={rc}", elapsed=time.monotonic()-started)
            exe = out / (APP_NAME + (".exe" if _is_windows() else ""))
            if _is_windows() and exe.exists():
                smoke_rc, smoke_out = self.run_cmd([str(exe), "--frozen-import-smoke"], label="pyinstaller_frozen_import_smoke", timeout=120)
                if smoke_rc != 0:
                    return BackendResult(backend, "failed", message="frozen import smoke failed: " + smoke_out[-1000:], elapsed=time.monotonic()-started)
            artifact = self.copy_exe(exe if exe.exists() else next(out.glob(APP_NAME + "*"), out / APP_NAME), "Prompt-PyInstaller.exe", backend=backend)
            return BackendResult(backend, "ok" if artifact else "failed", artifact=artifact, elapsed=time.monotonic()-started)
        except Exception as exc:
            return BackendResult(backend, "failed", message=f"{type(exc).__name__}: {exc}", elapsed=time.monotonic()-started)

    def build_pyinstaller_dir(self) -> BackendResult:
        backend = "pyinstaller_dir"
        started = time.monotonic()
        try:
            if not self.ensure_module("PyInstaller", "pyinstaller", backend=backend):
                return BackendResult(backend, "failed", message="PyInstaller unavailable for onedir build", elapsed=time.monotonic()-started)
            out = self.temp / backend / "dist"
            work = self.temp / backend / "work"
            spec = self.temp / backend / "spec"
            target = self.frozen_entry if self.frozen_entry.exists() else self.app_entry
            app_dir_name = "Prompt-PyInstallerDir"
            cmd = [
                sys.executable, "-m", "PyInstaller",
                "--noconfirm", "--clean", "--onedir", "--windowed",
                "--name", app_dir_name,
                "--distpath", str(out),
                "--workpath", str(work),
                "--specpath", str(spec),
            ]
            icon = self.root / "icon.ico"
            if icon.exists():
                cmd += ["--icon", str(icon)]
            cmd += self.data_args_pyinstaller()
            cmd += ["--hidden-import", "prompt_app", "--hidden-import", "sqlalchemy", "--hidden-import", "sqlalchemy.orm"]
            for module in ("PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets", "PySide6.QtPrintSupport", "PySide6.QtWebChannel", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets"):
                cmd += ["--hidden-import", module]
            cmd += ["--collect-binaries", "PySide6", "--collect-data", "PySide6"]
            for excluded in (
                "PySide6.scripts", "PySide6.scripts.deploy", "PySide6.scripts.deploy_lib",
                "pysqlite2", "MySQLdb", "psycopg2",
            ):
                cmd += ["--exclude-module", excluded]
            if self.pyinstaller_collection_mode in {"all", "collect-all", "full"}:
                self.warn("PyInstallerDir:COLLECTION mode=collect-all requested; PySide6.scripts.deploy_lib/project_lib warnings may return")
                cmd += ["--collect-all", "PySide6"]
            else:
                self.log("PyInstallerDir:COLLECTION mode=lean collect-binaries+collect-data explicit-hidden-imports excludes=PySide6.scripts.deploy_lib,pysqlite2,MySQLdb,psycopg2")
            cmd.append(str(target))
            rc, out_text = self.run_cmd(cmd, label="PyInstallerDir_build", timeout=7200)
            if rc != 0:
                return BackendResult(backend, "failed", message=f"PyInstaller onedir rc={rc}: {out_text[-1200:]}", elapsed=time.monotonic()-started)
            if self.dry_run:
                bundle = self.zip_bundle(self.temp / backend / "dry_run_onedir", "Prompt-PyInstallerDir-bundle.zip", backend=backend)
                artifact = self.log_bundle_as_exe_input(backend, bundle, context="post-backend-dependent-bundle")
                return BackendResult(backend, "ok" if artifact else "failed", artifact=artifact, elapsed=time.monotonic()-started)
            app_dir = out / app_dir_name
            if not app_dir.exists():
                # PyInstaller may normalize the output folder in unusual host/path cases.
                candidates = [item for item in out.iterdir() if item.is_dir()] if out.exists() else []
                app_dir = candidates[0] if candidates else app_dir
            candidates = list(app_dir.rglob(f"{app_dir_name}.exe")) or list(app_dir.rglob("Prompt*.exe")) or list(app_dir.rglob("*.exe"))
            if _is_windows() and candidates:
                smoke_rc, smoke_out = self.run_cmd([str(candidates[0]), "--frozen-import-smoke"], label="pyinstaller_dir_frozen_import_smoke", cwd=app_dir, timeout=120)
                if smoke_rc != 0:
                    return BackendResult(backend, "failed", message="onedir frozen import smoke failed: " + smoke_out[-1000:], elapsed=time.monotonic()-started)
            if not app_dir.exists() or not candidates:
                return BackendResult(backend, "failed", message="PyInstaller onedir produced no executable folder", elapsed=time.monotonic()-started)
            bundle = self.zip_bundle(app_dir, "Prompt-PyInstallerDir-bundle.zip", backend=backend)
            artifact = self.log_bundle_as_exe_input(backend, bundle, context="post-backend-dependent-bundle")
            if artifact:
                artifact.note = "PyInstaller --onedir runtime bundle; installer should use the bundle/payload, not a copied loose exe"
            return BackendResult(backend, "ok" if artifact else "failed", artifact=artifact, elapsed=time.monotonic()-started)
        except Exception as exc:
            self.fault("PyInstallerDir_backend", f"{type(exc).__name__}: {exc}", exc=exc)
            return BackendResult(backend, "failed", message=f"{type(exc).__name__}: {exc}", elapsed=time.monotonic()-started)

    def build_nuitka(self) -> BackendResult:
        backend = "nuitka"
        started = time.monotonic()
        try:
            python_cmd = self.backend_python_command(backend, prefer_build_safe=True)
            if not self.ensure_module_for_python("nuitka", "nuitka", backend=backend, python_cmd=python_cmd):
                return BackendResult(backend, "failed", message="Nuitka unavailable for selected backend Python", elapsed=time.monotonic()-started)
            out = self.temp / backend / "dist"
            target = self.frozen_entry if self.frozen_entry.exists() else self.app_entry
            cmd = [*python_cmd, "-m", "nuitka", "--standalone", "--onefile", "--assume-yes-for-downloads", "--enable-plugin=pyside6", "--include-module=prompt_app", "--include-package=sqlalchemy", f"--output-dir={out}", f"--output-filename={APP_NAME}"]
            icon = self.root / "icon.ico"
            if icon.exists():
                cmd.append(f"--windows-icon-from-ico={icon}")
            if _is_windows():
                cmd += ["--windows-company-name=AcquisitionInvest LLC", "--windows-product-name=Prompt", "--windows-file-version=1.0.0.0", "--windows-product-version=1.0.0.0", "--windows-file-description=Prompt 1.0 - Desktop Prompt Workbench"]
            cmd += self.data_args_nuitka()
            cmd.append(str(target))
            rc, out_text = self.run_cmd(cmd, label="Nuitka_build", timeout=14400)
            if rc != 0:
                return BackendResult(backend, "failed", message=f"Nuitka rc={rc}: {out_text[-1200:]}", elapsed=time.monotonic()-started)
            candidates = sorted(list(out.rglob("Prompt*.exe")) or list(out.rglob("Prompt*")), key=lambda p: p.stat().st_size if p.exists() else 0, reverse=True)
            artifact = self.copy_exe(candidates[0] if candidates else out / "Prompt.exe", "Prompt-Nuitka.exe", backend=backend)
            return BackendResult(backend, "ok" if artifact else "failed", artifact=artifact, elapsed=time.monotonic()-started)
        except Exception as exc:
            self.fault("Nuitka_backend", f"{type(exc).__name__}: {exc}", exc=exc)
            return BackendResult(backend, "failed", message=f"{type(exc).__name__}: {exc}", elapsed=time.monotonic()-started)

    def build_cx_freeze(self) -> BackendResult:
        backend = "cx_freeze"
        started = time.monotonic()
        try:
            if not self.ensure_module("cx_Freeze", "cx_Freeze", backend=backend):
                return BackendResult(backend, "failed", message="cx_Freeze unavailable")
            work = self.temp / backend
            build_exe = work / "build_exe"
            setup_py = work / "setup_cx_freeze.py"
            setup_py.parent.mkdir(parents=True, exist_ok=True)
            include_files = [str((self.root / name).resolve()) for name in DATA_DIRS + DATA_FILES if (self.root / name).exists() and name not in VOLATILE_DATA_DIRS]
            runtime_bins = [str(path) for path in self.python_runtime_binary_candidates()]
            setup_py.write_text(textwrap.dedent(f'''
                from cx_Freeze import Executable, setup
                build_exe_options = {{
                    "packages": ["sqlalchemy", "PySide6", "shiboken6", "encodings"],
                    "include_files": {include_files!r} + {runtime_bins!r},
                    "bin_includes": ["python3.dll", "python{sys.version_info.major}{sys.version_info.minor}.dll", "vcruntime140.dll", "vcruntime140_1.dll"],
                    "include_msvcr": True,
                    "excludes": [],
                }}
                setup(name="Prompt", version="{VERSION}", description="Prompt", options={{"build_exe": build_exe_options}}, executables=[Executable({str(self.frozen_entry if self.frozen_entry.exists() else self.app_entry)!r}, base="gui", target_name="Prompt-cx_Freeze.exe")])
            '''), encoding="utf-8")
            rc, _ = self.run_cmd([sys.executable, str(setup_py), "build_exe", "--build-exe", str(build_exe)], label="cx_Freeze_build", cwd=work, timeout=7200)
            if rc != 0:
                return BackendResult(backend, "failed", message=f"cx_Freeze rc={rc}", elapsed=time.monotonic()-started)
            if self.dry_run:
                bundle = self.zip_bundle(build_exe, "Prompt-cx_Freeze-bundle.zip", backend=backend)
                artifact = self.log_bundle_as_exe_input(backend, bundle, context="post-backend-dependent-bundle")
                return BackendResult(backend, "ok" if artifact else "failed", artifact=artifact, elapsed=time.monotonic()-started)
            self.log_cx_freeze_runtime_inventory(build_exe)
            candidates = list(build_exe.rglob("Prompt-cx_Freeze.exe")) or list(build_exe.rglob("*.exe"))
            if not candidates:
                return BackendResult(backend, "failed", message="cx_Freeze produced no exe in build folder", elapsed=time.monotonic()-started)
            stale_single = self.dist / "Prompt-cx_Freeze.exe"
            if stale_single.exists():
                self.warn(f"cx_freeze:REMOVING-LOOSE-STUB path={stale_single} reason=cx_Freeze needs adjacent runtime DLLs from bundle")
                _safe_unlink(stale_single)
            bundle = self.zip_bundle(build_exe, "Prompt-cx_Freeze-bundle.zip", backend=backend)
            artifact = self.log_bundle_as_exe_input(backend, bundle, context="post-backend-dependent-bundle")
            if artifact:
                artifact.note = "cx_Freeze runtime bundle; run/install the bundle, not a copied loose stub exe"
            return BackendResult(backend, "ok" if artifact else "failed", artifact=artifact, elapsed=time.monotonic()-started)
        except Exception as exc:
            return BackendResult(backend, "failed", message=f"{type(exc).__name__}: {exc}", elapsed=time.monotonic()-started)

    def build_py2exe(self) -> BackendResult:
        backend = "py2exe"
        started = time.monotonic()
        if not _is_windows():
            self.warn("py2exe:SKIP non-Windows host")
            return BackendResult(backend, "skipped", message="non-Windows host", elapsed=0)
        try:
            python_cmd = self.backend_python_command(backend, prefer_build_safe=True)
            version = self.probe_python_version(python_cmd, label="py2exe-selected")
            if version is not None and version >= (3, 14, 0):
                self.warn("py2exe:PYTHON-314 selected because no build-safe Python was found; py2exe may fail to install/build until Python 3.14 wheels exist")
            if not self.ensure_module_for_python("py2exe", "py2exe", backend=backend, python_cmd=python_cmd):
                return BackendResult(backend, "failed", message="py2exe unavailable for selected backend Python", elapsed=time.monotonic()-started)
            work = self.temp / backend
            dist_dir = work / "dist"
            setup_py = work / "setup_py2exe.py"
            setup_py.parent.mkdir(parents=True, exist_ok=True)
            setup_text = (
                "from setuptools import setup\n"
                "import py2exe\n"
                "setup(\n"
                f"    windows=[{{'script': {str(self.frozen_entry if self.frozen_entry.exists() else self.app_entry)!r}, 'dest_base': 'Prompt-py2exe'}}],\n"
                "    options={'py2exe': {\n"
                "        'bundle_files': 3,\n"
                "        'compressed': True,\n"
                "        'includes': ['prompt_app', 'sqlalchemy', 'PySide6.QtCore', 'PySide6.QtGui', 'PySide6.QtWidgets', 'PySide6.QtWebEngineWidgets', 'PySide6.QtWebEngineCore', 'PySide6.QtWebChannel'],\n"
                "        'excludes': ['pysqlite2', 'MySQLdb', 'psycopg2'],\n"
                "    }},\n"
                ")\n"
            )
            setup_py.write_text(setup_text, encoding="utf-8")
            rc, out_text = self.run_cmd([*python_cmd, str(setup_py), "py2exe", "--dist-dir", str(dist_dir)], label="py2exe_build", cwd=work, timeout=7200)
            if rc != 0:
                return BackendResult(backend, "failed", message=f"py2exe rc={rc}: {out_text[-1200:]}", elapsed=time.monotonic()-started)
            candidates = sorted(list(dist_dir.rglob("Prompt-py2exe.exe")) or list(dist_dir.rglob("*.exe")), key=lambda p: p.stat().st_size if p.exists() else 0, reverse=True)
            artifact = self.copy_exe(candidates[0] if candidates else dist_dir / "Prompt-py2exe.exe", "Prompt-py2exe.exe", backend=backend)
            bundle = self.zip_bundle(dist_dir, "Prompt-py2exe-bundle.zip", backend=backend) if artifact else None
            if artifact:
                artifact.bundle = bundle
                artifact.note = "py2exe dist folder bundle is copied too; the loose EXE may require adjacent DLLs depending on py2exe output mode"
            return BackendResult(backend, "ok" if artifact else "failed", artifact=artifact, elapsed=time.monotonic()-started)
        except Exception as exc:
            self.fault("py2exe_backend", f"{type(exc).__name__}: {exc}", exc=exc)
            return BackendResult(backend, "failed", message=f"{type(exc).__name__}: {exc}", elapsed=time.monotonic()-started)

    def cargo_candidates(self) -> list[Path]:
        candidates: list[Path] = []
        for exe in ("cargo.exe", "cargo"):
            found = shutil.which(exe)
            if found:
                candidates.append(Path(found))
        for base in (os.environ.get("CARGO_HOME"), os.environ.get("USERPROFILE"), str(Path.home())):
            if not base:
                continue
            b = Path(base)
            if b.name.lower() != ".cargo":
                b = b / ".cargo"
            for exe in ("cargo.exe", "cargo"):
                candidates.append(b / "bin" / exe)
        unique: list[Path] = []
        seen = set()
        for p in candidates:
            s = str(p).lower()
            if s not in seen:
                seen.add(s); unique.append(p)
        return unique

    def cargo_path(self) -> Path | None:
        for candidate in self.cargo_candidates():
            if candidate.exists():
                return candidate
        return None

    def log_pyapp_toolchain(self) -> None:
        self.log("PYAPP:TOOLCHAIN:BEGIN")
        for key in ("PATH", "USERPROFILE", "CARGO_HOME", "RUSTUP_HOME"):
            self.log(f"PYAPP:ENV {key}={os.environ.get(key, '')}")
        for tool in ("cargo", "rustc", "rustup", "winget", "cl", "link"):
            self.log(f"PYAPP:PROBE tool={tool} path={shutil.which(tool) or 'MISSING'}")
        for candidate in self.cargo_candidates():
            self.log(f"PYAPP:CARGO-CANDIDATE path={candidate} exists={candidate.exists()}")
        self.log("PYAPP:RUSTUP-INIT-URL https://win.rustup.rs/x86_64")

    def ensure_cargo(self) -> Path | None:
        self.log_pyapp_toolchain()
        cargo = self.cargo_path()
        if cargo:
            return cargo
        if self.dry_run:
            self.log("PYAPP:CARGO dry-run would install Rust/Cargo")
            return Path("cargo")
        if os.environ.get("PROMPT_AUTO_INSTALL_RUST", "1").lower() not in {"1", "true", "yes", "on"}:
            return None
        if _is_windows():
            winget = shutil.which("winget")
            if winget:
                self.run_cmd([winget, "install", "--id", "Rustlang.Rustup", "-e", "--source", "winget", "--accept-package-agreements", "--accept-source-agreements"], label="PyApp_rustup_winget", timeout=3600)
                cargo = self.cargo_path()
                if cargo:
                    return cargo
            rustup = shutil.which("rustup") or shutil.which("rustup.exe")
            if rustup:
                self.run_cmd([rustup, "default", "stable"], label="PyApp_rustup_default", timeout=1800)
                cargo = self.cargo_path()
                if cargo:
                    return cargo
            self.warn("PYAPP:CARGO missing after bootstrap attempts; install Rustup/Cargo manually or rerun after winget finishes.")
        return self.cargo_path()

    def build_pyapp(self) -> BackendResult:
        backend = "pyapp"
        started = time.monotonic()
        try:
            cargo = self.ensure_cargo()
            if not cargo:
                return BackendResult(backend, "failed", message="cargo missing")
            install_root = self.temp / "pyapp_install"
            requirements = self.root / "packaging" / "prompt_requirements.req"
            if not requirements.exists():
                requirements.parent.mkdir(parents=True, exist_ok=True)
                requirements.write_text("PySide6\nSQLAlchemy\n", encoding="utf-8")
            entry = self.root / "prompt_pyapp_entry.py"
            if not entry.exists():
                entry.write_text("import frozen_prompt_entry\nraise SystemExit(frozen_prompt_entry.main())\n", encoding="utf-8")
            env = {
                "PYAPP_PROJECT_NAME": "Prompt",
                "PYAPP_PROJECT_VERSION": VERSION,
                "PYAPP_PROJECT_DEPENDENCY_FILE": str(requirements),
                "PYAPP_EXEC_SCRIPT": str(entry),
                "PYAPP_PYTHON_VERSION": os.environ.get("PROMPT_PYAPP_PYTHON_VERSION", "3.13"),
                "PYAPP_IS_GUI": "1",
                "PYAPP_DISTRIBUTION_EMBED": "1",
                "PYAPP_FULL_ISOLATION": "1",
                "PYAPP_PIP_EXTRA_ARGS": "--prefer-binary",
            }
            self.log(f"PYAPP:BUILD:CWD {self.root}")
            self.log(f"PYAPP:BUILD:INSTALL_ROOT {install_root}")
            for k, v in env.items():
                self.log(f"PYAPP:ENV {k}={v}")
            cmd = [str(cargo), "install", "pyapp", "--force", "--root", str(install_root)]
            self.log("PYAPP:BUILD:COMMAND " + quote_cmd(cmd))
            rc, _ = self.run_cmd(cmd, label="PyApp_build", timeout=7200, env=env)
            if rc != 0:
                return BackendResult(backend, "failed", message=f"PyApp cargo rc={rc}", elapsed=time.monotonic()-started)
            exe = install_root / "bin" / ("pyapp.exe" if _is_windows() else "pyapp")
            artifact = self.copy_exe(exe, "Prompt-PyApp.exe", backend=backend)
            return BackendResult(backend, "ok" if artifact else "failed", artifact=artifact, elapsed=time.monotonic()-started)
        except Exception as exc:
            return BackendResult(backend, "failed", message=f"{type(exc).__name__}: {exc}", elapsed=time.monotonic()-started)

    def build_pyoxidizer(self) -> BackendResult:
        backend = "pyoxidizer"
        started = time.monotonic()
        try:
            command = shutil.which("pyoxidizer") or shutil.which("pyoxidizer.exe")
            if not command:
                cargo = self.cargo_path()
                if cargo:
                    self.run_cmd([str(cargo), "install", "pyoxidizer", "--force", "--root", str(self.temp / "pyoxidizer_install")], label="PyOxidizer_cargo_install", timeout=7200)
                    command = str((self.temp / "pyoxidizer_install" / "bin" / ("pyoxidizer.exe" if _is_windows() else "pyoxidizer")))
            if not command or (not self.dry_run and not Path(command).exists()):
                return BackendResult(backend, "failed", message="pyoxidizer command missing")
            config = self.temp / "pyoxidizer.bzl"
            config.write_text("# Placeholder PyOxidizer config generated for diagnostics.\n", encoding="utf-8")
            self.warn("PyOxidizer backend is diagnostic/optional in this CWV; real PyOxidizer config still needs final Windows tuning.")
            if self.dry_run:
                self.log("PyOxidizer_build:DRY-RUN command=" + str(command))
                return BackendResult(backend, "skipped", message="dry-run diagnostic backend", elapsed=time.monotonic()-started)
            return BackendResult(backend, "failed", message="PyOxidizer config not finalized", elapsed=time.monotonic()-started)
        except Exception as exc:
            return BackendResult(backend, "failed", message=f"{type(exc).__name__}: {exc}", elapsed=time.monotonic()-started)

    def build_briefcase(self) -> BackendResult:
        backend = "briefcase"
        started = time.monotonic()
        try:
            if not self.ensure_module("briefcase", "briefcase", backend=backend):
                return BackendResult(backend, "failed", message="Briefcase unavailable")
            self.warn("Briefcase backend is optional and usually produces a native app bundle/MSI rather than a single standalone EXE.")
            if self.dry_run:
                for cmd in ([sys.executable, "-m", "briefcase", "create", "windows", "app", "-v"], [sys.executable, "-m", "briefcase", "build", "windows", "app", "-u", "-r", "--update-resources", "-v"]):
                    self.log("Briefcase_build:DRY-RUN " + quote_cmd(cmd))
                return BackendResult(backend, "skipped", message="dry-run optional native bundle", elapsed=time.monotonic()-started)
            # Without a BeeWare pyproject template, fail softly with evidence.
            return BackendResult(backend, "failed", message="Briefcase pyproject app template not present", elapsed=time.monotonic()-started)
        except Exception as exc:
            return BackendResult(backend, "failed", message=f"{type(exc).__name__}: {exc}", elapsed=time.monotonic()-started)

    def build_backend(self, backend: str) -> BackendResult:
        func = {
            "pyinstaller": self.build_pyinstaller,
            "nuitka": self.build_nuitka,
            "cx_freeze": self.build_cx_freeze,
            "py2exe": self.build_py2exe,
            "pyoxidizer": self.build_pyoxidizer,
            "pyapp": self.build_pyapp,
            "pyinstaller_dir": self.build_pyinstaller_dir,
            "briefcase": self.build_briefcase,
        }.get(backend)
        if func is None:
            return BackendResult(backend, "skipped", message="unknown backend")
        self.log(f"BACKEND:BEGIN backend={backend}")
        self.dist_snapshot(f"before-backend:{backend}", limit=30)
        self.temp_artifact_snapshot(f"before-backend:{backend}", limit=30)
        result = func()
        self.dist_snapshot(f"after-backend:{backend}:status={result.status}", limit=40)
        self.temp_artifact_snapshot(f"after-backend:{backend}:status={result.status}", limit=40)
        if result.ok or (self.dry_run and result.status == 'ok'):
            artifact_path = result.artifact.path if result.artifact else self.dist / ('Prompt-' + backend + '.exe')
            self.log(f"BACKEND:SUCCESS backend={backend} path={artifact_path} elapsed={result.elapsed:.1f}s")
        else:
            self.warn(f"BACKEND:{result.status.upper()} backend={backend} message={result.message} elapsed={result.elapsed:.1f}s")
            # Every backend still gets a grep-friendly evidence line.
            self.log(f"EXE:MD5 backend={backend} name=Prompt-{backend}.exe path={self.dist / ('Prompt-' + backend + '.exe')} md5=missing bytes=0 human=0B valid=0 status={result.status} context=post-backend")
        return result

    def build_exes(self, backends: list[str]) -> int:
        self.clean_generated_before_build()
        self.log_backend_plan(backends)
        self.dist_snapshot("start-build-exes", limit=40)
        self.temp_artifact_snapshot("start-build-exes", limit=40)
        results = [self.build_backend(backend) for backend in backends]
        ok = [r for r in results if r.ok or (self.dry_run and r.status == "ok")]
        summary = {r.backend: {"status": r.status, "message": r.message, "artifact": str(r.artifact.path) if r.artifact else ""} for r in results}
        self.dist_snapshot("final-build-exes", limit=80)
        self.temp_artifact_snapshot("final-build-exes", limit=80)
        self.log("EXE:SUMMARY " + json.dumps(summary, ensure_ascii=False, sort_keys=True))
        for r in results:
            status = r.status
            artifact_path = str(r.artifact.path) if r.artifact else ""
            bundle_path = str(r.artifact.bundle) if (r.artifact and r.artifact.bundle) else ""
            self.log(f"EXE:RESULT backend={r.backend} status={status} artifact={artifact_path} bundle={bundle_path} message={str(r.message or '').replace(chr(10), ' ')[:1200]} elapsed={r.elapsed:.1f}")
        self.write_final_md5_manifest(results)
        missing = [r.backend for r in results if not (r.ok or (self.dry_run and r.status == "ok"))]
        if missing:
            self.warn(f"EXE:MISSING_BACKENDS count={len(missing)} names={','.join(missing)}")
        self.log(f"EXE:EXPECTED_BACKENDS count={len(backends)} names={','.join(backends)}")
        if self.dry_run:
            self.log("EXE:DRY-RUN complete")
            return 0
        if self.require_strict_exe and len(ok) != len(backends):
            self.warn("EXE:STRICT failed because not all requested backends succeeded")
            return 2
        if not ok:
            self.warn("EXE:FAILED no release-ready EXE backend succeeded")
            return 2
        self.log(f"EXE:SUCCESS count={len(ok)} strict={self.require_strict_exe}")
        return 0

    def _backend_from_exe_name(self, path: Path) -> str:
        stem = path.stem
        if stem.lower().startswith("prompt-"):
            stem = stem[len("Prompt-"):]
        return normalize_backend(stem)

    def _backend_from_bundle_name(self, path: Path) -> str:
        stem = path.stem
        if stem.lower().startswith("prompt-"):
            stem = stem[len("Prompt-"):]
        if stem.lower().endswith("-bundle"):
            stem = stem[:-len("-bundle")]
        return normalize_backend(stem)

    def discover_installer_inputs(self) -> list[InstallerInput]:
        # Discover release EXEs and prepare installer payload folders. NSIS/Inno/WiX
        # install the payload, not blindly a single file. For dependent EXE backends,
        # use companion bundle zips when present so DLLs/resources are installed too.
        inputs: list[InstallerInput] = []
        payload_root = self.temp / "installer_payloads"
        payload_root.mkdir(parents=True, exist_ok=True)
        exe_paths = sorted(self.dist.glob("Prompt-*.exe"))
        seen: set[str] = set()
        for exe in exe_paths:
            lower = exe.name.lower()
            if lower.startswith("promptsetup-") or lower.startswith("prompt-setup-"):
                continue
            backend = self._backend_from_exe_name(exe)
            if not backend or backend in seen:
                continue
            seen.add(backend)
            bundle = self.dist / f"Prompt-{backend}-bundle.zip"
            if not bundle.exists():
                camel = {
                    "cx_freeze": "Prompt-cx_Freeze-bundle.zip",
                    "py2exe": "Prompt-py2exe-bundle.zip",
                    "briefcase": "Prompt-Briefcase-bundle.zip",
                    "pyapp": "Prompt-PyApp-bundle.zip",
                    "pyinstaller_dir": "Prompt-PyInstallerDir-bundle.zip",
                }.get(backend, "")
                if camel:
                    alt = self.dist / camel
                    if alt.exists():
                        bundle = alt
            payload = payload_root / backend
            if payload.exists():
                shutil.rmtree(payload, ignore_errors=True)
            payload.mkdir(parents=True, exist_ok=True)
            exe_rel = "Prompt.exe"
            note = "standalone-exe"
            if bundle.exists():
                note = "bundle-expanded"
                try:
                    with zipfile.ZipFile(bundle, "r") as zf:
                        zf.extractall(payload)
                except Exception as exc:
                    self.warn(f"INSTALLER:PAYLOAD backend={backend} bundle_extract_failed={type(exc).__name__}: {exc}; falling back to exe-only")
                    shutil.copy2(exe, payload / exe_rel)
                    inputs.append(InstallerInput(backend, exe, payload, exe_rel, bundle_zip=None, note="bundle-extract-failed-exe-only"))
                    continue
                found = list(payload.rglob(exe.name)) or list(payload.rglob("Prompt*.exe")) or list(payload.rglob("*.exe"))
                if found:
                    try:
                        exe_rel = str(found[0].relative_to(payload)).replace("\\", "/")
                    except Exception:
                        exe_rel = found[0].name
                else:
                    shutil.copy2(exe, payload / exe.name)
                    exe_rel = exe.name
            else:
                shutil.copy2(exe, payload / exe_rel)
            inputs.append(InstallerInput(backend, exe, payload, exe_rel, bundle_zip=bundle if bundle.exists() else None, note=note))
            self.log(f"INSTALLER:INPUT exe_backend={backend} source={exe} payload={payload} exe_rel={exe_rel} bundle={bundle if bundle.exists() else ''} note={note}")
        for bundle in sorted(self.dist.glob("Prompt-*-bundle.zip")):
            backend = self._backend_from_bundle_name(bundle)
            if not backend or backend in seen:
                continue
            seen.add(backend)
            payload = payload_root / backend
            if payload.exists():
                shutil.rmtree(payload, ignore_errors=True)
            payload.mkdir(parents=True, exist_ok=True)
            try:
                with zipfile.ZipFile(bundle, "r") as zf:
                    zf.extractall(payload)
            except Exception as exc:
                self.warn(f"INSTALLER:PAYLOAD backend={backend} bundle_only_extract_failed={type(exc).__name__}: {exc}")
                continue
            found = list(payload.rglob("Prompt*.exe")) or list(payload.rglob("*.exe"))
            if not found:
                self.warn(f"INSTALLER:PAYLOAD backend={backend} bundle_only_no_exe bundle={bundle}")
                continue
            try:
                exe_rel = str(found[0].relative_to(payload)).replace("\\", "/")
            except Exception:
                exe_rel = found[0].name
            inputs.append(InstallerInput(backend, found[0], payload, exe_rel, bundle_zip=bundle, note="bundle-only-expanded"))
            self.log(f"INSTALLER:INPUT exe_backend={backend} source={found[0]} payload={payload} exe_rel={exe_rel} bundle={bundle} note=bundle-only-expanded")
        if not inputs and self.dry_run:
            for backend in DEFAULT_EXE_BACKENDS:
                payload = payload_root / backend
                payload.mkdir(parents=True, exist_ok=True)
                exe = self.dist / f"Prompt-{backend}.exe"
                inputs.append(InstallerInput(backend, exe, payload, "Prompt.exe", note="dry-run-virtual-input"))
                self.log(f"INSTALLER:INPUT exe_backend={backend} source={exe} payload={payload} exe_rel=Prompt.exe note=dry-run-virtual-input")
        return inputs

    def _tool_candidates(self, tool: str) -> list[Path]:
        candidates: list[Path] = []
        tool_value = str(tool or "").strip()
        aliases = [tool_value]
        lower = tool_value.lower()
        if lower in {"makensis", "makensis.exe"}:
            aliases.extend(["makensis", "makensis.exe"])
        elif lower in {"iscc", "iscc.exe"}:
            aliases.extend(["ISCC", "ISCC.exe", "iscc"])
        elif lower in {"wix", "wix.exe"}:
            aliases.extend(["wix", "wix.exe"])
        elif lower in {"makeappx", "makeappx.exe"}:
            aliases.extend(["makeappx", "makeappx.exe"])
        elif lower in {"advancedinstaller.com", "advancedinstaller", "advinst", "advinst.exe"}:
            aliases.extend(["AdvancedInstaller.com", "advancedinstaller.com", "advinst.exe"])
        for alias in aliases:
            found = shutil.which(alias)
            if found:
                candidates.append(Path(found))
        if _is_windows():
            program_files = [
                os.environ.get("ProgramFiles", ""),
                os.environ.get("ProgramFiles(x86)", ""),
                os.environ.get("LOCALAPPDATA", ""),
                os.environ.get("ProgramW6432", ""),
            ]
            userprofile = os.environ.get("USERPROFILE", "")
            if userprofile:
                candidates.append(Path(userprofile) / ".dotnet" / "tools" / ("wix.exe" if lower.startswith("wix") else tool_value))
            if lower in {"makensis", "makensis.exe"}:
                for base in program_files:
                    if base:
                        candidates.append(Path(base) / "NSIS" / "makensis.exe")
            elif lower in {"iscc", "iscc.exe"}:
                for base in program_files:
                    if base:
                        candidates.append(Path(base) / "Inno Setup 6" / "ISCC.exe")
                        candidates.append(Path(base) / "Inno Setup 5" / "ISCC.exe")
            elif lower in {"wix", "wix.exe"}:
                if userprofile:
                    candidates.append(Path(userprofile) / ".dotnet" / "tools" / "wix.exe")
                dotnet_tools = os.environ.get("DOTNET_TOOLS", "")
                if dotnet_tools:
                    candidates.append(Path(dotnet_tools) / "wix.exe")
            elif lower in {"makeappx", "makeappx.exe"}:
                for base in program_files:
                    if base:
                        kits = Path(base) / "Windows Kits" / "10" / "bin"
                        if kits.exists():
                            for path in sorted(kits.glob("*/*/makeappx.exe"), reverse=True):
                                candidates.append(path)
                            for path in sorted(kits.glob("*/makeappx.exe"), reverse=True):
                                candidates.append(path)
                        certkit = Path(base) / "Windows Kits" / "10" / "App Certification Kit" / "makeappx.exe"
                        candidates.append(certkit)
            elif lower in {"advancedinstaller.com", "advancedinstaller", "advinst", "advinst.exe"}:
                env_tool = os.environ.get("ADVINST_COM", "") or os.environ.get("ADVANCEDINSTALLER_COM", "")
                if env_tool:
                    candidates.append(Path(env_tool))
                for base in program_files:
                    if base:
                        caphyon = Path(base) / "Caphyon"
                        if caphyon.exists():
                            for path in sorted(caphyon.glob("Advanced Installer */bin/*/AdvancedInstaller.com"), reverse=True):
                                candidates.append(path)
                            for path in sorted(caphyon.glob("Advanced Installer */bin/*/advinst.exe"), reverse=True):
                                candidates.append(path)
                            for path in sorted(caphyon.glob("Advanced Installer */bin/AdvancedInstaller.com"), reverse=True):
                                candidates.append(path)
                            for path in sorted(caphyon.glob("Advanced Installer */bin/advinst.exe"), reverse=True):
                                candidates.append(path)
                        candidates.append(Path(base) / "Caphyon" / "Advanced Installer" / "bin" / "x86" / "AdvancedInstaller.com")
                        candidates.append(Path(base) / "Caphyon" / "Advanced Installer" / "bin" / "x86" / "advinst.exe")
                        candidates.append(Path(base) / "Caphyon" / "Advanced Installer" / "bin" / "AdvancedInstaller.com")
        unique: list[Path] = []
        seen: set[str] = set()
        for item in candidates:
            if not str(item or "").strip():
                continue
            key = str(item).lower()
            if key not in seen:
                seen.add(key)
                unique.append(item)
        return unique

    def _find_tool(self, tool: str) -> Path | None:
        for candidate in self._tool_candidates(tool):
            try:
                if candidate.exists():
                    self.log(f"INSTALLER:TOOL tool={tool} path={candidate} exists=1")
                    return candidate
            except OSError:
                pass
        found = shutil.which(str(tool or ""))
        if found:
            path = Path(found)
            self.log(f"INSTALLER:TOOL tool={tool} path={path} exists=1 source=PATH")
            return path
        self.warn(f"INSTALLER:TOOL tool={tool} missing candidates={[str(x) for x in self._tool_candidates(tool)]}")
        return None

    def _load_tool_state(self) -> dict[str, object]:
        try:
            if self.tool_state_path.exists():
                return json.loads(self.tool_state_path.read_text(encoding="utf-8", errors="replace") or "{}")
        except Exception as exc:
            self.warn(f"INSTALLER:STATE read failed path={self.tool_state_path} {type(exc).__name__}: {exc}")
        return {}

    def _save_tool_state(self, state: dict[str, object]) -> None:
        try:
            self.tool_state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.tool_state_path.with_suffix(self.tool_state_path.suffix + ".tmp")
            tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self.tool_state_path)
        except Exception as exc:
            self.warn(f"INSTALLER:STATE write failed path={self.tool_state_path} {type(exc).__name__}: {exc}")

    def _installer_tool_recent_attempt(self, maker: str, state: dict[str, object]) -> tuple[bool, str]:
        try:
            row = dict(state.get(maker, {}) or {})
            last = str(row.get("last_attempt_utc", "") or "")
            if not last:
                return False, "no-prior-attempt"
            last_dt = datetime.datetime.fromisoformat(last.replace("Z", "+00:00"))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=datetime.timezone.utc)
            age_hours = (datetime.datetime.now(datetime.timezone.utc) - last_dt).total_seconds() / 3600.0
            if age_hours < max(0.0, self.installer_tool_cooldown_hours):
                return True, f"recent-attempt age_hours={age_hours:.2f} cooldown_hours={self.installer_tool_cooldown_hours:.2f} status={row.get('status','')} rc={row.get('rc','')}"
        except Exception as exc:
            self.warn(f"INSTALLER:STATE parse failed maker={maker} {type(exc).__name__}: {exc}")
        return False, "attempt-old-or-missing"

    def _record_installer_tool_attempt(self, maker: str, *, status: str, rc: int | None = None, path: Path | None = None, command: list[str] | None = None, note: str = "") -> None:
        state = self._load_tool_state()
        row = dict(state.get(maker, {}) or {})
        row.update({
            "maker": maker,
            "status": status,
            "rc": rc,
            "path": str(path or ""),
            "command": quote_cmd(command or []),
            "note": note,
            "last_attempt_utc": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        })
        state[maker] = row
        self._save_tool_state(state)

    def _installer_tool_commands(self, spec: InstallerToolSpec) -> list[tuple[str, list[str]]]:
        winget = shutil.which("winget") or shutil.which("winget.exe")
        dotnet = shutil.which("dotnet") or shutil.which("dotnet.exe")
        if not dotnet and _is_windows():
            for candidate in (Path(os.environ.get("ProgramFiles", "")) / "dotnet" / "dotnet.exe", Path(os.environ.get("ProgramW6432", "")) / "dotnet" / "dotnet.exe"):
                if str(candidate) and candidate.exists():
                    dotnet = str(candidate)
                    break
        powershell = shutil.which("powershell") or shutil.which("powershell.exe") or shutil.which("pwsh") or shutil.which("pwsh.exe")
        commands: list[tuple[str, list[str]]] = []
        if spec.install_kind == "dotnet_tool":
            if dotnet:
                commands.append((f"{spec.maker}_dotnet_tool_install", [dotnet, "tool", "install", "--global", "wix"]))
                commands.append((f"{spec.maker}_dotnet_tool_update", [dotnet, "tool", "update", "--global", "wix"]))
            elif winget:
                commands.append((f"{spec.maker}_dotnet_sdk_winget", [winget, "install", "-e", "--id", "Microsoft.DotNet.SDK.8", "--accept-package-agreements", "--accept-source-agreements"]))
                if powershell:
                    ps = "$dotnet='C:\\Program Files\\dotnet\\dotnet.exe'; if(Test-Path $dotnet){ & $dotnet tool install --global wix; if($LASTEXITCODE -ne 0){ & $dotnet tool update --global wix } } else { exit 3 }"
                    commands.append((f"{spec.maker}_dotnet_tool_after_sdk", [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps]))
            return commands
        if spec.maker == "msix" and winget:
            for package_id in ("Microsoft.WindowsSDK", "Microsoft.WindowsSDK.10.0.22621", "Microsoft.WindowsSDK.10.0.26100"):
                commands.append((f"{spec.maker}_winget_install_{package_id.replace('.', '_')}", [winget, "install", "-e", "--id", package_id, "--accept-package-agreements", "--accept-source-agreements"]))
            return commands
        if spec.winget_id and winget:
            commands.append((f"{spec.maker}_winget_install", [winget, "install", "-e", "--id", spec.winget_id, "--accept-package-agreements", "--accept-source-agreements"]))
        if spec.winget_name and winget:
            commands.append((f"{spec.maker}_winget_name_install", [winget, "install", "-e", "--name", spec.winget_name, "--accept-package-agreements", "--accept-source-agreements"]))
        return commands

    def ensure_installer_tool(self, maker: str) -> Path | None:
        maker_key = str(maker or "").strip().lower().replace("-", "_")
        aliases = {"advancedinstaller": "advanced_installer", "advanced": "advanced_installer", "makeappx": "msix"}
        maker_key = aliases.get(maker_key, maker_key)
        spec = INSTALLER_TOOL_SPECS.get(maker_key)
        if not spec:
            self.warn(f"INSTALLER:DEPENDENCY unknown maker={maker}")
            return None
        self.log(f"INSTALLER:DEPENDENCY:CHECK maker={maker_key} tools={list(spec.tool_names)} note={spec.install_note}")
        for tool in spec.tool_names:
            found = self._find_tool(tool)
            if found:
                self._record_installer_tool_attempt(maker_key, status="found", rc=0, path=found, note="already-installed")
                self.log(f"INSTALLER:DEPENDENCY:FOUND maker={maker_key} tool={tool} path={found}")
                return found
        if self.dry_run:
            self.log(f"INSTALLER:DEPENDENCY:DRY-RUN maker={maker_key} would_install=1")
            return Path(spec.tool_names[0])
        if os.environ.get("PROMPT_AUTO_INSTALL_INSTALLER_TOOLS", "1").lower() not in {"1", "true", "yes", "on"}:
            self.warn(f"INSTALLER:DEPENDENCY:MISSING maker={maker_key} auto_install=disabled")
            self._record_installer_tool_attempt(maker_key, status="missing-auto-disabled", rc=None, note="PROMPT_AUTO_INSTALL_INSTALLER_TOOLS disabled")
            return None
        state = self._load_tool_state()
        recent, reason = self._installer_tool_recent_attempt(maker_key, state)
        if recent and os.environ.get("PROMPT_FORCE_INSTALLER_TOOL_INSTALL", "").lower() not in {"1", "true", "yes", "on"}:
            self.warn(f"INSTALLER:DEPENDENCY:SKIP-RECENT maker={maker_key} {reason}; set PROMPT_FORCE_INSTALLER_TOOL_INSTALL=1 to retry now")
            return None
        commands = self._installer_tool_commands(spec)
        if not commands:
            self.warn(f"INSTALLER:DEPENDENCY:NO-INSTALL-COMMAND maker={maker_key}; install manually. candidates={[str(x) for t in spec.tool_names for x in self._tool_candidates(t)]}")
            self._record_installer_tool_attempt(maker_key, status="no-install-command", note="winget/dotnet unavailable")
            return None
        last_rc: int | None = None
        for label, command in commands:
            self.log(f"INSTALLER:DEPENDENCY:INSTALL maker={maker_key} label={label} command={quote_cmd(command)}")
            rc, _ = self.run_cmd(command, label=f"InstallTool_{label}", timeout=int(os.environ.get("PROMPT_INSTALLER_TOOL_INSTALL_TIMEOUT", "7200") or "7200"))
            last_rc = rc
            for tool in spec.tool_names:
                found = self._find_tool(tool)
                if found:
                    self._record_installer_tool_attempt(maker_key, status="installed", rc=rc, path=found, command=command, note=f"installed after {label}")
                    self.log(f"INSTALLER:DEPENDENCY:INSTALLED maker={maker_key} tool={tool} path={found}")
                    return found
            # WiX dotnet tool update returns non-zero if install is needed; install and update are both tried.
            if rc != 0:
                self.warn(f"INSTALLER:DEPENDENCY:INSTALL-RC maker={maker_key} label={label} rc={rc}; will try next command if available")
        self._record_installer_tool_attempt(maker_key, status="install-failed", rc=last_rc, command=commands[-1][1] if commands else [], note="still missing after install attempts")
        self.warn(f"INSTALLER:DEPENDENCY:FAILED maker={maker_key} rc={last_rc}; candidates={[str(x) for t in spec.tool_names for x in self._tool_candidates(t)]}")
        return None

    def report_installer_toolchain(self, makers: list[str]) -> None:
        aliases = {"advancedinstaller": "advanced_installer", "advanced": "advanced_installer", "makeappx": "msix"}
        wanted: list[str] = []
        for maker in makers:
            key = aliases.get(str(maker or "").strip().lower().replace("-", "_"), str(maker or "").strip().lower().replace("-", "_"))
            if key in INSTALLER_TOOL_SPECS and key not in wanted:
                wanted.append(key)
        self.log(f"INSTALLER:DEPENDENCY:SUMMARY begin makers={wanted} auto_install={os.environ.get('PROMPT_AUTO_INSTALL_INSTALLER_TOOLS', '1')} cooldown_hours={self.installer_tool_cooldown_hours}")
        for maker in wanted:
            spec = INSTALLER_TOOL_SPECS[maker]
            found = None
            for tool in spec.tool_names:
                found = self._find_tool(tool)
                if found:
                    break
            self.log(f"INSTALLER:DEPENDENCY:STATUS maker={maker} found={bool(found)} path={found or ''} install_kind={spec.install_kind} winget_id={spec.winget_id} note={spec.install_note}")

    def _installer_output_md5(self, maker: str, inp: InstallerInput, output: Path, *, status: str, context: str) -> None:
        if output.exists() and output.is_file() and output.stat().st_size > 1024:
            size = output.stat().st_size
            self.log(f"INSTALLER:MD5 maker={maker} exe_backend={inp.exe_backend} name={output.name} path={output} md5={md5_file(output)} bytes={size} human={human_size(size)} valid=1 status={status} context={context}")
        else:
            self.log(f"INSTALLER:MD5 maker={maker} exe_backend={inp.exe_backend} name={output.name} path={output} md5=missing bytes=0 human=0B valid=0 status={status} context={context}")

    def _payload_files(self, payload: Path) -> list[Path]:
        return sorted([p for p in payload.rglob("*") if p.is_file()])

    def write_nsis_script(self, inp: InstallerInput, output: Path) -> Path:
        script = self.temp / "installer_scripts" / f"Prompt-{inp.exe_backend}-NSIS.nsi"
        script.parent.mkdir(parents=True, exist_ok=True)
        install_lines: list[str] = []
        for file in self._payload_files(inp.payload_dir):
            rel = file.relative_to(inp.payload_dir)
            rel_dir = str(rel.parent).replace("/", "\\")
            if rel_dir == ".":
                rel_dir = ""
            install_lines.append(f'SetOutPath "$INSTDIR{("\\" + rel_dir) if rel_dir else ""}"')
            install_lines.append(f'File "{str(file).replace(chr(92), chr(92)*2)}"')
        script.write_text(textwrap.dedent(f'''
            Unicode True
            Name "Prompt {inp.exe_backend}"
            OutFile "{str(output).replace(chr(92), chr(92)*2)}"
            InstallDir "$LOCALAPPDATA\\Prompt-{inp.exe_backend}"
            RequestExecutionLevel user
            Page directory
            Page instfiles
            UninstPage uninstConfirm
            UninstPage instfiles
            Section "Install"
              SetShellVarContext current
              CreateDirectory "$INSTDIR"
              {chr(10).join('  ' + line for line in install_lines)}
              CreateShortcut "$DESKTOP\\Prompt {inp.exe_backend}.lnk" "$INSTDIR\\{inp.exe_rel.replace('/', chr(92))}"
              CreateDirectory "$SMPROGRAMS\\Prompt"
              CreateShortcut "$SMPROGRAMS\\Prompt\\Prompt {inp.exe_backend}.lnk" "$INSTDIR\\{inp.exe_rel.replace('/', chr(92))}"
              WriteUninstaller "$INSTDIR\\Uninstall.exe"
            SectionEnd
            Section "Uninstall"
              Delete "$DESKTOP\\Prompt {inp.exe_backend}.lnk"
              Delete "$SMPROGRAMS\\Prompt\\Prompt {inp.exe_backend}.lnk"
              RMDir /r "$INSTDIR"
            SectionEnd
        '''), encoding="utf-8")
        self.log(f"INSTALLER:NSIS:SCRIPT exe_backend={inp.exe_backend} path={script} output={output}")
        return script

    def build_nsis_installer(self, inp: InstallerInput) -> Path | None:
        output = self.dist / f"PromptSetup-{inp.exe_backend}-NSIS.exe"
        if self.dry_run:
            self.log(f"INSTALLER:NSIS:DRY-RUN exe_backend={inp.exe_backend} output={output}")
            self._installer_output_md5("nsis", inp, output, status="dry-run", context="matrix")
            return output
        tool = self.ensure_installer_tool("nsis")
        if not tool:
            self._installer_output_md5("nsis", inp, output, status="tool-missing", context="matrix")
            return None
        script = self.write_nsis_script(inp, output)
        rc, _ = self.run_cmd([str(tool), str(script)], label=f"NSIS_{inp.exe_backend}", timeout=3600)
        self._installer_output_md5("nsis", inp, output, status="ok" if rc == 0 else f"failed-rc-{rc}", context="matrix")
        return output if rc == 0 and output.exists() else None

    def write_inno_script(self, inp: InstallerInput, output: Path) -> Path:
        script = self.temp / "installer_scripts" / f"Prompt-{inp.exe_backend}-Inno.iss"
        script.parent.mkdir(parents=True, exist_ok=True)
        files: list[str] = []
        for file in self._payload_files(inp.payload_dir):
            rel = file.relative_to(inp.payload_dir)
            dest = "{app}" if str(rel.parent) == "." else "{app}\\" + str(rel.parent).replace("/", "\\")
            files.append(f'Source: "{str(file)}"; DestDir: "{dest}"; Flags: ignoreversion')
        exe_rel_win = inp.exe_rel.replace('/', '\\')
        script.write_text(textwrap.dedent(f'''
            #define MyAppName "Prompt {inp.exe_backend}"
            #define MyAppVersion "{VERSION}"
            [Setup]
            AppId={{{{B6E2A8E1-7E18-4AE8-9DA1-{hashlib.md5(inp.exe_backend.encode()).hexdigest()[:12].upper()}}}}}
            AppName={{#MyAppName}}
            AppVersion={{#MyAppVersion}}
            DefaultDirName={{localappdata}}\\Prompt-{inp.exe_backend}
            DefaultGroupName=Prompt
            OutputDir={self.dist}
            OutputBaseFilename={output.stem}
            Compression=lzma2
            SolidCompression=yes
            PrivilegesRequired=lowest
            SetupIconFile={self.root / 'icon.ico' if (self.root / 'icon.ico').exists() else ''}
            [Files]
            {chr(10).join(files)}
            [Icons]
            Name: "{{autodesktop}}\\Prompt {inp.exe_backend}"; Filename: "{{app}}\\{exe_rel_win}"; Tasks: desktopicon
            Name: "{{group}}\\Prompt {inp.exe_backend}"; Filename: "{{app}}\\{exe_rel_win}"
            [Tasks]
            Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"
        '''), encoding="utf-8")
        self.log(f"INSTALLER:INNO:SCRIPT exe_backend={inp.exe_backend} path={script} output={output}")
        return script

    def build_inno_installer(self, inp: InstallerInput) -> Path | None:
        output = self.dist / f"PromptSetup-{inp.exe_backend}-Inno.exe"
        if self.dry_run:
            self.log(f"INSTALLER:INNO:DRY-RUN exe_backend={inp.exe_backend} output={output}")
            self._installer_output_md5("inno", inp, output, status="dry-run", context="matrix")
            return output
        tool = self.ensure_installer_tool("inno")
        if not tool:
            self._installer_output_md5("inno", inp, output, status="tool-missing", context="matrix")
            return None
        script = self.write_inno_script(inp, output)
        rc, _ = self.run_cmd([str(tool), str(script)], label=f"Inno_{inp.exe_backend}", timeout=3600)
        self._installer_output_md5("inno", inp, output, status="ok" if rc == 0 else f"failed-rc-{rc}", context="matrix")
        return output if rc == 0 and output.exists() else None

    def _wix_id(self, prefix: str, value: str) -> str:
        safe = ''.join(ch if ch.isalnum() else '_' for ch in str(value or 'root'))
        if not safe or safe[0].isdigit():
            safe = '_' + safe
        digest = hashlib.md5(str(value or '').encode('utf-8', 'replace')).hexdigest()[:8]
        return f"{prefix}_{safe[:42]}_{digest}"

    def _wix_escape(self, value: object) -> str:
        return str(value).replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;').replace('>', '&gt;')

    def write_wix_script(self, inp: InstallerInput, output: Path) -> Path:
        script = self.temp / "installer_scripts" / f"Prompt-{inp.exe_backend}-WiX.wxs"
        script.parent.mkdir(parents=True, exist_ok=True)
        payload_files = self._payload_files(inp.payload_dir)
        by_dir: dict[str, list[Path]] = {}
        for file in payload_files:
            rel = file.relative_to(inp.payload_dir)
            rel_dir = '' if str(rel.parent) == '.' else str(rel.parent).replace('\\', '/')
            by_dir.setdefault(rel_dir, []).append(file)
        component_refs: list[str] = []

        def emit_dir(rel_dir: str, depth: int = 0) -> str:
            indent = '  ' * depth
            if rel_dir:
                dir_id = self._wix_id('DIR', f'{inp.exe_backend}/{rel_dir}')
                dir_name = Path(rel_dir).name
                open_line = f'{indent}<Directory Id="{dir_id}" Name="{self._wix_escape(dir_name)}">'
                close_line = f'{indent}</Directory>'
                body_indent = depth + 1
            else:
                open_line = ''
                close_line = ''
                body_indent = depth
            lines: list[str] = []
            if open_line:
                lines.append(open_line)
            prefix = (rel_dir + '/') if rel_dir else ''
            child_dirs = sorted({key[len(prefix):].split('/', 1)[0] for key in by_dir if key.startswith(prefix) and key != rel_dir and '/' not in key[len(prefix):].strip('/')})
            for child in child_dirs:
                child_rel = prefix + child if prefix else child
                lines.append(emit_dir(child_rel, body_indent))
            for file in by_dir.get(rel_dir, []):
                rel = file.relative_to(inp.payload_dir)
                comp_id = self._wix_id('CMP', f'{inp.exe_backend}/{rel}')
                file_id = self._wix_id('FIL', f'{inp.exe_backend}/{rel}')
                source = self._wix_escape(file)
                name = self._wix_escape(rel.name)
                component_refs.append(f'<ComponentRef Id="{comp_id}" />')
                lines.append(f'{"  " * body_indent}<Component Id="{comp_id}" Guid="*"><File Id="{file_id}" Source="{source}" Name="{name}" KeyPath="yes" /></Component>')
            if close_line:
                lines.append(close_line)
            return '\n'.join(lines)

        directory_body = emit_dir('', 10)
        product_code_tail = hashlib.md5(("wix" + inp.exe_backend).encode()).hexdigest()[:12].upper()
        script_text = f'''<?xml version="1.0" encoding="UTF-8"?>
<Wix xmlns="http://wixtoolset.org/schemas/v4/wxs">
  <Package Name="Prompt {inp.exe_backend}" Manufacturer="Prompt" Version="{VERSION}" UpgradeCode="11111111-2222-3333-4444-{product_code_tail}" Scope="perUser">
    <MediaTemplate EmbedCab="yes" />
    <StandardDirectory Id="LocalAppDataFolder">
      <Directory Id="INSTALLFOLDER" Name="Prompt-{inp.exe_backend}">
{directory_body}
      </Directory>
    </StandardDirectory>
    <Feature Id="MainFeature" Title="Prompt" Level="1">
      {chr(10).join(component_refs)}
    </Feature>
  </Package>
</Wix>
'''
        script.write_text(script_text, encoding="utf-8")
        self.log(f"INSTALLER:WIX:SCRIPT exe_backend={inp.exe_backend} path={script} output={output} files={len(payload_files)}")
        return script

    def build_wix_installer(self, inp: InstallerInput) -> Path | None:
        output = self.dist / f"PromptSetup-{inp.exe_backend}-WiX.msi"
        if self.dry_run:
            self.log(f"INSTALLER:WIX:DRY-RUN exe_backend={inp.exe_backend} output={output}")
            self._installer_output_md5("wix", inp, output, status="dry-run", context="matrix")
            return output
        tool = self.ensure_installer_tool("wix")
        if not tool:
            self._installer_output_md5("wix", inp, output, status="tool-missing", context="matrix")
            return None
        script = self.write_wix_script(inp, output)
        rc, _ = self.run_cmd([str(tool), "build", str(script), "-o", str(output)], label=f"WiX_{inp.exe_backend}", timeout=3600)
        self._installer_output_md5("wix", inp, output, status="ok" if rc == 0 else f"failed-rc-{rc}", context="matrix")
        return output if rc == 0 and output.exists() else None

    def _copytree_contents(self, source: Path, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        for item in source.iterdir():
            target = destination / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            elif item.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)

    def _write_msix_placeholder_pngs(self, assets_dir: Path) -> None:
        # 1x1 transparent PNG. MakeAppx only needs package assets to exist; a real
        # branding pass can replace these later without touching installer logic.
        import base64 as _base64
        png = _base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/l9n7WQAAAABJRU5ErkJggg=="
        )
        assets_dir.mkdir(parents=True, exist_ok=True)
        for name in ("Square44x44Logo.png", "Square150x150Logo.png", "StoreLogo.png"):
            (assets_dir / name).write_bytes(png)

    def _msix_identity_name(self, inp: InstallerInput) -> str:
        safe = ''.join(ch if ch.isalnum() or ch == '.' else '.' for ch in f"Prompt.{inp.exe_backend}")
        safe = '.'.join(part for part in safe.split('.') if part) or 'Prompt.App'
        if not safe[0].isalpha():
            safe = 'Prompt.' + safe
        return safe[:50]

    def write_msix_manifest(self, inp: InstallerInput, content_dir: Path) -> Path:
        manifest = content_dir / "AppxManifest.xml"
        exe_rel = inp.exe_rel.replace('/', '\\')
        identity_name = self._msix_identity_name(inp)
        display_name = f"Prompt {inp.exe_backend}"
        publisher = os.environ.get("PROMPT_MSIX_PUBLISHER", "CN=Prompt")
        version = os.environ.get("PROMPT_MSIX_VERSION", f"{VERSION}.0" if VERSION.count('.') == 2 else VERSION)
        if version.count('.') != 3:
            version = "1.0.0.0"
        manifest.write_text(textwrap.dedent(f'''
            <?xml version="1.0" encoding="utf-8"?>
            <Package
              xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10"
              xmlns:uap="http://schemas.microsoft.com/appx/manifest/uap/windows10"
              xmlns:rescap="http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities"
              IgnorableNamespaces="uap rescap">
              <Identity Name="{identity_name}" Publisher="{publisher}" Version="{version}" ProcessorArchitecture="x64" />
              <Properties>
                <DisplayName>{display_name}</DisplayName>
                <PublisherDisplayName>Prompt</PublisherDisplayName>
                <Description>Prompt desktop application packaged from the {inp.exe_backend} executable.</Description>
                <Logo>Assets\\StoreLogo.png</Logo>
              </Properties>
              <Dependencies>
                <TargetDeviceFamily Name="Windows.Desktop" MinVersion="10.0.17763.0" MaxVersionTested="10.0.22621.0" />
              </Dependencies>
              <Resources>
                <Resource Language="en-us" />
              </Resources>
              <Applications>
                <Application Id="Prompt" Executable="{exe_rel}" EntryPoint="Windows.FullTrustApplication">
                  <uap:VisualElements DisplayName="{display_name}" Description="Prompt" Square150x150Logo="Assets\\Square150x150Logo.png" Square44x44Logo="Assets\\Square44x44Logo.png" BackgroundColor="transparent" />
                </Application>
              </Applications>
              <Capabilities>
                <rescap:Capability Name="runFullTrust" />
              </Capabilities>
            </Package>
        ''').lstrip(), encoding="utf-8")
        self.log(f"INSTALLER:MSIX:MANIFEST exe_backend={inp.exe_backend} path={manifest} executable={exe_rel} identity={identity_name}")
        return manifest

    def build_msix_installer(self, inp: InstallerInput) -> Path | None:
        output = self.dist / f"PromptSetup-{inp.exe_backend}-MSIX.msix"
        if self.dry_run:
            self.log(f"INSTALLER:MSIX:DRY-RUN exe_backend={inp.exe_backend} output={output}")
            self._installer_output_md5("msix", inp, output, status="dry-run", context="matrix")
            return output
        tool = self.ensure_installer_tool("msix")
        if not tool:
            self._installer_output_md5("msix", inp, output, status="tool-missing", context="matrix")
            return None
        content_dir = self.temp / "msix_content" / inp.exe_backend
        if content_dir.exists():
            shutil.rmtree(content_dir, ignore_errors=True)
        content_dir.mkdir(parents=True, exist_ok=True)
        self._copytree_contents(inp.payload_dir, content_dir)
        self._write_msix_placeholder_pngs(content_dir / "Assets")
        self.write_msix_manifest(inp, content_dir)
        rc, _ = self.run_cmd([str(tool), "pack", "/o", "/d", str(content_dir), "/p", str(output)], label=f"MSIX_{inp.exe_backend}", timeout=3600)
        self._installer_output_md5("msix", inp, output, status="ok" if rc == 0 else f"failed-rc-{rc}", context="matrix")
        return output if rc == 0 and output.exists() else None

    def _find_advanced_installer_tool(self) -> Path | None:
        return self.ensure_installer_tool("advanced_installer")

    def build_advanced_installer(self, inp: InstallerInput) -> Path | None:
        output = self.dist / f"PromptSetup-{inp.exe_backend}-AdvancedInstaller.msi"
        if self.dry_run:
            self.log(f"INSTALLER:ADVANCED_INSTALLER:DRY-RUN exe_backend={inp.exe_backend} output={output}")
            self._installer_output_md5("advanced_installer", inp, output, status="dry-run", context="matrix")
            return output
        tool = self._find_advanced_installer_tool()
        if not tool:
            self._installer_output_md5("advanced_installer", inp, output, status="tool-missing", context="matrix")
            return None
        project_dir = self.temp / "advanced_installer" / inp.exe_backend
        if project_dir.exists():
            shutil.rmtree(project_dir, ignore_errors=True)
        project_dir.mkdir(parents=True, exist_ok=True)
        aip = project_dir / f"Prompt-{inp.exe_backend}.aip"
        payload = project_dir / "payload"
        self._copytree_contents(inp.payload_dir, payload)
        exe_rel_win = inp.exe_rel.replace('/', '\\')
        commands: list[tuple[str, list[str]]] = [
            ("newproject", [str(tool), "/newproject", str(aip), "-type", "professional", "-lang", "en", "-overwrite"]),
            ("product_name", [str(tool), "/edit", str(aip), "/SetProperty", f"ProductName=Prompt {inp.exe_backend}"]),
            ("manufacturer", [str(tool), "/edit", str(aip), "/SetProperty", "Manufacturer=Prompt"]),
            ("version", [str(tool), "/edit", str(aip), "/SetVersion", VERSION]),
            ("appdir", [str(tool), "/edit", str(aip), "/SetAppdir", "-buildname", "DefaultBuild", "-path", f"[LocalAppDataFolder]Prompt-{inp.exe_backend}"]),
            ("shortcutdir", [str(tool), "/edit", str(aip), "/SetShortcutdir", "-buildname", "DefaultBuild", "-path", "[ProgramMenuFolder]Prompt"]),
            ("addfolder", [str(tool), "/edit", str(aip), "/AddFolder", "APPDIR", str(payload), "-install_in_parent_folder"]),
            ("shortcut", [str(tool), "/edit", str(aip), "/NewShortcut", "-name", f"Prompt {inp.exe_backend}", "-dir", "SHORTCUTDIR", "-target", f"APPDIR\\{exe_rel_win}", "-wkdir", "APPDIR"]),
            ("packagename", [str(tool), "/edit", str(aip), "/SetPackageName", output.name, "-buildname", "DefaultBuild"]),
            ("build", [str(tool), "/build", str(aip)]),
        ]
        self.log(f"INSTALLER:ADVANCED_INSTALLER:PROJECT exe_backend={inp.exe_backend} aip={aip} output={output}")
        for label, command in commands:
            rc, _ = self.run_cmd(command, label=f"AdvancedInstaller_{inp.exe_backend}_{label}", cwd=project_dir, timeout=3600)
            if rc != 0:
                self._installer_output_md5("advanced_installer", inp, output, status=f"failed-{label}-rc-{rc}", context="matrix")
                return None
        candidates = [output, project_dir / output.name, *project_dir.rglob(output.name)]
        found = next((c for c in candidates if c.exists() and c.is_file()), None)
        if found and found.resolve() != output.resolve():
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(found, output)
        self._installer_output_md5("advanced_installer", inp, output, status="ok", context="matrix")
        return output if output.exists() else None

    def build_installer_matrix(self, makers: list[str], inputs: list[InstallerInput]) -> list[Path]:
        made: list[Path] = []
        dispatch = {
            "nsis": self.build_nsis_installer,
            "inno": self.build_inno_installer,
            "wix": self.build_wix_installer,
            "msix": self.build_msix_installer,
            "advanced_installer": self.build_advanced_installer,
            "advancedinstaller": self.build_advanced_installer,
            "advanced": self.build_advanced_installer,
        }
        for maker in makers:
            func = dispatch.get(maker)
            if not func:
                self.warn(f"INSTALLER:{maker}:SKIP not a per-EXE wrapper maker; use nsis,inno,wix for the matrix")
                continue
            for inp in inputs:
                self.log(f"INSTALLER:MATRIX:BEGIN maker={maker} exe_backend={inp.exe_backend} payload={inp.payload_dir}")
                output = func(inp)
                if output and (self.dry_run or output.exists()):
                    made.append(output)
                    self.log(f"INSTALLER:MATRIX:SUCCESS maker={maker} exe_backend={inp.exe_backend} output={output}")
                else:
                    self.warn(f"INSTALLER:MATRIX:FAILED maker={maker} exe_backend={inp.exe_backend}")
        return made

    def build_installers(self, installers: list[str]) -> int:
        normalized = [str(x or '').strip().lower().replace('-', '_') for x in installers if str(x or '').strip()]
        aliases = {"advancedinstaller": "advanced_installer", "advanced": "advanced_installer", "makeappx": "msix"}
        wrappers = [aliases.get(x, x) for x in normalized if x]
        known_wrappers = {"nsis", "inno", "wix", "msix", "advanced_installer"}
        unknown = [x for x in wrappers if x not in known_wrappers]
        wrappers = [x for x in wrappers if x in known_wrappers]
        self.log("INSTALLERS:BEGIN requested=" + ",".join(normalized) + f" wrappers={wrappers} unknown={unknown}")
        inputs = self.discover_installer_inputs()
        if not inputs and not self.dry_run and os.environ.get("PROMPT_INSTALLERS_AUTO_BUILD_EXES", "1").lower() in {"1", "true", "yes", "on"}:
            self.warn("INSTALLERS:NO-EXES auto-building EXEs before installer matrix")
            exe_rc = self.build_exes(selected_backends(parse_args(self.argv)))
            if exe_rc != 0 and self.require_strict_installers:
                self.warn(f"INSTALLERS:STRICT auto EXE build failed rc={exe_rc}")
                return exe_rc
            inputs = self.discover_installer_inputs()
        if not inputs:
            self.warn("INSTALLERS:FAILED no Prompt-*.exe artifacts found in dist; run --build first or keep PROMPT_INSTALLERS_AUTO_BUILD_EXES=1")
            return 2 if self.require_strict_installers else 0
        active_wrappers = wrappers or list(DEFAULT_INSTALLERS)
        self.report_installer_toolchain(active_wrappers)
        made = self.build_installer_matrix(active_wrappers, inputs)
        for name in unknown:
            self.warn(f"INSTALLER:{name}:SKIP unknown installer backend; known={','.join(DEFAULT_INSTALLERS)}")
        expected = len(active_wrappers) * len(inputs)
        missing_matrix = []
        made_names = {Path(x).name.lower() for x in made}
        wrapper_names = {"nsis": ("NSIS", ".exe"), "inno": ("Inno", ".exe"), "wix": ("WiX", ".msi"), "msix": ("MSIX", ".msix"), "advanced_installer": ("AdvancedInstaller", ".msi")}
        for wrapper in active_wrappers:
            label, ext = wrapper_names.get(wrapper, (wrapper, ".installer"))
            for inp in inputs:
                expected_name = f"PromptSetup-{inp.exe_backend}-{label}{ext}".lower()
                if expected_name not in made_names:
                    missing_matrix.append(expected_name)
                    self.warn(f"INSTALLER:MATRIX:MISSING maker={wrapper} exe_backend={inp.exe_backend} expected={expected_name}")
        summary = {"inputs": [inp.exe_backend for inp in inputs], "wrappers": active_wrappers, "made": [str(x) for x in made], "missing": missing_matrix, "expected_matrix": expected, "strict": self.require_strict_installers}
        self.log("INSTALLERS:SUMMARY " + json.dumps(summary, ensure_ascii=False, sort_keys=True))
        if self.dry_run:
            self.log(f"INSTALLERS:DRY-RUN matrix_expected={expected}")
            return 0
        if self.require_strict_installers and len(made) != expected:
            self.warn(f"INSTALLERS:STRICT failed made={len(made)} expected={expected}")
            return 2
        if not made:
            self.warn("INSTALLERS:SOFT-FAILED no installers were created; install NSIS/Inno/WiX/MakeAppx/Advanced Installer or enable their tools, but build remains soft by default")
            return 0
        self.log(f"INSTALLERS:SUCCESS count={len(made)} expected_matrix={expected} strict={self.require_strict_installers}")
        return 0


def _release_emergency_log(message: str) -> None:
    """Last-ditch logger for unhandled release-runner faults before ReleaseRunner exists."""
    try:
        root = Path(os.environ.get("PROMPT_RELEASE_ROOT", "") or os.getcwd()).resolve()
    except Exception:
        root = Path.cwd()
    text = f"{_now()} [FAULT:PROMPT-RELEASE] UNHANDLED {message}\n"
    for path in (root / "debug.log", root / "logs" / "release_faults.log", root / "logs" / "early_build_release.raw.log"):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", errors="replace") as fh:
                fh.write(text)
        except OSError:
            pass


def _release_excepthook(exc_type, exc, tb) -> None:
    try:
        detail = "".join(traceback.format_exception(exc_type, exc, tb))
    except Exception:
        detail = f"{exc_type.__name__}: {exc}"
    _release_emergency_log(detail)
    try:
        sys.__excepthook__(exc_type, exc, tb)
    except Exception:
        pass


sys.excepthook = _release_excepthook


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--build", action="store_true")
    parser.add_argument("package", nargs="?")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force-rebuild", action="store_true")
    parser.add_argument("--offscreen", action="store_true")
    parser.add_argument("--exe-backends", "--backends", default="")
    parser.add_argument("--installers", default="")
    parser.add_argument("--check-installer-tools", action="store_true")
    parser.add_argument("--pyinstaller", action="store_true")
    parser.add_argument("--pyinstaller-dir", "--pyinstaller-onedir", dest="pyinstaller_dir", action="store_true")
    parser.add_argument("--nuitka", "--nikita", action="store_true")
    parser.add_argument("--cx-freeze", dest="cx_freeze", action="store_true")
    parser.add_argument("--py2exe", action="store_true")
    parser.add_argument("--pyoxidizer", action="store_true")
    parser.add_argument("--pyapp", action="store_true")
    parser.add_argument("--briefcase", action="store_true")
    return parser.parse_known_args(argv)[0]


def selected_backends(ns: argparse.Namespace) -> list[str]:
    explicit: list[str] = []
    if ns.exe_backends:
        for item in ns.exe_backends.replace(";", ",").split(","):
            if item.strip():
                explicit.append(normalize_backend(item))
    for key in ("pyinstaller", "pyinstaller_dir", "nuitka", "cx_freeze", "py2exe", "pyoxidizer", "pyapp", "briefcase"):
        if getattr(ns, key, False):
            explicit.append(normalize_backend(key))
    if not explicit:
        env = os.environ.get("PROMPT_EXE_BACKENDS", "").strip()
        if env:
            explicit = [normalize_backend(x) for x in env.replace(";", ",").split(",") if x.strip()]
    if not explicit and os.environ.get("PROMPT_INCLUDE_DIAGNOSTIC_EXE_BACKENDS", "").strip().lower() in {"1", "true", "yes", "on"}:
        explicit = list(DEFAULT_EXE_BACKENDS) + list(OPTIONAL_EXE_BACKENDS)
    if explicit:
        result=[]
        for b in explicit:
            if b and b not in result:
                result.append(b)
        return result
    return list(DEFAULT_EXE_BACKENDS)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Accept the old form: --build package --force-rebuild.
    package_mode = any(str(x).lower() in {"package", "--package", "installers", "--installers", "installer-package", "--installer-package"} for x in argv)
    ns = parse_args(argv)
    root = Path(ns.root).resolve()
    runner = ReleaseRunner(root, argv, dry_run=ns.dry_run, force=ns.force_rebuild)
    if ns.check_installer_tools:
        installers = [x.strip().lower() for x in (ns.installers or os.environ.get("PROMPT_INSTALLER_BACKENDS", "")).replace(";", ",").split(",") if x.strip()] or DEFAULT_INSTALLERS
        runner.report_installer_toolchain(installers)
        for maker in installers:
            if not runner.dry_run:
                runner.ensure_installer_tool(maker)
        return 0
    if package_mode:
        installers = [x.strip().lower() for x in (ns.installers or os.environ.get("PROMPT_INSTALLER_BACKENDS", "")).replace(";", ",").split(",") if x.strip()] or DEFAULT_INSTALLERS
        return runner.build_installers(installers)
    return runner.build_exes(selected_backends(ns))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as exc:
        _release_emergency_log("".join(traceback.format_exception(type(exc), exc, getattr(exc, "__traceback__", None))))
        raise
