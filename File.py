"""Prompt canonical File wrapper.

This module uses the richer File/Files design from vendor/claude/file_classes.zip
while preserving Prompt's existing static-call API:

    File.readText(path)
    File.writeText(path, text)
    File.copy2(src, dst)

The same methods also work on instances:

    f = File(path)
    f.readText()
    f.writeText(text)

All raw file I/O for the app should route through this module.
"""
from __future__ import annotations

import base64
import builtins
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

EMPTY_STRING = ''

try:
    from DebugLog import DebugLog as _PromptDebugLog  # type: ignore
except Exception as import_error:
    _PromptDebugLog = None
    try:
        print(f'[WARN:File.py] DebugLog import failed: {type(import_error).__name__}: {import_error}', file=sys.stderr, flush=True)
    except Exception:  # swallow-ok: final early-bootstrap fallback before DebugLog exists
        pass

try:
    from PhaseProcess import PhaseProcess as _PhaseProcess  # type: ignore
except Exception as import_error:
    _PhaseProcess = None
    try:
        print(f'[WARN:File.py] PhaseProcess import failed: {type(import_error).__name__}: {import_error}', file=sys.stderr, flush=True)
    except Exception:  # swallow-ok: final early-bootstrap fallback before DebugLog exists
        pass


def recordException(context: str, error: BaseException | None = None, *, handled: bool = True, source: str = 'File.py') -> int:
    try:
        if _PromptDebugLog is not None:
            fn = getattr(_PromptDebugLog, 'saveExceptionFallback', None)
            if callable(fn):
                return int(fn(error, source=source, context=context, handled=handled) or 0)
    except Exception:  # swallow-ok: file error recorder must not recurse
        pass
    try:
        print(f'[EXCEPTION:{source}:{context}] {type(error).__name__}: {error}', file=sys.stderr, flush=True)
    except Exception:  # swallow-ok: final fallback
        pass
    return 0


def _run_process(command: list[str], *, timeout: float, capture_output: bool = False, text: bool = False, check: bool = False, stdout: Any = None, stderr: Any = None, stdin: Any = None, phase_name: str = 'File.process') -> subprocess.CompletedProcess:
    kwargs: dict[str, Any] = {'timeout': timeout, 'check': check}
    if capture_output:
        kwargs['capture_output'] = True
    if text:
        kwargs['text'] = True
    if stdout is not None:
        kwargs['stdout'] = stdout
    if stderr is not None:
        kwargs['stderr'] = stderr
    if stdin is not None:
        kwargs['stdin'] = stdin
    if _PhaseProcess is not None:
        return _PhaseProcess.run(command, phase_name=phase_name, **kwargs)
    return subprocess.run(command, **kwargs)  # lifecycle-bypass-ok phase-ownership-ok: final fallback when PhaseProcess is unavailable


def _as_path(value: Any) -> Path:
    return value.path if isinstance(value, File) else (value if isinstance(value, Path) else Path(value))


def _maybe_callback(callback: Callable[[Path, Exception], None] | None, path: Path, error: Exception) -> None:
    if not callable(callback):
        return
    try:
        callback(path, error)
    except Exception as callback_error:  # swallow-ok: callback failure should not mask file operation error
        recordException('callback', callback_error)


def _who_locks(path: Path) -> list[dict[str, Any]]:
    if os.name != 'nt':
        return []
    try:
        from restartmgr import who_locks  # type: ignore
        return [{'pid': int(p.pid), 'name': str(p.app_name), 'type': str(p.app_type.name)} for p in who_locks(path)]
    except ImportError as exc:
        recordException('who_locks.restartmgr-missing', exc)
        return []
    except Exception as exc:
        recordException('who_locks', exc)
        return []


def _known_child_pids(parent_pid: int) -> set[int]:
    """Return known descendant PIDs without importing psutil.

    This is intentionally best-effort and routes through PhaseProcess so Windows
    process probes do not create dead console windows. Failures are recorded to
    the debugger-visible exception table.
    """
    if os.name != 'nt':
        return set()
    try:
        result = _run_process(
            ['wmic', 'process', 'get', 'ProcessId,ParentProcessId', '/FORMAT:CSV'],
            timeout=8,
            capture_output=True,
            text=True,
            check=False,
            phase_name='File.knownChildPids.wmic',
        )
        parent_to_children: dict[int, set[int]] = {}
        for raw_line in str(result.stdout or '').splitlines():
            line = raw_line.strip()
            if not line or 'ProcessId' in line:
                continue
            parts = [part.strip() for part in line.split(',') if part.strip()]
            if len(parts) < 3:
                continue
            try:
                ppid = int(parts[-2])
                pid = int(parts[-1])
            except ValueError as exc:
                recordException('knownChildPids.parse', exc)
                continue
            parent_to_children.setdefault(ppid, set()).add(pid)
        discovered: set[int] = set()
        stack = list(parent_to_children.get(int(parent_pid or 0), set()))
        while stack:
            pid = int(stack.pop())
            if pid in discovered:
                continue
            discovered.add(pid)
            stack.extend(parent_to_children.get(pid, set()))
        return discovered
    except Exception as exc:
        recordException('knownChildPids', exc)
        return set()


class File:
    """Canonical traced file value object and static compatibility gateway."""

    def __init__(self, path: str | Path, *, kind: str = 'file', language: str = '', mime: str = '') -> None:
        self.path = Path(path)
        self.kind = str(kind or 'file')  # noqa: nonconform
        self.language = str(language or EMPTY_STRING)  # noqa: nonconform
        self.mime = str(mime or EMPTY_STRING)  # noqa: nonconform
        self._on_read_error: Callable[[Path, Exception], None] | None = None
        self._on_write_error: Callable[[Path, Exception], None] | None = None
        self._on_permission_error: Callable[[Path, Exception], None] | None = None

    @staticmethod
    def _path(value: Any) -> Path:
        return _as_path(value)

    @staticmethod
    def ensureParent(path: Any) -> Path:  # noqa: N802
        target = _as_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def on_read_error(self, cb: Callable[[Path, Exception], None]) -> 'File':
        self._on_read_error = cb
        return self

    def on_write_error(self, cb: Callable[[Path, Exception], None]) -> 'File':
        self._on_write_error = cb
        return self

    def on_permission_error(self, cb: Callable[[Path, Exception], None]) -> 'File':
        self._on_permission_error = cb
        return self

    def onPermissionError(self, cb: Callable[[Path, Exception], None]) -> 'File':  # noqa: N802
        """CamelCase compatibility alias from the launcher File API.

        The uploaded FlatLine/start.py branch uses this name.  Keep it here so
        detector/deploy code can share the same File contract without adding a
        duplicate File class inside Prompt's start.py.
        """
        return self.on_permission_error(cb)

    def _fire_read_error(self, exc: Exception) -> None:
        if isinstance(exc, PermissionError):
            _maybe_callback(self._on_permission_error, self.path, exc)
        _maybe_callback(self._on_read_error, self.path, exc)

    def _fire_write_error(self, exc: Exception) -> None:
        if isinstance(exc, PermissionError):
            _maybe_callback(self._on_permission_error, self.path, exc)
        _maybe_callback(self._on_write_error, self.path, exc)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def stem(self) -> str:
        return self.path.stem

    @property
    def suffix(self) -> str:
        return self.path.suffix.lower()

    @property
    def exists(self) -> bool:
        return self.path.exists()

    @property
    def size(self) -> int:
        try:
            return int(self.path.stat().st_size)
        except Exception as exc:
            recordException('size', exc)
            return 0

    @property
    def mtime(self) -> float:
        try:
            return float(self.path.stat().st_mtime)
        except Exception as exc:
            recordException('mtime', exc)
            return 0.0

    @property
    def modified(self) -> float:
        return self.mtime

    def setMtime(self, timestamp: float | None = None) -> bool:  # noqa: N802
        try:
            target = _as_path(self)
            t = float(timestamp) if timestamp is not None else time.time()
            os.utime(target, (t, t))  # file-wrapper-ok
            return True
        except Exception as exc:
            recordException('setMtime', exc)
            if isinstance(self, File):
                self._fire_write_error(exc)
            return False

    def getMetadata(self, field: str | None = None) -> Any:  # noqa: N802
        try:
            target = _as_path(self)
            st = target.stat()
            data: dict[str, Any] = {
                'size': int(st.st_size),
                'mtime': float(st.st_mtime),
                'atime': float(st.st_atime),
                'ctime': float(st.st_ctime),
                'readable': os.access(target, os.R_OK),
                'writable': os.access(target, os.W_OK),
                'executable': os.access(target, os.X_OK),
            }
            if os.name == 'nt':
                try:
                    proc = _run_process(['icacls', str(target)], capture_output=True, text=True, timeout=5, check=False, phase_name='File.getMetadata.icacls')
                    data['icacls'] = proc.stdout.strip()
                except Exception as exc:
                    recordException('getMetadata.icacls', exc)
            else:
                data['mode_octal'] = oct(st.st_mode & 0o777)
                data['uid'] = getattr(st, 'st_uid', 0)
                data['gid'] = getattr(st, 'st_gid', 0)
            return data.get(field) if field is not None else data
        except Exception as exc:
            recordException('getMetadata', exc)
            return None if field is not None else {}

    def setMetadata(self, field: str, value: Any) -> bool:  # noqa: N802
        try:
            target = _as_path(self)
            if field == 'mtime':
                return File(target).setMtime(float(value))
            if field == 'atime':
                st = target.stat()
                os.utime(target, (float(value), st.st_mtime))  # file-wrapper-ok
                return True
            if field == 'mode' and os.name != 'nt':
                os.chmod(target, int(value))  # file-wrapper-ok
                return True
            recordException('setMetadata.unsupported', ValueError(f'unsupported field: {field!r}'))
            return False
        except Exception as exc:
            recordException('setMetadata', exc)
            if isinstance(self, File):
                self._fire_write_error(exc)
            return False

    def getPermissions(self) -> dict[str, Any]:  # noqa: N802
        return dict(self.getMetadata() or {})

    def setPermissions(self, mode: int | str) -> bool:  # noqa: N802
        """Set permissions without broad Windows ACL grants.

        The larger launcher branch explicitly avoided granting Everyone:(F).
        Prompt should do the same: use Python's chmod on all platforms, which
        safely handles the read-only bit on Windows and normal octal modes on
        POSIX.  String modes like '644' and '0o644' are accepted for
        config/deployer callers.
        """
        try:
            target = _as_path(self)
            if isinstance(mode, str):
                stripped = mode.strip().lower()
                parsed_mode = int(stripped, 8) if stripped.startswith('0o') or re.fullmatch(r'[0-7]{3,4}', stripped) else int(stripped, 10)
            else:
                parsed_mode = int(mode)
            os.chmod(target, parsed_mode)  # file-wrapper-ok
            return True
        except Exception as exc:
            recordException('setPermissions', exc)
            if isinstance(self, File):
                self._fire_write_error(exc)
            return False

    def getLockingProcesses(self) -> list[dict[str, Any]]:  # noqa: N802
        return _who_locks(_as_path(self))

    def isLocked(self) -> bool:  # noqa: N802
        return bool(self.getLockingProcesses())

    def killLockingProcesses(self, *, only_known_children: bool = False) -> list[dict[str, Any]]:  # noqa: N802
        lockers = self.getLockingProcesses()
        current_pid = os.getpid()
        if only_known_children:
            try:
                children = _known_child_pids(current_pid)
                lockers = [item for item in lockers if int(item.get('pid') or 0) in children]
            except Exception as exc:
                recordException('killLockingProcesses.children', exc)
        for item in lockers:
            pid = int(item.get('pid') or 0)
            if pid <= 0 or pid == current_pid:
                continue
            try:
                _run_process(['taskkill', '/PID', str(pid), '/T', '/F'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=15, phase_name='File.killLockingProcesses.taskkill')
            except Exception as exc:
                recordException('killLockingProcesses.taskkill', exc)
        return lockers

    def waitUntilUnlocked(self, timeout: float = 10.0, interval: float = 0.25) -> bool:  # noqa: N802
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            if not self.isLocked():
                return True
            time.sleep(float(interval))
        return not self.isLocked()

    def readText(self, encoding: str = 'utf-8', errors: str = 'replace') -> str:  # noqa: N802
        target = _as_path(self)
        try:
            return target.read_text(encoding=encoding, errors=errors)  # file-wrapper-ok
        except Exception as exc:
            recordException('readText', exc)
            if isinstance(self, File):
                self._fire_read_error(exc)
            return EMPTY_STRING

    managedFileReadText = readText
    tracedReadText = readText

    def readBytes(self) -> bytes:  # noqa: N802
        target = _as_path(self)
        try:
            return target.read_bytes()  # file-wrapper-ok
        except Exception as exc:
            recordException('readBytes', exc)
            if isinstance(self, File):
                self._fire_read_error(exc)
            return b''

    def readLines(self, encoding: str = 'utf-8', errors: str = 'replace') -> list[str]:  # noqa: N802
        return self.readText(encoding=encoding, errors=errors).splitlines()

    def writeText(self, text: Any, encoding: str = 'utf-8', errors: str = 'replace') -> int:  # noqa: N802
        target = _as_path(self)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            return int(target.write_text(str(text or EMPTY_STRING), encoding=encoding, errors=errors))  # file-wrapper-ok
        except Exception as exc:
            recordException('writeText', exc)
            if isinstance(self, File):
                self._fire_write_error(exc)
            return 0

    managedFileWriteText = writeText
    tracedWriteText = writeText

    def writeBytes(self, payload: bytes | bytearray | memoryview) -> int:  # noqa: N802
        target = _as_path(self)
        try:
            data = bytes(payload or b'')
            target.parent.mkdir(parents=True, exist_ok=True)
            return int(target.write_bytes(data))  # file-wrapper-ok
        except Exception as exc:
            recordException('writeBytes', exc)
            if isinstance(self, File):
                self._fire_write_error(exc)
            return 0

    def appendText(self, text: Any, encoding: str = 'utf-8', errors: str = 'replace') -> int:  # noqa: N802  # noqa: nonconform
        target = _as_path(self)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('a', encoding=encoding, errors=errors) as handle:  # file-wrapper-ok
                return int(handle.write(str(text or EMPTY_STRING)))
        except Exception as exc:
            recordException('appendText', exc)
            if isinstance(self, File):
                self._fire_write_error(exc)
            return 0

    def open(self, *args: Any, **kwargs: Any) -> Any:
        target = _as_path(self)
        try:
            mode = str(args[0] if args else kwargs.get('mode', 'r') or 'r')
            if any(flag in mode for flag in ('w', 'a', 'x', '+')):
                target.parent.mkdir(parents=True, exist_ok=True)
            return target.open(*args, **kwargs)  # file-wrapper-ok
        except Exception as exc:
            recordException('open', exc)
            if isinstance(self, File):
                if 'r' in str(args[0] if args else kwargs.get('mode', 'r')):
                    self._fire_read_error(exc)
                else:
                    self._fire_write_error(exc)
            raise

    tracedOpen = open

    @staticmethod
    def copy2(source: Any, target: Any, *args: Any, **kwargs: Any) -> Any:
        File.ensureParent(target)
        return shutil.copy2(source, target, *args, **kwargs)  # file-wrapper-ok

    tracedCopy2 = copy2
    managedCopy2 = copy2

    @staticmethod
    def copyFileObj(source: Any, target: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: N802
        return shutil.copyfileobj(source, target, *args, **kwargs)  # file-wrapper-ok

    tracedCopyFileObj = copyFileObj

    @staticmethod
    def copytree(source: Any, target: Any, *args: Any, **kwargs: Any) -> Any:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        return shutil.copytree(source, target, *args, **kwargs)  # file-wrapper-ok

    tracedCopytree = copytree

    def copyTo(self, target: str | Path) -> Path:  # noqa: N802
        destination = Path(target)
        File.copy2(_as_path(self), destination)
        return destination

    def moveTo(self, target: str | Path) -> bool:  # noqa: N802
        try:
            destination = Path(target)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(_as_path(self)), str(destination))  # file-wrapper-ok
            if isinstance(self, File):
                self.path = destination
            return True
        except Exception as exc:
            recordException('moveTo', exc)
            if isinstance(self, File):
                self._fire_write_error(exc)
            return False

    def delete(self) -> bool:
        try:
            _as_path(self).unlink(missing_ok=True)
            return True
        except Exception as exc:
            recordException('delete', exc)
            if isinstance(self, File):
                self._fire_write_error(exc)
            return False

    def zeroOut(self) -> bool:  # noqa: N802
        try:
            return bool(self.writeBytes(b'\x00' * int(self.size)))
        except Exception as exc:
            recordException('zeroOut', exc)
            if isinstance(self, File):
                self._fire_write_error(exc)
            return False

    def deleteSecure(self) -> bool:  # noqa: N802
        return self.zeroOut() and self.delete()

    def md5Hex(self, chunk_size: int = 1024 * 1024) -> str:  # noqa: N802
        target = _as_path(self)
        try:
            digest = hashlib.md5()
            with target.open('rb') as handle:  # file-wrapper-ok
                while True:  # noqa: badcode reviewed detector-style finding
                    chunk = handle.read(int(chunk_size or 1024 * 1024))
                    if not chunk:
                        break
                    digest.update(chunk)
            return digest.hexdigest()
        except Exception as exc:
            recordException('md5Hex', exc)
            return EMPTY_STRING

    def sha1Hex(self) -> str:  # noqa: N802
        try:
            return hashlib.sha1(self.readBytes()).hexdigest()
        except Exception as exc:
            recordException('sha1Hex', exc)
            return EMPTY_STRING

    def sha256Hex(self) -> str:  # noqa: N802
        try:
            return hashlib.sha256(self.readBytes()).hexdigest()
        except Exception as exc:
            recordException('sha256Hex', exc)
            return EMPTY_STRING

    def base64Encode(self) -> str:  # noqa: N802
        try:
            return base64.b64encode(self.readBytes()).decode('utf-8')
        except Exception as exc:
            recordException('base64Encode', exc)
            return EMPTY_STRING

    def base64Decode(self, encoded: str) -> int:  # noqa: N802
        try:
            return self.writeBytes(base64.b64decode(str(encoded or '').encode('utf-8')))
        except Exception as exc:
            recordException('base64Decode', exc)
            if isinstance(self, File):
                self._fire_write_error(exc)
            return 0

    def __fspath__(self) -> str:
        return str(self.path)

    def __repr__(self) -> str:
        return f'File({str(self.path)!r})'

    def __str__(self) -> str:
        return str(self.path)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, File):
            return self.path == other.path
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.path)


class CSSFile(File):
    def __init__(self, path: str | Path) -> None:
        super().__init__(path, kind='css', language='css', mime='text/css')


class JSFile(File):
    def __init__(self, path: str | Path) -> None:
        super().__init__(path, kind='js', language='javascript', mime='application/javascript')


class HTMLFile(File):
    def __init__(self, path: str | Path) -> None:
        super().__init__(path, kind='html', language='html', mime='text/html')


class AudioFile(File):
    def __init__(self, path: str | Path) -> None:
        super().__init__(path, kind='audio', mime='audio/wav')


class ImageFile(File):
    def __init__(self, path: str | Path) -> None:
        super().__init__(path, kind='image')


class FontFile(File):
    def __init__(self, path: str | Path) -> None:
        super().__init__(path, kind='font')
