#!/usr/bin/env python3
from __future__ import annotations
import atexit
import asyncio
import ast
import base64
import configparser
import contextlib
import glob
import importlib.util
import py_compile
import queue
import json
import html
import os
import re
import signal
import socket
import socketserver
import http.server
import urllib.error
import urllib.parse
import urllib.request
import datetime
import hashlib
import shutil
import shlex
import subprocess
import tempfile
import runpy
import zipfile
import tarfile
import io
import sys
import struct
import zlib
import threading  # thread-ok
import time
import traceback
import faulthandler
import platform
import ctypes.util
from pathlib import Path
from typing import Any, TYPE_CHECKING, cast

from File import File
from DebugLog import DebugLog
from Localization import Localization
from Lifecycle import Lifecycle
from PhaseProcess import PhaseProcess
DebugLog.install(source='start.py')
PROMPT_START_PATCH = 'V226_MERGED_SAFE_DIFFS'

# Audited: 04/29/26 Runtime exception capture surface v163
_PROMPT_EXCEPTION_CAPTURE_PATCH = 'V226_MERGED_SAFE_DIFFS'
_CAPTURE_EXCEPTION_REENTRANT = False

_RUN_LOG_STREAMS_INSTALLED = False
_RUN_LOG_REENTRANT = False


def _prompt_runtime_root_for_log() -> Path:
    raw = str(os.environ.get('PROMPT_RUN_LOG_ROOT', '') or '').strip()
    if raw:
        return Path(raw).expanduser().resolve()
    try:
        if bool(getattr(sys, 'frozen', False)):
            exe_dir = Path(sys.executable).resolve().parent
            if exe_dir.name.lower() == 'dist':
                return exe_dir.parent
            return exe_dir
        return Path(__file__).resolve().parent
    except Exception:  # swallow-ok: run.log fallback cannot depend on app state
        return Path.cwd().resolve()


def promptRunLogPath() -> Path:
    raw = str(os.environ.get('PROMPT_RUN_LOG', '') or '').strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return _prompt_runtime_root_for_log() / 'run.log'


def appendRunLog(message: object) -> None:
    """Append one timestamped line to root-level run.log for hidden-console EXE debugging."""
    global _RUN_LOG_REENTRANT
    if _RUN_LOG_REENTRANT:
        return
    _RUN_LOG_REENTRANT = True
    try:
        path = promptRunLogPath()
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().isoformat(timespec='seconds')
        text = str(message or EMPTY_STRING if 'EMPTY_STRING' in globals() else message or '')
        try:
            DebugLog.writeLine(text, level='RUNLOG', source='start.py', stream='run.log')
        except Exception as error:
            DebugLog.saveExceptionFallback(error, source='start.py', context='appendRunLog:debuglog-write', handled=True)
        with File.tracedOpen(path, 'a', encoding='utf-8', errors='replace') as handle:
            for line in (text.splitlines() or ['']):
                if str(line).strip():
                    handle.write(f'{stamp} [pid={os.getpid()}] {line.rstrip()}\n')
    except Exception as error:  # run.log must never crash startup; persist fallback row
        DebugLog.saveExceptionFallback(error, source='start.py', context='appendRunLog', handled=True)
    finally:
        _RUN_LOG_REENTRANT = False


def openRunLogStream():
    path = promptRunLogPath()
    path.parent.mkdir(parents=True, exist_ok=True)
    return File.tracedOpen(path, 'a', encoding='utf-8', errors='replace')


class _RunLogMirrorStream:
    def __init__(self, original, label: str):
        self.original = original  # noqa: nonconform
        self.label = str(label or 'STREAM')
        self._buffer = ''

    def write(self, text):
        raw = str(text if text is not None else '')
        if raw == '':
            return 0
        # Do not forward blank/control-only chunks to the console or run.log.
        # The child output relay already emits meaningful lines with [child:*]
        # prefixes; forwarding raw newlines here is what created giant blank gaps.
        visible_chunk = DebugLog.lineLooksVisible(raw)
        try:
            if visible_chunk and self.original is not None and hasattr(self.original, 'write'):
                self.original.write(raw)
        except Exception as stream_error:
            appendRunLog(f'[WARN:swallowed-exception] RunLogMirror.write {type(stream_error).__name__}: {stream_error}')
        if not visible_chunk:
            # print() often writes the text and the trailing newline as separate
            # chunks. Suppress repeated blank child-output spam, but preserve the
            # single newline that terminates a visible line.
            if '\n' in raw and DebugLog.lineLooksVisible(self._buffer):
                try:
                    if self.original is not None and hasattr(self.original, 'write'):
                        self.original.write('\n')
                except Exception as newline_error:
                    appendRunLog(f'[WARN:swallowed-exception] RunLogMirror.newline {type(newline_error).__name__}: {newline_error}')
                appendRunLog(f'[{self.label}] {DebugLog.visibleText(self._buffer)}')
                self._buffer = ''
            return len(raw)
        self._buffer += raw
        while '\n' in self._buffer:
            line, self._buffer = self._buffer.split('\n', 1)
            if DebugLog.lineLooksVisible(line):
                appendRunLog(f'[{self.label}] {DebugLog.visibleText(line)}')
        return len(raw)

    def flush(self):
        if self._buffer.strip():
            appendRunLog(f'[{self.label}] {self._buffer.rstrip()}')
            self._buffer = ''
        try:
            if self.original is not None and hasattr(self.original, 'flush'):
                self.original.flush()
        except Exception:  # swallow-ok: mirror flush failure must not hide the real child/build failure.
            # WindowsApps/PowerShell hosts can report OSError(22) during parent
            # relay shutdown.  Do not spam run.log on every child line; debug.log
            # still receives real builder failures from the release process.
            return

    def isatty(self):
        try:
            return bool(self.original is not None and hasattr(self.original, 'isatty') and self.original.isatty())
        except Exception as isatty_error:
            appendRunLog(f'[WARN:swallowed-exception] RunLogMirror.isatty {type(isatty_error).__name__}: {isatty_error}')
            return False

    @property
    def encoding(self):
        return getattr(self.original, 'encoding', 'utf-8') or 'utf-8'

    def fileno(self):
        if self.original is not None and hasattr(self.original, 'fileno'):
            return self.original.fileno()
        raise OSError('stream has no fileno')


def installRunLogMirrors() -> None:
    global _RUN_LOG_STREAMS_INSTALLED
    if _RUN_LOG_STREAMS_INSTALLED or getattr(sys, '_prompt_run_log_streams_installed', False):
        return
    _RUN_LOG_STREAMS_INSTALLED = True
    setattr(sys, '_prompt_run_log_streams_installed', True)
    try:
        if not str(os.environ.get('PROMPT_APPEND_RUN_LOG', '') or '').strip() and not str(os.environ.get('PROMPT_RUN_LOG_TRUNCATED', '') or '').strip():
            try:
                path = promptRunLogPath()
                path.parent.mkdir(parents=True, exist_ok=True)
                with File.tracedOpen(path, 'w', encoding='utf-8', errors='replace') as handle:
                    handle.write('')
                os.environ['PROMPT_RUN_LOG_TRUNCATED'] = '1'
            except Exception as truncate_error:
                DebugLog.exception(truncate_error, source='start.py', context='truncate-run-log', handled=True, save_db=True)
                DebugLog.saveExceptionFallback(truncate_error, source='start.py', context='truncate-run-log:fallback', handled=True)
        appendRunLog('RUNLOG:INSTALL root=' + str(_prompt_runtime_root_for_log()) + ' argv=' + repr(sys.argv))
        sys.stdout = cast(Any, _RunLogMirrorStream(getattr(sys, 'stdout', None), 'stdout'))
        sys.stderr = cast(Any, _RunLogMirrorStream(getattr(sys, 'stderr', None), 'stderr'))
        try:
            fault_path = _prompt_runtime_root_for_log() / 'run_faults.log'
            fault_handle = File.tracedOpen(fault_path, 'a', encoding='utf-8', errors='replace')
            faulthandler.enable(fault_handle, all_threads=True)
            setattr(sys, '_prompt_run_fault_handle', fault_handle)
            appendRunLog('RUNLOG:FAULT-HANDLER path=' + str(fault_path))
        except Exception as fault_error:  # run.log mirror still useful without faulthandler
            captureException(fault_error, source='start.py', context='installRunLogMirrors:faulthandler', handled=True)
            appendRunLog(f'RUNLOG:FAULT-HANDLER-FAILED {type(fault_error).__name__}: {fault_error}')
    except Exception as install_error:
        DebugLog.saveExceptionFallback(install_error, source='start.py', context='installRunLogMirrors', handled=True)
        appendRunLog(f'[WARN:swallowed-exception] installRunLogMirrors {type(install_error).__name__}: {install_error}')


installRunLogMirrors()


# FAST-RUNTIME-DETECTOR v185: --fast bypasses the debugger/bootstrap layers and
# runs the bootstrapped app file directly after proving Qt can import.  This is
# intentionally different from --build: the live runtime detector can now launch
# ``start.py --fast --build --force-rebuild`` and catch bugs caused by build args
# reaching the wrong layer instead of being hidden by start.py's normal early
# build entrypoint.
def _prompt_fast_token(value: object) -> str:
    raw = str(value or '').strip().strip('"\'')
    while raw.startswith('/'):
        raw = '-' + raw[1:]
    return raw.lower().replace('_', '-')


def _prompt_fast_requested(argv: list[str] | None = None) -> bool:
    aliases = {'fast', '--fast', '-fast', '/fast', 'fast-run', '--fast-run', 'fast-bootstrap', '--fast-bootstrap'}
    return any(_prompt_fast_token(token) in aliases for token in list(argv or sys.argv[1:]))


def _prompt_strip_fast_args(argv: list[str]) -> list[str]:
    aliases = {'fast', '--fast', '-fast', '/fast', 'fast-run', '--fast-run', 'fast-bootstrap', '--fast-bootstrap'}
    out: list[str] = []
    for token in list(argv or []):
        if _prompt_fast_token(token) in aliases:
            continue
        out.append(str(token))
    return out


def _prompt_fast_offscreen_requested(argv: list[str] | None = None) -> bool:
    tokens = {_prompt_fast_token(token) for token in list(argv or sys.argv[1:])}
    return bool(tokens.intersection({'offscreen', '--offscreen', '-offscreen', '/offscreen', 'xdummy', '--xdummy', 'xpra', '--xpra'}))


def _prompt_fast_configure_headless_environment(argv: list[str] | None = None) -> None:
    if _prompt_fast_offscreen_requested(argv) or (os.name != 'nt' and not str(os.environ.get('DISPLAY', '') or '').strip()):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        os.environ.setdefault('TRIO_OFFSCREEN_ACTIVE', '1')
        os.environ.setdefault('TRIO_MANAGED_DISPLAY_ACTIVE', '1')
        os.environ.setdefault('PROMPT_FAST_OFFSCREEN', '1')
    if os.name != 'nt' and str(os.environ.get('QT_QPA_PLATFORM', '') or '').strip().lower() == 'offscreen':
        os.environ.setdefault('QTWEBENGINE_DISABLE_SANDBOX', '1')
    existing_flags = str(os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', '') or '').strip()
    for flag in ('--disable-gpu', '--disable-features=CalculateNativeWinOcclusion', '--no-sandbox'):
        if flag not in existing_flags:
            existing_flags = (existing_flags + ' ' + flag).strip()
    os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = existing_flags


def _prompt_fast_import_qt_modules() -> None:
    # Import explicit modules instead of only PySide6 so missing QtWebEngine or
    # QtWidgets breaks here with a detector-visible traceback.
    from PySide6.QtCore import QTimer  # noqa: F401
    from PySide6.QtWidgets import QApplication  # noqa: F401
    from PySide6.QtWebEngineCore import QWebEnginePage  # noqa: F401
    from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: F401


def _prompt_fast_pip_timeout_seconds() -> int:
    raw = str(os.environ.get('PROMPT_PIP_INSTALL_TIMEOUT_SECONDS', '') or '').strip()
    try:
        value = int(raw) if raw else 7200
    except ValueError:
        value = 7200
    return max(900, value)


def _prompt_fast_pip_retry_count() -> int:
    raw = str(os.environ.get('PROMPT_PIP_INSTALL_RETRIES', '') or '').strip()
    try:
        value = int(raw) if raw else 2
    except ValueError:
        value = 2
    return max(1, value)


def _prompt_fast_install_package(package: str, *, module: str, reason: str) -> None:
    """Install a fast-path runtime dependency with bounded retries.

    The live runtime detector deliberately runs ``start.py --fast`` so it can
    exercise the bootstrapped app path without the full launcher masking errors.
    That path still has to repair the small runtime deps the app imports before
    Qt can open; otherwise the detector only reports local environment drift.
    """
    if str(os.environ.get('PROMPT_DISABLE_AUTO_PIP_INSTALL', '') or '').strip().lower() in {'1', 'true', 'yes', 'on'}:
        raise ModuleNotFoundError(f'{module} is missing and PROMPT_DISABLE_AUTO_PIP_INSTALL is set')
    timeout = _prompt_fast_pip_timeout_seconds()
    retries = _prompt_fast_pip_retry_count()
    for attempt in range(1, retries + 1):
        print(f'[FAST:DEP-INSTALL] module={module} package={package} reason={reason} attempt={attempt}/{retries} timeout={timeout}s', file=sys.stderr, flush=True)
        appendRunLog(f'[FAST:DEP-INSTALL] module={module} package={package} reason={reason} attempt={attempt}/{retries} timeout={timeout}s')
        try:
            completed = PhaseProcess.run([sys.executable, '-m', 'pip', 'install', package], timeout=timeout, check=False, capture_output=True, text=True, encoding='utf-8', errors='replace', phase_name=f'prompt-fast-install-{module}')
            rc = int(getattr(completed, 'returncode', 1) or 0)
            stdout_text = str(getattr(completed, 'stdout', '') or '')[-4000:]
            stderr_text = str(getattr(completed, 'stderr', '') or '')[-4000:]
            if stdout_text.strip():
                appendRunLog(f'[FAST:DEP-INSTALL:STDOUT-TAIL module={module}] ' + stdout_text)
            if stderr_text.strip():
                appendRunLog(f'[FAST:DEP-INSTALL:STDERR-TAIL module={module}] ' + stderr_text)
            if rc == 0:
                importlib.import_module(module)
                print(f'[FAST:DEP-INSTALL] module={module} import OK after install', flush=True)
                appendRunLog(f'[FAST:DEP-INSTALL] module={module} import OK after install')
                return
            print(f'[FAST:DEP-INSTALL:WARN] module={module} pip exited {rc}', file=sys.stderr, flush=True)
        except BaseException as install_error:
            DebugLog.exception(install_error, source='start.py', context=f'fast-bootstrap-install-{module}-attempt-{attempt}', handled=True, save_db=True)
            print(f'[FAST:DEP-INSTALL:WARN] module={module} attempt {attempt}/{retries} failed: {type(install_error).__name__}: {install_error}', file=sys.stderr, flush=True)
        if attempt < retries:
            print(f'[FAST:DEP-INSTALL] module={module} retrying; pip should reuse cached/resumed package work', file=sys.stderr, flush=True)
            appendRunLog(f'[FAST:DEP-INSTALL] module={module} retrying; pip should reuse cached/resumed package work')
    raise ModuleNotFoundError(f'{module} is still missing after {retries} install attempt(s)')


def _prompt_fast_ensure_import(module: str, package: str | None = None, *, reason: str = 'fast-runtime') -> None:
    try:
        importlib.import_module(module)
        appendRunLog(f'[FAST:DEP] module={module} import OK')
        return
    except ModuleNotFoundError as error:
        missing_top = str(getattr(error, 'name', '') or '').split('.')[0]
        expected_top = str(module or '').split('.')[0]
        if missing_top != expected_top:
            raise
        _prompt_fast_install_package(package or module, module=module, reason=reason)


def _prompt_fast_import_app_runtime_dependencies() -> None:
    # prompt_app imports SQLAlchemy at module import time.  The normal launcher
    # can repair deps before app launch, but --fast intentionally bypasses that
    # launcher.  Repair SQLAlchemy here so the live detector reaches the real
    # GUI/build-arg path instead of stopping at a local missing package.
    _prompt_fast_ensure_import('sqlalchemy', 'SQLAlchemy', reason='prompt_app-import')



def _prompt_fast_build_requested(argv: list[str] | None = None) -> bool:
    tokens = {_prompt_fast_token(token) for token in list(argv or [])}
    build_aliases = {
        'build', '--build', '-build',
        'rebuild', '--rebuild', '-rebuild',
        'force-rebuild', '--force-rebuild', '-force-rebuild',
        'package', '--package', '-package',
        'packages', '--packages', '-packages',
        'installer', '--installer', '-installer',
        'installers', '--installers', '-installers',
    }
    return bool(tokens.intersection(build_aliases))


def _prompt_fast_run_build_path(root: Path, passthrough: list[str]) -> int:
    runner = root / 'tools' / 'run_prompt_release.py'
    if not runner.exists():
        print('[FAST:BUILD:FAILED] missing release runner=' + str(runner), file=sys.stderr, flush=True)
        return 91
    build_args = [str(item) for item in passthrough if _prompt_fast_token(item) not in {'offscreen', 'xdummy', 'xpra', 'gui', 'native-window'}]
    if not _prompt_fast_build_requested(build_args):
        build_args.insert(0, '--build')
    cmd = [sys.executable, str(runner), '--root', str(root), *build_args]
    timeout_raw = str(os.environ.get('PROMPT_FAST_BUILD_TIMEOUT_SECONDS', '') or '').strip()
    try:
        timeout = int(timeout_raw) if timeout_raw else 7200
    except ValueError:
        timeout = 7200
    print('[FAST:BUILD] command=' + ' '.join(repr(part) for part in cmd), flush=True)
    appendRunLog('[FAST:BUILD] command=' + ' '.join(repr(part) for part in cmd))
    completed = PhaseProcess.run(cmd, cwd=str(root), timeout=max(60, timeout), check=False, text=True, encoding='utf-8', errors='replace', stdout=subprocess.PIPE, stderr=subprocess.STDOUT, phase_name='prompt-fast-build-passthrough')
    output = str(getattr(completed, 'stdout', '') or '')
    rc = int(getattr(completed, 'returncode', 1) or 0)
    if output.strip():
        if rc == 0:
            line_count = len(output.splitlines())
            appendRunLog(f'[FAST:BUILD:OUTPUT-SUMMARY] rc=0 lines={line_count} full_output_suppressed=1')
            print(f'[FAST:BUILD:OUTPUT-SUMMARY] rc=0 lines={line_count} full_output_suppressed=1', flush=True)
        else:
            tail = output[-12000:]
            appendRunLog('[FAST:BUILD:OUTPUT-TAIL]\n' + tail)
            print(tail, flush=True)
    print(f'[FAST:BUILD:DONE] rc={rc}', flush=True)
    appendRunLog(f'[FAST:BUILD:DONE] rc={rc}')
    return rc

def _prompt_fast_attempt_qt_install(first_error: BaseException) -> None:
    if str(os.environ.get('PROMPT_DISABLE_AUTO_PIP_INSTALL', '') or '').strip().lower() in {'1', 'true', 'yes', 'on'}:
        raise first_error
    timeout = _prompt_fast_pip_timeout_seconds()
    retries = _prompt_fast_pip_retry_count()
    for attempt in range(1, retries + 1):
        print(f'[FAST:QT-INSTALL] PySide6 missing; pip install attempt {attempt}/{retries} timeout={timeout}s', file=sys.stderr, flush=True)
        appendRunLog(f'[FAST:QT-INSTALL] PySide6 missing; pip install attempt {attempt}/{retries} timeout={timeout}s')
        try:
            completed = PhaseProcess.run([sys.executable, '-m', 'pip', 'install', 'PySide6'], timeout=timeout, check=False, capture_output=True, text=True, encoding='utf-8', errors='replace', phase_name='prompt-fast-install-pyside6')
            rc = int(getattr(completed, 'returncode', 1) or 0)
            stdout_text = str(getattr(completed, 'stdout', '') or '')[-4000:]
            stderr_text = str(getattr(completed, 'stderr', '') or '')[-4000:]
            if stdout_text.strip():
                appendRunLog('[FAST:QT-INSTALL:STDOUT-TAIL] ' + stdout_text)
            if stderr_text.strip():
                appendRunLog('[FAST:QT-INSTALL:STDERR-TAIL] ' + stderr_text)
            if rc == 0:
                _prompt_fast_import_qt_modules()
                print('[FAST:QT-INSTALL] PySide6 import OK after install', flush=True)
                appendRunLog('[FAST:QT-INSTALL] PySide6 import OK after install')
                return
            print(f'[FAST:QT-INSTALL:WARN] pip exited {rc}', file=sys.stderr, flush=True)
        except BaseException as install_error:
            DebugLog.exception(install_error, source='start.py', context=f'fast-bootstrap-pyside-install-attempt-{attempt}', handled=True, save_db=True)
            print(f'[FAST:QT-INSTALL:WARN] attempt {attempt}/{retries} failed: {type(install_error).__name__}: {install_error}', file=sys.stderr, flush=True)
        if attempt < retries:
            print('[FAST:QT-INSTALL] retrying; pip should reuse cached/resumed package work', file=sys.stderr, flush=True)
            appendRunLog('[FAST:QT-INSTALL] retrying; pip should reuse cached/resumed package work')
    raise first_error


def _prompt_fast_import_qt() -> None:
    """Import the Qt pieces Prompt actually needs before the fast handoff."""
    os.environ.setdefault('QT_OPENGL', 'software')
    existing_flags = str(os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', '') or '').strip()
    for flag in ('--disable-gpu', '--disable-features=CalculateNativeWinOcclusion'):
        if flag not in existing_flags:
            existing_flags = (existing_flags + ' ' + flag).strip()
    os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = existing_flags
    try:
        _prompt_fast_import_qt_modules()
    except ModuleNotFoundError as error:
        if str(getattr(error, 'name', '') or '').split('.')[0] == 'PySide6':
            _prompt_fast_attempt_qt_install(error)
            return
        raise


def _prompt_fast_bootstrap_entrypoint() -> None:
    if not _prompt_fast_requested(sys.argv[1:]):
        return
    root = Path(__file__).resolve().parent
    target = root / 'prompt_app.py'
    passthrough = _prompt_strip_fast_args([str(item) for item in sys.argv[1:]])
    os.environ['PROMPT_FAST_BOOTSTRAP'] = '1'
    _prompt_fast_configure_headless_environment(passthrough)
    os.environ.setdefault('PROMPT_RUN_LOG_ROOT', str(root))
    os.environ.setdefault('PROMPT_RUN_LOG', str(promptRunLogPath()))
    os.environ.setdefault('PROMPT_DEBUG_LOG', str(DebugLog.debugLogPath()))
    os.environ.setdefault('PYTHONDONTWRITEBYTECODE', '1')
    print('[FAST:START] target=' + str(target) + ' args=' + repr(passthrough), flush=True)
    appendRunLog('[FAST:START] target=' + str(target) + ' args=' + repr(passthrough))
    try:
        _prompt_fast_import_qt()
        print('[FAST:QT] imports OK', flush=True)
        appendRunLog('[FAST:QT] imports OK')
        _prompt_fast_import_app_runtime_dependencies()
        print('[FAST:DEPS] app runtime imports OK', flush=True)
        appendRunLog('[FAST:DEPS] app runtime imports OK')
        if _prompt_fast_build_requested(passthrough):
            raise SystemExit(_prompt_fast_run_build_path(root, passthrough))
    except SystemExit:
        raise
    except BaseException as exc:
        context = 'fast-bootstrap-build' if _prompt_fast_build_requested(passthrough) else 'fast-bootstrap-qt-import'
        DebugLog.exception(exc, source='start.py', context=context, handled=False, extra='target=' + str(target), save_db=True)
        print(f'[FAST:FAILED] {context} failed: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        raise SystemExit(88)
    if not target.exists():
        print('[FAST:FAILED] missing bootstrapped target=' + str(target), file=sys.stderr, flush=True)
        raise SystemExit(89)
    sys.argv = [str(target), *passthrough]
    try:
        runpy.run_path(str(target), run_name='__main__')
    except SystemExit:
        raise
    except BaseException as exc:
        DebugLog.exception(exc, source='start.py', context='fast-bootstrap-run-target', handled=False, extra='argv=' + repr(sys.argv), save_db=True)
        print(f'[FAST:FAILED] target crashed: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        traceback.print_exc()
        raise SystemExit(90)


_prompt_fast_bootstrap_entrypoint()


def _prompt_log_invocation_mode() -> None:
    try:
        root = Path(__file__).resolve().parent
        argv = [str(item) for item in sys.argv[1:]]
        mode = 'build' if _prompt_early_build_requested(argv) else 'runtime'
        DebugLog.stage('PROMPT INVOCATION', f'mode={mode} root={root} argv={argv!r} python={sys.executable} version={sys.version.split()[0]}', source='start.py')
        artifact_dir = root / 'dist'
        if root.name.lower() == 'dist':
            DebugLog.stage('PROMPT ROOT WARNING', f'start.py is running from a folder named dist: {root}. The required repo shape is start.py at the repo root and dist/ as the artifact-only output shelf.', source='start.py', level='WARN', stream='stderr')
        else:
            DebugLog.stage('PROMPT ARTIFACT SHELF', f'build artifacts will be written only to {artifact_dir}; --build creates EXEs, --build package creates installers', source='start.py')
    except Exception as mode_error:
        captureException(mode_error, source='start.py', context='prompt-log-invocation-mode', handled=True)
        appendRunLog(f'[WARN:swallowed-exception] prompt invocation mode trace failed {type(mode_error).__name__}: {mode_error}')



def _prompt_early_build_requested(argv: list[str] | None = None) -> bool:
    tokens = [str(item or '').strip().lower() for item in list(argv or sys.argv[1:]) if str(item or '').strip()]
    build_aliases = {'build', '--build', '-build', '/build', 'rebuild', '--rebuild', '--force-build', '--force-rebuild'}
    return any(token in build_aliases for token in tokens)


def _prompt_early_has_force(argv: list[str] | None = None) -> bool:
    tokens = [str(item or '').strip().lower() for item in list(argv or sys.argv[1:]) if str(item or '').strip()]
    force_aliases = {'--force-rebuild', '-force-rebuild', '/force-rebuild', 'force-rebuild', '--rebuild', '-rebuild', '/rebuild', 'rebuild'}
    return any(token in force_aliases for token in tokens)


def _prompt_early_has_explicit_build(argv: list[str] | None = None) -> bool:
    tokens = [str(item or '').strip().lower() for item in list(argv or sys.argv[1:]) if str(item or '').strip()]
    explicit_build_aliases = {'build', '--build', '-build', '/build'}
    return any(token in explicit_build_aliases for token in tokens)


def _prompt_early_claude_detector_requested(argv: list[str] | None = None) -> bool:
    """Return True when CLI requested a detector, so build flags can be passed through.

    Without this guard, a command such as
    ``python start.py --live-run-error-detector -- --build --force-rebuild``
    is stolen by the early build path before the live detector can launch the
    build process under observation.
    """
    detector_aliases = {
        'claude-detectors', '--claude-detectors', '-claude-detectors', '/claude-detectors',
        'all-detectors', '--all-detectors', '--claude-detector-suite', 'certify', '--certify',
        'live', '--live', 'live-run', '--live-run', 'live-run-error', '--live-run-error',
        'live-run-error-detector', '--live-run-error-detector', 'runtime', '--runtime',
        'runtime-errors', '--runtime-errors',
        'bypass-detector', '--bypass-detector', 'lifecycle-bypass-detector', '--lifecycle-bypass-detector',
        'monkeypatch-detector', '--monkeypatch-detector', 'rawsql-detector', '--rawsql-detector',
        'recursion-detector', '--recursion-detector', 'redundant-detector', '--redundant-detector',
        'swallowed-exceptions-detector', '--swallowed-exceptions-detector', 'swallow-detector', '--swallow-detector',
        'fileio-detector', '--fileio-detector', 'processfaults-detector', '--processfaults-detector',
        'phaseownership-detector', '--phaseownership-detector', 'thread-detector', '--thread-detector',
        'badcode-detector', '--badcode-detector', 'unlocalized-detector', '--unlocalized-detector',
        'depcheck', '--depcheck', 'phasehooks', '--phasehooks', 'nonconform', '--nonconform', 'comport', '--comport', 'brokenimports', '--brokenimports', 'broken-imports', '--broken-imports', 'astscan', '--astscan', 'ast-scan', '--ast-scan', 'deadcode', '--deadcode', 'dead-code', '--dead-code',
    }
    for raw in list(argv or sys.argv[1:]):
        lowered = str(raw or '').strip().lower()
        if not lowered or lowered == '--':
            continue
        if lowered in detector_aliases or lowered.endswith('-detector') or lowered.endswith('_detector'):
            return True
    return False


def _prompt_early_has_offscreen(argv: list[str] | None = None) -> bool:
    tokens = {_prompt_fast_token(item) for item in list(argv or [])}
    return bool(tokens.intersection({'offscreen', '--offscreen', '-offscreen', '/offscreen', 'xdummy', '--xdummy', '-xdummy', '/xdummy', 'xpra', '--xpra', '-xpra', '/xpra'}))


def _prompt_early_normalize_build_argv(argv: list[str]) -> list[str]:
    normalized = [str(item) for item in list(argv or [])]
    if not _prompt_early_has_explicit_build(normalized):
        normalized.insert(0, '--build')
        print('[BUILD:EARLY] Added --build because a build-only flag was used without an explicit --build token.', flush=True)
    if not _prompt_early_has_offscreen(normalized):
        normalized.append('--offscreen')
        print('[BUILD:EARLY] Added --offscreen because build mode must never open a GUI window.', flush=True)
    if not _prompt_early_has_force(normalized):
        normalized.append('--force-rebuild')
        print('[BUILD:EARLY] Added --force-rebuild because plain --build must rebuild artifacts.', flush=True)
    return normalized


def _prompt_early_python_version(command: list[str]) -> tuple[int, int, int] | None:
    try:
        completed = PhaseProcess.run(
            command + ['-c', 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}")'],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=8,
            check=False,
            phase_name='prompt-early-python-version',
            window_policy='helper-no-window',
        )
        if int(getattr(completed, 'returncode', 1) or 0) != 0:
            return None
        raw = str(getattr(completed, 'stdout', '') or '').strip().splitlines()[-1:]
        if not raw:
            return None
        pieces = [int(part) for part in raw[0].strip().split('.')[:3]]
        while len(pieces) < 3:
            pieces.append(0)
        return (pieces[0], pieces[1], pieces[2])
    except Exception as error:
        captureException(error, source='start.py', context='prompt-build-python-version', handled=True, extra=f'command={command!r}')
        appendRunLog(f'[BUILD:PYTHON-VERSION:FAILED] command={command!r} {type(error).__name__}: {error}')
        return None


def _prompt_early_probe_python(command: list[str], *, build_safe: bool = True) -> bool:
    version = _prompt_early_python_version(command)
    if version is None:
        return False
    if version < (3, 10, 0):
        appendRunLog(f'[BUILD:PYTHON-PROBE:SKIP] command={command!r} version={version} reason=too-old')
        return False
    if build_safe and version >= (3, 14, 0):
        appendRunLog(f'[BUILD:PYTHON-PROBE:SKIP] command={command!r} version={version} reason=python-3.14-not-safe-for-nuitka-build')
        return False
    return True


def _prompt_early_build_python_command() -> list[str]:
    """Prefer a real build-safe Python so --build does not use WindowsApps or 3.14.

    Nuitka currently supports Python through 3.13, so the default all-EXE build
    must prefer 3.13/3.12/3.11 even when the Windows ``python`` alias points at
    3.14.  If only 3.14 exists we still return the current interpreter so
    PyInstaller can report a useful failure/partial result instead of opening a
    dead helper window and vanishing with no build log.
    """
    env_keys = ('PROMPT_BUILD_PYTHON', 'PROMPT_PYTHON', 'PYTHON312', 'PYTHON_EXE')
    fallback: list[str] | None = None
    for key in env_keys:
        raw = str(os.environ.get(key, '') or '').strip().strip('"')
        if raw:
            candidate = Path(raw).expanduser()
            command = [str(candidate)]
            if candidate.exists() and _prompt_early_probe_python(command, build_safe=True):
                return command
            if candidate.exists() and _prompt_early_probe_python(command, build_safe=False):
                fallback = fallback or command
            appendRunLog(f'[BUILD:PYTHON-PROBE:SKIP] env={key} path={raw} reason=missing-unusable-or-not-build-safe')
    if os.name == 'nt':
        candidates = [
            r'C:\Python313\python.exe',
            r'C:\Python312\python.exe',
            r'C:\Python311\python.exe',
            r'C:\Python310\python.exe',
        ]
        localapp = os.environ.get('LOCALAPPDATA', '') or ''
        if localapp:
            candidates.extend([
                str(Path(localapp) / 'Programs' / 'Python' / 'Python313' / 'python.exe'),
                str(Path(localapp) / 'Programs' / 'Python' / 'Python312' / 'python.exe'),
                str(Path(localapp) / 'Programs' / 'Python' / 'Python311' / 'python.exe'),
                str(Path(localapp) / 'Programs' / 'Python' / 'Python310' / 'python.exe'),
            ])
        for raw in candidates:
            candidate = Path(raw)
            command = [str(candidate)]
            if candidate.exists() and _prompt_early_probe_python(command, build_safe=True):
                return command
            if candidate.exists() and _prompt_early_probe_python(command, build_safe=False):
                fallback = fallback or command
        py_launcher = shutil.which('py') or shutil.which('py.exe')
        if py_launcher:
            for version in ('-3.13', '-3.12', '-3.11', '-3.10'):
                command = [py_launcher, version]
                if _prompt_early_probe_python(command, build_safe=True):
                    return command
                if _prompt_early_probe_python(command, build_safe=False):
                    fallback = fallback or command
    current = [str(sys.executable or 'python')]
    if _prompt_early_probe_python(current, build_safe=True):
        return current
    if fallback:
        appendRunLog(f'[BUILD:PYTHON-PROBE:WARN] using fallback python={fallback!r}; Nuitka may be skipped/fail if version is 3.14+')
        return fallback
    appendRunLog(f'[BUILD:PYTHON-PROBE:WARN] no build-safe Python 3.10-3.13 found; using current={current!r}')
    return current


def _prompt_early_stream_release_command(command: list[str], *, root: Path, env: dict[str, str]) -> int:
    """Run the release builder hidden, tee output, and keep parent-side fault evidence.

    A plain ``for line in proc.stdout`` can block forever when the release child
    stops producing output but is still alive or wedged inside a native builder.
    Use a reader thread plus a polling loop so the parent writes heartbeats, raw
    log tail, exit code, and hard-fault context even when the child is silent.
    """
    raw_path = root / 'logs' / 'early_build_release.raw.log'
    fault_path = root / 'logs' / 'early_build_faults.log'

    def _raw_tail(path: Path, *, lines: int = 180, chars: int = 14000) -> str:
        try:
            text = File(path).readText()
            return '\n'.join(text.splitlines()[-max(1, int(lines or 1)):])[-max(1000, int(chars or 1000)):]
        except Exception:
            return ''

    def _fault(message: str, *, proc_obj=None, exc: BaseException | None = None, rc: int | None = None) -> None:
        try:
            pid_text = str(getattr(proc_obj, 'pid', '') if proc_obj is not None else '')
            trace = ''
            if exc is not None:
                try:
                    trace = ''.join(traceback.format_exception(type(exc), exc, getattr(exc, '__traceback__', None)))
                except Exception:
                    trace = f'{type(exc).__name__}: {exc}'
            tail = _raw_tail(raw_path)
            block = f'[BUILD:EARLY:FAULT] rc={rc if rc is not None else ""} pid={pid_text} raw_log={raw_path} message={message}'
            if trace:
                block += '\nTRACEBACK:\n' + trace.rstrip()
            if tail:
                block += '\nRAW-TAIL-BEGIN\n' + tail.rstrip() + '\nRAW-TAIL-END'
            print(block, file=sys.stderr, flush=True)
            appendRunLog(block)
            for path in (fault_path, DebugLog.debugLogPath()):
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with File.tracedOpen(path, 'a', encoding='utf-8', errors='replace') as handle:
                        handle.write(datetime.datetime.now().isoformat(timespec='seconds') + ' ' + block + '\n')
                except Exception:
                    pass
        except Exception as fault_error:
            captureException(fault_error, source='start.py', context='early-build-fault-logger', handled=True)

    try:
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        with File.tracedOpen(raw_path, 'w', encoding='utf-8', errors='replace') as handle:
            handle.write('')
        with File.tracedOpen(fault_path, 'w', encoding='utf-8', errors='replace') as handle:
            handle.write('')
    except Exception as reset_error:
        captureException(reset_error, source='start.py', context='early-build-raw-log-reset', handled=True)
    print(f'[BUILD:EARLY] raw release log: {raw_path}', flush=True)
    appendRunLog('[BUILD:EARLY] raw release log=' + str(raw_path))
    started = time.monotonic()
    proc = None
    q = queue.Queue()
    reader_done = threading.Event()
    line_count = 0
    last_line = ''
    try:
        proc = PhaseProcess.popen(
            command,
            cwd=str(root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            stdin=subprocess.DEVNULL,
            phase_name='prompt-early-build-entrypoint',
            window_policy='helper-no-window',
            windowDelaySeconds=0.2,
        )
        assert proc.stdout is not None

        def _reader() -> None:
            try:
                for raw in proc.stdout:
                    q.put(str(raw or ''))
            except BaseException as read_error:
                q.put(('__reader_error__', read_error))
            finally:
                reader_done.set()

        reader_thread = threading.Thread(target=_reader, name='prompt-release-output-reader', daemon=True)
        reader_thread.start()
        next_heartbeat = started + 20.0
        while True:
            try:
                item = q.get(timeout=0.25)
            except queue.Empty:
                item = None
            if isinstance(item, tuple) and item and item[0] == '__reader_error__':
                _fault('release child stdout reader failed', proc_obj=proc, exc=item[1])
            elif isinstance(item, str):
                line = item.rstrip('\r\n')
                if line.strip():
                    line_count += 1
                    last_line = line
                    try:
                        with File.tracedOpen(raw_path, 'a', encoding='utf-8', errors='replace') as handle:
                            handle.write(line + '\n')
                    except Exception as write_error:
                        captureException(write_error, source='start.py', context='early-build-raw-log-write', handled=True)
                    print(line, flush=True)
                    appendRunLog('[BUILD:RELEASE] ' + line)
            now = time.monotonic()
            if now >= next_heartbeat:
                raw_bytes = 0
                try:
                    raw_bytes = int(raw_path.stat().st_size)
                except Exception:
                    pass
                alive = proc.poll() is None
                heartbeat = f'[BUILD:EARLY] release heartbeat pid={getattr(proc, "pid", "")} alive={alive} elapsed={int(now-started)}s lines={line_count} raw_bytes={raw_bytes} last={last_line[-240:]}'
                print(heartbeat, flush=True)
                appendRunLog(heartbeat)
                try:
                    with File.tracedOpen(raw_path, 'a', encoding='utf-8', errors='replace') as handle:
                        handle.write(heartbeat + '\n')
                except Exception:
                    pass
                next_heartbeat = now + 20.0
            if proc.poll() is not None and reader_done.is_set() and q.empty():
                break
        rc = int(proc.wait(timeout=30) or 0)
        elapsed = int(time.monotonic() - started)
        line = f'[BUILD:EARLY] release command exited rc={rc} elapsed={elapsed}s lines={line_count} raw_log={raw_path} last={last_line[-240:]}'
        print(line, flush=True)
        appendRunLog(line)
        try:
            with File.tracedOpen(raw_path, 'a', encoding='utf-8', errors='replace') as handle:
                handle.write(line + '\n')
        except Exception:
            pass
        if rc != 0:
            _fault('release command exited non-zero', proc_obj=proc, rc=rc)
        return rc
    except KeyboardInterrupt as interrupt:
        captureException(interrupt, source='start.py', context='early-build-keyboardinterrupt', handled=False)
        _fault('early build interrupted; possible watcher stop, console close, or user interrupt', proc_obj=proc, exc=interrupt, rc=130)
        if proc is not None:
            with contextlib.suppress(Exception):
                PhaseProcess._kill_process_tree(proc, reason='early-build-keyboardinterrupt')
        return 130
    except BaseException as error:
        captureException(error, source='start.py', context='early-build-entrypoint-stream', handled=False)
        _fault(f'early build stream failed: {type(error).__name__}: {error}', proc_obj=proc, exc=error, rc=1)
        if proc is not None:
            with contextlib.suppress(Exception):
                PhaseProcess._kill_process_tree(proc, reason='early-build-stream-error')
        return 1


def _prompt_early_build_entrypoint() -> None:
    """Run --build before ORM/GUI imports unless a detector is wrapping that build."""
    if _prompt_early_claude_detector_requested(sys.argv[1:]):
        return
    if not _prompt_early_build_requested(sys.argv[1:]):
        return
    root = Path(__file__).resolve().parent
    runner = root / 'tools' / 'run_prompt_release.py'
    DebugLog.stage('BUILD EARLY ENTRYPOINT', f'--build detected; root={root}; runner={runner}', source='start.py')
    print(f'[BUILD:EARLY] Python current={sys.executable} version={sys.version.split()[0]}', flush=True)
    if sys.version_info[:2] >= (3, 14):
        print('[BUILD:EARLY:WARN] Python 3.14 detected. Prompt packaging is safer with Python 3.12/3.11 because PySide6/PyInstaller wheels may lag new Python releases. Set PROMPT_BUILD_PYTHON=C:\\Python312\\python.exe or install/use py -3.12 if dependency imports fail.', file=sys.stderr, flush=True)
    if not runner.exists():
        print(f'[BUILD:FAILED] Missing release runner: {runner}', file=sys.stderr, flush=True)
        raise SystemExit(2)
    argv = _prompt_early_normalize_build_argv([str(item) for item in sys.argv[1:]])
    command = _prompt_early_build_python_command() + ['-m', 'tools.run_prompt_release', '--root', str(root), *argv]
    env = dict(os.environ)
    env['PROMPT_RELEASE_ROOT'] = str(root)
    env['PROMPT_RUN_LOG_ROOT'] = str(root)
    env['PROMPT_RUN_LOG'] = str(promptRunLogPath())
    env['PROMPT_DEBUG_LOG'] = str(DebugLog.debugLogPath())
    env['PROMPT_RUN_LOG_TRUNCATED'] = os.environ.get('PROMPT_RUN_LOG_TRUNCATED', '1')
    env['PROMPT_DEBUG_LOG_TRUNCATED'] = os.environ.get('PROMPT_DEBUG_LOG_TRUNCATED', '1')
    # Parent streams child output into run.log.  Do not let the release child
    # open root run.log directly on Windows; that produced PermissionError
    # spam and hid the useful build evidence.
    env['PROMPT_RELEASE_PARENT_STREAMED'] = '1'
    env['PROMPT_BUILD_MODE'] = '1'
    env['PROMPT_CLI_ONLY'] = '1'
    env['PROMPT_NO_GUI_DURING_BUILD'] = '1'
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    # The build runner itself uses CREATE_NO_WINDOW for subprocesses.  Disable
    # fail-fast dead-window killing inside the packager process so a false
    # positive cannot silently murder a long EXE build.
    env.setdefault('PROMPT_WINDOW_MONITOR', os.environ.get('PROMPT_BUILD_WINDOW_MONITOR', '0'))
    # Do not pin PROMPT_BUILD_PYTHON to the release-runner interpreter.
    # The release runner needs freedom to choose per-backend Python builds,
    # and pinning this to Python 3.14 kept py2exe/Nuitka from probing safer
    # interpreters.  It can still read sys.executable for its own launcher.
    print('[BUILD:EARLY] Running release command: ' + ' '.join(shlex.quote(part) for part in command), flush=True)
    appendRunLog('[BUILD:EARLY] command=' + ' '.join(shlex.quote(part) for part in command))
    rc = _prompt_early_stream_release_command(command, root=root, env=env)
    raise SystemExit(int(rc or 0))


_prompt_log_invocation_mode()
_prompt_early_build_entrypoint()


def _capture_exception_log_path() -> Path:
    try:
        base = Path(__file__).resolve().parent
    except Exception:  # swallow-ok: emergency exception recorder must never recurse
        base = Path.cwd()
    try:
        log_dir = globals().get('LOG_DIR') or (base / 'logs')
        return Path(log_dir) / 'exceptions.log'
    except Exception:  # swallow-ok: emergency exception recorder must never recurse
        return base / 'logs' / 'exceptions.log'


def _capture_exception_db_path() -> Path:
    raw = str(os.environ.get('TRIO_SQLITE_PATH', '') or '').strip()
    if raw:
        return Path(raw).expanduser()
    if os.name == 'nt':
        appdata = os.environ.get('APPDATA') or os.environ.get('LOCALAPPDATA') or str(Path.home() / 'AppData' / 'Roaming')
        return Path(appdata) / 'TrioDesktop' / 'triodesktop.db'
    return Path(__file__).resolve().parent / 'workspaces' / 'prompt_debugger.sqlite3'


def captureException(exc: BaseException | None = None, *, source: str = 'start.py', context: str = '', handled: bool = True, extra: str = '') -> int:
    """Trace and persist an exception so the debugger can read it from the exceptions table."""
    global _CAPTURE_EXCEPTION_REENTRANT
    if _CAPTURE_EXCEPTION_REENTRANT:
        return 0
    _CAPTURE_EXCEPTION_REENTRANT = True
    row_id = 0
    try:
        if exc is None:
            exc = sys.exc_info()[1]
        type_name = type(exc).__name__ if exc is not None else 'UnknownException'
        message = str(exc or '')
        tb = getattr(exc, '__traceback__', None) if exc is not None else sys.exc_info()[2]
        traceback_text = ''.join(traceback.format_exception(type(exc), exc, tb)) if exc is not None else ''.join(traceback.format_stack())
        created = datetime.datetime.now().isoformat(sep=' ', timespec='microseconds')
        source_text = str(source or Path(__file__).name)
        context_text = str(context or '')
        appendRunLog(f'[CAPTURED-EXCEPTION] source={source_text} context={context_text} handled={int(bool(handled))} type={type_name} message={message}')
        try:
            fallback_row = DebugLog.exception(exc, source=source_text, context=context_text, handled=handled, extra=extra, db_path=_capture_exception_db_path(), save_db=not bool(globals().get('HAS_SQLALCHEMY', False)))
            if fallback_row and not row_id:
                row_id = int(fallback_row)
        except Exception as error:
            DebugLog.saveExceptionFallback(error, source='start.py', context=f'{context_text}:debuglog-fallback', handled=True, extra=traceback_text)
        try:
            target = _capture_exception_log_path()
            target.parent.mkdir(parents=True, exist_ok=True)
            with File.tracedOpen(target, 'a', encoding='utf-8', errors='replace') as handle:
                handle.write(f'{created} [CAPTURED-EXCEPTION] source={source_text} context={context_text} handled={int(bool(handled))} type={type_name} message={message}\n')
                if extra:
                    handle.write(f'{created} [CAPTURED-EXCEPTION:EXTRA] {extra}\n')
                handle.write(traceback_text.rstrip() + '\n')
        except Exception:  # swallow-ok: emergency exception recorder must never recurse
            pass
        try:
            print(f'[CAPTURED-EXCEPTION] source={source_text} context={context_text} type={type_name}: {message}', file=sys.stderr, flush=True)
        except Exception:  # swallow-ok: emergency exception recorder must never recurse
            pass
        try:
            if bool(globals().get('HAS_SQLALCHEMY', False)):
                engine_factory = globals().get('create_engine')
                session_factory = globals().get('sessionmaker')
                base_cls = globals().get('StartOrmBase')
                record_cls = globals().get('DebuggerExceptionRecord')
                if engine_factory is not None and session_factory is not None and base_cls is not None and record_cls is not None:
                    db_path = _capture_exception_db_path()
                    db_path.parent.mkdir(parents=True, exist_ok=True)
                    engine = engine_factory(f'sqlite:///{db_path}', future=True)
                    try:
                        base_cls.metadata.create_all(engine)
                        SessionFactory = session_factory(bind=engine, future=True)
                        with SessionFactory() as session:
                            row = record_cls(created=created, source=source_text, context=context_text, typeName=type_name, message=message, tracebackText=traceback_text, sourceContext=extra, thread=str(getattr(threading.current_thread(), 'name', '') or ''), pid=int(os.getpid()), handled=1 if handled else 0, processed=0)
                            session.add(row)
                            session.flush()
                            row_id = int(getattr(row, 'id', 0) or 0)
                            session.commit()
                    finally:
                        try:
                            engine.dispose()
                        except Exception:  # swallow-ok: emergency exception recorder must never recurse
                            pass
        except Exception as db_error:  # swallow-ok: emergency DB fallback writes file only
            try:
                fallback_row = DebugLog.exception(db_error, source=source_text, context=f'{context_text}:sqlalchemy-db-fallback', handled=True, extra=traceback_text, db_path=_capture_exception_db_path(), save_db=True)
                if fallback_row and not row_id:
                    row_id = int(fallback_row)
            except Exception as error:
                DebugLog.saveExceptionFallback(error, source='start.py', context=f'{context_text}:sqlalchemy-db-fallback-debuglog', handled=True, extra=traceback_text)
            try:
                target = _capture_exception_log_path()
                target.parent.mkdir(parents=True, exist_ok=True)
                with File.tracedOpen(target, 'a', encoding='utf-8', errors='replace') as handle:
                    handle.write(f'{datetime.datetime.now().isoformat(sep=" ", timespec="microseconds")} [CAPTURED-EXCEPTION-DB-FAILED] {type(db_error).__name__}: {db_error}\n')
                    handle.write(traceback.format_exc().rstrip() + '\n')
            except Exception:  # swallow-ok: emergency exception recorder must never recurse
                pass
        return row_id
    finally:
        _CAPTURE_EXCEPTION_REENTRANT = False
# Runtime: 0.000s ExceptionCaptureSurface Outcome (defines trace+DB exception capture for debugger) \\greenSUCCESS!





def _prompt_earliest_ruff_entrypoint() -> None:
    """Earliest Ruff/Rough CLI path: avoid optional SQLAlchemy/GUI imports entirely."""
    tokens = [str(token or '').strip() for token in sys.argv[1:] if str(token or '').strip()]
    aliases = {'ruff', '--ruff', '-ruff', '/ruff', 'rough', '--rough', '-rough', '/rough'}
    if not any(token.lower() in aliases for token in tokens):
        return
    base_dir = Path(__file__).resolve().parent
    report_path = base_dir / 'ruff.txt'
    timeout_seconds = str(os.environ.get('RUFF_TIMEOUT', '180') or '180')
    ruff_command = str(os.environ.get('RUFF_COMMAND', '') or '')
    python_path = str(os.environ.get('RUFF_PYTHONPATH', '') or '')
    output_aliases = {'--ruff-output', '-ruff-output', '/ruff-output', 'ruff-output', '--ruff-log', '-ruff-log', '/ruff-log', 'ruff-log', '--rough-output', '-rough-output', '/rough-output', 'rough-output', '--rough-log', '-rough-log', '/rough-log', 'rough-log'}
    timeout_aliases = {'--ruff-timeout', '-ruff-timeout', '/ruff-timeout', 'ruff-timeout', '--rough-timeout', '-rough-timeout', '/rough-timeout', 'rough-timeout'}
    command_aliases = {'--ruff-command', '-ruff-command', '/ruff-command', 'ruff-command', '--rough-command', '-rough-command', '/rough-command', 'rough-command'}
    pythonpath_aliases = {'--ruff-pythonpath', '-ruff-pythonpath', '/ruff-pythonpath', 'ruff-pythonpath', '--rough-pythonpath', '-rough-pythonpath', '/rough-pythonpath', 'rough-pythonpath'}
    extra_targets: list[Path] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        lowered = token.lower()
        if lowered in aliases:
            index += 1
            continue
        if lowered in output_aliases and index + 1 < len(tokens):
            candidate = Path(tokens[index + 1].strip('"\''))
            report_path = candidate if candidate.is_absolute() else base_dir / candidate
            index += 2
            continue
        if lowered in timeout_aliases and index + 1 < len(tokens):
            timeout_seconds = tokens[index + 1]
            index += 2
            continue
        if lowered in command_aliases and index + 1 < len(tokens):
            ruff_command = tokens[index + 1]
            index += 2
            continue
        if lowered in pythonpath_aliases and index + 1 < len(tokens):
            python_path = tokens[index + 1]
            index += 2
            continue
        if lowered.startswith('--') or lowered.startswith('-') or lowered.startswith('/'):
            index += 1
            continue
        if lowered.endswith('.py') or Path(token).exists() or (base_dir / token).exists():
            candidate = Path(token.strip('"\''))
            extra_targets.append(candidate if candidate.is_absolute() else base_dir / candidate)
        index += 1
    runner_path = base_dir / 'tools' / 'run_ruff.py'
    try:
        if not runner_path.exists():
            raise RuntimeError('tools/run_ruff.py is missing')
        python_exe = str(sys.executable or 'python')
        runner_args = [python_exe, '-S', str(runner_path), '--root', str(base_dir), '--output', str(report_path), '--timeout', str(timeout_seconds)]
        if ruff_command:
            runner_args.extend(['--ruff-command', ruff_command])
        if python_path:
            runner_args.extend(['--pythonpath', python_path])
        runner_args.extend([str(base_dir / 'start.py'), str(base_dir / 'prompt_app.py')])
        runner_args.extend(str(path) for path in extra_targets)
        os.execv(python_exe, runner_args)  # lifecycle-bypass-ok phase-ownership-ok early-cli-exec-replaces-current-process
        os._exit(1)  # lifecycle-ok: execv only returns if the earliest Ruff CLI tool could not replace the process.
    except BaseException as exc:
        captureException(None, source='start.py', context='except@108')
        try:
            File.writeText(report_path, f'ERROR: {type(exc).__name__}: {exc}\n{traceback.format_exc()}\n', encoding='utf-8')
        except Exception as error:
            captureException(None, source='start.py', context='except@111')
            print(f"[WARN:swallowed-exception] start.py:early-ruff-entrypoint {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        print(f'[RUFF] fatal: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        os._exit(1)  # lifecycle-ok: fatal earliest Ruff CLI failure before launcher lifecycle exists.


_prompt_earliest_ruff_entrypoint()


def _prompt_earliest_pyrite_entrypoint() -> None:
    """Earliest Pyrite/Pyright/Plyrite CLI path: generate a report and exit before optional GUI/ORM imports."""
    tokens = [str(token or '').strip() for token in sys.argv[1:] if str(token or '').strip()]
    aliases = {'pyrite', '--pyrite', '-pyrite', '/pyrite', 'pyright', '--pyright', '-pyright', '/pyright', 'pirate', '--pirate', '-pirate', '/pirate', 'plyrate', '--plyrate', '-plyrate', '/plyrate', 'plyrite', '--plyrite', '-plyrite', '/plyrite', 'pylrite', '--pylrite', '-pylrite', '/pylrite'}
    if not any(token.lower() in aliases for token in tokens):
        return
    base_dir = Path(__file__).resolve().parent
    report_path = base_dir / 'Pyrite.log'
    json_path = base_dir / 'Pyrite.json'
    timeout_seconds = str(os.environ.get('PYRITE_TARGET_TIMEOUT', os.environ.get('PYRITE_TIMEOUT', '180')) or '180')
    python_path = str(os.environ.get('PYRITE_PYTHONPATH', os.environ.get('PYRIGHT_PYTHONPATH', '')) or '')
    config_path = base_dir / 'pyrightconfig.json'
    output_aliases = {'--pyrite-output', '-pyrite-output', '/pyrite-output', 'pyrite-output', '--pyrite-log', '-pyrite-log', '/pyrite-log', 'pyrite-log', '--pyright-output', '-pyright-output', '/pyright-output', 'pyright-output'}
    json_aliases = {'--pyrite-json', '-pyrite-json', '/pyrite-json', 'pyrite-json', '--pyright-json', '-pyright-json', '/pyright-json', 'pyright-json'}
    timeout_aliases = {'--pyrite-timeout', '-pyrite-timeout', '/pyrite-timeout', 'pyrite-timeout', '--pyright-timeout', '-pyright-timeout', '/pyright-timeout', 'pyright-timeout'}
    config_aliases = {'--pyrite-config', '-pyrite-config', '/pyrite-config', 'pyrite-config', '--pyright-config', '-pyright-config', '/pyright-config', 'pyright-config'}
    pythonpath_aliases = {'--pyrite-pythonpath', '-pyrite-pythonpath', '/pyrite-pythonpath', 'pyrite-pythonpath', '--pyright-pythonpath', '-pyright-pythonpath', '/pyright-pythonpath', 'pyright-pythonpath', '--pythonpath', '-pythonpath', '/pythonpath', 'pythonpath'}
    extra_targets: list[Path] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        lowered = token.lower()
        if lowered in aliases:
            index += 1
            continue
        if lowered in output_aliases and index + 1 < len(tokens):
            candidate = Path(tokens[index + 1].strip('"\''))
            report_path = candidate if candidate.is_absolute() else base_dir / candidate
            index += 2
            continue
        if lowered in json_aliases and index + 1 < len(tokens):
            candidate = Path(tokens[index + 1].strip('"\''))
            json_path = candidate if candidate.is_absolute() else base_dir / candidate
            index += 2
            continue
        if lowered in timeout_aliases and index + 1 < len(tokens):
            timeout_seconds = tokens[index + 1]
            index += 2
            continue
        if lowered in config_aliases and index + 1 < len(tokens):
            candidate = Path(tokens[index + 1].strip('"\''))
            config_path = candidate if candidate.is_absolute() else base_dir / candidate
            index += 2
            continue
        if lowered in pythonpath_aliases and index + 1 < len(tokens):
            python_path = tokens[index + 1]
            index += 2
            continue
        if lowered.startswith('--') or lowered.startswith('-') or lowered.startswith('/'):
            index += 1
            continue
        if lowered.endswith('.py') or Path(token).exists() or (base_dir / token).exists():
            candidate = Path(token.strip('"\''))
            extra_targets.append(candidate if candidate.is_absolute() else base_dir / candidate)
        index += 1
    runner_path = base_dir / 'tools' / 'run_pyrite.py'
    try:
        if not runner_path.exists():
            raise RuntimeError('tools/run_pyrite.py is missing')
        python_exe = str(sys.executable or 'python')
        targets = [base_dir / 'start.py', base_dir / 'prompt_app.py', *extra_targets]
        runner_args = [python_exe, '-S', str(runner_path), '--root', str(base_dir), '--output', str(report_path), '--json-output', str(json_path), '--config', str(config_path), '--timeout', str(timeout_seconds)]
        if python_path:
            runner_args.extend(['--pythonpath', python_path])
        runner_args.extend(str(path) for path in targets)
        os.execv(python_exe, runner_args)  # lifecycle-bypass-ok phase-ownership-ok early-cli-exec-replaces-current-process
        os._exit(1)  # lifecycle-ok: execv only returns if the earliest Pyrite CLI tool could not replace the process.
    except BaseException as exc:
        captureException(None, source='start.py', context='except@187')
        try:
            File.writeText(report_path, f'ERROR: {type(exc).__name__}: {exc}\n{traceback.format_exc()}\n', encoding='utf-8')
        except Exception as error:
            captureException(None, source='start.py', context='except@190')
            print(f"[WARN:swallowed-exception] start.py:early-pyrite-entrypoint {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        print(f'[PYRITE] fatal: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        os._exit(1)  # lifecycle-ok: fatal earliest Pyrite CLI failure before launcher lifecycle exists.


_prompt_earliest_pyrite_entrypoint()



def _prompt_earliest_claude_detector_entrypoint() -> None:
    """Earliest Claude detector CLI path: run static reports before optional GUI/ORM imports."""
    tokens = [str(token or '').strip() for token in sys.argv[1:] if str(token or '').strip()]
    aliases_by_token = {
        'bypass-detector': 'bypass', '--bypass-detector': 'bypass', '-bypass-detector': 'bypass', '/bypass-detector': 'bypass',
        'lifecycle-bypass-detector': 'bypass', '--lifecycle-bypass-detector': 'bypass', '--lifecycle-bypass': 'bypass', '--lifecycle-detector': 'bypass', '--lifecycle': 'bypass',
        'monkeypatch-detector': 'monkeypatch', '--monkeypatch-detector': 'monkeypatch', '-monkeypatch-detector': 'monkeypatch', '/monkeypatch-detector': 'monkeypatch',
        'monkey-patch-detector': 'monkeypatch', '--monkey-patch-detector': 'monkeypatch', '--monkeypatch': 'monkeypatch',
        'monkey': 'monkeypatch', '--monkey': 'monkeypatch', '-monkey': 'monkeypatch', '/monkey': 'monkeypatch',
        'rawsql-detector': 'rawsql', '--rawsql-detector': 'rawsql', '-rawsql-detector': 'rawsql', '/rawsql-detector': 'rawsql',
        'raw-sql-detector': 'rawsql', '--raw-sql-detector': 'rawsql', '--rawsql': 'rawsql', '--raw-sql': 'rawsql', '--raw': 'rawsql',
        'recursion-detector': 'recursion', '--recursion-detector': 'recursion', '-recursion-detector': 'recursion', '/recursion-detector': 'recursion', '--recursion': 'recursion',
        'redundant-detector': 'redundant', '--redundant-detector': 'redundant', '-redundant-detector': 'redundant', '/redundant-detector': 'redundant', '--redundant': 'redundant', '--redundancy': 'redundant',
        'swallowed-exceptions-detector': 'swallowed', '--swallowed-exceptions-detector': 'swallowed', '-swallowed-exceptions-detector': 'swallowed', '/swallowed-exceptions-detector': 'swallowed',
        'swallow-detector': 'swallowed', '--swallow-detector': 'swallowed', '--swallowed-exceptions': 'swallowed', '--swallowed': 'swallowed', '--swallow': 'swallowed',
        'file-io': 'fileio', '--file-io': 'fileio', 'fileio': 'fileio', '--fileio': 'fileio', 'fileio-detector': 'fileio', '--fileio-detector': 'fileio',
        'process-faults': 'processfaults', '--process-faults': 'processfaults', 'process-fault': 'processfaults', '--process-fault': 'processfaults', 'processfaults-detector': 'processfaults', '--processfaults-detector': 'processfaults',
        'phase-ownership': 'phaseownership', '--phase-ownership': 'phaseownership', 'phase': 'phaseownership', '--phase': 'phaseownership', 'phaseownership-detector': 'phaseownership', '--phaseownership-detector': 'phaseownership',
        'threads': 'threads', '--threads': 'threads', 'thread-safety': 'threads', '--thread-safety': 'threads', 'thread-detector': 'threads', '--thread-detector': 'threads',
        'bad-code': 'badcode', '--bad-code': 'badcode', 'badcode': 'badcode', '--badcode': 'badcode', 'badcode-detector': 'badcode', '--badcode-detector': 'badcode',
        'unlocalized': 'unlocalized', '--unlocalized': 'unlocalized', 'localization-detector': 'unlocalized', '--localization-detector': 'unlocalized', 'unlocalized-detector': 'unlocalized', '--unlocalized-detector': 'unlocalized',
        'depcheck': 'depcheck', '--depcheck': 'depcheck', 'dep-check': 'depcheck', '--dep-check': 'depcheck', 'dependency-check': 'depcheck', '--dependency-check': 'depcheck',
        'phasehooks': 'phasehooks', '--phasehooks': 'phasehooks', 'phase-hooks': 'phasehooks', '--phase-hooks': 'phasehooks', 'phase-hook': 'phasehooks', '--phase-hook': 'phasehooks',
        'nonconform': 'nonconform', '--nonconform': 'nonconform', 'non-conform': 'nonconform', '--non-conform': 'nonconform', 'nonconformance': 'nonconform', '--nonconformance': 'nonconform',
        'comport': 'comport', '--comport': 'comport', 'conformity': 'comport', '--conformity': 'comport',
        'startpydebugger': 'startpydebugger', '--startpydebugger': 'startpydebugger', 'startpy-debugger': 'startpydebugger', '--startpy-debugger': 'startpydebugger', 'debugger-compliance': 'startpydebugger', '--debugger-compliance': 'startpydebugger',
        'brokenimports': 'brokenimports', '--brokenimports': 'brokenimports', 'broken-imports': 'brokenimports', '--broken-imports': 'brokenimports', 'bad-imports': 'brokenimports', '--bad-imports': 'brokenimports',
        'astscan': 'astscan', '--astscan': 'astscan', 'ast-scan': 'astscan', '--ast-scan': 'astscan', 'ast': 'astscan', '--ast': 'astscan',
        'deadcode': 'deadcode', '--deadcode': 'deadcode', 'dead-code': 'deadcode', '--dead-code': 'deadcode',
        'live': 'live', '--live': 'live', 'live-run': 'live', '--live-run': 'live', 'live-run-error': 'live', '--live-run-error': 'live', 'live-run-error-detector': 'live', '--live-run-error-detector': 'live', 'runtime': 'live', '--runtime': 'live', 'runtime-errors': 'live', '--runtime-errors': 'live',
        'certify': 'all', '--certify': 'all',
        'claude-detectors': 'all', '--claude-detectors': 'all', '-claude-detectors': 'all', '/claude-detectors': 'all',
        'all-detectors': 'all', '--all-detectors': 'all', '--claude-detector-suite': 'all',
    }
    selected: list[str] = []
    output_path: Path | None = None
    target_tokens: list[str] = []
    live_app_args: list[str] = []
    explicit_live_app_args = False
    base_dir = Path(__file__).resolve().parent
    output_aliases = {'--detector-output', '-detector-output', '/detector-output', 'detector-output', '--report', '-report', '/report', 'report'}
    timeout_aliases = {'--detector-timeout', '-detector-timeout', '/detector-timeout', 'detector-timeout', '--timeout', '-timeout', '/timeout', 'timeout'}
    live_arg_aliases = {'--app-arg', '-app-arg', '/app-arg', 'app-arg', '--runtime-arg', '-runtime-arg', '/runtime-arg', 'runtime-arg', '--start-arg', '-start-arg', '/start-arg', 'start-arg'}
    live_args_remainder_aliases = {'--app-args', '-app-args', '/app-args', 'app-args', '--runtime-args', '-runtime-args', '/runtime-args', 'runtime-args', '--start-args', '-start-args', '/start-args', 'start-args'}
    build_passthrough_aliases = {'build', '--build', '-build', '/build', 'rebuild', '--rebuild', '--force-build', '--force-rebuild', '-force-rebuild', '/force-rebuild', 'force-rebuild', 'package', '--package', '-package', '/package', 'packages', '--packages', 'installer-package', '--installer-package', 'installers', '--installers', 'pyinstaller', '--pyinstaller', 'nuitka', '--nuitka', 'nikita', '--nikita', 'cx-freeze', '--cx-freeze', 'py2exe', '--py2exe', 'pyoxidizer', '--pyoxidizer', 'pyapp', '--pyapp', 'briefcase', '--briefcase'}
    timeout_seconds = '300'
    index = 0
    while index < len(tokens):
        token = tokens[index]
        lowered = token.lower()
        if lowered == '--':
            live_app_args.extend(tokens[index + 1:])
            explicit_live_app_args = True
            break
        if lowered in aliases_by_token:
            name = aliases_by_token[lowered]
            if name not in selected:
                selected.append(name)
            index += 1
            continue
        if lowered in output_aliases and index + 1 < len(tokens):
            candidate = Path(tokens[index + 1].strip('"\''))
            output_path = candidate if candidate.is_absolute() else base_dir / candidate
            index += 2
            continue
        if lowered in timeout_aliases and index + 1 < len(tokens):
            timeout_seconds = tokens[index + 1]
            index += 2
            continue
        if lowered in live_arg_aliases and index + 1 < len(tokens):
            live_app_args.append(tokens[index + 1])
            explicit_live_app_args = True
            index += 2
            continue
        if lowered in live_args_remainder_aliases:
            remainder = tokens[index + 1:]
            if remainder and remainder[0] == '--':
                remainder = remainder[1:]
            live_app_args.extend(remainder)
            explicit_live_app_args = True
            break
        if lowered in build_passthrough_aliases:
            live_app_args.append(token)
            index += 1
            continue
        if lowered.startswith('--') or lowered.startswith('-') or lowered.startswith('/'):
            index += 1
            continue
        if lowered.endswith('.py') or Path(token).exists() or (base_dir / token).exists():
            target_tokens.append(token)
        index += 1
    if not selected:
        return
    if 'all' in selected:
        selected = ['all']
    if 'live' not in selected and selected != ['all']:
        live_app_args = []
    elif not live_app_args and not explicit_live_app_args:
        live_app_args = ['--offscreen']
    default_names = {
        'bypass': 'reports/claude/bypass_report.txt',
        'monkeypatch': 'reports/claude/monkeypatch_report.txt',
        'rawsql': 'reports/claude/rawsql_report.txt',
        'recursion': 'reports/claude/recursion_report.txt',
        'redundant': 'reports/claude/redundant_report.txt',
        'swallowed': 'reports/claude/swallowed_exceptions_report.txt',
        'fileio': 'reports/claude/fileio_report.txt',
        'processfaults': 'reports/claude/process_faults_report.txt',
        'phaseownership': 'reports/claude/phase_ownership_report.txt',
        'threads': 'reports/claude/thread_safety_report.txt',
        'badcode': 'reports/claude/badcode_report.txt',
        'unlocalized': 'reports/claude/unlocalized_report.txt',
        'depcheck': 'reports/claude/depcheck_report.txt',
        'phasehooks': 'reports/claude/phase_hooks_report.txt',
        'nonconform': 'reports/claude/nonconform_report.txt',
        'comport': 'reports/claude/comport_report.txt',
        'startpydebugger': 'reports/claude/startpy_debugger_compliance.txt',
        'live': 'reports/claude/live_run_error_detector.txt',
        'all': 'reports/claude/claude_detectors_report.txt',
    }
    report_path = output_path or (base_dir / default_names.get(selected[0], 'reports/claude/report.txt'))
    targets: list[Path] = []
    for raw in target_tokens:
        candidate = Path(raw.strip('"\''))
        candidate = candidate if candidate.is_absolute() else base_dir / candidate
        try:
            resolved = candidate.resolve()
        except Exception as resolve_error:
            captureException(resolve_error, source='start.py', context='claude-detector-target-resolve', handled=True, extra=str(candidate))
            print(f"[WARN:swallowed-exception] start.py:claude-detector-target-resolve {type(resolve_error).__name__}: {resolve_error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            continue
        if resolved.exists() and resolved.suffix.lower() == '.py' and resolved not in targets:
            targets.append(resolved)
    runner_path = base_dir / 'tools' / 'run_claude_detector.py'
    try:
        if not runner_path.exists():
            raise RuntimeError('tools/run_claude_detector.py is missing')
        python_exe = str(sys.executable or 'python')
        runner_args = [python_exe, '-S', str(runner_path), '--root', str(base_dir), '--output', str(report_path), '--timeout', str(timeout_seconds)]
        for name in selected:
            runner_args.extend(['--detector', name])
        if live_app_args:
            runner_args.extend(['--app-args', *live_app_args])
        runner_args.extend(str(path) for path in targets)
        os.execv(python_exe, runner_args)  # lifecycle-bypass-ok phase-ownership-ok early-cli-exec-replaces-current-process
        os._exit(1)  # lifecycle-ok: execv only returns if the earliest Claude detector CLI tool could not replace the process.
    except BaseException as exc:
        try:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            File.writeText(report_path, f'ERROR: {type(exc).__name__}: {exc}\n{traceback.format_exc()}\n', encoding='utf-8')
        except Exception as report_error:
            print(f"[WARN:swallowed-exception] claude-detector-report-failed {type(report_error).__name__}: {report_error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        print(f'[CLAUDE-DETECTOR] fatal: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        os._exit(1)  # lifecycle-ok: fatal earliest Claude detector CLI failure before launcher lifecycle exists.


_prompt_earliest_claude_detector_entrypoint()

BUNDLE_IMPORT_WARNINGS: list[str] = []

class _MissingSqlAlchemySymbol:
    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f'SQLAlchemy is required for debugger database access: {SQLALCHEMY_IMPORT_ERROR!r}')

    def __getattr__(self, _name: str) -> Any:
        return self

    def in_(self, _values: Any) -> Any:
        return self

    def asc(self) -> Any:
        return self

    def desc(self) -> Any:
        return self


Column: Any
Float: Any
Integer: Any
Text: Any
create_engine: Any
sessionmaker: Any
DeclarativeBase: Any
SQLALCHEMY_IMPORT_ERROR: BaseException | None = None

try:
    from sqlalchemy import Column as _SqlColumn, Float as _SqlFloat, Integer as _SqlInteger, Text as _SqlText, create_engine as _SqlCreateEngine
    from sqlalchemy.orm import DeclarativeBase as _SqlDeclarativeBase, sessionmaker as _SqlSessionmaker
    Column = cast(Any, _SqlColumn)
    Float = cast(Any, _SqlFloat)
    Integer = cast(Any, _SqlInteger)
    Text = cast(Any, _SqlText)
    create_engine = cast(Any, _SqlCreateEngine)
    sessionmaker = cast(Any, _SqlSessionmaker)
    DeclarativeBase = cast(Any, _SqlDeclarativeBase)
    HAS_SQLALCHEMY = True
except Exception as error:
    captureException(None, source='start.py', context='except@237')
    print(f"[WARN:swallowed-exception] start.py:46 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
    HAS_SQLALCHEMY = False
    SQLALCHEMY_IMPORT_ERROR = error
    _missingSqlAlchemy = _MissingSqlAlchemySymbol()
    Column = Float = Integer = Text = create_engine = sessionmaker = cast(Any, _missingSqlAlchemy)
    class _FallbackDeclarativeBase:
        metadata: Any = _missingSqlAlchemy
        __table__: Any = _missingSqlAlchemy
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(f'SQLAlchemy is required for debugger database access: {SQLALCHEMY_IMPORT_ERROR!r}')
    DeclarativeBase = cast(Any, _FallbackDeclarativeBase)


class _FallbackTrioDesktopCoreString:
    EMPTY_STRING = ''


TrioDesktopCoreString = cast(Any, _FallbackTrioDesktopCoreString)
_language_spec = importlib.util.find_spec('languages')
if _language_spec is not None:
    try:
        _languages_module = importlib.import_module('languages')
        TrioDesktopCoreString = cast(Any, getattr(_languages_module, 'TrioDesktopCoreString'))
    except Exception as import_error:
        # Optional legacy TrioDesktop compatibility. Do not persist as a crash/fault
        # in Prompt; it is not part of the Prompt app.
        BUNDLE_IMPORT_WARNINGS.append(f'optional languages import failed: {type(import_error).__name__}: {import_error}')
        if str(os.environ.get('PROMPT_TRACE_OPTIONAL_IMPORTS', '') or '').strip():
            DebugLog.writeLine(BUNDLE_IMPORT_WARNINGS[-1], level='WARN', source='start.py', stream='stderr')


class _FallbackTrioDesktopEmbeddedData:
    class ColorLibrary:
        @staticmethod
        def ResolveWord(value: str):
            return None
    @staticmethod
    def get(key: str, default: str = '') -> str:
        return str(default or '')


TrioDesktopEmbeddedData = cast(Any, _FallbackTrioDesktopEmbeddedData)
_embedded_spec = importlib.util.find_spec('triodesktop_embedded_data')
if _embedded_spec is not None:
    try:
        _embedded_data_module = importlib.import_module('triodesktop_embedded_data')
        TrioDesktopEmbeddedData = cast(Any, getattr(_embedded_data_module, 'TrioDesktopEmbeddedData'))
    except Exception as import_error:
        # Optional legacy TrioDesktop compatibility. Do not persist as a crash/fault
        # in Prompt; it is not part of the Prompt app.
        BUNDLE_IMPORT_WARNINGS.append(f'optional triodesktop_embedded_data import failed: {type(import_error).__name__}: {import_error}')
        if str(os.environ.get('PROMPT_TRACE_OPTIONAL_IMPORTS', '') or '').strip():
            DebugLog.writeLine(BUNDLE_IMPORT_WARNINGS[-1], level='WARN', source='start.py', stream='stderr')

EMPTY_STRING = TrioDesktopCoreString.EMPTY_STRING
HERE = Path(__file__).resolve().parent
BASE_DIR = HERE
CHILD_APP_PATH = BASE_DIR / 'prompt_app.py'
GTP_PATH = CHILD_APP_PATH
PROMPT_RELEASE_PATCH_LEVEL = 'V174_STANDALONE_BUNDLES_LICENSES'


def _prompt_fast_release_entrypoint() -> None:
    """Disabled by default so release/build code cannot bypass --build gating."""
    if str(os.environ.get('PROMPT_ENABLE_FAST_RELEASE', '') or '').strip() != '1':
        return
    tokens = [str(token or '').strip().lower() for token in sys.argv[1:] if str(token or '').strip()]
    build_aliases = {'build', '--build', '-build', '/build', 'rebuild', '--rebuild'}
    if not any(token in build_aliases for token in tokens):
        return
    runner = BASE_DIR / 'tools' / 'run_prompt_release.py'
    if not runner.exists():
        return
    python_exe = sys.executable or 'python'
    os.execv(python_exe, [python_exe, '-S', '-m', 'tools.run_prompt_release', '--root', str(BASE_DIR), *sys.argv[1:]])  # lifecycle-bypass-ok phase-ownership-ok early-release-exec-replaces-current-process


_prompt_fast_release_entrypoint()



def _prompt_fast_claude_detector_entrypoint() -> None:
    """Fast Claude detector CLI path: run static detector reports without launching Prompt."""
    tokens = [str(token or '').strip() for token in sys.argv[1:] if str(token or '').strip()]
    aliases_by_token = {
        'bypass-detector': 'bypass', '--bypass-detector': 'bypass', '-bypass-detector': 'bypass', '/bypass-detector': 'bypass',
        'lifecycle-bypass-detector': 'bypass', '--lifecycle-bypass-detector': 'bypass', '--lifecycle-bypass': 'bypass', '--lifecycle-detector': 'bypass', '--lifecycle': 'bypass',
        'monkeypatch-detector': 'monkeypatch', '--monkeypatch-detector': 'monkeypatch', '-monkeypatch-detector': 'monkeypatch', '/monkeypatch-detector': 'monkeypatch',
        'monkey-patch-detector': 'monkeypatch', '--monkey-patch-detector': 'monkeypatch', '--monkeypatch': 'monkeypatch',
        'monkey': 'monkeypatch', '--monkey': 'monkeypatch', '-monkey': 'monkeypatch', '/monkey': 'monkeypatch',
        'rawsql-detector': 'rawsql', '--rawsql-detector': 'rawsql', '-rawsql-detector': 'rawsql', '/rawsql-detector': 'rawsql',
        'raw-sql-detector': 'rawsql', '--raw-sql-detector': 'rawsql', '--rawsql': 'rawsql', '--raw-sql': 'rawsql', '--raw': 'rawsql',
        'recursion-detector': 'recursion', '--recursion-detector': 'recursion', '-recursion-detector': 'recursion', '/recursion-detector': 'recursion', '--recursion': 'recursion',
        'redundant-detector': 'redundant', '--redundant-detector': 'redundant', '-redundant-detector': 'redundant', '/redundant-detector': 'redundant', '--redundant': 'redundant', '--redundancy': 'redundant',
        'swallowed-exceptions-detector': 'swallowed', '--swallowed-exceptions-detector': 'swallowed', '-swallowed-exceptions-detector': 'swallowed', '/swallowed-exceptions-detector': 'swallowed',
        'swallow-detector': 'swallowed', '--swallow-detector': 'swallowed', '--swallowed-exceptions': 'swallowed', '--swallowed': 'swallowed', '--swallow': 'swallowed',
        'file-io': 'fileio', '--file-io': 'fileio', 'fileio': 'fileio', '--fileio': 'fileio',
        'process-faults': 'processfaults', '--process-faults': 'processfaults', 'process-fault': 'processfaults', '--process-fault': 'processfaults',
        'phase-ownership': 'phaseownership', '--phase-ownership': 'phaseownership', 'phase': 'phaseownership', '--phase': 'phaseownership',
        'threads': 'threads', '--threads': 'threads', 'thread-safety': 'threads', '--thread-safety': 'threads',
        'bad-code': 'badcode', '--bad-code': 'badcode', 'badcode': 'badcode', '--badcode': 'badcode',
        'unlocalized': 'unlocalized', '--unlocalized': 'unlocalized', 'localization-detector': 'unlocalized', '--localization-detector': 'unlocalized',
        'depcheck': 'depcheck', '--depcheck': 'depcheck', 'dep-check': 'depcheck', '--dep-check': 'depcheck', 'dependency-check': 'depcheck', '--dependency-check': 'depcheck',
        'phasehooks': 'phasehooks', '--phasehooks': 'phasehooks', 'phase-hooks': 'phasehooks', '--phase-hooks': 'phasehooks', 'phase-hook': 'phasehooks', '--phase-hook': 'phasehooks',
        'nonconform': 'nonconform', '--nonconform': 'nonconform', 'non-conform': 'nonconform', '--non-conform': 'nonconform', 'nonconformance': 'nonconform', '--nonconformance': 'nonconform',
        'comport': 'comport', '--comport': 'comport', 'conformity': 'comport', '--conformity': 'comport',
        'startpydebugger': 'startpydebugger', '--startpydebugger': 'startpydebugger', 'startpy-debugger': 'startpydebugger', '--startpy-debugger': 'startpydebugger', 'debugger-compliance': 'startpydebugger', '--debugger-compliance': 'startpydebugger',
        'brokenimports': 'brokenimports', '--brokenimports': 'brokenimports', 'broken-imports': 'brokenimports', '--broken-imports': 'brokenimports', 'bad-imports': 'brokenimports', '--bad-imports': 'brokenimports',
        'astscan': 'astscan', '--astscan': 'astscan', 'ast-scan': 'astscan', '--ast-scan': 'astscan', 'ast': 'astscan', '--ast': 'astscan',
        'deadcode': 'deadcode', '--deadcode': 'deadcode', 'dead-code': 'deadcode', '--dead-code': 'deadcode',
        'live': 'live', '--live': 'live', 'live-run': 'live', '--live-run': 'live', 'live-run-error': 'live', '--live-run-error': 'live', 'live-run-error-detector': 'live', '--live-run-error-detector': 'live', 'runtime': 'live', '--runtime': 'live', 'runtime-errors': 'live', '--runtime-errors': 'live',
        'certify': 'all', '--certify': 'all',
        'claude-detectors': 'all', '--claude-detectors': 'all', '-claude-detectors': 'all', '/claude-detectors': 'all',
        'all-detectors': 'all', '--all-detectors': 'all', '--claude-detector-suite': 'all',
    }
    selected: list[str] = []
    output_path: Path | None = None
    target_tokens: list[str] = []
    live_app_args: list[str] = []
    explicit_live_app_args = False
    output_aliases = {'--detector-output', '-detector-output', '/detector-output', 'detector-output', '--report', '-report', '/report', 'report'}
    live_arg_aliases = {'--app-arg', '-app-arg', '/app-arg', 'app-arg', '--runtime-arg', '-runtime-arg', '/runtime-arg', 'runtime-arg', '--start-arg', '-start-arg', '/start-arg', 'start-arg'}
    live_args_remainder_aliases = {'--app-args', '-app-args', '/app-args', 'app-args', '--runtime-args', '-runtime-args', '/runtime-args', 'runtime-args', '--start-args', '-start-args', '/start-args', 'start-args'}
    build_passthrough_aliases = {'build', '--build', '-build', '/build', 'rebuild', '--rebuild', '--force-build', '--force-rebuild', '-force-rebuild', '/force-rebuild', 'force-rebuild', 'package', '--package', '-package', '/package', 'packages', '--packages', 'installer-package', '--installer-package', 'installers', '--installers', 'pyinstaller', '--pyinstaller', 'nuitka', '--nuitka', 'nikita', '--nikita', 'cx-freeze', '--cx-freeze', 'py2exe', '--py2exe', 'pyoxidizer', '--pyoxidizer', 'pyapp', '--pyapp', 'briefcase', '--briefcase'}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        lowered = token.lower()
        if lowered == '--':
            live_app_args.extend(tokens[index + 1:])
            explicit_live_app_args = True
            break
        if lowered in aliases_by_token:
            name = aliases_by_token[lowered]
            if name not in selected:
                selected.append(name)
            index += 1
            continue
        if lowered in output_aliases and index + 1 < len(tokens):
            candidate = Path(tokens[index + 1].strip('"\''))
            output_path = candidate if candidate.is_absolute() else BASE_DIR / candidate
            index += 2
            continue
        if lowered in live_arg_aliases and index + 1 < len(tokens):
            live_app_args.append(tokens[index + 1])
            explicit_live_app_args = True
            index += 2
            continue
        if lowered in live_args_remainder_aliases:
            remainder = tokens[index + 1:]
            if remainder and remainder[0] == '--':
                remainder = remainder[1:]
            live_app_args.extend(remainder)
            explicit_live_app_args = True
            break
        if lowered in build_passthrough_aliases:
            live_app_args.append(token)
            index += 1
            continue
        if lowered.startswith('--') or lowered.startswith('-') or lowered.startswith('/'):
            index += 1
            continue
        if lowered.endswith('.py') or Path(token).exists() or (BASE_DIR / token).exists():
            target_tokens.append(token)
        index += 1
    if not selected:
        return
    if 'all' in selected:
        selected = ['all']
    if 'live' not in selected and selected != ['all']:
        live_app_args = []
    elif not live_app_args and not explicit_live_app_args:
        live_app_args = ['--offscreen']
    default_names = {
        'bypass': 'reports/claude/bypass_report.txt',
        'monkeypatch': 'reports/claude/monkeypatch_report.txt',
        'rawsql': 'reports/claude/rawsql_report.txt',
        'recursion': 'reports/claude/recursion_report.txt',
        'redundant': 'reports/claude/redundant_report.txt',
        'swallowed': 'reports/claude/swallowed_exceptions_report.txt',
        'fileio': 'reports/claude/fileio_report.txt',
        'processfaults': 'reports/claude/process_faults_report.txt',
        'phaseownership': 'reports/claude/phase_ownership_report.txt',
        'threads': 'reports/claude/thread_safety_report.txt',
        'badcode': 'reports/claude/badcode_report.txt',
        'unlocalized': 'reports/claude/unlocalized_report.txt',
        'depcheck': 'reports/claude/depcheck_report.txt',
        'phasehooks': 'reports/claude/phase_hooks_report.txt',
        'nonconform': 'reports/claude/nonconform_report.txt',
        'comport': 'reports/claude/comport_report.txt',
        'startpydebugger': 'reports/claude/startpy_debugger_compliance.txt',
        'live': 'reports/claude/live_run_error_detector.txt',
        'all': 'reports/claude/claude_detectors_report.txt',
    }
    report_path = output_path or (BASE_DIR / default_names.get(selected[0], 'report.txt'))
    targets: list[Path] = []
    candidates: list[Path] = []
    for raw in target_tokens:
        candidate = Path(raw.strip('"\''))
        candidates.append(candidate if candidate.is_absolute() else BASE_DIR / candidate)
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception as error:
            captureException(None, source='start.py', context='except@374')
            print(f"[WARN:swallowed-exception] start.py:145 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            continue
        if resolved.exists() and resolved.suffix.lower() == '.py' and resolved not in targets:
            targets.append(resolved)
    try:
        runner_path = BASE_DIR / 'tools' / 'run_claude_detector.py'
        if not runner_path.exists():
            raise RuntimeError('tools/run_claude_detector.py is missing')
        python_exe = str(sys.executable or 'python')
        runner_args = [python_exe, '-S', str(runner_path), '--root', str(BASE_DIR), '--output', str(report_path)]
        for name in selected:
            runner_args.extend(['--detector', name])
        if live_app_args:
            runner_args.extend(['--app-args', *live_app_args])
        runner_args.extend(str(path) for path in targets)
        os.execv(python_exe, runner_args)  # lifecycle-bypass-ok phase-ownership-ok early-cli-exec-replaces-current-process
        os._exit(1)  # lifecycle-ok: execv only returns if the early CLI tool could not replace the process.
    except BaseException as exc:
        captureException(None, source='start.py', context='except@390')
        try:
            File.writeText(report_path, f'ERROR: {type(exc).__name__}: {exc}\n{traceback.format_exc()}\n', encoding='utf-8')
        except Exception as error:
            captureException(None, source='start.py', context='except@393')
            print(f"[WARN:swallowed-exception] start.py:171 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        print(f'[CLAUDE-DETECTOR] fatal: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        os._exit(1)  # lifecycle-ok: fatal early Claude detector CLI failure before launcher lifecycle exists.


_prompt_fast_claude_detector_entrypoint()

def _prompt_fast_pyrite_entrypoint() -> None:
    """Fast Pyrite/Pyright CLI path: check start.py first, then the bootstrapped app."""
    tokens = [str(token or '').strip() for token in sys.argv[1:] if str(token or '').strip()]
    aliases = {'pyrite', '--pyrite', '-pyrite', '/pyrite', 'pyright', '--pyright', '-pyright', '/pyright', 'pirate', '--pirate', '-pirate', '/pirate', 'plyrate', '--plyrate', '-plyrate', '/plyrate', 'plyrite', '--plyrite', '-plyrite', '/plyrite', 'pylrite', '--pylrite', '-pylrite', '/pylrite'}
    if not any(token.lower() in aliases for token in tokens):
        return
    output_aliases = {'--pyrite-output', '-pyrite-output', '/pyrite-output', 'pyrite-output', '--pyrite-log', '-pyrite-log', '/pyrite-log', 'pyrite-log', '--pyright-output', '-pyright-output', '/pyright-output', 'pyright-output'}
    json_aliases = {'--pyrite-json', '-pyrite-json', '/pyrite-json', 'pyrite-json', '--pyright-json', '-pyright-json', '/pyright-json', 'pyright-json'}
    timeout_aliases = {'--pyrite-timeout', '-pyrite-timeout', '/pyrite-timeout', 'pyrite-timeout', '--pyright-timeout', '-pyright-timeout', '/pyright-timeout', 'pyright-timeout'}
    config_aliases = {'--pyrite-config', '-pyrite-config', '/pyrite-config', 'pyrite-config', '--pyright-config', '-pyright-config', '/pyright-config', 'pyright-config'}
    pythonpath_aliases = {'--pyrite-pythonpath', '-pyrite-pythonpath', '/pyrite-pythonpath', 'pyrite-pythonpath', '--pyright-pythonpath', '-pyright-pythonpath', '/pyright-pythonpath', 'pyright-pythonpath', '--pythonpath', '-pythonpath', '/pythonpath', 'pythonpath'}
    report_path = BASE_DIR / 'Pyrite.log'
    json_path = BASE_DIR / 'Pyrite.json'
    config_path = BASE_DIR / 'pyrightconfig.json'
    timeout_seconds = str(os.environ.get('PYRITE_TIMEOUT', '900') or '900')
    python_path = str(os.environ.get('PYRITE_PYTHONPATH', '') or '')
    extra_targets: list[Path] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        lowered = token.lower()
        if lowered in aliases:
            index += 1
            continue
        if lowered in output_aliases and index + 1 < len(tokens):
            candidate = Path(tokens[index + 1].strip('"\''))
            report_path = candidate if candidate.is_absolute() else BASE_DIR / candidate
            index += 2
            continue
        if lowered in json_aliases and index + 1 < len(tokens):
            candidate = Path(tokens[index + 1].strip('"\''))
            json_path = candidate if candidate.is_absolute() else BASE_DIR / candidate
            index += 2
            continue
        if lowered in timeout_aliases and index + 1 < len(tokens):
            timeout_seconds = tokens[index + 1]
            index += 2
            continue
        if lowered in config_aliases and index + 1 < len(tokens):
            candidate = Path(tokens[index + 1].strip('"\''))
            config_path = candidate if candidate.is_absolute() else BASE_DIR / candidate
            index += 2
            continue
        if lowered in pythonpath_aliases and index + 1 < len(tokens):
            python_path = tokens[index + 1].strip('"\'')
            index += 2
            continue
        if lowered.endswith('.py') or Path(token).exists() or (BASE_DIR / token).exists():
            candidate = Path(token.strip('"\''))
            extra_targets.append(candidate if candidate.is_absolute() else BASE_DIR / candidate)
        index += 1
    targets: list[Path] = []
    for candidate in [BASE_DIR / 'start.py', GTP_PATH, *extra_targets]:
        try:
            resolved = candidate.resolve()
        except Exception as error:
            captureException(None, source='start.py', context='except@457')
            print(f"[WARN:swallowed-exception] start.py:234 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            continue
        if resolved.exists() and resolved.suffix.lower() == '.py' and resolved not in targets:
            targets.append(resolved)
    try:
        runner_path = BASE_DIR / 'tools' / 'run_pyrite.py'
        if not runner_path.exists():
            raise RuntimeError('tools/run_pyrite.py is missing')
        python_exe = str(sys.executable or 'python')
        runner_args = [python_exe, '-S', str(runner_path), '--root', str(BASE_DIR), '--output', str(report_path), '--json-output', str(json_path), '--config', str(config_path), '--timeout', str(timeout_seconds)]
        if python_path:
            runner_args.extend(['--pythonpath', python_path])
        runner_args.extend(str(path) for path in targets)
        os.execv(python_exe, runner_args)  # lifecycle-bypass-ok phase-ownership-ok early-cli-exec-replaces-current-process
        os._exit(1)  # lifecycle-ok: early Pyrite execv failure before launcher lifecycle exists.
    except BaseException as exc:
        captureException(None, source='start.py', context='except@473')
        try:
            File.writeText(report_path, f'ERROR: {type(exc).__name__}: {exc}\n{traceback.format_exc()}\n', encoding='utf-8')
        except Exception as error:
            captureException(None, source='start.py', context='except@476')
            print(f"[WARN:swallowed-exception] start.py:252 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        print(f'[PYRITE] fatal: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        os._exit(1)  # lifecycle-ok: fatal early Pyrite CLI failure before launcher lifecycle exists.


_prompt_fast_pyrite_entrypoint()

def _prompt_fast_ruff_entrypoint() -> None:
    """Fast Ruff/Rough CLI path: run real Ruff diagnostics without launching Prompt."""
    tokens = [str(token or '').strip() for token in sys.argv[1:] if str(token or '').strip()]
    aliases = {'ruff', '--ruff', '-ruff', '/ruff', 'rough', '--rough', '-rough', '/rough'}
    if not any(token.lower() in aliases for token in tokens):
        return
    output_aliases = {'--ruff-output', '-ruff-output', '/ruff-output', 'ruff-output', '--ruff-log', '-ruff-log', '/ruff-log', 'ruff-log', '--rough-output', '-rough-output', '/rough-output', 'rough-output', '--rough-log', '-rough-log', '/rough-log', 'rough-log'}
    timeout_aliases = {'--ruff-timeout', '-ruff-timeout', '/ruff-timeout', 'ruff-timeout', '--rough-timeout', '-rough-timeout', '/rough-timeout', 'rough-timeout'}
    command_aliases = {'--ruff-command', '-ruff-command', '/ruff-command', 'ruff-command', '--rough-command', '-rough-command', '/rough-command', 'rough-command'}
    pythonpath_aliases = {'--ruff-pythonpath', '-ruff-pythonpath', '/ruff-pythonpath', 'ruff-pythonpath', '--rough-pythonpath', '-rough-pythonpath', '/rough-pythonpath', 'rough-pythonpath'}
    report_path = BASE_DIR / 'ruff.txt'
    timeout_seconds = str(os.environ.get('RUFF_TIMEOUT', '180') or '180')
    ruff_command = str(os.environ.get('RUFF_COMMAND', '') or '')
    python_path = str(os.environ.get('RUFF_PYTHONPATH', '') or '')
    extra_targets: list[Path] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        lowered = token.lower()
        if lowered in aliases:
            index += 1
            continue
        if lowered in output_aliases and index + 1 < len(tokens):
            candidate = Path(tokens[index + 1].strip('"\''))
            report_path = candidate if candidate.is_absolute() else BASE_DIR / candidate
            index += 2
            continue
        if lowered in timeout_aliases and index + 1 < len(tokens):
            timeout_seconds = tokens[index + 1]
            index += 2
            continue
        if lowered in command_aliases and index + 1 < len(tokens):
            ruff_command = tokens[index + 1]
            index += 2
            continue
        if lowered in pythonpath_aliases and index + 1 < len(tokens):
            python_path = tokens[index + 1]
            index += 2
            continue
        if lowered.startswith('--') or lowered.startswith('-') or lowered.startswith('/'):
            index += 1
            continue
        if lowered.endswith('.py') or Path(token).exists() or (BASE_DIR / token).exists():
            candidate = Path(token.strip('"\''))
            extra_targets.append(candidate if candidate.is_absolute() else BASE_DIR / candidate)
        index += 1
    try:
        runner_path = BASE_DIR / 'tools' / 'run_ruff.py'
        if not runner_path.exists():
            raise RuntimeError('tools/run_ruff.py is missing')
        python_exe = str(sys.executable or 'python')
        runner_args = [python_exe, '-S', str(runner_path), '--root', str(BASE_DIR), '--output', str(report_path), '--timeout', str(timeout_seconds)]
        if ruff_command:
            runner_args.extend(['--ruff-command', ruff_command])
        if python_path:
            runner_args.extend(['--pythonpath', python_path])
        runner_args.extend([str(BASE_DIR / 'start.py'), str(GTP_PATH)])
        runner_args.extend(str(path) for path in extra_targets)
        os.execv(python_exe, runner_args)  # lifecycle-bypass-ok phase-ownership-ok early-cli-exec-replaces-current-process
        os._exit(1)  # lifecycle-ok: execv only returns if the early Ruff CLI tool could not replace the process.
    except BaseException as exc:
        captureException(None, source='start.py', context='except@545')
        try:
            File.writeText(report_path, f'ERROR: {type(exc).__name__}: {exc}\n{traceback.format_exc()}\n', encoding='utf-8')
        except Exception as error:
            captureException(None, source='start.py', context='except@548')
            print(f"[WARN:swallowed-exception] start.py:ruff-entrypoint {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        print(f'[RUFF] fatal: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        os._exit(1)  # lifecycle-ok: fatal early Ruff CLI failure before launcher lifecycle exists.


_prompt_fast_ruff_entrypoint()

def _prompt_fast_vulture_entrypoint() -> None:
    """Fast self-Vulture CLI path: scan start.py first, then the bootstrapped app."""
    tokens = [str(token or '').strip() for token in sys.argv[1:] if str(token or '').strip()]
    aliases = {'vulture', '--vulture', '-vulture', '/vulture', 'dead-code', '--dead-code', '-dead-code', '/dead-code', 'deadcode', '--deadcode', '-deadcode', '/deadcode', 'dead-code-report', '--dead-code-report', '-dead-code-report', '/dead-code-report'}
    if not any(token.lower() in aliases for token in tokens):
        return
    output_aliases = {'--vulture-output', '-vulture-output', '/vulture-output', 'vulture-output', '--vulture-report', '-vulture-report', '/vulture-report', 'vulture-report', '--vulture-file', '-vulture-file', '/vulture-file', 'vulture-file'}
    confidence_aliases = {'--vulture-min-confidence', '-vulture-min-confidence', '/vulture-min-confidence', 'vulture-min-confidence', '--vulture-confidence', '-vulture-confidence', '/vulture-confidence', 'vulture-confidence'}
    report_path = BASE_DIR / 'Vulture.txt'
    confidence = 60
    extra_targets: list[Path] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        lowered = token.lower()
        if lowered in aliases:
            index += 1
            continue
        if lowered in output_aliases:
            if index + 1 < len(tokens):
                candidate = Path(tokens[index + 1].strip('"\''))
                report_path = candidate if candidate.is_absolute() else BASE_DIR / candidate
                index += 2
                continue
        if lowered in confidence_aliases:
            if index + 1 < len(tokens):
                try:
                    confidence = max(0, min(100, int(float(tokens[index + 1]))))
                except Exception as error:
                    captureException(None, source='start.py', context='except@584')
                    print(f"[WARN:swallowed-exception] start.py:288 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    confidence = 60
                index += 2
                continue
        if lowered.endswith('.py') or Path(token).exists() or (BASE_DIR / token).exists():
            candidate = Path(token.strip('"\''))
            extra_targets.append(candidate if candidate.is_absolute() else BASE_DIR / candidate)
        index += 1
    targets: list[Path] = []
    for candidate in [BASE_DIR / 'start.py', GTP_PATH, *extra_targets]:
        try:
            resolved = candidate.resolve()
        except Exception as error:
            captureException(None, source='start.py', context='except@597')
            print(f"[WARN:swallowed-exception] start.py:300 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            continue
        if resolved.exists() and resolved not in targets:
            targets.append(resolved)
    try:
        runner_path = BASE_DIR / 'tools' / 'run_vulture.py'
        if not runner_path.exists():
            raise RuntimeError('tools/run_vulture.py is missing')
        python_exe = str(sys.executable or 'python')
        os.execv(python_exe, [python_exe, '-S', str(runner_path), '--root', str(BASE_DIR), '--output', str(report_path), '--min-confidence', str(confidence), *[str(path) for path in targets]])  # lifecycle-bypass-ok phase-ownership-ok early-vulture-exec-replaces-current-process
        os._exit(1)  # lifecycle-ok: early Vulture execv failure before launcher lifecycle exists.
    except BaseException as exc:
        captureException(None, source='start.py', context='except@609')
        try:
            File.writeText(report_path, f'ERROR: {type(exc).__name__}: {exc}\n{traceback.format_exc()}\n', encoding='utf-8')
        except Exception as error:
            captureException(None, source='start.py', context='except@612')
            print(f"[WARN:swallowed-exception] start.py:314 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        print(f'[VULTURE] fatal: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        os._exit(1)  # lifecycle-ok: fatal early Vulture CLI failure before launcher lifecycle exists.


_prompt_fast_vulture_entrypoint()


def childDebuggerSourceNames() -> tuple[str, ...]:
    names: list[str] = []
    for raw in (str(getattr(GTP_PATH, 'name', EMPTY_STRING) or EMPTY_STRING).strip(), 'gtp.py'):
        lowered = str(raw or EMPTY_STRING).strip().lower()
        if lowered and lowered not in names:
            names.append(lowered)
    return tuple(names)


def childDebuggerTargetLabel() -> str:
    return str(getattr(GTP_PATH, 'name', 'child') or 'child').strip() or 'child'
LOG_DIR = BASE_DIR / 'logs'
SCREENSHOTS_DIR = BASE_DIR / 'screenshots'
DEBUG_LOG_PATH = BASE_DIR / 'debug.log'
SNAPSHOT_LOG_PATH = BASE_DIR / 'snapshots.txt'
EXCEPTION_LOG_PATH = LOG_DIR / 'exceptions.log'
PARENT_FAULT_LOG_PATH = LOG_DIR / 'start_fault.log'
BUNDLE_REQUIRED_RELATIVE_PATHS = (
    Path('start.py'),
    Path('prompt_app.py'),
    Path('js/prompt_generator.js'),
    Path('prompts/01_session_bootstrap/01_initial_response_preferences.prompt.md'),
    Path('doctypes/01_normal_doctype.md'),
    Path('vendor/jquery/jquery-4.0.0.min.js'),
)
DEBUG_ARTIFACT_FILE_NAMES = {
    'debug.log',
    'exceptions.log',
    'snapshots.txt',
    'start_fault.log',
    'trace.log',
    'trace-full.log',
}
DEBUGGER_VERSION = '1.0.0'
DEPLOY_MONITOR_DIR = BASE_DIR / 'debs'
LINUX_RUNTIME_DIR = BASE_DIR / 'linux_runtime'
PYTHON_DEPENDENCIES = (
    {'import_name': 'PySide6', 'pip_package': 'PySide6', 'apt_package': 'python3-pyside6.qtwidgets', 'pacman_package': 'mingw-w64-ucrt-x86_64-pyside6', 'gui_only': True, 'required': True},
    {'import_name': 'PySide6.QtCore', 'pip_package': 'PySide6', 'apt_package': 'python3-pyside6.qtcore', 'pacman_package': 'mingw-w64-ucrt-x86_64-pyside6', 'gui_only': True, 'required': True},
    {'import_name': 'PySide6.QtGui', 'pip_package': 'PySide6', 'apt_package': 'python3-pyside6.qtgui', 'pacman_package': 'mingw-w64-ucrt-x86_64-pyside6', 'gui_only': True, 'required': True},
    {'import_name': 'PySide6.QtWidgets', 'pip_package': 'PySide6', 'apt_package': 'python3-pyside6.qtwidgets', 'pacman_package': 'mingw-w64-ucrt-x86_64-pyside6', 'gui_only': True, 'required': True},
    {'import_name': 'PySide6.QtPrintSupport', 'pip_package': 'PySide6', 'apt_package': 'python3-pyside6.qtprintsupport', 'pacman_package': 'mingw-w64-ucrt-x86_64-pyside6', 'gui_only': True, 'required': True},
    {'import_name': 'PySide6.QtWebChannel', 'pip_package': 'PySide6', 'apt_package': 'python3-pyside6.qtwebchannel', 'pacman_package': 'mingw-w64-ucrt-x86_64-pyside6', 'gui_only': True, 'required': True},
    {'import_name': 'PySide6.QtWebEngineCore', 'pip_package': 'PySide6', 'apt_package': 'python3-pyside6.qtwebenginecore', 'pacman_package': 'mingw-w64-ucrt-x86_64-pyside6', 'gui_only': True, 'required': True},
    {'import_name': 'PySide6.QtWebEngineWidgets', 'pip_package': 'PySide6', 'apt_package': 'python3-pyside6.qtwebenginewidgets', 'pacman_package': 'mingw-w64-ucrt-x86_64-pyside6', 'gui_only': True, 'required': True},
    {'import_name': 'fontTools.ttLib', 'pip_package': 'fonttools', 'gui_only': False, 'required': True},
    {'import_name': 'fontTools.subset', 'pip_package': 'fonttools', 'gui_only': False, 'required': True},
    {'import_name': 'fontTools.ttLib.woff2', 'pip_package': 'fonttools', 'gui_only': False, 'required': True},
    {'import_name': 'sqlalchemy', 'pip_package': 'SQLAlchemy', 'gui_only': False, 'required': True},
    {'import_name': 'brotli', 'pip_package': 'brotli', 'gui_only': False, 'required': False},
    {'import_name': 'brotlicffi', 'pip_package': 'brotlicffi', 'gui_only': False, 'required': False},
    {'import_name': 'paramiko', 'pip_package': 'paramiko', 'gui_only': False, 'required': False},
    {'import_name': 'vulture', 'pip_package': 'vulture', 'gui_only': False, 'required': False},
    {'import_name': 'psutil', 'pip_package': 'psutil', 'gui_only': False, 'required': False},
    {'import_name': 'restartmgr', 'pip_package': 'restartmgr', 'gui_only': False, 'required': False},
)

KNOWN_CHILD_DEBUGGER_SURFACES = (
    'heartbeat',
    'vardump',
    'poll',
    'debugger-exec-command',
    'debugger-cron-command',
    'accepts-proxy',
)

DEBUGGER_COMMAND_ALIASES = {
    'quit': {'q', 'quit', 'bye', 'esc'},
    'vars': {'2', 'v', 'vars', 'locals', 'snapshot'},
    'exceptions': {'1', 'i', 'exception', 'exceptions', 'errors', 'dberrors', 'dbexceptions'},
    'close': {'3', 'x', 'close', 'requestclose', 'exit'},
    'kill': {'5', 'k', 'kill', 'forcekill'},
    'clear_screen': {'6', 'b', 'f', 'blank', 'clear', 'cls', 'wipe', 'redraw', 'refresh'},
    'exec_code': {'7', 'e', 'exec', 'execute', 'executecode', 'code'},
    'create_cron': {'8', 'c', 'cron', 'createcron', 'schedule'},
    'connection_monitor': {'m', 'connwatch', 'watchconn', 'monitorconn', 'monitor'},
    'logs': {'d', 'dump', 'dumplogs', 'logs', 'log', 'printlogs'},
    'status': {'s', 'status'},
    'help': {'h', '?', 'help', 'menu'},
    'restart': {'r', 'restart', 'relaunch'},
    'crash_context': {'l', 'line', 'lines', 'crash', 'context', 'crashline', 'source'},
    'ast_tree': {'9', 'g', 'ast', 'tree', 'asttree', 'generateast', 'generateasttree'},
    'git_tools': {'j', 'git', '.git', 'gittools'},
}






class StartExecutionLifecycle:
    """Central lifecycle surface for start.py-owned threads and processes."""

    def __init__(self):
        self.phases: dict[int, dict[str, Any]] = {}
        self.threads: dict[str, Any] = {}
        self.processes: dict[str, Any] = {}
        self.phaseCounter = 100000  # noqa: nonconform

    def nextPhaseKey(self) -> int:
        self.phaseCounter += 1
        return self.phaseCounter

    def registerPhase(self, name: str, kind: str, ttl: float = 0.0, handle=None) -> int:
        key = self.nextPhaseKey()
        self.phases[key] = {
            'key': key,
            'name': str(name or f'phase-{key}'),
            'kind': str(kind or 'unknown'),
            'ttl': float(ttl or 0.0),
            'handle': handle,
            'started_at': time.time(),
            'status': 'registered',
            'pid': int(getattr(handle, 'pid', 0) or os.getpid()),
        }
        return key

    def startThread(self, name: str, target, daemon: bool = False):
        thread_ctor = getattr(threading, 'Thread')
        thread = thread_ctor(target=target, name=str(name or 'StartLifecycleThread'), daemon=bool(daemon))
        self.threads[str(name or getattr(thread, 'name', 'thread'))] = thread
        key = self.registerPhase(str(name or getattr(thread, 'name', 'thread')), 'thread', handle=thread)
        self.phases[key]['status'] = 'running'
        thread.start()
        return thread

    def startProcess(self, name: str, args, **kwargs):
        command_text = subprocess.list2cmdline([str(part) for part in args]) if isinstance(args, (list, tuple)) else str(args)
        cwd_text = str(kwargs.get('cwd') or os.getcwd())
        if int(kwargs.get('creationflags', 0) or 0):
            appendRunLog(f'[PROCESS:WARN] {name} requested creationflags={kwargs.get("creationflags")} command={command_text}')
        appendRunLog(f'[PROCESS:START] name={name} cwd={cwd_text} command={command_text}')
        proc = PhaseProcess.popen(args, phase_name=str(name or 'StartExecutionLifecycle.startProcess'), **kwargs)
        self.processes[str(name or getattr(proc, 'pid', 'process'))] = proc
        key = self.registerPhase(str(name or f'process-{getattr(proc, "pid", 0)}'), 'process', handle=proc)
        self.phases[key]['pid'] = int(getattr(proc, 'pid', 0) or 0)
        self.phases[key]['status'] = 'running'
        self.phases[key]['command'] = command_text
        appendRunLog(f'[PROCESS:STARTED] name={name} pid={int(getattr(proc, "pid", 0) or 0)} phase={key}')
        return proc

    def runCommand(self, name: str, args, **kwargs):
        key = self.registerPhase(str(name or 'subprocess-run'), 'subprocess-run', handle=None)
        self.phases[key]['status'] = 'running'
        self.phases[key]['command'] = subprocess.list2cmdline([str(part) for part in args]) if isinstance(args, (list, tuple)) else str(args)
        try:
            result = PhaseProcess.run(args, phase_name=str(name or 'StartExecutionLifecycle.runCommand'), **kwargs)
            self.phases[key]['status'] = 'complete' if int(getattr(result, 'returncode', 0) or 0) == 0 else 'errored'
            self.phases[key]['exit_code'] = int(getattr(result, 'returncode', 0) or 0)
            return result
        except Exception as error:
            captureException(None, source='start.py', context='except@763')
            self.phases[key]['status'] = 'exception'
            self.phases[key]['error'] = f'{type(error).__name__}: {error}'
            raise

    def snapshots(self) -> list[dict[str, Any]]:
        return [dict(value) for key, value in sorted(self.phases.items())]


START_EXECUTION_LIFECYCLE = StartExecutionLifecycle()


def startLifecycleRunCommand(args, **kwargs):
    return START_EXECUTION_LIFECYCLE.runCommand('subprocess-run', args, **kwargs)


_IMPORT_MODULE_CACHE: dict[tuple[str, str], bool] = {}
_PREFERRED_CHILD_CACHE: dict[tuple[bool], str] = {}

if HAS_SQLALCHEMY or TYPE_CHECKING:
    class StartOrmBase(DeclarativeBase):
        pass

    class DebuggerHeartbeatRecord(StartOrmBase):
        __tablename__ = "heartbeat"
        id = Column(Integer, primary_key=True, autoincrement=True)
        created = Column(Text)
        heartbeatMicrotime = Column("heartbeat_microtime", Float)
        source = Column(Text)
        eventKind = Column("event_kind", Text)
        reason = Column(Text)
        caller = Column(Text)
        phase = Column(Text)
        pid = Column(Integer)
        stackTrace = Column("stack_trace", Text)
        varDump = Column("var_dump", Text)
        processSnapshot = Column("process_snapshot", Text)
        execCode = Column("exec", Text)
        execIsFile = Column("exec_is_file", Integer, default=0)
        cronCode = Column("cron", Text)
        cronIsFile = Column("cron_is_file", Integer, default=0)
        cronIntervalSeconds = Column("cron_interval_seconds", Float, default=0.0)
        processed = Column(Integer, default=0)

    class DebuggerTrafficRecord(StartOrmBase):
        __tablename__ = "traffic"
        id = Column(Integer, primary_key=True, autoincrement=True)
        created = Column(Text)
        headers = Column(Text)
        data = Column(Text)
        status = Column(Text)
        error = Column(Text)
        length = Column(Integer)
        destination = Column(Text)
        roundtripMicrotime = Column("roundtrip_microtime", Float)
        processed = Column(Integer, default=0)
        caller = Column(Text)
        responsePreview = Column("response_preview", Text)

    class DebuggerExceptionRecord(StartOrmBase):
        __tablename__ = "exceptions"
        id = Column(Integer, primary_key=True, autoincrement=True)
        created = Column(Text)
        source = Column(Text)
        context = Column(Text)
        typeName = Column("type_name", Text)
        message = Column(Text)
        tracebackText = Column("traceback_text", Text)
        sourceContext = Column("source_context", Text)
        thread = Column(Text)
        pid = Column(Integer)
        handled = Column(Integer, default=1)
        processed = Column(Integer, default=0)

    class DebuggerFaultRecord(StartOrmBase):
        __tablename__ = "faults"
        id = Column(Integer, primary_key=True, autoincrement=True)
        created = Column(Text)
        source = Column(Text)
        reason = Column(Text)
        caller = Column(Text)
        stackTrace = Column("stack_trace", Text)
        varDump = Column("var_dump", Text)
        processSnapshot = Column("process_snapshot", Text)
        thread = Column(Text)
        pid = Column(Integer)
        processed = Column(Integer, default=0)

    class DebuggerProcessRecord(StartOrmBase):
        __tablename__ = "processes"
        id = Column(Integer, primary_key=True, autoincrement=True)
        created = Column(Text)
        updated = Column(Text)
        source = Column(Text)
        phaseKey = Column("phase_key", Text)
        phaseName = Column("phase_name", Text)
        processName = Column("process_name", Text)
        kind = Column(Text)
        pid = Column(Integer)
        status = Column(Text)
        startedAt = Column("started_at", Float)
        endedAt = Column("ended_at", Float)
        ttlSeconds = Column("ttl_seconds", Float)
        exitCode = Column("exit_code", Integer)
        errorType = Column("error_type", Text)
        errorMessage = Column("error_message", Text)
        tracebackText = Column("traceback_text", Text)
        faultReason = Column("fault_reason", Text)
        command = Column(Text)
        metadataText = Column("metadata", Text)
        processed = Column(Integer, default=0)
else:
    StartOrmBase = cast(Any, None)
    DebuggerHeartbeatRecord = cast(Any, None)
    DebuggerTrafficRecord = cast(Any, None)
    DebuggerExceptionRecord = cast(Any, None)
    DebuggerFaultRecord = cast(Any, None)
    DebuggerProcessRecord = cast(Any, None)


def buildCliAliasSet(*names: str, includeQuestion: bool = False) -> set[str]:
    aliases: set[str] = set()
    for rawName in list(names or []):
        name = str(rawName or EMPTY_STRING).strip().lower()
        if not name:
            continue
        aliases.add(name)
        if name == '?':
            aliases.add('/?')
            continue
        aliases.add('/' + name)
        aliases.add('-' + name)
        aliases.add('--' + name)
    if includeQuestion:
        aliases.update({'?', '/?'})
    return aliases


def stripCliValueQuotes(value: str) -> str:
    text = str(value or EMPTY_STRING).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1].strip()
    return text


def cliTokenLooksLikeOption(token: str, knownAliases: set[str] | None = None) -> bool:
    lowered = str(token or EMPTY_STRING).strip().lower()
    if not lowered:
        return False
    aliases = set(knownAliases or set())
    if lowered in aliases:
        return True
    for alias in list(aliases):
        for separator in ('=', ':'):
            if lowered.startswith(alias + separator):
                return True
    return lowered.startswith('-') or lowered.startswith('/') or lowered == '?'


def readCliOption(argv=None, aliases=None, takesValue: bool = False, knownAliases: set[str] | None = None) -> dict[str, object]:
    tokens = [str(token or EMPTY_STRING).strip() for token in list(argv or []) if str(token or EMPTY_STRING).strip()]
    aliasSet = {str(alias or EMPTY_STRING).strip().lower() for alias in list(aliases or []) if str(alias or EMPTY_STRING).strip()}
    state = {'present': False, 'value': EMPTY_STRING, 'missing_value': False, 'token': EMPTY_STRING, 'index': -1}
    for index, token in enumerate(tokens):
        lowered = str(token or EMPTY_STRING).strip().lower()
        for alias in list(aliasSet):
            if lowered == alias:
                state.update({'present': True, 'token': token, 'index': index})
                if takesValue:
                    if index + 1 < len(tokens) and not cliTokenLooksLikeOption(tokens[index + 1], knownAliases or aliasSet):
                        state['value'] = stripCliValueQuotes(tokens[index + 1])
                    else:
                        state['missing_value'] = True
                return state
            for separator in ('=', ':'):
                prefix = alias + separator
                if lowered.startswith(prefix):
                    valueText = stripCliValueQuotes(token[len(alias) + 1:])
                    state.update({'present': True, 'token': token, 'index': index, 'value': valueText})
                    if takesValue and not valueText:
                        state['missing_value'] = True
                    return state
    return state


TRACE_FLAGS = buildCliAliasSet('trace')
TRACE_VERBOSE_FLAGS = buildCliAliasSet('trace-verbose', 'verbose-trace')
HEADLESS_FLAGS = buildCliAliasSet('headless')
OFFSCREEN_FLAGS = buildCliAliasSet('offscreen')
XDUMMY_FLAGS = buildCliAliasSet('xdummy', 'dummy')
XPRA_FLAGS = buildCliAliasSet('xpra')
BG_COLOR_FLAGS = buildCliAliasSet('bgcolor', 'bg', 'background-color')
BG_ALPHA_FLAGS = buildCliAliasSet('bgalpha', 'bg-alpha', 'background-alpha')
OFFSCREEN_CLICK_FLAGS = buildCliAliasSet('offscreen-click', 'click')
OFFSCREEN_MOVE_FLAGS = buildCliAliasSet('offscreen-move', 'move', 'hover')
OFFSCREEN_WAIT_FLAGS = buildCliAliasSet('offscreen-wait', 'wait', 'pause', 'sleep')
OFFSCREEN_KEY_FLAGS = buildCliAliasSet('offscreen-key', 'key')
OFFSCREEN_TYPE_FLAGS = buildCliAliasSet('offscreen-type', 'type')
DEBUG_FLAGS = buildCliAliasSet('debug')
MARIA_FLAGS = buildCliAliasSet('maria', 'mariadb')
CHILD_LANGUAGE_FLAGS = buildCliAliasSet('language', 'lang', 'lan', 'l', 'langauge', 'launguage', 'english', 'en', 'spanish', 'es')
CHILD_HELP_FLAGS = buildCliAliasSet('help', 'h', 'man', includeQuestion=True)
CHILD_USAGE_FLAGS = buildCliAliasSet('usage', 'u')
CHILD_ABOUT_FLAGS = buildCliAliasSet('about', 'a')
CHILD_VERSION_FLAGS = buildCliAliasSet('version', 'ver', 'v')
CHILD_DEBUGGER_QUERY_SURFACES_FLAGS = buildCliAliasSet('debugger-query-surfaces')
DEPLOY_MONITOR_FLAGS = buildCliAliasSet('deploy-monitor', 'deploy', 'deployzips', 'autodeploy')
DEPLOY_ONCE_FLAGS = buildCliAliasSet('deploy-once')
PROXY_DAEMON_FLAGS = buildCliAliasSet('is-proxy-daemon')
PROXY_BIND_FLAGS = buildCliAliasSet('proxy-bind')
GIT_FLAGS = buildCliAliasSet('git')
PYINSTALLER_FLAGS = buildCliAliasSet('pyinstaller', 'py-installer', 'build-pyinstaller', 'compile-pyinstaller')
NIKITA_FLAGS = buildCliAliasSet('nikita', 'nuitka', 'build-nikita', 'build-nuitka', 'compile-nikita', 'compile-nuitka')
PACKAGE_BUILD_FLAGS = buildCliAliasSet('package', 'packages', 'build-package', 'installer-package', 'installers', 'wix', 'inno', 'msi')
PACKAGE_DRY_RUN_FLAGS = buildCliAliasSet('package-dry-run', 'packager-dry-run', 'dry-run-package', 'dry-run')
BUILD_FLAGS = buildCliAliasSet('build', 'rebuild', 'force-build', 'force-rebuild')
INSTALLER_FLAGS = buildCliAliasSet('installer', 'nsis', 'build-installer', 'build-nsis', 'compile-installer')
PUSH_FLAGS = buildCliAliasSet('push', 'publish', 'upload-release', 'deploy-update', 'push-server', 'server-push', 'winscp-deploy', 'deploy', 'deploy-preview')
VULTURE_FLAGS = buildCliAliasSet('vulture', 'dead-code', 'deadcode', 'dead-code-report')
RUFF_FLAGS = buildCliAliasSet('ruff', 'rough')
RUFF_OUTPUT_FLAGS = buildCliAliasSet('ruff-output', 'ruff-log', 'rough-output', 'rough-log')
RUFF_TIMEOUT_FLAGS = buildCliAliasSet('ruff-timeout', 'rough-timeout')
VULTURE_OUTPUT_FLAGS = buildCliAliasSet('vulture-output', 'vulture-report', 'vulture-file')
VULTURE_MIN_CONFIDENCE_FLAGS = buildCliAliasSet('vulture-min-confidence', 'vulture-confidence')
PYRITE_FLAGS = buildCliAliasSet('pyrite', 'pyright', 'pirate', 'plyrate')
PYRITE_OUTPUT_FLAGS = buildCliAliasSet('pyrite-output', 'pyrite-log', 'pyright-output')
PYRITE_JSON_FLAGS = buildCliAliasSet('pyrite-json', 'pyright-json')
PYRITE_TIMEOUT_FLAGS = buildCliAliasSet('pyrite-timeout', 'pyright-timeout')
PYRITE_CONFIG_FLAGS = buildCliAliasSet('pyrite-config', 'pyright-config')


# Valid start.py / gtp.py CLI surface area.
# Keep the accepted aliases high in the file so dead CLI switches are obvious,
# warnings are consistent, and both start.py and gtp.py can recognize the same
# case-insensitive spellings like /l, --language, language=Russian, /u, /?, /a,
# and ver without sprinkling one-off checks across the code.
START_ARG_FLAGS = buildCliAliasSet('arg')
START_ARGS_FLAGS = buildCliAliasSet('args')
START_VALID_FLAG_GROUPS = {
    'trace': set(TRACE_FLAGS),
    'trace_verbose': set(TRACE_VERBOSE_FLAGS),
    'headless': set(HEADLESS_FLAGS),
    'offscreen': set(OFFSCREEN_FLAGS),
    'xdummy': set(XDUMMY_FLAGS),
    'xpra': set(XPRA_FLAGS),
    'bgcolor': set(BG_COLOR_FLAGS),
    'bgalpha': set(BG_ALPHA_FLAGS),
    'offscreen_click': set(OFFSCREEN_CLICK_FLAGS),
    'offscreen_move': set(OFFSCREEN_MOVE_FLAGS),
    'offscreen_wait': set(OFFSCREEN_WAIT_FLAGS),
    'offscreen_key': set(OFFSCREEN_KEY_FLAGS),
    'offscreen_type': set(OFFSCREEN_TYPE_FLAGS),
    'debug': set(DEBUG_FLAGS),
    'maria': set(MARIA_FLAGS),
    'arg': set(START_ARG_FLAGS),
    'args': set(START_ARGS_FLAGS),
    'proxy_daemon': set(PROXY_DAEMON_FLAGS),
    'proxy_bind': set(PROXY_BIND_FLAGS),
    'git': set(GIT_FLAGS),
    'vulture': set(VULTURE_FLAGS),
    'ruff': set(RUFF_FLAGS),
    'ruff_output': set(RUFF_OUTPUT_FLAGS),
    'ruff_timeout': set(RUFF_TIMEOUT_FLAGS),
    'vulture_output': set(VULTURE_OUTPUT_FLAGS),
    'vulture_min_confidence': set(VULTURE_MIN_CONFIDENCE_FLAGS),
    'pyrite': set(PYRITE_FLAGS),
    'pyrite_output': set(PYRITE_OUTPUT_FLAGS),
    'pyrite_json': set(PYRITE_JSON_FLAGS),
    'pyrite_timeout': set(PYRITE_TIMEOUT_FLAGS),
    'pyrite_config': set(PYRITE_CONFIG_FLAGS),
}
KNOWN_CHILD_FLAG_GROUPS = {
    'headless': set(HEADLESS_FLAGS),
    'offscreen': set(OFFSCREEN_FLAGS),
    'xdummy': set(XDUMMY_FLAGS),
    'xpra': set(XPRA_FLAGS),
    'bgcolor': set(BG_COLOR_FLAGS),
    'bgalpha': set(BG_ALPHA_FLAGS),
    'offscreen_click': set(OFFSCREEN_CLICK_FLAGS),
    'offscreen_move': set(OFFSCREEN_MOVE_FLAGS),
    'offscreen_wait': set(OFFSCREEN_WAIT_FLAGS),
    'offscreen_key': set(OFFSCREEN_KEY_FLAGS),
    'offscreen_type': set(OFFSCREEN_TYPE_FLAGS),
    'debug': set(DEBUG_FLAGS),
    'maria': set(MARIA_FLAGS),
    'help': set(CHILD_HELP_FLAGS),
    'usage': set(CHILD_USAGE_FLAGS),
    'about': set(CHILD_ABOUT_FLAGS),
    'version': set(CHILD_VERSION_FLAGS),
    'debugger_query_surfaces': set(CHILD_DEBUGGER_QUERY_SURFACES_FLAGS),
    'language': set(CHILD_LANGUAGE_FLAGS),
    'pyinstaller': set(PYINSTALLER_FLAGS),
    'nikita': set(NIKITA_FLAGS),
    'package_build': set(PACKAGE_BUILD_FLAGS),
    'package_dry_run': set(PACKAGE_DRY_RUN_FLAGS),
    'installer': set(INSTALLER_FLAGS),
    'push': set(PUSH_FLAGS),
}
START_VALID_CLI_ALIASES = set().union(*START_VALID_FLAG_GROUPS.values(), *KNOWN_CHILD_FLAG_GROUPS.values())


# https://www.triodesktop.com — FLATLINE launcher CLI and debugger entry helpers.
START_MAN_SECTIONS = {
    'toc': """FLATLINE Debugger v1.0.0 — CLI Table of Contents

Site: https://www.triodesktop.com

Sections:
  man toc       Show this table of contents
  man aliases   CLI aliases and quick entry points
  man launch    Core start.py launch modes
  man capture   Offscreen / Xdummy / Xpra capture flags
  man proxy     Traffic monitor / proxy flags
  man deploy    Deploy monitor / zip extraction modes
  man debugger  Debugger console commands
  man build     Build/package/release pipeline
  man detectors Claude static detector suite
  man exec      Execute / cron transport notes
  man notes     Ownership, logs, and runtime notes
  man about     Author / contact / homepage block

Quick aliases:
  /help  -help  --help  /?  man
  usage  -usage  --usage  -u
  ver  /v  --version  -version
  about  /about  -about  --about  /a
""",
    'aliases': """FLATLINE Debugger — CLI aliases

Site: https://www.triodesktop.com

  /help  -help  --help  /?  man
  usage  -usage  --usage  -u
  ver  /v  --version  -version
  about  /about  -about  --about  /a

Examples:
  start.py man toc
  start.py man debugger
  start.py /a
""",
    'launch': """FLATLINE Debugger — core launch

Site: https://www.triodesktop.com

  start.py                     Launch prompt_app.py through the FLATLINE parent launcher
  start.py --debug             Launch with debugger relay, crash console, heartbeat, and DB debugger transport
  start.py --vulture [target]  Run vendored Vulture against start.py first, then the bootstrapped app file, and write Vulture.txt
  start.py --pyrite           Run Pyrite/Pyright against start.py first, then the bootstrapped app file, write Pyrite.log, and print diagnostics
  start.py --ruff             Run real Ruff diagnostics against start.py and prompt_app.py, write ruff.txt/rough.txt, print the report, and exit
  start.py --rough            Alias for --ruff
  start.py --bypass-detector  Run vendored Claude lifecycle-bypass detector and write reports/claude/bypass_report.txt
  start.py --monkey / --raw-sql / --recursion / --redundant / --swallowed
  start.py --file-io / --process-faults / --phase-ownership / --threads / --bad-code / --unlocalized
  start.py --depcheck / --phasehooks / --nonconform / --comport
  start.py --claude-detectors / --certify Run all vendored Claude detectors and write reports/claude/claude_detectors_report.txt
  start.py --build           Build executable artifacts only: Prompt-PyInstaller.exe and Prompt-Nuitka.exe
  start.py --build package   Build installer artifacts only from an already-tested executable
  start.py --push             Push existing release artifacts only; add --build to rebuild first
  start.py --headless          Fast child/headless path
  start.py --mariadb           Prefer MariaDB runtime path when configured
  start.py /l russian          Forward a language override to gtp.py
""",
    'build': """FLATLINE Debugger — build / installer pipeline

Site: https://www.triodesktop.com

Build safety rule:
  No executable builder, installer builder, or installer-tool downloader runs unless --build is present. Plain --build stops after executable creation so you can test the EXEs before packaging.

Commands:
  start.py --build                   Build standalone EXEs only; no installers
  start.py --build --pyinstaller     Build only dist/Prompt-PyInstaller.exe
  start.py --build --nikita          Build only dist/Prompt-Nuitka.exe
  start.py --build package           Build NSIS, Inno, and WiX installers from an already-tested EXE
  start.py --push                    Push existing release artifacts only
  start.py --push --build            Rebuild first, then push

Installer tool discovery for `start.py --build package`:
  NSIS: winget install -e --id NSIS.NSIS
  Inno Setup: winget install -e --id JRSoftware.InnoSetup
  WiX: dotnet tool install --global wix

Runtime behavior:
  Build/download/installer work runs in a separate normal Python process. Watch run.log plus logs/build_pipeline.log for PHASE:* and HEARTBEAT lines.
  Empty stdout/stderr lines are ignored so the autoload watcher does not print blank child stderr spam.

Persisted config:
  config.ini [metadata] version=1.0.0, author, coded_by
  config.ini [installer_tools] nsis_path/inno_path/wix_path and versions after discovery
""",
    'detectors': """FLATLINE Debugger — Claude detector suite

Site: https://www.triodesktop.com

Detector commands:
  start.py --claude-detectors          Run all vendored Claude detectors
  start.py --certify                   Alias for the full detector certification suite
  start.py --monkey                    Monkey-patch/protocol-swap scan
  start.py --lifecycle-bypass          Direct subprocess/thread/Qt exec bypass scan
  start.py --raw-sql                   Raw SQL connector/cursor/execute scan
  start.py --recursion                 Direct self-recursion scan
  start.py --swallowed                 Swallowed/trace-only exception handler scan
  start.py --redundant                 Consecutive duplicate-shape code scan
  start.py --file-io                   File I/O outside traced wrappers
  start.py --process-faults            Process launches missing fault/error callbacks
  start.py --phase-ownership           Lifecycle phase ownership bypasses
  start.py --threads                   Thread/process/phase safety scan
  start.py --bad-code                  General AST bad-code scan
  start.py --unlocalized               Raw Qt UI strings without localize()
  start.py --depcheck                  Dependency registration scan
  start.py --phasehooks                Phase hook architecture scan
  start.py --nonconform                Nonconformance architecture scan
  start.py --comport                   Comport/nonconformance scan

Reports:
  logs/monkeypatches.txt
  logs/lifecyclebypass.txt
  logs/rawsql.txt
  logs/recursion.txt
  logs/swallowed.txt
  logs/redundant.txt
  logs/fileio.txt
  logs/process_faults.txt
  logs/phase_ownership.txt
  logs/thread_safety.txt
  logs/badcode.txt
  logs/unlocalized.txt
  reports/claude/claude_detectors_report.txt

Alias policy:
  --monkey is the canonical monkey-patch route. Legacy spellings are accepted, but there is no second monkey detector path.
""",
    'capture': """FLATLINE Debugger — offscreen / capture

Site: https://www.triodesktop.com

  --offscreen                  Run the child inside a managed offscreen X11 display owned by start.py
  --xdummy / --dummy           Use the Xdummy capture engine
  --xpra                       Use the Xpra capture engine
  --bgcolor VALUE              Offscreen fallback background color
  --bgalpha VALUE              Offscreen fallback background alpha
  --click X,Y[,BUTTON]         Scripted click after launch
  --move X,Y                   Scripted hover / move after launch
  --wait SECONDS               Scripted pause after launch
  --key VALUE                  Scripted key action after launch
  --type VALUE                 Scripted type action after launch
""",
    'proxy': """FLATLINE Debugger — traffic monitor / proxy

Site: https://www.triodesktop.com

  --is-proxy-daemon=1          Run only the FlatLine traffic monitor HTTP server
  --proxy-bind HOST:PORT       Bind the traffic monitor to a custom address
""",
    'deploy': """FLATLINE Debugger — deploy / zip monitor

Site: https://www.triodesktop.com

  --deploy-monitor             Watch the current directory, auto-extract zip drops, flatten wrappers,
                               touch extracted files to the deployment timestamp, and relaunch the best entry script
  --deploy-once                Extract zip files in the current directory one time, flatten wrappers, then exit
  --git <args...>              Run repo git helpers against the TrioDesktop branch root
  R                            Rerun the candidate entrypoint while deploy monitor mode is active
  Q / Esc / any other key      Quit the deploy monitor
""",
    'debugger': """FLATLINE Debugger — debugger console

Site: https://www.triodesktop.com

  L  Lines of Crash            Read-only Prism / WebEngine source viewer with print / save / PDF
  S  Status                    Show launcher / child status, proxy target, files, hashes, and heartbeat
  D  Dump Logs                 Print launcher and child logs
  V  Variables                 Parent var dump + child var dump request
  M  Monitor Connections       Tail traffic-table rows from the proxy daemon
  E  Execute Code              Editable Prism / WebEngine editor, submit into heartbeat exec transport
  C  Create Cron               Editable Prism / WebEngine editor, submit repeating heartbeat cron transport
  G  Generate AST Tree         Two-tab AST viewer: Qt tree + Prism source view
  J  Git Tools                 Git / GitHub helpers, aliases, auth modal, gitignore editor
  K  Kill Child                Force kill gtp.py
  R  Restart Child             Relaunch prompt_app.py
  B  Blank Screen              Clear and redraw the debugger
  H  Help                      Show debugger help
  X  Exit Child Gracefully     Ask the child to shut down
  Q  Quit                      Leave the debugger
""",
    'exec': """FLATLINE Debugger — execute / cron transport

Site: https://www.triodesktop.com

  Inline Python works directly.
  >inject.py or >>inject.py marks the payload as a file and runs that Python file inside gtp.py.
  The child clears one-shot exec rows before running them so slow code does not double-run.
""",
    'notes': """FLATLINE Debugger — runtime notes

Site: https://www.triodesktop.com

  • start.py owns dependency/bootstrap, offscreen display setup, proxy/traffic monitor, debugger UI, and child supervision.
  • prompt_app.py owns the Prompt Qt application runtime.
  • The launcher writes logs/debug.log, logs/exceptions.log, logs/start_fault.log, snapshots.txt,
    logs/trace.log, logs/trace-full.log, screenshots/offscreen_*.png, and heartbeat/traffic DB rows.
  • Bundled Debian runtime packages are searched under /debs and /linux_runtime for offline Linux dependency recovery.
""",
}
START_MAN_SECTION_ALIASES = {
    'help': 'toc',
    'usage': 'toc',
    'toc': 'toc',
    'table': 'toc',
    'table-of-contents': 'toc',
    'contents': 'toc',
    'alias': 'aliases',
    'aliases': 'aliases',
    'launch': 'launch',
    'core': 'launch',
    'capture': 'capture',
    'offscreen': 'capture',
    'xdummy': 'capture',
    'xpra': 'capture',
    'proxy': 'proxy',
    'monitor': 'proxy',
    'deploy': 'deploy',
    'zip': 'deploy',
    'debugger': 'debugger',
    'console': 'debugger',
    'detectors': 'detectors',
    'detector': 'detectors',
    'claude': 'detectors',
    'exec': 'exec',
    'execute': 'exec',
    'cron': 'exec',
    'notes': 'notes',
    'build': 'build',
    'installer': 'build',
    'installers': 'build',
    'package': 'build',
    'about': 'about',
}


PROMPT_APP_VERSION = '1.0.0'

START_LOCALIZED_TEXT = {
    'EN': {
        'version_title': 'Prompt {version}',
        'md5_label': 'prompt_app.py MD5',
        'path_label': 'Application file',
        'usage': """Prompt 1.0.0 — Usage

Usage:
  python start.py [options]

Common options:
  -v, -ver, --version        Print Prompt version and prompt_app.py MD5
  /?, /help, --help          Print command help
  man [section]              Print a detailed manual page
  --usage                    Print this usage page
  --spanish, es              Start or print help in Spanish
  --english, en              Start or print help in English
  --build                    Start the executable + installer build pipeline in a background process
  --push                     Push existing release files; add --build to rebuild first
  --debug                    Launch through debugger relay
  --claude-detectors         Run all vendored detectors and write reports
  --monkey, --raw-sql, --recursion, --redundant, --swallowed
  --file-io, --process-faults, --phase-ownership, --threads, --bad-code, --unlocalized
  --depcheck, --phasehooks, --nonconform, --comport
                             Run one detector and exit

Language:
  config.ini [ui] language = EN or ES
  CLI language flags override config.ini for that run.
""",
        'about_extra': 'Prompt is a desktop prompt generator/workbench for prompts, doctypes, workflows, packaging, and release push.',
    },
    'ES': {
        'version_title': 'Prompt {version}',
        'md5_label': 'MD5 de prompt_app.py',
        'path_label': 'Archivo de aplicación',
        'usage': """Prompt 1.0.0 — Uso

Uso:
  python start.py [opciones]

Opciones comunes:
  -v, -ver, --version        Muestra la versión de Prompt y el MD5 de prompt_app.py
  /?, /help, --help          Muestra la ayuda de comandos
  man [sección]              Muestra una página de manual más detallada
  --usage                    Muestra esta página de uso
  --spanish, es              Inicia o muestra ayuda en español
  --english, en              Inicia o muestra ayuda en inglés
  --build                    Inicia el flujo de compilación e instaladores en un proceso separado
  --push                     Sube archivos existentes; agrega --build para recompilar primero
  --debug                    Inicia con el depurador
  --claude-detectors         Ejecuta todos los detectores vendorizados y escribe reportes
  --monkey, --raw-sql, --recursion, --redundant, --swallowed, --file-io, --process-faults, --phase-ownership, --threads, --bad-code, --unlocalized
  --depcheck, --phasehooks, --nonconform, --comport
                             Ejecuta un detector y sale

Idioma:
  config.ini [ui] language = EN o ES
  Las opciones de idioma de CLI reemplazan config.ini durante esa ejecución.
""",
        'about_extra': 'Prompt es un generador de prompts y banco de trabajo de escritorio para prompts, doctypes, flujos, empaquetado y publicación.',
    },
}


def startNormalizeLanguage(value: Any, default: str = 'EN') -> str:
    raw = str(value or EMPTY_STRING).strip().lower()
    if raw in {'es', 'esp', 'spanish', 'espanol', 'español', 'castellano', '-es', '--es', '/es', '-spanish', '--spanish', '/spanish'}:
        return 'ES'
    if raw in {'en', 'eng', 'english', 'ingles', 'inglés', '-en', '--en', '/en', '-english', '--english', '/english'}:
        return 'EN'
    return default if default in {'EN', 'ES'} else 'EN'


def startCliLanguageCode(tokens=None) -> str:
    raw_tokens = [str(value or EMPTY_STRING).strip() for value in list(tokens or []) if str(value or EMPTY_STRING).strip()]
    for token in raw_tokens:
        lowered = token.lower()
        if lowered in {'es', '-es', '--es', '/es', 'spanish', '-spanish', '--spanish', '/spanish'}:
            return 'ES'
        if lowered in {'en', '-en', '--en', '/en', 'english', '-english', '--english', '/english'}:
            return 'EN'
        for prefix in ('--language=', '-language=', '/language=', 'language=', '--lang=', '-lang=', '/lang=', 'lang=', '--language:', '-language:', '/language:', 'language:', '--lang:', '-lang:', '/lang:', 'lang:'):
            if lowered.startswith(prefix):
                return startNormalizeLanguage(token.split(lowered[len(prefix)-1], 1)[1] if False else token[len(prefix):])
    return EMPTY_STRING


def promptConfigPath() -> Path:
    return BASE_DIR / 'config.ini'


def promptBuildInfoPath() -> Path:
    """Path to build_info.ini — the single source of truth for every string
    baked into the built executables and installers (VERSIONINFO + Add/Remove
    Programs entries + WiX manufacturer / Inno publisher / NSIS VIAddVersionKey)."""
    return BASE_DIR / 'build_info.ini'


_BUILD_INFO_CACHE: configparser.RawConfigParser | None = None


def promptBuildInfoParser() -> configparser.RawConfigParser:
    """Cached parser for build_info.ini. If the file is missing, returns an
    empty parser — callers fall back to config.ini[metadata] / hardcoded
    defaults via promptBuildInfo()."""
    global _BUILD_INFO_CACHE
    if _BUILD_INFO_CACHE is not None:
        return _BUILD_INFO_CACHE
    parser = configparser.RawConfigParser()
    path = promptBuildInfoPath()
    try:
        if path.exists():
            parser.read(path, encoding='utf-8')
    except Exception as error:
        captureException(None, source='start.py', context='except@promptBuildInfoParser')
        print(f"[WARN:start.py.build_info] {type(error).__name__}: {error}", file=sys.stderr, flush=True)
    _BUILD_INFO_CACHE = parser
    return parser


def promptBuildInfo(section: str, key: str, fallback: str = EMPTY_STRING) -> str:
    """Read a single value from build_info.ini. Falls back to the supplied
    default if the section/key is absent. All EXE + installer builders go
    through this so editing build_info.ini propagates everywhere."""
    parser = promptBuildInfoParser()
    try:
        if parser.has_option(section, key):
            v = parser.get(section, key, fallback=fallback)
            return (v or fallback).strip()
    except Exception:
        pass
    return fallback


def promptConfigParser() -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser()
    path = promptConfigPath()
    try:
        if path.exists():
            parser.read(path, encoding='utf-8')
    except Exception as error:
        captureException(None, source='start.py', context='except@promptConfigParser')
        print(f"[WARN:start.py.config] {type(error).__name__}: {error}", file=sys.stderr, flush=True)
    return parser


def ensurePromptMetadataConfig() -> None:
    parser = promptConfigParser()
    changed = False
    if not parser.has_section('metadata'):
        parser.add_section('metadata')
        changed = True
    defaults = {
        'app_name': 'Prompt',
        'version': PROMPT_APP_VERSION,
        'author': 'Trenton Tompkins',
        'coded_by': 'ChatGPT',
        'company': 'AcquisitionInvest LLC',
        'copyright': 'AcquisitionInvest LLC © 2026',
    }
    for key, value in defaults.items():
        if not parser.has_option('metadata', key):
            parser.set('metadata', key, value)
            changed = True
    if changed:
        try:
            with File.tracedOpen(promptConfigPath(), 'w', encoding='utf-8') as handle:
                parser.write(handle)
        except Exception as error:
            captureException(None, source='start.py', context='except@ensurePromptMetadataConfig')
            print(f"[WARN:start.py.config] could not write metadata config: {type(error).__name__}: {error}", file=sys.stderr, flush=True)


def promptConfiguredVersion(default: str = PROMPT_APP_VERSION) -> str:
    parser = promptConfigParser()
    try:
        if parser.has_option('metadata', 'version'):
            value = str(parser.get('metadata', 'version', fallback=default) or default).strip()
            return value or default
    except Exception as error:
        captureException(None, source='start.py', context='except@promptConfiguredVersion')
    return default


def promptVersionTuple4(version: str) -> tuple[int, int, int, int]:
    parts: list[int] = []
    for piece in re.split(r'[^0-9]+', str(version or PROMPT_APP_VERSION)):
        if piece.strip():
            try:
                parts.append(int(piece.strip()))
            except ValueError:
                parts.append(0)
    while len(parts) < 4:
        parts.append(0)
    return (parts[0], parts[1], parts[2], parts[3])


def startConfigLanguageCode() -> str:
    parser = configparser.RawConfigParser()
    path = promptConfigPath()
    try:
        if path.exists():
            parser.read(path, encoding='utf-8')
            for section, option in (('ui', 'language'), ('prompt', 'language'), ('language', 'current')):
                if parser.has_option(section, option):
                    return startNormalizeLanguage(parser.get(section, option, fallback='EN'))
    except Exception as error:
        captureException(None, source='start.py', context='except@1340')
        print(f"[WARN:start.py.language-config] {type(error).__name__}: {error}", file=sys.stderr, flush=True)
    return 'EN'


def startActiveLanguageCode(tokens=None) -> str:
    return startCliLanguageCode(tokens) or startNormalizeLanguage(os.environ.get('PROMPT_LANGUAGE', ''), default=startConfigLanguageCode())


def startLocalized(key: str, tokens=None, **kwargs) -> str:
    code = startActiveLanguageCode(tokens)
    value = START_LOCALIZED_TEXT.get(code, START_LOCALIZED_TEXT['EN']).get(key, START_LOCALIZED_TEXT['EN'].get(key, key))
    try:
        return str(value).format(**kwargs)
    except Exception:
        captureException(None, source='start.py', context='except@1354')
        return str(value)

def startManSectionName(tokens=None) -> str:
    raw_tokens = [str(value or EMPTY_STRING).strip() for value in list(tokens or []) if str(value or EMPTY_STRING).strip()]
    normalized = [token.lower() for token in raw_tokens]
    for index, token in enumerate(normalized):
        if token in {'man', '/man', '-man', '--man'}:
            if index + 1 < len(normalized):
                return START_MAN_SECTION_ALIASES.get(normalized[index + 1], 'toc')
            return 'toc'
    return 'toc'


def startUsageText(tokens=None) -> str:
    return startLocalized('usage', tokens)


def startVersionText(tokens=None) -> str:
    app_md5 = safeFileMd5Hex(GTP_PATH) or '????????????????????????????????'
    return '\n'.join([
        startLocalized('version_title', tokens, version=promptConfiguredVersion()),
        f"{startLocalized('path_label', tokens)}: {GTP_PATH}",
        f"{startLocalized('md5_label', tokens)}: {app_md5}",
    ])


def startAboutText(tokens=None) -> str:
    return '\n'.join([
        startVersionText(tokens),
        startLocalized('about_extra', tokens),
        'Trenton Tompkins (c) 2026',
        'https://www.triodesktop.com',
        'https://github.com/tibberous',
        'https://trentontompkins.com',
        '(724) 431-5207',
        '<trenttompkins@gmail.com>',
        f'Base directory: {BASE_DIR}',
    ])


def startHelpText(section: str = 'toc', tokens=None) -> str:
    key = START_MAN_SECTION_ALIASES.get(str(section or 'toc').strip().lower(), 'toc')
    if key == 'about':
        return startAboutText(tokens)
    text = START_MAN_SECTIONS.get(key, START_MAN_SECTIONS['toc'])
    if startActiveLanguageCode(tokens) == 'ES':
        if key == 'toc':
            return startUsageText(tokens) + '\n\nManual: use "man launch", "man deploy", "man debugger" para secciones detalladas.'
        return 'Prompt 1.0 — Manual\n\n' + text.replace('FLATLINE Debugger', 'Depurador FLATLINE').replace('Site:', 'Sitio:').replace('Sections:', 'Secciones:')
    return text


def startImmediateExit(code: int = 0) -> None:
    appendRunLog(f'[IMMEDIATE-EXIT] code={int(code or 0)}')
    try:
        stdout = getattr(sys, 'stdout', None)
        stderr = getattr(sys, 'stderr', None)
        for stream in (stdout, stderr):
            original = getattr(stream, 'original', None)
            if original is not None and hasattr(original, 'flush'):
                original.flush()
    except Exception as error:
        captureException(None, source='start.py', context='except@1411')
        try:
            print(f"[WARN:swallowed-exception] start.py:971 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=getattr(sys, '__stderr__', sys.stderr), flush=True)
        except Exception as flush_error:
            captureException(flush_error, source='start.py', context='flush-run-log-mirrors', handled=True)
            print(f"[WARN:swallowed-exception] start.py:flush-run-log-mirrors {type(flush_error).__name__}: {flush_error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
    os._exit(int(code or 0))  # lifecycle-ok: immediate info CLI exit after writing run.log.


def runStartCliInfo(argv=None):
    tokens = list(argv or [])
    man_section = startManSectionName(tokens)
    if bool(readCliOption(tokens, KNOWN_CHILD_FLAG_GROUPS.get('help', set()), takesValue=False, knownAliases=START_VALID_CLI_ALIASES).get('present')):
        print(startHelpText(man_section, tokens), flush=True)
        startImmediateExit(0)
    if bool(readCliOption(tokens, KNOWN_CHILD_FLAG_GROUPS.get('usage', set()), takesValue=False, knownAliases=START_VALID_CLI_ALIASES).get('present')):
        print(startUsageText(tokens), flush=True)
        startImmediateExit(0)
    if bool(readCliOption(tokens, KNOWN_CHILD_FLAG_GROUPS.get('about', set()), takesValue=False, knownAliases=START_VALID_CLI_ALIASES).get('present')):
        print(startAboutText(tokens), flush=True)
        startImmediateExit(0)
    if bool(readCliOption(tokens, KNOWN_CHILD_FLAG_GROUPS.get('version', set()), takesValue=False, knownAliases=START_VALID_CLI_ALIASES).get('present')):
        print(startVersionText(tokens), flush=True)
        startImmediateExit(0)
    if bool(readCliOption(tokens, KNOWN_CHILD_FLAG_GROUPS.get('debugger_query_surfaces', set()), takesValue=False, knownAliases=START_VALID_CLI_ALIASES).get('present')):
        print('heartbeat vardump poll debugger-exec-command debugger-cron-command accepts-proxy', flush=True)
        startImmediateExit(0)
    return None


def startOnlyOptionConsumesExtraTokens(raw_tokens: list[str], index: int, lowered: str) -> int:
    def next_token(offset: int) -> str:
        position = index + offset
        if position < len(raw_tokens):
            return str(raw_tokens[position] or EMPTY_STRING).strip()
        return EMPTY_STRING
    if lowered in OFFSCREEN_FLAGS or lowered in XDUMMY_FLAGS or lowered in XPRA_FLAGS or lowered in DEBUG_FLAGS or lowered in TRACE_FLAGS or lowered in TRACE_VERBOSE_FLAGS or lowered in HEADLESS_FLAGS or lowered in MARIA_FLAGS:
        return 0
    if lowered in BG_ALPHA_FLAGS or lowered in OFFSCREEN_KEY_FLAGS or lowered in OFFSCREEN_TYPE_FLAGS or lowered in OFFSCREEN_WAIT_FLAGS:
        return 1 if next_token(1) and not cliTokenLooksLikeOption(next_token(1), START_VALID_CLI_ALIASES) else 0
    if lowered in BG_COLOR_FLAGS:
        if next_token(1) and not cliTokenLooksLikeOption(next_token(1), START_VALID_CLI_ALIASES):
            if parseFlexibleRgbSpec(next_token(1)) is not None:
                return 1
            triplet = [next_token(1), next_token(2), next_token(3)]
            if all(value and not cliTokenLooksLikeOption(value, START_VALID_CLI_ALIASES) for value in triplet):
                try:
                    if parseFlexibleRgbSpec(' '.join(triplet)) is not None:
                        return 3
                except Exception as error:
                    captureException(None, source='start.py', context='except@1457')
                    print(f"[WARN:swallowed-exception] start.py:1016 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    pass
            return 1
        return 0
    if lowered in OFFSCREEN_CLICK_FLAGS or lowered in OFFSCREEN_MOVE_FLAGS:
        if next_token(1) and not cliTokenLooksLikeOption(next_token(1), START_VALID_CLI_ALIASES):
            if next_token(2) and not cliTokenLooksLikeOption(next_token(2), START_VALID_CLI_ALIASES):
                if next_token(3) and not cliTokenLooksLikeOption(next_token(3), START_VALID_CLI_ALIASES):
                    return 3
                return 2
            return 1
        return 0
    return 0


def parseStartCli(argv=None) -> tuple[list[str], list[str], list[str]]:
    raw_tokens = [str(token or EMPTY_STRING).strip() for token in list(argv or []) if str(token or EMPTY_STRING).strip()]
    child_tokens: list[str] = []
    passthrough_tokens: list[str] = []
    unknown_tokens: list[str] = []
    index = 0
    while index < len(raw_tokens):
        token = str(raw_tokens[index] or EMPTY_STRING).strip()
        lowered = token.lower()
        if lowered in START_ARG_FLAGS:
            if index + 1 < len(raw_tokens):
                passthrough_tokens.append(str(raw_tokens[index + 1] or EMPTY_STRING).strip())
                index += 2
                continue
            unknown_tokens.append(token)
            index += 1
            continue
        if any(lowered.startswith(prefix) for prefix in tuple(alias + '=' for alias in START_ARG_FLAGS)):
            passthrough_tokens.append(stripCliValueQuotes(token.split('=', 1)[1]))
            index += 1
            continue
        if lowered in START_ARGS_FLAGS:
            passthrough_tokens.extend(str(item or EMPTY_STRING).strip() for item in raw_tokens[index + 1:] if str(item or EMPTY_STRING).strip())
            break
        if any(lowered.startswith(prefix) for prefix in tuple(alias + '=' for alias in START_ARGS_FLAGS)):
            payload = token.split('=', 1)[1]
            if payload.strip():
                passthrough_tokens.extend([part for part in re.split(r'[;,\s]+', payload) if str(part or EMPTY_STRING).strip()])
            index += 1
            continue
        if lowered in OFFSCREEN_FLAGS or lowered in BG_COLOR_FLAGS or lowered in BG_ALPHA_FLAGS or lowered in OFFSCREEN_CLICK_FLAGS or lowered in OFFSCREEN_MOVE_FLAGS or lowered in OFFSCREEN_WAIT_FLAGS or lowered in OFFSCREEN_KEY_FLAGS or lowered in OFFSCREEN_TYPE_FLAGS or lowered in BUILD_FLAGS or lowered in PYINSTALLER_FLAGS or lowered in NIKITA_FLAGS or lowered in PACKAGE_DRY_RUN_FLAGS or lowered in INSTALLER_FLAGS or lowered in PUSH_FLAGS or lowered in VULTURE_FLAGS or lowered in VULTURE_OUTPUT_FLAGS or lowered in VULTURE_MIN_CONFIDENCE_FLAGS:
            index += 1 + startOnlyOptionConsumesExtraTokens(raw_tokens, index, lowered)
            continue
        if any(lowered.startswith(prefix) for prefix in tuple(alias + '=' for alias in set().union(BG_COLOR_FLAGS, BG_ALPHA_FLAGS, OFFSCREEN_CLICK_FLAGS, OFFSCREEN_MOVE_FLAGS, OFFSCREEN_WAIT_FLAGS, OFFSCREEN_KEY_FLAGS, OFFSCREEN_TYPE_FLAGS, VULTURE_OUTPUT_FLAGS, VULTURE_MIN_CONFIDENCE_FLAGS))):
            index += 1
            continue
        child_tokens.append(token)
        flag_name = lowered.split('=', 1)[0].split(':', 1)[0]
        if (lowered.startswith('-') or lowered.startswith('/')) and flag_name not in START_VALID_CLI_ALIASES:
            unknown_tokens.append(token)
        index += 1
    return raw_tokens, child_tokens + passthrough_tokens, unknown_tokens

def normalizedCliTokens(argv=None) -> set[str]:
    return {str(token or EMPTY_STRING).strip().lower() for token in list(argv or []) if str(token or EMPTY_STRING).strip()}

def cliHasAnyFlag(argv=None, flags=None) -> bool:
    tokens = normalizedCliTokens(argv)
    for token in tokens:
        if token in set(flags or []):
            return True
    return False

def traceRequested(argv=None) -> bool:
    return cliHasAnyFlag(argv, TRACE_FLAGS)


def traceVerboseRequested(argv=None) -> bool:
    return cliHasAnyFlag(argv, TRACE_VERBOSE_FLAGS)


def anyTraceRequested(argv=None) -> bool:
    return bool(traceRequested(argv) or traceVerboseRequested(argv))


def xdummyRequested(argv=None) -> bool:
    return cliHasAnyFlag(argv, XDUMMY_FLAGS)


def xpraRequested(argv=None) -> bool:
    return cliHasAnyFlag(argv, XPRA_FLAGS)


def requestedCaptureEngineKind(argv=None) -> str:
    if xdummyRequested(argv):
        return 'xdummy'
    if xpraRequested(argv):
        return 'xpra'
    # Default to Xvfb for plain --offscreen.  Auto-selecting Xdummy just because
    # Xorg/Xdummy exists can pull in extra xrandr/cvt dependencies and fail before
    # the child app has a chance to render.  Xdummy remains available explicitly via
    # --xdummy when that stronger engine is requested.
    return 'xvfb'


def offscreenRequested(argv=None) -> bool:
    return cliHasAnyFlag(argv, OFFSCREEN_FLAGS) or xdummyRequested(argv) or xpraRequested(argv)




def parseFlexibleRgbSpec(text: str) -> tuple[int, int, int] | None:
    pending = str(text or EMPTY_STRING).strip()
    seen: set[str] = set()
    for _attempt in range(8):
        raw = str(pending or EMPTY_STRING).strip()
        if not raw:
            return None
        lowered = raw.lower().strip()
        if lowered in {'transparent', 'none', 'clear'}:
            return (0, 0, 0)
        if lowered.startswith('#'):
            lowered = lowered[1:]
        if re.fullmatch(r'[0-9a-f]{3}', lowered):
            return (int(lowered[0] * 2, 16), int(lowered[1] * 2, 16), int(lowered[2] * 2, 16))
        if re.fullmatch(r'[0-9a-f]{6}', lowered):
            return (int(lowered[0:2], 16), int(lowered[2:4], 16), int(lowered[4:6], 16))
        named = TrioDesktopEmbeddedData.ColorLibrary.ResolveWord(raw)
        if isinstance(named, (list, tuple)) and len(named) >= 3:
            try:
                return (max(0, min(255, int(float(named[0])))), max(0, min(255, int(float(named[1])))), max(0, min(255, int(float(named[2])))))
            except Exception as error:
                captureException(None, source='start.py', context='except@1581')
                print(f"[WARN:swallowed-exception] start.py:1169 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                return None
        if isinstance(named, str) and str(named or EMPTY_STRING).strip():
            next_text = str(named or EMPTY_STRING).strip()
            key = next_text.lower()
            if key in seen:
                return None
            seen.add(key)
            pending = next_text
            continue
        parts = [part for part in re.split(r'[\s,;x]+', raw) if str(part or EMPTY_STRING).strip()]
        if len(parts) == 3:
            try:
                return (max(0, min(255, int(float(parts[0])))), max(0, min(255, int(float(parts[1])))), max(0, min(255, int(float(parts[2])))))
            except Exception as error:
                captureException(None, source='start.py', context='except@1592')
                print(f"[WARN:swallowed-exception] start.py:1180 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                return None
        return None
    return None

def parseBackgroundAlpha(rawValue: str = EMPTY_STRING, defaultPercent: float = 0.0) -> int:
    text = str(rawValue or EMPTY_STRING).strip().lower()
    if not text:
        return max(0, min(255, int(round((float(defaultPercent or 0.0) / 100.0) * 255.0))))
    text = text.rstrip('%').strip()
    try:
        numeric = float(text)
    except Exception as error:
        captureException(None, source='start.py', context='except@1605')
        print(f"[WARN:swallowed-exception] start.py:1192 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        numeric = float(defaultPercent or 0.0)
    if numeric <= 1.0:
        numeric *= 100.0
    numeric = max(0.0, min(100.0, numeric))
    return max(0, min(255, int(round((numeric / 100.0) * 255.0))))


def parseBackgroundColorAndAlpha(argv=None) -> tuple[tuple[int, int, int] | None, int]:
    tokens = [str(token or EMPTY_STRING).strip() for token in list(argv or []) if str(token or EMPTY_STRING).strip()]
    color = None
    alphaToken = EMPTY_STRING
    index = 0
    while index < len(tokens):
        token = tokens[index]
        lowered = token.lower()
        matched = False
        for alias in BG_COLOR_FLAGS:
            if lowered == alias:
                matched = True
                pieces: list[str] = []
                cursor = index + 1
                while cursor < len(tokens) and len(pieces) < 3 and not cliTokenLooksLikeOption(tokens[cursor], START_VALID_CLI_ALIASES):
                    pieces.append(stripCliValueQuotes(tokens[cursor]))
                    cursor += 1
                if pieces:
                    candidate = parseFlexibleRgbSpec(' '.join(pieces)) or parseFlexibleRgbSpec(pieces[0])
                    if candidate is not None:
                        color = candidate
                index = cursor
                break
            for separator in ('=', ':'):
                prefix = alias + separator
                if lowered.startswith(prefix):
                    matched = True
                    candidate = parseFlexibleRgbSpec(stripCliValueQuotes(token[len(alias) + 1:]))
                    if candidate is not None:
                        color = candidate
                    index += 1
                    break
            if matched:
                break
        if matched:
            continue
        for alias in BG_ALPHA_FLAGS:
            if lowered == alias:
                matched = True
                if index + 1 < len(tokens) and not cliTokenLooksLikeOption(tokens[index + 1], START_VALID_CLI_ALIASES):
                    alphaToken = stripCliValueQuotes(tokens[index + 1])
                    index += 2
                else:
                    index += 1
                break
            for separator in ('=', ':'):
                prefix = alias + separator
                if lowered.startswith(prefix):
                    matched = True
                    alphaToken = stripCliValueQuotes(token[len(alias) + 1:])
                    index += 1
                    break
            if matched:
                break
        if not matched:
            index += 1
    alpha = parseBackgroundAlpha(alphaToken, defaultPercent=100.0 if color is not None else 0.0)
    return color, alpha


def parseOffscreenActionPlan(argv=None) -> list[dict[str, Any]]:
    tokens = [str(token or EMPTY_STRING).strip() for token in list(argv or []) if str(token or EMPTY_STRING).strip()]
    plan: list[dict[str, Any]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        lowered = token.lower()
        matched = False
        for alias in OFFSCREEN_CLICK_FLAGS:
            if lowered == alias:
                matched = True
                pieces: list[str] = []
                cursor = index + 1
                while cursor < len(tokens) and len(pieces) < 3 and not cliTokenLooksLikeOption(tokens[cursor], START_VALID_CLI_ALIASES):
                    pieces.append(stripCliValueQuotes(tokens[cursor]))
                    cursor += 1
                parts = [part for part in re.split(r'[\s,;x]+', ' '.join(pieces)) if str(part or EMPTY_STRING).strip()]
                if len(parts) >= 2:
                    try:
                        plan.append({'kind': 'click', 'x': int(float(parts[0])), 'y': int(float(parts[1])), 'button': int(float(parts[2])) if len(parts) >= 3 else 1})
                    except Exception as error:
                        captureException(None, source='start.py', context='except@1694')
                        print(f"[WARN:swallowed-exception] start.py:1280 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                        pass
                index = cursor
                break
            for separator in ('=', ':'):
                prefix = alias + separator
                if lowered.startswith(prefix):
                    matched = True
                    parts = [part for part in re.split(r'[\s,;x]+', stripCliValueQuotes(token[len(alias) + 1:])) if str(part or EMPTY_STRING).strip()]
                    if len(parts) >= 2:
                        try:
                            plan.append({'kind': 'click', 'x': int(float(parts[0])), 'y': int(float(parts[1])), 'button': int(float(parts[2])) if len(parts) >= 3 else 1})
                        except Exception as error:
                            captureException(None, source='start.py', context='except@1707')
                            print(f"[WARN:swallowed-exception] start.py:1292 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                            pass
                    index += 1
                    break
            if matched:
                break
        if matched:
            continue
        for alias in OFFSCREEN_MOVE_FLAGS:
            if lowered == alias:
                matched = True
                pieces = []
                cursor = index + 1
                while cursor < len(tokens) and len(pieces) < 2 and not cliTokenLooksLikeOption(tokens[cursor], START_VALID_CLI_ALIASES):
                    pieces.append(stripCliValueQuotes(tokens[cursor]))
                    cursor += 1
                parts = [part for part in re.split(r'[\s,;x]+', ' '.join(pieces)) if str(part or EMPTY_STRING).strip()]
                if len(parts) >= 2:
                    try:
                        plan.append({'kind': 'move', 'x': int(float(parts[0])), 'y': int(float(parts[1]))})
                    except Exception as error:
                        captureException(None, source='start.py', context='except@1728')
                        print(f"[WARN:swallowed-exception] start.py:1312 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                        pass
                index = cursor
                break
            for separator in ('=', ':'):
                prefix = alias + separator
                if lowered.startswith(prefix):
                    matched = True
                    parts = [part for part in re.split(r'[\s,;x]+', stripCliValueQuotes(token[len(alias) + 1:])) if str(part or EMPTY_STRING).strip()]
                    if len(parts) >= 2:
                        try:
                            plan.append({'kind': 'move', 'x': int(float(parts[0])), 'y': int(float(parts[1]))})
                        except Exception as error:
                            captureException(None, source='start.py', context='except@1741')
                            print(f"[WARN:swallowed-exception] start.py:1324 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                            pass
                    index += 1
                    break
            if matched:
                break
        if matched:
            continue
        for aliasToken in OFFSCREEN_WAIT_FLAGS:
            if lowered == aliasToken:
                matched = True
                if index + 1 < len(tokens) and not cliTokenLooksLikeOption(tokens[index + 1], START_VALID_CLI_ALIASES):
                    value = stripCliValueQuotes(tokens[index + 1])
                    try:
                        delay = float(value)
                    except Exception as error:
                        captureException(None, source='start.py', context='except@1757')
                        print(f"[WARN:swallowed-exception] start.py:1339 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                        delay = None
                    if delay is not None:
                        plan.append({'kind': 'wait', 'seconds': max(0.0, float(delay))})
                    index += 2
                else:
                    index += 1
                break
            for separator in ('=', ':'):
                prefix = aliasToken + separator
                if lowered.startswith(prefix):
                    matched = True
                    value = stripCliValueQuotes(token[len(aliasToken) + 1:])
                    try:
                        delay = float(value)
                    except Exception as error:
                        captureException(None, source='start.py', context='except@1773')
                        print(f"[WARN:swallowed-exception] start.py:1354 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                        delay = None
                    if delay is not None:
                        plan.append({'kind': 'wait', 'seconds': max(0.0, float(delay))})
                    index += 1
                    break
            if matched:
                break
        if matched:
            continue
        for alias, kind in ((OFFSCREEN_KEY_FLAGS, 'key'), (OFFSCREEN_TYPE_FLAGS, 'type')):
            found = False
            for aliasToken in alias:
                if lowered == aliasToken:
                    found = True
                    matched = True
                    if index + 1 < len(tokens) and not cliTokenLooksLikeOption(tokens[index + 1], START_VALID_CLI_ALIASES):
                        value = stripCliValueQuotes(tokens[index + 1])
                        if value:
                            plan.append({'kind': kind, 'value': value})
                        index += 2
                    else:
                        index += 1
                    break
                for separator in ('=', ':'):
                    prefix = aliasToken + separator
                    if lowered.startswith(prefix):
                        found = True
                        matched = True
                        value = stripCliValueQuotes(token[len(aliasToken) + 1:])
                        if value:
                            plan.append({'kind': kind, 'value': value})
                        index += 1
                        break
                if found:
                    break
            if matched:
                break
        if not matched:
            index += 1
    return plan


def prepareTraceEnvironment(argv, env: dict, debugWritesAllowed: bool = False) -> None:
    trace_requested = bool(anyTraceRequested(argv))
    trace_verbose_requested = bool(traceVerboseRequested(argv))
    trace_env_allowed = bool(debugWritesAllowed or trace_requested or trace_verbose_requested)
    if not trace_env_allowed:
        for key in ('TRIO_TRACE_ENABLED', 'TRIO_TRACE_SOURCE_PATH', 'TRIO_TRACE_TOTAL_LINES', 'TRIO_TRACE_LOG_PATH', 'TRIO_TRACE_FULL_LOG_PATH', 'TRIO_TRACE_MODE', 'TRIO_TRACE_VERBOSE'):
            env.pop(key, None)
        return
    if not trace_requested:
        for key in ('TRIO_TRACE_ENABLED', 'TRIO_TRACE_SOURCE_PATH', 'TRIO_TRACE_TOTAL_LINES', 'TRIO_TRACE_LOG_PATH', 'TRIO_TRACE_FULL_LOG_PATH', 'TRIO_TRACE_MODE', 'TRIO_TRACE_VERBOSE'):
            env.pop(key, None)
        return
    try:
        source_text = File.readText(GTP_PATH, encoding='utf-8', errors='replace')
        total_lines = len(source_text.splitlines())
    except Exception as error:
        captureException(None, source='start.py', context='except@1832')
        print(f"[WARN:swallowed-exception] start.py:1412 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        total_lines = 0
    trace_log_path = LOG_DIR / 'trace.log'
    trace_full_log_path = LOG_DIR / 'trace-full.log'
    try:
        trace_log_path.parent.mkdir(parents=True, exist_ok=True)
        trace_full_log_path.parent.mkdir(parents=True, exist_ok=True)
        File.writeText(trace_log_path, EMPTY_STRING, encoding='utf-8')
        File.writeText(trace_full_log_path, EMPTY_STRING, encoding='utf-8')
    except Exception as error:
        captureException(None, source='start.py', context='except@1842')
        print(f"[WARN:swallowed-exception] start.py:1421 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        pass
    tokens = normalizedCliTokens(argv)
    regular_trace_requested = bool(traceRequested(argv))
    trace_mode = 'headless-verbose' if trace_verbose_requested else 'trace'
    if not any(flag in tokens for flag in HEADLESS_FLAGS):
        trace_mode = 'qt-verbose' if trace_verbose_requested else 'qt'
    elif regular_trace_requested and not trace_verbose_requested:
        trace_mode = 'qt-headless'
    env['TRIO_TRACE_ENABLED'] = '1'
    env['TRIO_TRACE_SOURCE_PATH'] = str(GTP_PATH.resolve())
    env['TRIO_TRACE_TOTAL_LINES'] = str(int(total_lines or 0))
    env['TRIO_TRACE_LOG_PATH'] = str(trace_log_path.resolve())
    env['TRIO_TRACE_FULL_LOG_PATH'] = str(trace_full_log_path.resolve())
    env['TRIO_TRACE_MODE'] = trace_mode
    env['TRIO_TRACE_VERBOSE'] = '1' if trace_verbose_requested else '0'



class CaptureEngine:
    DEFAULT_SCREEN = '1024x900x24'

    def __init__(self, owner=None, screenSpec: str = EMPTY_STRING, backgroundColor=None, backgroundAlpha: int = 0):
        self.owner = owner  # noqa: nonconform
        self.screenSpec = self.normalizeScreenSpec(screenSpec)
        self.displayNumber = 0  # noqa: nonconform
        self.displayName = EMPTY_STRING  # noqa: nonconform
        self.tempDir = None  # noqa: nonconform
        self.xauthorityPath = EMPTY_STRING  # noqa: nonconform
        self.framebufferDir = EMPTY_STRING  # noqa: nonconform
        self.proc = None  # noqa: nonconform
        self.ready = False  # noqa: nonconform
        self.windowCache = {}  # noqa: nonconform
        self.backgroundColor = tuple(backgroundColor[:3]) if isinstance(backgroundColor, (tuple, list)) and len(backgroundColor) >= 3 else None  # noqa: nonconform
        self.backgroundAlpha = max(0, min(255, int(backgroundAlpha or 0)))  # noqa: nonconform

    def toolPath(self, name: str) -> str:
        return str(packagedLinuxRuntimeToolPath(str(name or EMPTY_STRING)) or EMPTY_STRING)

    def hasTool(self, name: str) -> bool:
        return bool(self.toolPath(name))

    def requireTool(self, name: str) -> str:
        path = self.toolPath(name)
        if not path:
            raise RuntimeError(f'Missing required offscreen tool: {name}')
        return path

    def emit(self, text: str, path: str = EMPTY_STRING) -> None:
        if not DebugLog.lineLooksVisible(text):
            return
        owner = getattr(self, 'owner', None)
        if owner is not None and callable(getattr(owner, 'emit', None)):
            owner.emit(text, path or getattr(owner, 'logPath', EMPTY_STRING))
            return
        for line in DebugLog.iterVisibleLines(text):
            print(line, flush=True)

    def normalizeScreenSpec(self, rawValue: str = EMPTY_STRING) -> str:
        text = str(rawValue or EMPTY_STRING).strip().lower()
        if not text:
            return self.DEFAULT_SCREEN
        match = re.match(r'^(\d{2,5})x(\d{2,5})(?:x(\d{1,2}))?$', text)
        if not match:
            return self.DEFAULT_SCREEN
        width = max(320, int(match.group(1) or 1920))
        height = max(200, int(match.group(2) or 1080))
        depth = int(match.group(3) or 24)
        if depth <= 0:
            depth = 24
        return f'{width}x{height}x{depth}'

    def parseScreenSpec(self) -> tuple[int, int, int]:
        match = re.match(r'^(\d{2,5})x(\d{2,5})(?:x(\d{1,2}))?$', str(self.screenSpec or self.DEFAULT_SCREEN).strip().lower())
        if not match:
            return (1920, 1080, 24)
        width = max(320, int(match.group(1) or 1920))
        height = max(200, int(match.group(2) or 1080))
        depth = max(8, int(match.group(3) or 24))
        return (width, height, depth)

    def windowGeometryArgument(self) -> str:
        width, height, _ = self.parseScreenSpec()
        return f'{width}x{height}+0+0'

    def screenDimensions(self) -> tuple[int, int]:
        width, height, _ = self.parseScreenSpec()
        return (width, height)

    def chooseDisplayNumber(self, start: int = 99, end: int = 140) -> int:
        xdpyinfo = self.toolPath('xdpyinfo')
        for number in range(int(start), int(end) + 1):
            socketPath = Path(f'/tmp/.X11-unix/X{number}')
            if socketPath.exists():
                continue
            if xdpyinfo:
                probe = startLifecycleRunCommand([xdpyinfo, '-display', f':{number}'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                if int(getattr(probe, 'returncode', 1)) == 0:
                    continue
            return number
        raise RuntimeError('Could not find a free offscreen X11 display number in :99-:140')

    def makeCookie(self) -> str:
        return os.urandom(16).hex()

    def buildEnvironment(self, baseEnv: dict | None = None) -> dict:
        env = dict(baseEnv or os.environ.copy())
        env['DISPLAY'] = str(self.displayName or EMPTY_STRING)
        env['XAUTHORITY'] = str(self.xauthorityPath or EMPTY_STRING)
        env['TRIO_OFFSCREEN'] = '1'
        env['TRIO_MANAGED_DISPLAY'] = '1'
        if os.name != 'nt':
            prependEnvPathList(env, 'PATH', packagedLinuxRuntimeBinDirs())
            prependEnvPathList(env, 'LD_LIBRARY_PATH', packagedLinuxRuntimeLibDirs() + pythonRuntimeLibraryDirs())
            platformName = str(packagedQtPlatformName('xcb') or 'xcb').strip() or 'xcb'
            env['TRIO_QT_PLATFORM'] = platformName
            env['QT_QPA_PLATFORM'] = platformName
            env.setdefault('QT_XCB_GL_INTEGRATION', 'none')
            env.setdefault('QT_OPENGL', 'software')
            env.setdefault('LIBGL_ALWAYS_SOFTWARE', '1')
            env.setdefault('QTWEBENGINE_DISABLE_SANDBOX', '1')
            chromiumFlags = str(env.get('QTWEBENGINE_CHROMIUM_FLAGS', EMPTY_STRING) or EMPTY_STRING).strip()
            for flag in ('--no-sandbox', '--disable-gpu', '--disable-gpu-compositing'):
                if flag not in chromiumFlags:
                    chromiumFlags = (chromiumFlags + ' ' + flag).strip()
            if chromiumFlags:
                env['QTWEBENGINE_CHROMIUM_FLAGS'] = chromiumFlags
        return env

    def waitUntilReady(self, timeoutSeconds: float = 8.0) -> bool:
        xdpyinfo = self.requireTool('xdpyinfo')
        deadline = time.time() + float(timeoutSeconds or 8.0)
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise RuntimeError(f'Offscreen X11 server exited early with code {self.proc.returncode}')
            result = startLifecycleRunCommand(
                [xdpyinfo, '-display', self.displayName],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self.buildEnvironment(),
                check=False,
            )
            if int(getattr(result, 'returncode', 1)) == 0:
                self.ready = True
                return True
            time.sleep(0.10)
        raise RuntimeError(f'Offscreen X11 server did not become ready on {self.displayName}')

    def start(self) -> bool:
        if os.name == 'nt':
            raise RuntimeError('Managed offscreen X11 capture engines are only supported on Linux/X11')
        if self.proc is not None and self.proc.poll() is None and self.ready:
            return True
        xvfb = self.requireTool('Xvfb')
        self.requireTool('xauth')
        self.requireTool('xdpyinfo')
        self.tempDir = tempfile.TemporaryDirectory(prefix='trio-xvfb-')
        tempPath = Path(self.tempDir.name)
        self.displayNumber = self.chooseDisplayNumber()
        self.displayName = f':{self.displayNumber}'
        self.xauthorityPath = str(tempPath / 'Xauthority')
        self.framebufferDir = str(tempPath / 'fb')
        Path(self.framebufferDir).mkdir(parents=True, exist_ok=True)
        cookie = self.makeCookie()
        xauth = self.requireTool('xauth')
        Path(self.xauthorityPath).touch()
        command = [xauth, '-f', self.xauthorityPath, 'add', self.displayName, '.', cookie]
        result = startLifecycleRunCommand(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if int(getattr(result, 'returncode', 1)) != 0:
            raise RuntimeError(f'Failed preparing Xauthority for {self.displayName}')
        command = [
            xvfb,
            self.displayName,
            '-screen', '0', self.screenSpec,
            '-auth', self.xauthorityPath,
            '-fbdir', self.framebufferDir,
            '-nolisten', 'tcp',
        ]
        self.proc = START_EXECUTION_LIFECYCLE.startProcess(
            'XvfbCaptureEngine',
            command,
            cwd=str(BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=self.buildEnvironment(),
            start_new_session=True,
        )
        self.waitUntilReady()
        self.emit(f'[PromptDebugger] Offscreen Xvfb ready  display={self.displayName}  screen={self.screenSpec}', getattr(self.owner, 'logPath', EMPTY_STRING))
        return True

    def stop(self) -> bool:
        proc = self.proc
        self.proc = None
        self.ready = False
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=2.0)
            except Exception as error:
                captureException(None, source='start.py', context='except@2041')
                print(f"[WARN:swallowed-exception] start.py:1619 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                try:
                    proc.kill()
                except Exception as error:
                    captureException(None, source='start.py', context='except@2045')
                    print(f"[WARN:swallowed-exception] start.py:1622 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    pass
        tempDir = self.tempDir
        self.tempDir = None
        if tempDir is not None:
            try:
                tempDir.cleanup()
            except Exception as error:
                captureException(None, source='start.py', context='except@2053')
                print(f"[WARN:swallowed-exception] start.py:1629 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
        return True

    def framebufferSnapshotPath(self) -> str:
        if not self.framebufferDir or not self.displayNumber:
            return EMPTY_STRING
        candidate = Path(self.framebufferDir) / 'Xvfb_screen0'
        if candidate.exists():
            return str(candidate)
        return EMPTY_STRING

    def rgbPixel(self, rgbBytes: bytes, width: int, x: int, y: int) -> tuple[int, int, int]:
        index = ((int(y) * int(width)) + int(x)) * 3
        return (int(rgbBytes[index]), int(rgbBytes[index + 1]), int(rgbBytes[index + 2]))

    def cropRgbBuffer(self, width: int, height: int, rgbBytes: bytes, x: int, y: int, cropWidth: int, cropHeight: int) -> tuple[int, int, bytes]:
        sourceWidth = int(width or 0)
        sourceHeight = int(height or 0)
        left = max(0, min(sourceWidth, int(x or 0)))
        top = max(0, min(sourceHeight, int(y or 0)))
        right = max(left, min(sourceWidth, left + max(1, int(cropWidth or 0))))
        bottom = max(top, min(sourceHeight, top + max(1, int(cropHeight or 0))))
        targetWidth = max(1, right - left)
        targetHeight = max(1, bottom - top)
        rowStride = sourceWidth * 3
        cropped = bytearray(targetWidth * targetHeight * 3)
        targetOffset = 0
        for rowIndex in range(top, bottom):
            start = (rowIndex * rowStride) + (left * 3)
            end = start + (targetWidth * 3)
            segment = rgbBytes[start:end]
            cropped[targetOffset:targetOffset + len(segment)] = segment
            targetOffset += len(segment)
        return targetWidth, targetHeight, bytes(cropped)

    def windowGeometry(self, windowId: str) -> dict[str, int]:
        token = str(windowId or EMPTY_STRING).strip()
        if not token:
            return {}
        cached = self.windowCache.get(token)
        if isinstance(cached, dict) and cached.get('width') and cached.get('height'):
            return dict(cached)
        xwininfo = self.toolPath('xwininfo')
        if not xwininfo:
            return {}
        result = startLifecycleRunCommand([xwininfo, '-display', self.displayName, '-id', token], capture_output=True, text=True, encoding='utf-8', errors='replace', env=self.buildEnvironment(), check=False)
        if int(getattr(result, 'returncode', 1)) != 0:
            return {}
        stdout = str(result.stdout or EMPTY_STRING)
        def read_int(label: str) -> int:
            match = re.search(rf'{re.escape(label)}:\s*(-?\d+)', stdout)
            return int(match.group(1)) if match else 0
        geometry = {
            'x': read_int('Absolute upper-left X'),
            'y': read_int('Absolute upper-left Y'),
            'width': read_int('Width'),
            'height': read_int('Height'),
        }
        if geometry['width'] > 0 and geometry['height'] > 0:
            self.windowCache[token] = dict(geometry)
            return dict(geometry)
        return {}

    def activeWindowId(self) -> str:
        xprop = self.toolPath('xprop')
        if not xprop:
            return EMPTY_STRING
        result = startLifecycleRunCommand([xprop, '-display', self.displayName, '-root', '_NET_ACTIVE_WINDOW'], capture_output=True, text=True, encoding='utf-8', errors='replace', env=self.buildEnvironment(), check=False)
        match = re.search(r'(0x[0-9a-fA-F]+)', str(result.stdout or EMPTY_STRING))
        token = str(match.group(1) if match else EMPTY_STRING).strip()
        if token.lower() in {'0x0', '0'}:
            return EMPTY_STRING
        return token

    def bestChildWindow(self, pid: int = 0) -> dict[str, Any]:
        activeId = self.activeWindowId()
        best: dict[str, Any] = {}
        bestScore = -1
        for entry in self.findChildWindows(pid):
            windowId = str(entry.get('id', EMPTY_STRING) or EMPTY_STRING).strip()
            if not windowId:
                continue
            geometry = self.windowGeometry(windowId)
            width = int(geometry.get('width', 0) or 0)
            height = int(geometry.get('height', 0) or 0)
            if width < 64 or height < 64:
                continue
            title = str(entry.get('title', EMPTY_STRING) or EMPTY_STRING).strip().lower()
            area = width * height
            bonus = 0
            if windowId == activeId:
                bonus += 15000000
            if 'help center' in title:
                bonus += 10000000
            if 'trio' in title:
                bonus += 5000000
            if 'desktop' in title:
                bonus += 2500000
            if 'root window' in title or 'has no name' in title:
                bonus -= 10000000
            score = area + bonus
            if score > bestScore:
                bestScore = score
                best = {'id': windowId, 'title': title, 'geometry': geometry}
        return best

    def backgroundReplacementRgba(self, rootRgb: tuple[int, int, int]) -> tuple[int, int, int, int] | None:
        if self.backgroundColor is not None:
            return (int(self.backgroundColor[0]), int(self.backgroundColor[1]), int(self.backgroundColor[2]), int(self.backgroundAlpha))
        if tuple(int(value) for value in rootRgb) == (0, 0, 0):
            return (0, 0, 0, 0)
        return None

    def writeRgbaPng(self, outputPath: str | Path, width: int, height: int, rgbaBytes: bytes) -> str:
        target = Path(outputPath)
        target.parent.mkdir(parents=True, exist_ok=True)
        expected = int(width or 0) * int(height or 0) * 4
        if len(rgbaBytes) != expected:
            raise RuntimeError(f'RGBA payload length mismatch: expected {expected}, got {len(rgbaBytes)}')
        def chunk(tag: bytes, payload: bytes) -> bytes:
            crc = zlib.crc32(tag + payload) & 0xffffffff
            return struct.pack('>I', len(payload)) + tag + payload + struct.pack('>I', crc)
        scanlines = bytearray()
        stride = width * 4
        for rowIndex in range(height):
            start = rowIndex * stride
            scanlines.extend(b'\x00')
            scanlines.extend(rgbaBytes[start:start + stride])
        ihdr = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
        payload = b'\x89PNG\r\n\x1a\n'  # noqa: badcode reviewed detector-style finding
        payload += chunk(b'IHDR', ihdr)  # noqa: badcode reviewed detector-style finding
        payload += chunk(b'IDAT', zlib.compress(bytes(scanlines), level=9))  # noqa: badcode reviewed detector-style finding
        payload += chunk(b'IEND', b'')
        target.write_bytes(payload)
        return str(target)

    def rootRgbWithSyntheticBackground(self, width: int, height: int, rgbBytes: bytes) -> tuple[int, int, bytes, bool]:
        if width <= 0 or height <= 0 or not rgbBytes:
            return width, height, rgbBytes, False
        rootColor = self.rgbPixel(rgbBytes, width, 0, 0)
        replacement = self.backgroundReplacementRgba(rootColor)
        if replacement is None:
            return width, height, rgbBytes, False
        rgba = bytearray(width * height * 4)
        target = 0
        for index in range(0, len(rgbBytes), 3):
            pixel = (rgbBytes[index], rgbBytes[index + 1], rgbBytes[index + 2])
            if pixel == rootColor:
                rgba[target:target + 4] = bytes(replacement)
            else:
                rgba[target:target + 4] = bytes((pixel[0], pixel[1], pixel[2], 255))
            target += 4
        return width, height, bytes(rgba), True

    def decodeXwdFramebufferToRgb(self, sourcePath: str | Path) -> tuple[int, int, bytes]:
        source = Path(sourcePath)
        raw = source.read_bytes()
        if len(raw) < 160:
            raise RuntimeError(f'Framebuffer file too small: {source}')
        header = struct.unpack('>25I', raw[:100])
        headerSize = int(header[0] or 0)
        pixmapFormat = int(header[2] or 0)
        width = int(header[4] or 0)
        height = int(header[5] or 0)
        byteOrder = int(header[6] or 0)
        bitsPerPixel = int(header[11] or 0)
        bytesPerLine = int(header[12] or 0)
        redMask = int(header[14] or 0)
        greenMask = int(header[15] or 0)
        blueMask = int(header[16] or 0)
        if pixmapFormat != 2:
            raise RuntimeError(f'Unsupported XWD pixmap format: {pixmapFormat}')
        if width <= 0 or height <= 0 or bytesPerLine <= 0:
            raise RuntimeError('Invalid XWD framebuffer geometry')
        pixelData = raw[headerSize:headerSize + (height * bytesPerLine)]
        if len(pixelData) < height * bytesPerLine:
            raise RuntimeError('Incomplete XWD framebuffer payload')
        if bitsPerPixel == 32 and redMask == 0x00ff0000 and greenMask == 0x0000ff00 and blueMask == 0x000000ff:
            rgb = bytearray(width * height * 3)
            offset = 0
            for rowIndex in range(height):
                row = pixelData[rowIndex * bytesPerLine:(rowIndex + 1) * bytesPerLine][:width * 4]
                if byteOrder == 0:
                    rgb[offset:offset + width * 3:3] = row[2::4]
                    rgb[offset + 1:offset + width * 3:3] = row[1::4]
                    rgb[offset + 2:offset + width * 3:3] = row[0::4]
                else:
                    rgb[offset:offset + width * 3:3] = row[1::4]
                    rgb[offset + 1:offset + width * 3:3] = row[2::4]
                    rgb[offset + 2:offset + width * 3:3] = row[3::4]
                offset += width * 3
            return width, height, bytes(rgb)
        if bitsPerPixel not in {24, 32}:
            raise RuntimeError(f'Unsupported XWD bits_per_pixel: {bitsPerPixel}')
        bytesPerPixel = max(1, bitsPerPixel // 8)
        shifts = []
        for mask in (redMask, greenMask, blueMask):
            shift = 0
            if mask:
                while ((mask >> shift) & 1) == 0:
                    shift += 1
            shifts.append(shift)
        unpackOrder = 'little' if byteOrder == 0 else 'big'
        rgb = bytearray(width * height * 3)
        target = 0
        for rowIndex in range(height):
            row = pixelData[rowIndex * bytesPerLine:(rowIndex + 1) * bytesPerLine]
            for colIndex in range(width):
                start = colIndex * bytesPerPixel
                pixelBytes = row[start:start + bytesPerPixel]
                value = int.from_bytes(pixelBytes, unpackOrder, signed=False)
                rgb[target] = (value & redMask) >> shifts[0] if redMask else 0
                rgb[target + 1] = (value & greenMask) >> shifts[1] if greenMask else 0
                rgb[target + 2] = (value & blueMask) >> shifts[2] if blueMask else 0
                target += 3
        return width, height, bytes(rgb)

    def writeRgbPng(self, outputPath: str | Path, width: int, height: int, rgbBytes: bytes) -> str:
        target = Path(outputPath)
        target.parent.mkdir(parents=True, exist_ok=True)
        expected = int(width or 0) * int(height or 0) * 3
        if len(rgbBytes) != expected:
            raise RuntimeError(f'RGB payload length mismatch: expected {expected}, got {len(rgbBytes)}')
        def chunk(tag: bytes, payload: bytes) -> bytes:
            crc = zlib.crc32(tag + payload) & 0xffffffff
            return struct.pack('>I', len(payload)) + tag + payload + struct.pack('>I', crc)
        scanlines = bytearray()
        stride = width * 3
        for rowIndex in range(height):
            start = rowIndex * stride
            scanlines.extend(b'\x00')
            scanlines.extend(rgbBytes[start:start + stride])
        ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
        payload = b'\x89PNG\r\n\x1a\n'  # noqa: badcode reviewed detector-style finding
        payload += chunk(b'IHDR', ihdr)  # noqa: badcode reviewed detector-style finding
        payload += chunk(b'IDAT', zlib.compress(bytes(scanlines), level=9))  # noqa: badcode reviewed detector-style finding
        payload += chunk(b'IEND', b'')
        target.write_bytes(payload)
        return str(target)

    def convertFramebufferToPng(self, sourcePath: str | Path, outputPath: str | Path) -> str:
        width, height, rgb = self.decodeXwdFramebufferToRgb(sourcePath)
        return self.writeRgbPng(outputPath, width, height, rgb)

    def bestWindowId(self, pid: int = 0) -> str:
        entry = self.bestChildWindow(pid)
        return str(entry.get('id', EMPTY_STRING) or EMPTY_STRING).strip()

    def focusWindow(self, windowId: str) -> bool:
        xdotool = self.toolPath('xdotool')
        token = str(windowId or EMPTY_STRING).strip()
        if not xdotool or not token:
            return False
        commands = (
            [xdotool, 'windowactivate', '--sync', token],
            [xdotool, 'windowfocus', '--sync', token],
        )
        for command in commands:
            result = startLifecycleRunCommand(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.buildEnvironment(), check=False)
            if int(getattr(result, 'returncode', 1)) == 0:
                return True
        probe = startLifecycleRunCommand([xdotool, 'mousemove', '--window', token, '1', '1'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.buildEnvironment(), check=False)
        return int(getattr(probe, 'returncode', 1)) == 0

    def captureScreenshot(self, outputPath: str | Path | None = None, windowId: str = EMPTY_STRING) -> str:
        if not self.ready:
            raise RuntimeError('Offscreen display is not ready')
        target = Path(outputPath or (SCREENSHOTS_DIR / 'offscreen-root.png')).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        resolvedWindowId = str(windowId or EMPTY_STRING).strip()
        if not resolvedWindowId:
            resolvedWindowId = self.bestWindowId(getattr(getattr(self, 'owner', None), 'childPid', 0))

        def crop_and_write(width: int, height: int, rgb: bytes, token: str = EMPTY_STRING) -> str:
            token = str(token or EMPTY_STRING).strip()
            if token:
                geometry = self.windowGeometry(token)
                if geometry.get('width') and geometry.get('height'):
                    cropWidth, cropHeight, cropped = self.cropRgbBuffer(width, height, rgb, geometry.get('x', 0), geometry.get('y', 0), geometry.get('width', 0), geometry.get('height', 0))
                    return self.writeRgbPng(target.with_suffix('.png'), cropWidth, cropHeight, cropped)
            rootWidth, rootHeight, payload, hasAlpha = self.rootRgbWithSyntheticBackground(width, height, rgb)
            if hasAlpha:
                return self.writeRgbaPng(target.with_suffix('.png'), rootWidth, rootHeight, payload)
            return self.writeRgbPng(target.with_suffix('.png'), rootWidth, rootHeight, payload)

        xwdPath = self.toolPath('xwd')
        if xwdPath:
            try:
                rootTarget = target.with_name(target.stem + '-root').with_suffix('.xwd')
                command = [xwdPath, '-silent', '-display', self.displayName, '-root', '-out', str(rootTarget)]
                result = startLifecycleRunCommand(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.buildEnvironment(), check=False)
                if int(getattr(result, 'returncode', 1)) == 0 and rootTarget.exists():
                    width, height, rgb = self.decodeXwdFramebufferToRgb(rootTarget)
                    return crop_and_write(width, height, rgb, resolvedWindowId)
            except Exception as error:
                captureException(None, source='start.py', context='except@2349')
                print(f"[WARN:swallowed-exception] start.py:1924 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass

        framebufferSource = self.framebufferSnapshotPath()
        if framebufferSource:
            width, height, rgb = self.decodeXwdFramebufferToRgb(framebufferSource)
            return crop_and_write(width, height, rgb, resolvedWindowId)

        imageImport = self.toolPath('import')
        if imageImport:
            command = [imageImport, '-display', self.displayName, '-window', str(resolvedWindowId or 'root'), str(target)]
            result = startLifecycleRunCommand(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.buildEnvironment(), check=False)
            if int(getattr(result, 'returncode', 1)) == 0 and target.exists():
                return str(target)

        if xwdPath:
            xwdTarget = target if target.suffix.lower() == '.xwd' else target.with_suffix('.xwd')
            command = [xwdPath, '-silent', '-display', self.displayName, '-id', str(resolvedWindowId)] if resolvedWindowId else [xwdPath, '-silent', '-display', self.displayName, '-root']
            command += ['-out', str(xwdTarget)]
            result = startLifecycleRunCommand(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.buildEnvironment(), check=False)
            if int(getattr(result, 'returncode', 1)) == 0 and xwdTarget.exists():
                return self.convertFramebufferToPng(xwdTarget, target.with_suffix('.png'))
        raise RuntimeError('Could not capture an offscreen screenshot from the managed offscreen X11 session')

    def visibleWindows(self) -> list[dict[str, str]]:
        if not self.ready:
            return []
        xwininfo = self.toolPath('xwininfo')
        if not xwininfo:
            return []
        result = startLifecycleRunCommand([xwininfo, '-root', '-tree', '-display', self.displayName], capture_output=True, text=True, encoding='utf-8', errors='replace', env=self.buildEnvironment(), check=False)
        windows = []
        for rawLine in result.stdout.splitlines():
            line = str(rawLine or EMPTY_STRING).rstrip()
            match = re.match(r'\s*(0x[0-9a-fA-F]+)\s+"([^"]*)"', line)
            if not match:
                continue
            windows.append({'id': match.group(1), 'title': match.group(2)})
        return windows

    def windowPid(self, windowId: str) -> int:
        xprop = self.toolPath('xprop')
        if not xprop:
            return 0
        result = startLifecycleRunCommand([xprop, '-display', self.displayName, '-id', str(windowId), '_NET_WM_PID'], capture_output=True, text=True, encoding='utf-8', errors='replace', env=self.buildEnvironment(), check=False)
        match = re.search(r'=\s*(\d+)', str(result.stdout or EMPTY_STRING))
        return int(match.group(1)) if match else 0

    def findChildWindows(self, pid: int = 0) -> list[dict[str, str]]:
        childPid = int(pid or 0)
        windows = []
        xdotool = self.toolPath('xdotool')
        if childPid > 0 and xdotool:
            result = startLifecycleRunCommand([xdotool, 'search', '--all', '--pid', str(childPid), '--onlyvisible'], capture_output=True, text=True, encoding='utf-8', errors='replace', env=self.buildEnvironment(), check=False)
            for line in result.stdout.splitlines():
                windowId = str(line or EMPTY_STRING).strip()
                if windowId:
                    windows.append({'id': windowId, 'title': EMPTY_STRING})
            if windows:
                return windows
        visible = self.visibleWindows()
        for entry in visible:
            if childPid <= 0 or self.windowPid(str(entry.get('id', EMPTY_STRING) or EMPTY_STRING)) == childPid:
                windows.append(entry)
        if windows:
            return windows
        titled = [entry for entry in visible if str(entry.get('title', EMPTY_STRING) or EMPTY_STRING).strip() and str(entry.get('title', EMPTY_STRING) or EMPTY_STRING).strip().lower() not in {'(has no name)', 'the root window'}]
        if childPid > 0 and len(titled) == 1:
            return titled
        return visible if childPid <= 0 else []

    def sendKey(self, windowId: str, keySpec: str) -> bool:
        xdotool = self.requireTool('xdotool')
        command = [xdotool, 'key', '--delay', '0', '--window', str(windowId), str(keySpec or EMPTY_STRING)]
        result = startLifecycleRunCommand(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.buildEnvironment(), check=False)
        return int(getattr(result, 'returncode', 1)) == 0

    def typeText(self, windowId: str, textValue: str) -> bool:
        xdotool = self.requireTool('xdotool')
        command = [xdotool, 'type', '--delay', '1', '--window', str(windowId), str(textValue or EMPTY_STRING)]
        result = startLifecycleRunCommand(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.buildEnvironment(), check=False)
        return int(getattr(result, 'returncode', 1)) == 0

    def move(self, windowId: str, x: int, y: int) -> bool:
        xdotool = self.requireTool('xdotool')
        moveResult = startLifecycleRunCommand([xdotool, 'mousemove', '--window', str(windowId), str(int(x)), str(int(y))], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.buildEnvironment(), check=False)
        return int(getattr(moveResult, 'returncode', 1)) == 0

    def click(self, windowId: str, x: int, y: int, button: int = 1) -> bool:
        if not self.move(windowId, x, y):
            return False
        if int(button or 0) <= 0:
            return True
        xdotool = self.requireTool('xdotool')
        clickResult = startLifecycleRunCommand([xdotool, 'click', '--window', str(windowId), str(int(button or 1))], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.buildEnvironment(), check=False)
        return int(getattr(clickResult, 'returncode', 1)) == 0





def packagedLinuxRuntimeLibDirs() -> list[str]:
    roots = [
        LINUX_RUNTIME_DIR / 'usr' / 'lib' / 'x86_64-linux-gnu',
        LINUX_RUNTIME_DIR / 'lib' / 'x86_64-linux-gnu',
        LINUX_RUNTIME_DIR / 'usr' / 'local' / 'lib',
    ]
    result: list[str] = []
    for root in roots:
        try:
            if root.exists():
                value = str(root.resolve())
                if value not in result:
                    result.append(value)
        except Exception as error:
            captureException(None, source='start.py', context='except@2464')
            print(f"[WARN:swallowed-exception] start.py:2038 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            continue
    return result


def pythonRuntimeLibraryDirs() -> list[str]:
    """Return Python-package native library dirs needed by child Qt processes.

    PySide6 wheels keep libpyside6/libshiboken beside the Python package.
    A child process launched from start.py can miss those directories when the
    loader resolves Qt extension modules, especially under managed Xvfb.
    """
    result: list[str] = []
    candidate_roots: list[Path] = []
    try:
        import site
        for value in list(getattr(site, 'getsitepackages', lambda: [])() or []):
            candidate_roots.append(Path(value))
    except Exception as error:
        captureException(None, source='start.py', context='except@2483')
        print(f"[WARN:swallowed-exception] start.py:2056 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        pass
    try:
        user_site = getattr(__import__('site'), 'getusersitepackages', lambda: EMPTY_STRING)()
        if user_site:
            candidate_roots.append(Path(user_site))
    except Exception as error:
        captureException(None, source='start.py', context='except@2490')
        print(f"[WARN:swallowed-exception] start.py:2062 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        pass
    for raw in list(sys.path or []):
        try:
            if raw:
                candidate_roots.append(Path(raw))
        except Exception as error:
            captureException(None, source='start.py', context='except@2497')
            print(f"[WARN:swallowed-exception] start.py:2068 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
    seen_roots: list[Path] = []
    for root in candidate_roots:
        try:
            resolved_root = root.resolve()
        except Exception as error:
            captureException(None, source='start.py', context='except@2504')
            print(f"[WARN:swallowed-exception] start.py:2074 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            continue
        if resolved_root in seen_roots:
            continue
        seen_roots.append(resolved_root)
        for relative in ('PySide6', 'shiboken6', 'PySide6/Qt/lib'):
            candidate = resolved_root / relative
            try:
                if candidate.exists() and candidate.is_dir():
                    value = str(candidate.resolve())
                    if value not in result:
                        result.append(value)
            except Exception as error:
                captureException(None, source='start.py', context='except@2517')
                print(f"[WARN:swallowed-exception] start.py:2086 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                continue
    return result


def packagedLinuxRuntimeBinDirs() -> list[str]:
    roots = [
        LINUX_RUNTIME_DIR / 'usr' / 'local' / 'bin',
        LINUX_RUNTIME_DIR / 'usr' / 'bin',
        LINUX_RUNTIME_DIR / 'bin',
        LINUX_RUNTIME_DIR / 'usr' / 'local' / 'sbin',
        LINUX_RUNTIME_DIR / 'usr' / 'sbin',
        LINUX_RUNTIME_DIR / 'sbin',
    ]
    result: list[str] = []
    for root in roots:
        try:
            if root.exists():
                value = str(root.resolve())
                if value not in result:
                    result.append(value)
        except Exception as error:
            captureException(None, source='start.py', context='except@2539')
            print(f"[WARN:swallowed-exception] start.py:2107 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            continue
    return result


def packagedLinuxRuntimeToolPath(name: str) -> str:
    tool_name = str(name or EMPTY_STRING).strip()
    if not tool_name:
        return EMPTY_STRING
    for root in packagedLinuxRuntimeBinDirs():
        try:
            candidate = (Path(root) / tool_name).resolve()
        except Exception as error:
            captureException(None, source='start.py', context='except@2552')
            print(f"[WARN:swallowed-exception] start.py:2119 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            continue
        if candidate.exists() and candidate.is_file() and os.access(str(candidate), os.X_OK):
            return str(candidate)
    return str(shutil.which(tool_name) or EMPTY_STRING)


def _readDebArEntries(deb_path: Path) -> dict[str, bytes]:
    payload = Path(deb_path)
    data = payload.read_bytes()
    if not data.startswith(b'!<arch>\n'):
        raise RuntimeError(f'Not a Debian ar archive: {payload.name}')
    offset = 8
    entries: dict[str, bytes] = {}
    total = len(data)
    while offset + 60 <= total:
        header = data[offset:offset + 60]
        offset += 60
        name = header[:16].decode('utf-8', errors='replace').strip()
        size_text = header[48:58].decode('utf-8', errors='replace').strip()
        try:
            size_value = int(size_text or '0')
        except Exception as error:
            captureException(None, source='start.py', context='except@2575')
            print(f"[WARN:swallowed-exception] start.py:2141 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            size_value = 0
        body = data[offset:offset + size_value]
        offset += size_value
        if offset % 2 == 1:
            offset += 1
        normalized = name.rstrip('/')
        if normalized.startswith('#1/'):
            try:
                long_len = int(normalized.split('/', 1)[1] or '0')
            except Exception as error:
                captureException(None, source='start.py', context='except@2586')
                print(f"[WARN:swallowed-exception] start.py:2151 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                long_len = 0
            real_name = body[:long_len].decode('utf-8', errors='replace').rstrip('\x00').strip()
            body = body[long_len:]
            normalized = real_name.rstrip('/')
        entries[normalized] = body
    return entries


def _safeExtractTarBytes(data_bytes: bytes, destination: Path) -> None:
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data_bytes), mode='r:*') as archive:  # file-io-ok
        for member in archive.getmembers():
            member_name = str(getattr(member, 'name', EMPTY_STRING) or EMPTY_STRING)
            if not member_name:
                continue
            target_path = (destination / member_name).resolve()
            try:
                target_path.relative_to(destination.resolve())
            except Exception as error:
                captureException(None, source='start.py', context='except@2607')
                print(f"[WARN:swallowed-exception] start.py:2171 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                raise RuntimeError(f'Unsafe path in tar archive: {member_name}')
        archive.extractall(str(destination))


def extractDebianPackageToRuntime(deb_path: Path, destination: Path | None = None) -> bool:
    payload = Path(deb_path)
    target_root = Path(destination or LINUX_RUNTIME_DIR)
    if not payload.exists():
        return False
    target_root.mkdir(parents=True, exist_ok=True)
    dpkg_deb = shutil.which('dpkg-deb')
    if dpkg_deb:
        result = startLifecycleRunCommand([dpkg_deb, '-x', str(payload), str(target_root)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return int(getattr(result, 'returncode', 1)) == 0
    entries = _readDebArEntries(payload)
    data_entry = EMPTY_STRING
    for candidate in ('data.tar.xz', 'data.tar.gz', 'data.tar.bz2', 'data.tar.zst', 'data.tar'):
        if candidate in entries:
            data_entry = candidate
            break
    if not data_entry:
        raise RuntimeError(f'No data.tar.* payload found in {payload.name}')
    _safeExtractTarBytes(entries[data_entry], target_root)
    return True


def prependEnvPathList(env: dict, key: str, values) -> None:
    existing = [part for part in str(env.get(key, EMPTY_STRING) or EMPTY_STRING).split(os.pathsep) if str(part or EMPTY_STRING).strip()]
    ordered: list[str] = []
    for raw in list(values or []):
        value = str(raw or EMPTY_STRING).strip()
        if value and value not in ordered:
            ordered.append(value)
    for value in existing:
        if value and value not in ordered:
            ordered.append(value)
    if ordered:
        env[key] = os.pathsep.join(ordered)


def packagedQtPlatformName(default: str = 'offscreen') -> str:
    libdirs = packagedLinuxRuntimeLibDirs()
    for root in libdirs:
        try:
            if (Path(root) / 'libxcb-cursor.so.0').exists():
                return 'xcb'
        except Exception as error:
            captureException(None, source='start.py', context='except@2655')
            print(f"[WARN:swallowed-exception] start.py:2218 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
    return str(default or 'offscreen')

def preflightCompileOrDie(path: Path) -> None:
    try:
        py_compile.compile(str(path), doraise=True)
    except py_compile.PyCompileError as error:
        captureException(None, source='start.py', context='except@2663')
        message = str(error)
        print(f'[FATAL:compile] Failed compiling {path.name}', file=sys.stderr, flush=True)
        print(message, file=sys.stderr, flush=True)
        raise
    except Exception as error:
        captureException(None, source='start.py', context='except@2668')
        print(f'[FATAL:compile] Unexpected compile failure for {path.name}: {type(error).__name__}: {error}', file=sys.stderr, flush=True)
        raise
def hasModule(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception as error:
        captureException(None, source='start.py', context='except@2674')
        print(f"[WARN:swallowed-exception] start.py:2236 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        return False


def canInterpreterImportModule(pythonExecutable: str, moduleName: str) -> bool:
    executable = str(pythonExecutable or EMPTY_STRING).strip()
    module_text = str(moduleName or EMPTY_STRING).strip()
    if not executable or not module_text:
        return False
    cache_key = (executable, module_text)
    if cache_key in _IMPORT_MODULE_CACHE:
        return bool(_IMPORT_MODULE_CACHE.get(cache_key))
    try:
        probe_code = (
            "import importlib.util, os, sys; "
            f"module_name = {module_text!r}; "
            "ok = importlib.util.find_spec(module_name) is not None; "
            "os._exit(0 if ok else 1)"
        )
        result = startLifecycleRunCommand(
            [executable, '-c', probe_code],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=8,
        )
        ok = int(getattr(result, 'returncode', 1)) == 0
    except Exception as error:
        captureException(None, source='start.py', context='except@2702')
        print(f"[WARN:swallowed-exception] start.py:2263 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        ok = False
    _IMPORT_MODULE_CACHE[cache_key] = bool(ok)
    return bool(ok)



MSYS_PATH_MARKERS = ('msys2', 'msys64', 'mingw', 'ucrt64', 'clang64', 'openshotbuild')
MSYS_SOABI_MARKERS = ('mingw', 'ucrt', 'gnu')


def safeFileMd5Hex(pathValue: str | Path) -> str:
    try:
        target = Path(pathValue).resolve()
        if not target.exists() or not target.is_file():
            return EMPTY_STRING
        digest = hashlib.md5()
        with File.tracedOpen(target, 'rb') as handle:
            while True:  # noqa: badcode reviewed detector-style finding
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except Exception as error:
        captureException(None, source='start.py', context='except@2727')
        print(f"[WARN:swallowed-exception] start.py:2287 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        return EMPTY_STRING


def clearPycacheDirectories(rootValue: str | Path = BASE_DIR, *, reason: str = EMPTY_STRING) -> dict[str, int]:
    root = Path(rootValue).resolve()
    removed_dirs = 0
    removed_files = 0
    if not root.exists():
        return {'dirs': 0, 'files': 0}
    candidates = sorted([path for path in root.rglob('__pycache__') if path.is_dir()], key=lambda item: len(str(item)), reverse=True)
    for cache_dir in candidates:
        try:
            removed_files += sum(1 for child in cache_dir.rglob('*') if child.is_file())
        except Exception as error:
            captureException(None, source='start.py', context='except@2742')
            print(f"[WARN:swallowed-exception] start.py:2301 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        try:
            shutil.rmtree(cache_dir, ignore_errors=False)
            removed_dirs += 1
        except FileNotFoundError:
            captureException(None, source='start.py', context='except@2748')
            continue
        except Exception as error:
            captureException(None, source='start.py', context='except@2750')
            print(f'[WARN:pycache] Failed removing {cache_dir}: {type(error).__name__}: {error}', file=sys.stderr, flush=True)
    if removed_dirs:
        suffix = f' ({reason})' if str(reason or EMPTY_STRING).strip() else EMPTY_STRING
        print(f'[TRACE:pycache] removed {removed_dirs} __pycache__ director{"y" if removed_dirs == 1 else "ies"} and {removed_files} file{"s" if removed_files != 1 else EMPTY_STRING}{suffix}', flush=True)
    return {'dirs': removed_dirs, 'files': removed_files}


def requiredBundleArtifactPaths(baseDir: str | Path = BASE_DIR) -> list[Path]:
    root = Path(baseDir or BASE_DIR)
    return [root / rel_path for rel_path in BUNDLE_REQUIRED_RELATIVE_PATHS]


def seedPromptLauncherAssetsFromZip(baseDir: str | Path = BASE_DIR) -> tuple[int, int]:
    root = Path(baseDir or BASE_DIR)
    child_name = str(CHILD_APP_PATH.name or EMPTY_STRING).strip().lower()
    if child_name == 'gtp.py':
        return 0, 0
    bundle = root / 'assets' / 'assets.zip'
    if not bundle.exists():
        return 0, 0
    try:
        import data as prompt_embedded_data
        written, skipped = prompt_embedded_data.BUNDLE.write_all(root, overwrite=False)
        print(f'[TRACE:bundle-seed] assets={bundle} target={root} written={written} skipped={skipped}', file=sys.stderr, flush=True)
        return int(written), int(skipped)
    except Exception as error:
        captureException(error, source='start.py', context='bundle-seed-assets-zip')
        print(f'[WARN:bundle-seed] failed assets={bundle}: {type(error).__name__}: {error}', file=sys.stderr, flush=True)
        return 0, 0


def missingBundleArtifacts(baseDir: str | Path = BASE_DIR) -> list[Path]:
    root = Path(baseDir or BASE_DIR)
    missing: list[Path] = []
    for path in requiredBundleArtifactPaths(root):
        if not path.exists():
            missing.append(path)
    if missing and (root / 'assets' / 'assets.zip').exists():
        seedPromptLauncherAssetsFromZip(root)
        missing = [path for path in requiredBundleArtifactPaths(root) if not path.exists()]
    return missing


def bundleWarningMessages(baseDir: str | Path = BASE_DIR) -> list[str]:
    root = Path(baseDir or BASE_DIR)
    messages: list[str] = []
    child_name = str(CHILD_APP_PATH.name or EMPTY_STRING).strip().lower()
    if child_name == 'gtp.py':
        messages.extend(list(BUNDLE_IMPORT_WARNINGS or []))
    missing_paths = missingBundleArtifacts(root)
    if missing_paths:
        rel_list = ', '.join(str(path.relative_to(root)) for path in missing_paths)
        if child_name == 'gtp.py':
            messages.append(f'incomplete TrioDesktop bundle next to start.py: {rel_list}')
        else:
            messages.append(f'incomplete Prompt bundle next to start.py: {rel_list}')
    return messages


def verifyBundleArtifactsOrDie(baseDir: str | Path = BASE_DIR) -> None:
    messages = bundleWarningMessages(baseDir)
    if not messages:
        return
    details = '\n'.join(f'- {line}' for line in messages)
    child_name = str(CHILD_APP_PATH.name or EMPTY_STRING).strip().lower()
    if child_name == 'gtp.py':
        raise RuntimeError('TrioDesktop bundle is incomplete.\n' + details + '\nLaunch the full bundle that includes languages/ and triodesktop_embedded_data.py.')
    raise RuntimeError('Prompt bundle is incomplete.\n' + details + '\nLaunch the full Prompt bundle next to start.py.')


def printLauncherIdentity(targetPath: str | Path = GTP_PATH, *, preferredChildPython: str = EMPTY_STRING, context: str = 'startup') -> None:
    timestamp = datetime.datetime.now().astimezone().isoformat()
    launcher_path = Path(__file__).resolve()
    target_path = Path(targetPath).resolve()
    launcher_md5 = safeFileMd5Hex(launcher_path) or '(unavailable)'
    target_md5 = safeFileMd5Hex(target_path) or '(unavailable)'
    current_python = str(Path(sys.executable).resolve()) if str(sys.executable or EMPTY_STRING).strip() else '(unavailable)'
    lines = [
        f'[PromptDebugger] timestamp={timestamp} context={context}',
        f'[PromptDebugger] launcher name={launcher_path.name}',
        f'[PromptDebugger] launcher path={launcher_path}',
        f'[PromptDebugger] launcher md5={launcher_md5}',
        f'[PromptDebugger] current python={current_python}',
        f'[PromptDebugger] target name={target_path.name}',
        f'[PromptDebugger] target path={target_path}',
        f'[PromptDebugger] target md5={target_md5}',
    ]
    preferred = str(preferredChildPython or EMPTY_STRING).strip()
    if preferred:
        lines.append(f'[PromptDebugger] preferred child python={preferred}')
    for line in lines:
        print(line, flush=True)


def _candidatePythonExecutables() -> list[str]:
    candidates: list[str] = []

    def add(candidate) -> None:
        text = str(candidate or EMPTY_STRING).strip().strip('"')
        if text and text not in candidates:
            candidates.append(text)

    add(sys.executable)
    if os.name == 'nt':
        for candidate in (
            r'C:\Python312\python.exe',
            r'C:\Python311\python.exe',
            r'C:\Python310\python.exe',
            r'C:\Python313\python.exe',
        ):
            add(candidate)
        local_app_data = str(os.environ.get('LOCALAPPDATA', EMPTY_STRING) or EMPTY_STRING).strip()
        if local_app_data:
            for version in ('312', '311', '310', '313'):
                add(Path(local_app_data) / 'Programs' / 'Python' / f'Python{version}' / 'python.exe')
    for candidate in (shutil.which('python3'), shutil.which('python'), shutil.which('py')):
        add(candidate)
    return candidates


def _probePythonCommand(command: str) -> tuple[bool, str, tuple[int, int, int], str]:
    executable = str(command or EMPTY_STRING).strip().strip('"')
    if not executable:
        return False, EMPTY_STRING, (0, 0, 0), EMPTY_STRING
    # Do not attempt to spawn obvious missing interpreter paths. This keeps
    # diagnostic commands such as --pyrite/--plyrite from burying the real
    # report under repeated WinError 2 traceback noise while still allowing
    # PATH commands like "py" and "python" to be resolved normally.
    if not Path(executable).exists() and shutil.which(executable) is None:
        return False, EMPTY_STRING, (0, 0, 0), EMPTY_STRING
    try:
        payload = (
            'import json, sys, sysconfig;'
            'print(json.dumps({'
            '"exe": sys.executable,'
            '"version": list(sys.version_info[:3]),'
            '"soabi": str(sysconfig.get_config_var("EXT_SUFFIX") or "")'
            '}))'
        )
        completed = startLifecycleRunCommand([executable, '-c', payload], capture_output=True, text=True, check=False, timeout=20)
        if int(getattr(completed, 'returncode', 1)) != 0:
            return False, EMPTY_STRING, (0, 0, 0), EMPTY_STRING
        data = json.loads(str(completed.stdout or EMPTY_STRING).strip())
        exe = str(data.get('exe', EMPTY_STRING) or EMPTY_STRING).strip() or executable
        version_list = list(data.get('version', []) or [])
        while len(version_list) < 3:
            version_list.append(0)
        version = (int(version_list[0]), int(version_list[1]), int(version_list[2]))
        soabi = str(data.get('soabi', EMPTY_STRING) or EMPTY_STRING).strip()
        return True, exe, version, soabi
    except Exception as error:
        captureException(None, source='start.py', context='except@2879')
        print(f"[WARN:swallowed-exception] start.py:2431 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        return False, EMPTY_STRING, (0, 0, 0), EMPTY_STRING


def _isStandardCPythonExe(executable: str, soabi: str = EMPTY_STRING) -> bool:
    exe_text = str(executable or EMPTY_STRING).strip().lower()
    soabi_text = str(soabi or EMPTY_STRING).strip().lower()
    if any(marker in exe_text for marker in MSYS_PATH_MARKERS):
        return False
    if any(marker in soabi_text for marker in MSYS_SOABI_MARKERS):
        return False
    return True


def _scorePreferredPythonExecutable(executable: str, version: tuple[int, int, int], soabi: str = EMPTY_STRING) -> int:
    if not _isStandardCPythonExe(executable, soabi):
        return 99
    major = int(version[0] if len(version) > 0 else 0)  # noqa: badcode reviewed detector-style finding
    minor = int(version[1] if len(version) > 1 else 0)
    if major == 3 and minor in (10, 11, 12):
        return 0
    if major == 3 and minor >= 13:
        return 1
    if major == 3 and minor >= 10:
        return 2
    return 50


def resolvePreferredPythonCommand(guiRequired: bool = True, minVersion: tuple[int, int] = (3, 10)) -> str:
    candidates = _candidatePythonExecutables()
    required_modules = ['PySide6.QtWidgets', 'PySide6.QtGui'] if bool(guiRequired) and os.name == 'nt' else []
    if os.name != 'nt':
        if not required_modules:
            return candidates[0] if candidates else str(sys.executable or EMPTY_STRING)
        for candidate in candidates:
            if all(canInterpreterImportModule(candidate, module_name) for module_name in required_modules):
                return candidate
        return candidates[0] if candidates else str(sys.executable or EMPTY_STRING)
    scored: list[tuple[int, int, int, str, tuple[int, int, int], str]] = []
    for candidate in candidates:
        ok, executable, version, soabi = _probePythonCommand(candidate)
        if not ok:
            continue
        if tuple(version[:2]) < tuple(minVersion[:2]):
            continue
        base_score = _scorePreferredPythonExecutable(executable, version, soabi)
        module_penalty = 0
        if required_modules and not all(canInterpreterImportModule(executable, module_name) for module_name in required_modules):
            module_penalty = 5
        scored.append((base_score + module_penalty, base_score, module_penalty, executable, version, soabi))
    if scored:
        scored.sort(key=lambda item: (item[0], item[1], item[2], item[4], item[3].lower()))
        return str(scored[0][3] or EMPTY_STRING).strip()
    return candidates[0] if candidates else str(sys.executable or EMPTY_STRING)



def childPythonCommandPrefix(child_python: str) -> list[str]:
    executable = str(child_python or sys.executable or EMPTY_STRING).strip() or str(sys.executable or EMPTY_STRING)
    command = [executable]
    try:
        no_site_requested = bool(int(getattr(sys.flags, 'no_site', 0) or 0)) or str(os.environ.get('PROMPT_CHILD_NO_SITE', EMPTY_STRING) or EMPTY_STRING).strip().lower() in {'1', 'true', 'yes', 'on'}
    except Exception as error:
        captureException(None, source='start.py', context='except@2942')
        print(f"[WARN:swallowed-exception] start.py:2493 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        no_site_requested = False
    if no_site_requested:
        command.append('-S')
    return command


def resolvePreferredChildPython(guiRequired: bool = True) -> str:
    cache_key = (bool(guiRequired),)
    cached = str(_PREFERRED_CHILD_CACHE.get(cache_key, EMPTY_STRING) or EMPTY_STRING).strip()
    if cached:
        return cached
    env_override = str(os.environ.get('PROMPT_CHILD_PYTHON', EMPTY_STRING) or os.environ.get('TRIO_CHILD_PYTHON', EMPTY_STRING) or EMPTY_STRING).strip()
    if env_override:
        _PREFERRED_CHILD_CACHE[cache_key] = env_override
        return env_override
    # Linux containers and bundled model sandboxes often expose a PATH python that can
    # hang during import probes. For non-Windows launches, prefer the interpreter that
    # is already running start.py; the dependency installer can then repair that exact
    # interpreter instead of probing a stale or poisoned PATH entry.
    if os.name != 'nt':
        chosen = str(sys.executable or shutil.which('python3') or shutil.which('python') or EMPTY_STRING).strip()
        _PREFERRED_CHILD_CACHE[cache_key] = chosen
        return chosen
    candidates = []
    for candidate in (sys.executable, '/usr/bin/python3', shutil.which('python3'), shutil.which('python')):
        candidate_text = str(candidate or EMPTY_STRING).strip()
        if candidate_text and candidate_text not in candidates:
            candidates.append(candidate_text)
    required_modules = ['PySide6.QtWidgets', 'PySide6.QtGui'] if bool(guiRequired) else []
    if not required_modules:
        chosen = candidates[0] if candidates else str(sys.executable or EMPTY_STRING)
        _PREFERRED_CHILD_CACHE[cache_key] = str(chosen or EMPTY_STRING).strip()
        return str(chosen or EMPTY_STRING).strip()
    for candidate in candidates:
        if all(canInterpreterImportModule(candidate, module_name) for module_name in required_modules):
            _PREFERRED_CHILD_CACHE[cache_key] = str(candidate or EMPTY_STRING).strip()
            return str(candidate or EMPTY_STRING).strip()
    chosen = candidates[0] if candidates else str(sys.executable or EMPTY_STRING)
    _PREFERRED_CHILD_CACHE[cache_key] = str(chosen or EMPTY_STRING).strip()
    return str(chosen or EMPTY_STRING).strip()


class XvfbCaptureEngine(CaptureEngine):
    ENGINE_KIND = 'xvfb'


class XdummyCaptureEngine(CaptureEngine):
    ENGINE_KIND = 'xdummy'
    DUMMY_DRIVER_CANDIDATE_PATHS = (
        '/usr/lib/xorg/modules/drivers/dummy_drv.so',
        '/usr/lib64/xorg/modules/drivers/dummy_drv.so',
        '/usr/lib/x86_64-linux-gnu/xorg/modules/drivers/dummy_drv.so',
        '/usr/lib/aarch64-linux-gnu/xorg/modules/drivers/dummy_drv.so',
    )
    PRECONFIGURED_MODE_STRINGS = (
        '1920x1080', '1920x1200', '1680x1050', '1600x1200', '1600x900',
        '1440x900', '1366x768', '1360x768', '1280x1024', '1280x800',
        '1280x768', '1280x720', '1152x864', '1024x768', '1024x600', '800x600',
        '2560x1440', '2560x1600', '3840x2160', '3840x2560',
    )
    DUMMY_VIDEO_RAM_KIB = 1048576
    MODELINE_PIXEL_CLOCK = '108.9'
    MODELINE_TOTAL = 33000
    MODELINE_HSYNC_KHZ = '3.3'
    MODELINE_VREFRESH_HZ = '0.1'

    def parseModeToken(self, token: str) -> tuple[int, int] | None:
        match = re.match(r'^(\d{2,5})x(\d{2,5})$', str(token or EMPTY_STRING).strip().lower())
        if not match:
            return None
        return int(match.group(1)), int(match.group(2))

    def preconfiguredModeSizes(self) -> list[tuple[int, int]]:
        requestedWidth, requestedHeight, _ = self.parseScreenSpec()
        modes: list[tuple[int, int]] = [(requestedWidth, requestedHeight)]
        for raw in self.PRECONFIGURED_MODE_STRINGS:
            parsed = self.parseModeToken(raw)
            if parsed is not None:
                modes.append(parsed)
        unique: list[tuple[int, int]] = []
        seen: set[tuple[int, int]] = set()
        for width, height in modes:
            if width < 320 or height < 200:
                continue
            if width > self.MODELINE_TOTAL or height > self.MODELINE_TOTAL:
                continue
            key = (width, height)
            if key in seen:
                continue
            seen.add(key)
            unique.append(key)
        return unique

    def modelineName(self, width: int, height: int) -> str:
        return f'{int(width)}x{int(height)}'

    def buildConfigModeline(self, width: int, height: int) -> str:
        total = int(self.MODELINE_TOTAL)
        return f'    Modeline "{self.modelineName(width, height)}" {self.MODELINE_PIXEL_CLOCK} {int(width)} {total - 2} {total - 1} {total} {int(height)} {total - 2} {total - 1} {total}'
    def dummyDriverPath(self) -> str:
        for candidate in self.DUMMY_DRIVER_CANDIDATE_PATHS:
            if Path(candidate).exists():
                return str(candidate)
        return EMPTY_STRING

    def prepareSessionArtifacts(self) -> None:
        self.stop()
        self.tempDir = tempfile.TemporaryDirectory(prefix='trio-xdummy-')
        tempPath = Path(self.tempDir.name)
        self.displayNumber = self.chooseDisplayNumber()
        self.displayName = f':{self.displayNumber}'
        self.xauthorityPath = str(tempPath / 'Xauthority')
        self.framebufferDir = EMPTY_STRING
        self.windowCache = {}
        cookie = self.makeCookie()
        xauth = self.requireTool('xauth')
        Path(self.xauthorityPath).touch()
        result = startLifecycleRunCommand([xauth, '-f', self.xauthorityPath, 'add', self.displayName, '.', cookie], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if int(getattr(result, 'returncode', 1)) != 0:
            raise RuntimeError(f'Failed preparing Xauthority for {self.displayName}')

    def writeDummyConfig(self) -> str:
        dummyDriver = self.dummyDriverPath()
        if not dummyDriver:
            raise RuntimeError('Could not find dummy_drv.so for the Xorg dummy driver')
        width, height, depth = self.parseScreenSpec()
        modeSizes = self.preconfiguredModeSizes()
        maxWidth = max(size[0] for size in modeSizes)
        maxHeight = max(size[1] for size in modeSizes)
        modeNames = [self.modelineName(modeWidth, modeHeight) for modeWidth, modeHeight in modeSizes]
        modelines = '\n'.join(self.buildConfigModeline(modeWidth, modeHeight) for modeWidth, modeHeight in modeSizes)
        quotedModes = ' '.join(f'"{name}"' for name in modeNames)
        tempPath = Path(getattr(self.tempDir, 'name', EMPTY_STRING) or tempfile.gettempdir())
        modulePath = str(Path(dummyDriver).resolve().parent.parent)
        configPath = tempPath / 'xorg-dummy.conf'
        configText = f'''Section "ServerFlags"
    Option "DontVTSwitch" "true"
    Option "AllowMouseOpenFail" "true"
    Option "PciForceNone" "true"
    Option "AutoEnableDevices" "false"
    Option "AutoAddDevices" "false"
    Option "BlankTime" "0"
    Option "StandbyTime" "0"
    Option "SuspendTime" "0"
    Option "OffTime" "0"
EndSection

Section "Files"
    ModulePath "{modulePath}"
EndSection

Section "Monitor"
    Identifier "Monitor0"
    HorizSync {self.MODELINE_HSYNC_KHZ}
    VertRefresh {self.MODELINE_VREFRESH_HZ}
    Option "PreferredMode" "{self.modelineName(width, height)}"
{modelines}
EndSection

Section "Device"
    Identifier "Card0"
    Driver "dummy"
    VideoRam {self.DUMMY_VIDEO_RAM_KIB}
EndSection

Section "Screen"
    Identifier "Screen0"
    Device "Card0"
    Monitor "Monitor0"
    DefaultDepth {depth}
    SubSection "Display"
        Depth {depth}
        Viewport 0 0
        Virtual {maxWidth} {maxHeight}
        Modes {quotedModes}
    EndSubSection
EndSection

Section "ServerLayout"
    Identifier "Layout0"
    Screen "Screen0"
EndSection
'''
        File.writeText(configPath, configText, encoding='utf-8')
        return str(configPath)
    def xrandrOutputState(self) -> tuple[str, list[str], str]:
        xrandr = self.toolPath('xrandr')
        if not xrandr:
            return EMPTY_STRING, [], EMPTY_STRING
        result = startLifecycleRunCommand([xrandr, '--display', self.displayName, '--query'], capture_output=True, text=True, encoding='utf-8', errors='replace', env=self.buildEnvironment(), check=False)
        stdout = str(result.stdout or EMPTY_STRING)
        chosen = EMPTY_STRING
        modes: list[str] = []
        currentOutput = EMPTY_STRING
        for rawLine in stdout.splitlines():
            line = str(rawLine or EMPTY_STRING).rstrip()
            header = re.match(r'^(\S+)\s+(connected|disconnected)\b', line)
            if header:
                currentOutput = str(header.group(1) or EMPTY_STRING).strip()
                if str(header.group(2) or EMPTY_STRING) == 'connected' and not chosen:
                    chosen = currentOutput
                continue
            if currentOutput and re.match(r'^\s+\d+x\d+\s', line):
                token = str(line.strip().split()[0] or EMPTY_STRING).strip()
                if currentOutput == chosen and token:
                    modes.append(token)
        return chosen, modes, stdout

    def ensureRequestedMode(self) -> bool:
        xrandr = self.toolPath('xrandr')
        if not xrandr:
            return False
        width, height, _ = self.parseScreenSpec()
        targetMode = f'{width}x{height}'
        outputName, modes, _ = self.xrandrOutputState()
        if not outputName:
            return False
        env = self.buildEnvironment()
        if targetMode not in modes:
            cvt = self.toolPath('cvt')
            if cvt:
                cvtResult = startLifecycleRunCommand([cvt, str(width), str(height), '60'], capture_output=True, text=True, encoding='utf-8', errors='replace', env=env, check=False)
                modelineMatch = re.search(r'^Modeline\s+"([^"]+)"\s+(.+)$', str(cvtResult.stdout or EMPTY_STRING), re.MULTILINE)
                if modelineMatch:
                    modeName = str(modelineMatch.group(1) or targetMode).strip()
                    modelineBody = str(modelineMatch.group(2) or EMPTY_STRING).strip()
                    startLifecycleRunCommand([xrandr, '--display', self.displayName, '--newmode', modeName, *shlex.split(modelineBody)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, check=False)
                    startLifecycleRunCommand([xrandr, '--display', self.displayName, '--addmode', outputName, modeName], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, check=False)
                    targetMode = modeName
        setMode = startLifecycleRunCommand([xrandr, '--display', self.displayName, '--fb', f'{width}x{height}', '--output', outputName, '--mode', targetMode], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, check=False)
        if int(getattr(setMode, 'returncode', 1)) != 0:
            return False
        finalOutput, _, finalState = self.xrandrOutputState()
        return bool(finalOutput == outputName and re.search(rf'^Screen 0:.*current\s+{width} x {height}\b', finalState, re.MULTILINE))

    def launchViaXdummyWrapper(self) -> None:
        xdummy = self.requireTool('Xdummy')
        width, height, depth = self.parseScreenSpec()
        command = [
            xdummy,
            self.displayName,
            '-geometry', f'{width}x{height}',
            '-depth', str(depth),
            '-auth', self.xauthorityPath,
            '-nolisten', 'tcp',
        ]
        self.proc = START_EXECUTION_LIFECYCLE.startProcess('ManagedCaptureProcess', command, cwd=str(BASE_DIR), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.buildEnvironment(), start_new_session=True)

    def launchViaXorgDummy(self) -> None:
        xorg = self.requireTool('Xorg')
        configPath = self.writeDummyConfig()
        logPath = str(Path(getattr(self.tempDir, 'name', tempfile.gettempdir())) / 'Xorg.dummy.log')
        command = [
            xorg,
            self.displayName,
            '-config', configPath,
            '-auth', self.xauthorityPath,
            '-nolisten', 'tcp',
            '-noreset',
            '-logfile', logPath,
        ]
        self.proc = START_EXECUTION_LIFECYCLE.startProcess('ManagedCaptureProcess', command, cwd=str(BASE_DIR), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.buildEnvironment(), start_new_session=True)

    def start(self):
        if os.name == 'nt':
            raise RuntimeError('Managed offscreen X11 capture engines are only supported on Linux/X11')
        if self.proc is not None and self.proc.poll() is None and self.ready:
            return True
        self.requireTool('xauth')
        self.requireTool('xdpyinfo')
        directError = None
        self.prepareSessionArtifacts()
        try:
            self.launchViaXorgDummy()
            self.waitUntilReady()
            modeOk = self.ensureRequestedMode()
            if not modeOk:
                self.emit(f'[WARN:offscreen] Xdummy display {self.displayName} did not confirm requested mode {self.screenSpec}', getattr(self.owner, 'logPath', EMPTY_STRING))
            self.emit(f'[PromptDebugger] Offscreen Xdummy ready  display={self.displayName}  screen={self.screenSpec}  launcher=Xorg-dummy', getattr(self.owner, 'logPath', EMPTY_STRING))
            return True
        except Exception as error:
            captureException(None, source='start.py', context='except@3224')
            print(f"[WARN:swallowed-exception] start.py:2762 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            directError = error
            self.emit(f'[WARN:offscreen] Direct Xorg dummy launch failed on {self.displayName}: {type(error).__name__}: {error}; falling back to Xdummy wrapper if available', getattr(self.owner, 'logPath', EMPTY_STRING))
            self.stop()
        if self.hasTool('Xdummy'):
            self.prepareSessionArtifacts()
            try:
                self.launchViaXdummyWrapper()
                self.waitUntilReady()
                modeOk = self.ensureRequestedMode()
                if not modeOk:
                    self.emit(f'[WARN:offscreen] Xdummy display {self.displayName} did not confirm requested mode {self.screenSpec}', getattr(self.owner, 'logPath', EMPTY_STRING))
                self.emit(f'[PromptDebugger] Offscreen Xdummy ready  display={self.displayName}  screen={self.screenSpec}  launcher=Xdummy-wrapper', getattr(self.owner, 'logPath', EMPTY_STRING))
                return True
            except Exception as error:
                captureException(None, source='start.py', context='except@3239')
                print(f"[WARN:swallowed-exception] start.py:2776 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                self.stop()
                raise RuntimeError(f'Direct Xorg dummy launch failed ({type(directError).__name__}: {directError}) and Xdummy wrapper fallback failed ({type(error).__name__}: {error})')
        raise RuntimeError(f'Direct Xorg dummy launch failed ({type(directError).__name__}: {directError}) and no Xdummy wrapper fallback is available')

class XpraCaptureEngine(CaptureEngine):
    ENGINE_KIND = 'xpra'

    def __init__(self, owner=None, screenSpec: str = EMPTY_STRING, backgroundColor=None, backgroundAlpha: int = 0):
        super().__init__(owner, screenSpec=screenSpec, backgroundColor=backgroundColor, backgroundAlpha=backgroundAlpha)
        self.displayProc = None  # noqa: nonconform
        self.socketDir = EMPTY_STRING  # noqa: nonconform
        self.xpraLogPath = EMPTY_STRING  # noqa: nonconform
        self.xpraLogHandle = None  # noqa: nonconform
        self.launchMode = 'wrap-xvfb'  # noqa: nonconform

    def socketArguments(self) -> list[str]:
        socketDir = str(self.socketDir or EMPTY_STRING).strip()
        if not socketDir:
            return []
        return [f'--socket-dir={socketDir}']

    def openXpraLogHandle(self):
        self.closeXpraLogHandle()
        logPath = str(self.xpraLogPath or EMPTY_STRING).strip()
        if not logPath:
            return None
        Path(logPath).parent.mkdir(parents=True, exist_ok=True)
        self.xpraLogHandle = File.tracedOpen(logPath, 'ab', buffering=0)
        return self.xpraLogHandle

    def closeXpraLogHandle(self) -> None:
        handle = getattr(self, 'xpraLogHandle', None)
        self.xpraLogHandle = None
        if handle is not None:
            try:
                handle.close()
            except Exception as error:
                captureException(None, source='start.py', context='except@3277')
                print(f"[WARN:swallowed-exception] start.py:2814 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass

    def xpraLogTail(self, limitBytes: int = 8192) -> str:
        logPath = str(self.xpraLogPath or EMPTY_STRING).strip()
        if not logPath:
            return EMPTY_STRING
        try:
            data = Path(logPath).read_bytes()
            tail = data[-max(256, int(limitBytes or 8192)):]
            return tail.decode('utf-8', errors='replace').strip()
        except Exception as error:
            captureException(None, source='start.py', context='except@3289')
            print(f"[WARN:swallowed-exception] start.py:2825 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return EMPTY_STRING

    def xpraReadyCommands(self) -> list[list[str]]:
        xpra = self.requireTool('xpra')
        socketArgs = self.socketArguments()
        return [
            [xpra, 'info', self.displayName, *socketArgs],
            [xpra, 'id', self.displayName, *socketArgs],
            [xpra, 'list', *socketArgs],
        ]

    def xpraOutputLooksReady(self, stdoutText: str, stderrText: str = EMPTY_STRING) -> bool:
        haystack = '\n'.join([str(stdoutText or EMPTY_STRING), str(stderrText or EMPTY_STRING)]).strip()
        if not haystack:
            return False
        lowered = haystack.lower()
        displayToken = str(self.displayName or EMPTY_STRING).strip().lower()
        if displayToken and displayToken in lowered:
            return True
        readyMarkers = (
            'session-name',
            'server.ready',
            'server_state',
            'uuid=',
            'socket',
            'mode=',
            'desktop',
            'start time',
        )
        return any(marker in lowered for marker in readyMarkers)

    def waitForXpraReady(self, timeoutSeconds: float = 12.0) -> bool:
        deadline = time.time() + float(timeoutSeconds or 12.0)
        commands = self.xpraReadyCommands()
        lastError = EMPTY_STRING
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                logTail = self.xpraLogTail()
                detail = f'Xpra server exited early with code {self.proc.returncode}'
                if logTail:
                    detail += f'\n--- xpra.log tail ---\n{logTail}'
                raise RuntimeError(detail)
            for command in commands:
                result = startLifecycleRunCommand(command, capture_output=True, text=True, encoding='utf-8', errors='replace', env=self.buildEnvironment(), check=False)
                stdoutText = str(result.stdout or EMPTY_STRING)
                stderrText = str(result.stderr or EMPTY_STRING)
                if int(getattr(result, 'returncode', 1)) == 0 and self.xpraOutputLooksReady(stdoutText, stderrText):
                    self.ready = True
                    return True
                if stderrText.strip():
                    lastError = stderrText.strip()
            time.sleep(0.15)
        logTail = self.xpraLogTail()
        detail = f'Xpra server did not become ready on {self.displayName}'
        if lastError:
            detail += f'\nLast xpra probe error: {lastError}'
        if logTail:
            detail += f'\n--- xpra.log tail ---\n{logTail}'
        raise RuntimeError(detail)

    def xpraStartCommand(self) -> list[str]:
        xpra = self.requireTool('xpra')
        return [
            xpra,
            'start', self.displayName,
            '--use-display=yes',
            '--daemon=no',
            '--attach=no',
            '--mdns=no',
            '--notifications=no',
            '--pulseaudio=no',
            '--dbus-launch=no',
            '--dbus-proxy=no',
            '--dbus-control=no',
            '--printing=no',
            '--file-transfer=no',
            '--open-files=no',
            '--webcam=no',
            '--clipboard=no',
            '--mmap=no',
            '--systemd-run=no',
            '--resize-display=no',
            '--sync-xvfb=50',
            '--start-new-commands=no',
            '--exit-with-client=no',
            '--bell=no',
            '--microphone=no',
            '--speaker=no',
            '--xsettings=no',
            '--session-name=TrioDesktop-Offscreen-Xpra',
            *self.socketArguments(),
        ]

    def start(self):
        if os.name == 'nt':
            raise RuntimeError('Managed offscreen X11 capture engines are only supported on Linux/X11')
        if self.proc is not None and self.proc.poll() is None and self.ready:
            return True
        self.requireTool('xpra')
        CaptureEngine.start(self)
        self.displayProc = self.proc
        self.proc = None
        self.ready = False
        tempPath = Path(getattr(self.tempDir, 'name', tempfile.gettempdir()))
        socketPath = tempPath / 'xpra-sockets'
        socketPath.mkdir(parents=True, exist_ok=True)
        self.socketDir = str(socketPath)
        self.xpraLogPath = str(tempPath / 'xpra.log')
        env = self.buildEnvironment()
        env.setdefault('XPRA_LOG_DIR', str(tempPath))
        env.setdefault('XPRA_SOCKET_DIR', self.socketDir)
        command = self.xpraStartCommand()
        self.emit(f'[PromptDebugger] Starting Xpra wrapper  display={self.displayName}  screen={self.screenSpec}  socketDir={self.socketDir}', getattr(self.owner, 'logPath', EMPTY_STRING))
        logHandle = self.openXpraLogHandle()
        self.proc = START_EXECUTION_LIFECYCLE.startProcess(
            'XpraCaptureEngine',
            command,
            cwd=str(BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=logHandle if logHandle is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if logHandle is not None else subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        try:
            self.waitForXpraReady()
            self.emit(f'[PromptDebugger] Offscreen Xpra ready  display={self.displayName}  screen={self.screenSpec}  socketDir={self.socketDir}  mode={self.launchMode}', getattr(self.owner, 'logPath', EMPTY_STRING))
            return True
        except Exception as error:
            captureException(None, source='start.py', context='except@3419')
            print(f"[WARN:swallowed-exception] start.py:2956 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            logTail = self.xpraLogTail()
            self.stop()
            detail = str(error or EMPTY_STRING).strip() or f'Failed starting xpra on {self.displayName}'
            if logTail and 'xpra.log tail' not in detail:
                detail += f'\n--- xpra.log tail ---\n{logTail}'
            raise RuntimeError(detail)

    def stop(self) -> bool:
        xpraProc = self.proc
        xpraPath = self.toolPath('xpra')
        if xpraPath and str(self.displayName or EMPTY_STRING).strip() and str(self.socketDir or EMPTY_STRING).strip():
            startLifecycleRunCommand([xpraPath, 'stop', self.displayName, *self.socketArguments()], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.buildEnvironment(), check=False)
        self.proc = None
        self.ready = False
        if xpraProc is not None:
            try:
                if xpraProc.poll() is None:
                    xpraProc.terminate()
                    xpraProc.wait(timeout=2.0)
            except Exception as error:
                captureException(None, source='start.py', context='except@3440')
                print(f"[WARN:swallowed-exception] start.py:2977 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                try:
                    xpraProc.kill()
                except Exception as error:
                    captureException(None, source='start.py', context='except@3444')
                    print(f"[WARN:swallowed-exception] start.py:2980 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    pass
        self.closeXpraLogHandle()
        self.proc = self.displayProc
        self.displayProc = None
        self.socketDir = EMPTY_STRING
        self.xpraLogPath = EMPTY_STRING
        self.launchMode = 'wrap-xvfb'
        return CaptureEngine.stop(self)


def createCaptureEngine(kind: str, debugger, screenSpec: str = EMPTY_STRING, backgroundColor=None, backgroundAlpha: int = 0):
    engineKind = str(kind or 'xvfb').strip().lower() or 'xvfb'
    engineMap = {
        'xvfb': XvfbCaptureEngine,
        'xdummy': XdummyCaptureEngine,
        'xpra': XpraCaptureEngine,
    }
    engineClass = engineMap.get(engineKind, XvfbCaptureEngine)
    return engineClass(debugger, screenSpec=screenSpec, backgroundColor=backgroundColor, backgroundAlpha=backgroundAlpha)


class StartDependencyRegistry:
    PYTHON_DEPENDENCY_TABLE = tuple(dict(item) for item in PYTHON_DEPENDENCIES)
    LINUX_SYSTEM_GROUPS = {
        'qt_x11_runtime': (
            {'kind': 'library', 'name': 'libxcb-cursor.so.0', 'lookup': 'xcb-cursor', 'package': 'libxcb-cursor0'},
        ),
        'offscreen_common_tools': (
            {'kind': 'tool', 'name': 'xauth', 'package': 'xauth'},
            {'kind': 'tool', 'name': 'xdpyinfo', 'package': 'x11-utils'},
            {'kind': 'tool', 'name': 'xwininfo', 'package': 'x11-utils'},
            {'kind': 'tool', 'name': 'xprop', 'package': 'x11-utils'},
            {'kind': 'tool', 'name': 'xwd', 'package': 'x11-apps', 'required': True},
            {'kind': 'tool', 'name': 'xdotool', 'package': 'xdotool', 'required': True},
            {'kind': 'tool', 'name': 'convert', 'package': 'imagemagick'},
            {'kind': 'tool', 'name': 'import', 'package': 'imagemagick'},
        ),
        'xvfb_tools': (
            {'kind': 'tool', 'name': 'Xvfb', 'package': 'xvfb'},
        ),
        'xdummy_tools': (
            {'kind': 'tool', 'name': 'Xorg', 'package': 'xserver-xorg-core'},
            {'kind': 'tool', 'name': 'xrandr', 'package': 'x11-xserver-utils', 'required': True},
            {'kind': 'tool', 'name': 'cvt', 'package': 'xcvt', 'required': True},
            {'kind': 'path', 'name': 'dummy_drv.so', 'package': 'xserver-xorg-video-dummy', 'candidates': list(XdummyCaptureEngine.DUMMY_DRIVER_CANDIDATE_PATHS)},
            {'kind': 'tool', 'name': 'Xdummy', 'package': 'x11vnc'},
        ),
        'xpra_tools': (
            {'kind': 'tool', 'name': 'xpra', 'package': 'xpra', 'required': True},
        ),
    }

    def __init__(self, argv=None):
        self.argv = list(argv or [])
        self.osFamily = self.detectOsFamily()  # noqa: nonconform
        self._moduleAvailabilityCache = {}
        self._targetPythonCache = {}

    @staticmethod
    def detectOsFamily() -> str:
        name = str(platform.system() or EMPTY_STRING).strip().lower()
        if name.startswith('win'):
            return 'windows'
        if name.startswith('linux'):
            return 'linux'
        if name.startswith('darwin') or name.startswith('mac'):
            return 'mac'
        return name or 'unknown'

    def isWindows(self) -> bool:
        return self.osFamily == 'windows'

    def isLinux(self) -> bool:
        return self.osFamily == 'linux'

    def isGuiRequired(self) -> bool:
        tokens = normalizedCliTokens(self.argv)
        return not any(flag in tokens for flag in HEADLESS_FLAGS)

    def pythonDependencyEntries(self, requiredOnly: bool = False) -> list[dict[str, Any]]:
        gui_required = self.isGuiRequired()
        entries: list[dict[str, Any]] = []
        for item in self.PYTHON_DEPENDENCY_TABLE:
            entry = dict(item)
            if bool(entry.get('gui_only')) and not gui_required:
                continue
            if bool(requiredOnly) and not bool(entry.get('required', True)):
                continue
            entries.append(entry)
        return entries

    def requiredPythonModules(self) -> dict[str, str]:
        required: dict[str, str] = {}
        for entry in self.pythonDependencyEntries(requiredOnly=True):
            import_name = str(entry.get('import_name', EMPTY_STRING) or EMPTY_STRING).strip()
            pip_package = str(entry.get('pip_package', EMPTY_STRING) or EMPTY_STRING).strip()
            if import_name:
                required[import_name] = pip_package
        return required

    @staticmethod
    def dependencyPackageName(entry: dict[str, Any], field_name: str) -> str:
        return str(entry.get(field_name, EMPTY_STRING) or EMPTY_STRING).strip()

    def targetPythonExecutable(self, guiRequired: bool | None = None) -> str:
        required = self.isGuiRequired() if guiRequired is None else bool(guiRequired)
        cache_key = bool(required)
        if cache_key in self._targetPythonCache:
            return str(self._targetPythonCache.get(cache_key) or EMPTY_STRING).strip()
        if self.isWindows():
            value = str(resolvePreferredPythonCommand(guiRequired=required) or sys.executable or EMPTY_STRING).strip()
        else:
            preferred = str(resolvePreferredChildPython(guiRequired=required) or EMPTY_STRING).strip()
            if preferred:
                value = preferred
            else:
                preferred = str(resolvePreferredPythonCommand(guiRequired=required) or EMPTY_STRING).strip()
                value = preferred or str(sys.executable or EMPTY_STRING).strip()
        self._targetPythonCache[cache_key] = value
        return str(value or EMPTY_STRING).strip()

    def moduleAvailableInTargetPython(self, module_name: str) -> bool:
        module_text = str(module_name or EMPTY_STRING).strip()
        if not module_text:
            return False
        if module_text.startswith('PySide6.'):
            module_text = 'PySide6'
        cache_key = (str(self.targetPythonExecutable() or EMPTY_STRING).strip(), module_text)
        cached = self._moduleAvailabilityCache.get(cache_key)
        if cached is not None:
            return bool(cached)
        target_python = self.targetPythonExecutable()
        if target_python:
            result = canInterpreterImportModule(target_python, module_text)
        else:
            result = hasModule(module_text)
        self._moduleAvailabilityCache[cache_key] = bool(result)
        return bool(result)

    def missingPythonDependencyEntries(self, requiredOnly: bool = False) -> list[dict[str, Any]]:
        missing: list[dict[str, Any]] = []
        for entry in self.pythonDependencyEntries(requiredOnly=requiredOnly):
            import_name = str(entry.get('import_name', EMPTY_STRING) or EMPTY_STRING).strip()
            if import_name and not self.moduleAvailableInTargetPython(import_name):
                missing.append(dict(entry))
        return missing

    def missingPythonModules(self, requiredOnly: bool = False) -> list[tuple[str, str]]:
        missing_entries = self.missingPythonDependencyEntries(requiredOnly=requiredOnly)
        return [
            (
                str(entry.get('import_name', EMPTY_STRING) or EMPTY_STRING).strip(),
                self.dependencyPackageName(entry, 'pip_package'),
            )
            for entry in missing_entries
        ]

    @staticmethod
    def uniquePackagesFromDependencyEntries(entries: list[dict[str, Any]], field_name: str) -> list[str]:
        packages: list[str] = []
        for entry in list(entries or []):
            package_name = str(entry.get(field_name, EMPTY_STRING) or EMPTY_STRING).strip()
            if package_name and package_name not in packages:
                packages.append(package_name)
        return packages

    @staticmethod
    def uniquePackagesForMissingModules(missing_modules: list[tuple[str, str]]) -> list[str]:
        packages: list[str] = []
        for _, package_name in list(missing_modules or []):
            package_text = str(package_name or EMPTY_STRING).strip()
            if package_text and package_text not in packages:
                packages.append(package_text)
        return packages


    @staticmethod
    def isWindowsAdmin() -> bool:
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception as error:
            captureException(None, source='start.py', context='except@3627')
            print(f"[WARN:swallowed-exception] start.py:3164 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return False

    @staticmethod
    def isPosixRoot() -> bool:
        geteuid = getattr(os, 'geteuid', None)
        if not callable(geteuid):
            return False
        try:
            return int(cast(Any, geteuid)()) == 0
        except Exception as error:
            captureException(None, source='start.py', context='except@3638')
            print(f"[WARN:swallowed-exception] start.py:3174 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return False

    @staticmethod
    def libExists(lookupName: str, reportedName: str = EMPTY_STRING) -> bool:
        try:
            located = ctypes.util.find_library(lookupName)
            if located:
                return True
        except Exception as error:
            captureException(None, source='start.py', context='except@3648')
            print(f"[WARN:swallowed-exception] start.py:3183 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        report = str(reportedName or lookupName or EMPTY_STRING).strip()
        packaged = [str((Path(root) / report)) for root in packagedLinuxRuntimeLibDirs()]
        candidates = tuple(packaged) + (
            f'/usr/lib/x86_64-linux-gnu/{report}',
            f'/usr/lib64/{report}',
            f'/usr/lib/{report}',
            f'/lib/x86_64-linux-gnu/{report}',
            f'/lib64/{report}',
            f'/lib/{report}',
        )
        return any(Path(candidate).exists() for candidate in candidates)

    def linuxSystemRequirements(self) -> list[dict[str, Any]]:
        if not self.isLinux():
            return []
        requirements: list[dict[str, Any]] = []
        if self.isGuiRequired():
            requirements.extend(list(self.LINUX_SYSTEM_GROUPS.get('qt_x11_runtime', ())))
        if offscreenRequested(self.argv):
            requirements.extend(list(self.LINUX_SYSTEM_GROUPS.get('offscreen_common_tools', ())))
            engineKind = requestedCaptureEngineKind(self.argv)
            if engineKind == 'xdummy':
                requirements.extend(list(self.LINUX_SYSTEM_GROUPS.get('xdummy_tools', ())))
            elif engineKind == 'xpra':
                requirements.extend(list(self.LINUX_SYSTEM_GROUPS.get('xvfb_tools', ())))
                requirements.extend(list(self.LINUX_SYSTEM_GROUPS.get('xpra_tools', ())))
            else:
                requirements.extend(list(self.LINUX_SYSTEM_GROUPS.get('xvfb_tools', ())))
        return [dict(item) for item in requirements]

    def missingLinuxSystemRequirements(self) -> list[dict[str, Any]]:
        missing: list[dict[str, Any]] = []
        for requirement in self.linuxSystemRequirements():
            kind = str(requirement.get('kind', EMPTY_STRING) or EMPTY_STRING)
            name = str(requirement.get('name', EMPTY_STRING) or EMPTY_STRING)
            is_missing = False
            if kind == 'tool':
                is_missing = not bool(packagedLinuxRuntimeToolPath(name))
            elif kind == 'library':
                lookup_name = str(requirement.get('lookup', EMPTY_STRING) or name)
                is_missing = not self.libExists(lookup_name, reportedName=name)
            elif kind == 'path':
                candidates = [str(item or EMPTY_STRING).strip() for item in list(requirement.get('candidates', []) or []) if str(item or EMPTY_STRING).strip()]
                is_missing = not any(Path(candidate).exists() for candidate in candidates)
            if is_missing:
                missing.append(requirement)
        return missing


    @staticmethod
    def runCommandDetailed(command: list[str], *, label: str = EMPTY_STRING, timeout: int = 900) -> tuple[bool, str, str, int]:
        try:
            print(f"[TRACE:deps] {label or 'running'}: {' '.join(command)}", file=sys.stderr, flush=True)
            completed = startLifecycleRunCommand(command, capture_output=True, text=True, timeout=timeout, check=False)
            stdout_text = str(completed.stdout or EMPTY_STRING).strip()
            stderr_text = str(completed.stderr or EMPTY_STRING).strip()
            if stdout_text:
                print(stdout_text, file=sys.stderr, flush=True)
            if stderr_text:
                print(stderr_text, file=sys.stderr, flush=True)
            return int(getattr(completed, 'returncode', 1)) == 0, stdout_text, stderr_text, int(getattr(completed, 'returncode', 1))
        except Exception as error:
            captureException(None, source='start.py', context='except@3712')
            print(f"[WARN:deps] {label or 'command'} failed: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
            return False, EMPTY_STRING, str(error), 1

    @staticmethod
    def runCommand(command: list[str], *, label: str = EMPTY_STRING, timeout: int = 900) -> bool:
        succeeded, _, _, _ = StartDependencyRegistry.runCommandDetailed(command, label=label, timeout=timeout)
        return succeeded

    @staticmethod
    def outputLooksLikePermissionError(stdout_text: str, stderr_text: str) -> bool:
        haystack = '\n'.join([str(stdout_text or EMPTY_STRING), str(stderr_text or EMPTY_STRING)]).lower()
        permission_markers = (
            'permission denied',
            'access is denied',
            'winerror 5',
            'errno 13',
            'operation not permitted',
            'could not install packages due to an oserror',
            'site-packages is not writeable',
            'site-packages is not writable',
            'not writeable',
            'not writable',
            'requires administrator',
            'externally-managed-environment',
            'externally managed',
            '--break-system-packages',
            'pep 668',
        )
        return any(marker in haystack for marker in permission_markers)

    @staticmethod
    def outputLooksLikeExternallyManaged(stdout_text: str, stderr_text: str) -> bool:
        haystack = '\n'.join([str(stdout_text or EMPTY_STRING), str(stderr_text or EMPTY_STRING)]).lower()
        return any(marker in haystack for marker in (
            'externally-managed-environment',
            'externally managed',
            '--break-system-packages',
            'pep 668',
        ))

    def pipInstallTimeoutSeconds(self, packages: list[str] | None = None) -> int:
        raw = str(os.environ.get('PROMPT_PIP_INSTALL_TIMEOUT_SECONDS', EMPTY_STRING) or EMPTY_STRING).strip()
        try:
            explicit = int(raw) if raw else 0
        except ValueError:
            explicit = 0
        if explicit > 0:
            return explicit
        text = ' '.join(str(item or EMPTY_STRING).lower() for item in list(packages or []))
        if 'pyside6' in text or 'pyinstaller' in text or 'nuitka' in text:
            return 7200
        return 3600

    def pipInstallRetryCount(self) -> int:
        raw = str(os.environ.get('PROMPT_PIP_INSTALL_RETRIES', EMPTY_STRING) or EMPTY_STRING).strip()
        try:
            count = int(raw) if raw else 2
        except ValueError:
            count = 2
        return max(1, count)

    def runPipInstallWithRetries(self, command: list[str], *, label: str, packages: list[str]) -> tuple[bool, str, str, int]:
        retries = self.pipInstallRetryCount()
        timeout = self.pipInstallTimeoutSeconds(packages)
        last: tuple[bool, str, str, int] = (False, EMPTY_STRING, EMPTY_STRING, 1)
        for attempt in range(1, retries + 1):
            attempt_label = f'{label} attempt {attempt}/{retries}'
            print(f'[TRACE:deps] pip retry policy label={label} attempt={attempt}/{retries} timeout={timeout}s packages={packages}', file=sys.stderr, flush=True)
            last = self.runCommandDetailed(command, label=attempt_label, timeout=timeout)
            if last[0]:
                return last
            if attempt < retries:
                print(f'[WARN:deps] {label} failed or timed out; retrying once more so large packages can finish from pip cache/resume.', file=sys.stderr, flush=True)
        return last


    def attemptWindowsElevatedPipInstall(self, packages: list[str], *, pythonExecutable: str = EMPTY_STRING) -> bool:
        if not self.isWindows() or not list(packages or []):
            return False
        powershell = shutil.which('powershell.exe') or shutil.which('powershell')
        if not powershell:
            print('[WARN:deps] Windows elevation requested but PowerShell was not found.', file=sys.stderr, flush=True)
            return False
        package_list = ','.join(["'" + str(package).replace("'", "''") + "'" for package in packages])
        executable = str(pythonExecutable or self.targetPythonExecutable() or sys.executable or EMPTY_STRING).strip().replace("'", "''")
        if not executable:
            return False
        script = (
            "$ErrorActionPreference='Stop';"
            f"$packages=@({package_list});"
            f"$arguments=@('-m','pip','install') + $packages;"
            f"$process=Start-Process -FilePath '{executable}' -ArgumentList $arguments -Verb RunAs -Wait -PassThru;"
            "exit [int]$process.ExitCode"
        )
        for attempt in range(1, self.pipInstallRetryCount() + 1):
            if self.runCommand([powershell, '-NoProfile', '-NonInteractive', '-Command', script], label=f'windows elevated pip install attempt {attempt}/{self.pipInstallRetryCount()}', timeout=self.pipInstallTimeoutSeconds(packages)):
                return True
            if attempt < self.pipInstallRetryCount():
                print('[WARN:deps] Windows elevated pip install failed/timed out; retrying with pip cache.', file=sys.stderr, flush=True)
        return False

    def attemptLinuxSudoPipInstall(self, packages: list[str], *, pythonExecutable: str = EMPTY_STRING) -> bool:
        if not self.isLinux() or not list(packages or []):
            return False
        executable = str(pythonExecutable or self.targetPythonExecutable() or sys.executable or EMPTY_STRING).strip()
        if not executable:
            return False
        if self.isPosixRoot():
            return self.runPipInstallWithRetries([executable, '-m', 'pip', 'install', '--break-system-packages', *packages], label='linux root pip install --break-system-packages', packages=packages)[0]
        sudo = shutil.which('sudo')
        if not sudo:
            print('[WARN:deps] Linux sudo was not found; falling back to non-elevated pip.', file=sys.stderr, flush=True)
            return False
        command = [sudo, '-n', executable, '-m', 'pip', 'install', *packages]
        succeeded = self.runPipInstallWithRetries(command, label='linux sudo pip install', packages=packages)[0]
        if not succeeded:
            print('[WARN:deps] Linux sudo pip install was denied or failed; falling back to non-elevated pip.', file=sys.stderr, flush=True)
        return succeeded


    def attemptPlainPipInstall(self, packages: list[str], *, pythonExecutable: str = EMPTY_STRING, useUser: bool = False, label: str = EMPTY_STRING) -> tuple[bool, bool]:
        if not list(packages or []):
            return True, False
        executable = str(pythonExecutable or self.targetPythonExecutable() or sys.executable or EMPTY_STRING).strip()
        if not executable:
            return False, False
        command = [executable, '-m', 'pip', 'install']
        if useUser:
            command.append('--user')
        command.extend(list(packages or []))
        succeeded, stdout_text, stderr_text, _ = self.runPipInstallWithRetries(command, label=label or f'{self.osFamily} pip install', packages=list(packages or []))
        return succeeded, self.outputLooksLikePermissionError(stdout_text, stderr_text)


    def installMissingPythonDependencies(self) -> None:
        missing_entries = self.missingPythonDependencyEntries(requiredOnly=True)
        missing_modules = [str(entry.get('import_name', EMPTY_STRING) or EMPTY_STRING).strip() for entry in missing_entries]
        missing_packages = self.uniquePackagesFromDependencyEntries(missing_entries, 'pip_package')
        if not missing_packages:
            return
        module_names = ', '.join([name for name in missing_modules if name])
        package_names = ', '.join(missing_packages)
        target_python = self.targetPythonExecutable()
        print(f"[TRACE:deps] platform={self.osFamily}", file=sys.stderr, flush=True)
        print(f"[TRACE:deps] target python={target_python or '(unavailable)'}", file=sys.stderr, flush=True)
        print(f"[TRACE:deps] installing missing modules: {module_names}", file=sys.stderr, flush=True)
        print(f"[TRACE:deps] pip packages: {package_names}", file=sys.stderr, flush=True)
        if self.isWindows():
            direct_ok, direct_permission = self.attemptPlainPipInstall(missing_packages, pythonExecutable=target_python, useUser=False, label='windows pip install')
            if direct_ok:
                self._moduleAvailabilityCache.clear()
                _IMPORT_MODULE_CACHE.clear()
                return
            user_ok, user_permission = self.attemptPlainPipInstall(missing_packages, pythonExecutable=target_python, useUser=True, label='windows user pip install')
            if user_ok:
                self._moduleAvailabilityCache.clear()
                _IMPORT_MODULE_CACHE.clear()
                return
            if direct_permission or user_permission:
                if self.isWindowsAdmin():
                    admin_ok, _ = self.attemptPlainPipInstall(missing_packages, pythonExecutable=target_python, useUser=False, label='windows admin pip install')
                    if admin_ok:
                        self._moduleAvailabilityCache.clear()
                        _IMPORT_MODULE_CACHE.clear()
                        return
                else:
                    elevated_ok = self.attemptWindowsElevatedPipInstall(missing_packages, pythonExecutable=target_python)
                    if elevated_ok:
                        self._moduleAvailabilityCache.clear()
                        _IMPORT_MODULE_CACHE.clear()
                        return
                    print('[WARN:deps] Windows elevation was denied or failed after a real permissions error.', file=sys.stderr, flush=True)
            else:
                print('[WARN:deps] Windows pip install failed without a permissions error; skipping UAC elevation.', file=sys.stderr, flush=True)
            return
        if self.isLinux():
            direct_ok, direct_permission = self.attemptPlainPipInstall(missing_packages, pythonExecutable=target_python, useUser=False, label='linux pip install')
            if direct_ok:
                self._moduleAvailabilityCache.clear()
                _IMPORT_MODULE_CACHE.clear()
                return
            if direct_permission and self.attemptLinuxSudoPipInstall(missing_packages, pythonExecutable=target_python):
                self._moduleAvailabilityCache.clear()
                _IMPORT_MODULE_CACHE.clear()
                return
            break_command = [target_python or sys.executable, '-m', 'pip', 'install', '--break-system-packages', *missing_packages]
            break_ok = self.runPipInstallWithRetries(break_command, label='linux pip install --break-system-packages', packages=missing_packages)[0]
            if break_ok:
                self._moduleAvailabilityCache.clear()
                _IMPORT_MODULE_CACHE.clear()
                return
            print('[WARN:deps] Linux pip install failed for the target interpreter, including --break-system-packages fallback.', file=sys.stderr, flush=True)
            return
        succeeded = self.runPipInstallWithRetries([target_python or sys.executable, '-m', 'pip', 'install', *missing_packages], label='generic pip install', packages=missing_packages)[0]
        if succeeded:
            self._moduleAvailabilityCache.clear()
            _IMPORT_MODULE_CACHE.clear()
            return
        print('[WARN:deps] Generic pip install failed.', file=sys.stderr, flush=True)


    def requiredLinuxSystemPackages(self, missing_requirements: list[dict[str, Any]]) -> list[str]:
        packages: list[str] = []
        for requirement in list(missing_requirements or []):
            if not bool(requirement.get('required')):
                continue
            package_name = str(requirement.get('package', EMPTY_STRING) or EMPTY_STRING).strip()
            if package_name and package_name not in packages:
                packages.append(package_name)
        return packages

    def attemptLinuxSystemPackageInstall(self, missing_requirements: list[dict[str, Any]]) -> bool:
        if not self.isLinux():
            return False
        packages = self.requiredLinuxSystemPackages(missing_requirements)
        if not packages:
            packages = []
            for requirement in list(missing_requirements or []):
                package_name = str(requirement.get('package', EMPTY_STRING) or EMPTY_STRING).strip()
                if package_name and package_name not in packages:
                    packages.append(package_name)
        if not packages:
            return False
        command: list[str] = []
        if shutil.which('apt-get'):
            command = ['apt-get', 'install', '-y', *packages]
        elif shutil.which('apt'):
            command = ['apt', 'install', '-y', *packages]
        elif shutil.which('dnf'):
            command = ['dnf', '-y', 'install', *packages]
        elif shutil.which('yum'):
            command = ['yum', '-y', 'install', *packages]
        elif shutil.which('zypper'):
            command = ['zypper', '--non-interactive', 'install', *packages]
        elif shutil.which('apk'):
            command = ['apk', 'add', *packages]
        elif shutil.which('pacman'):
            command = ['pacman', '--noconfirm', '-S', *packages]
        if not command:
            print('[WARN:deps] No supported Linux system package manager was found for auto-install.', file=sys.stderr, flush=True)
            return False
        if not self.isPosixRoot():
            sudo = shutil.which('sudo')
            if sudo:
                command = [sudo, '-n', *command]
            else:
                print('[WARN:deps] Linux system package install needs root or sudo; continuing without auto-install.', file=sys.stderr, flush=True)
                return False
        succeeded = self.runCommand(command, label='linux system package install', timeout=1800)
        if not succeeded:
            print('[WARN:deps] Linux system package manager install failed or was denied.', file=sys.stderr, flush=True)
        return succeeded


    def bundledLinuxPackageCandidates(self, package_name: str) -> list[Path]:
        package_text = str(package_name or EMPTY_STRING).strip()
        if not package_text:
            return []
        roots = [DEPLOY_MONITOR_DIR, BASE_DIR / 'debs', BASE_DIR, LINUX_RUNTIME_DIR]
        prefixes = {package_text, package_text.replace(':', '_')}
        candidates: list[Path] = []
        for root in roots:
            try:
                root_path = Path(root)
            except Exception as error:
                captureException(None, source='start.py', context='except@3936')
                print(f"[WARN:swallowed-exception] start.py:3456 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                continue
            if not root_path.exists():
                continue
            patterns = []
            for prefix in list(prefixes):
                patterns.extend([
                    f'{prefix}_*.deb',
                    f'{prefix}-*.deb',
                    f'{prefix}.deb',
                    f'**/{prefix}_*.deb',
                    f'**/{prefix}-*.deb',
                    f'**/{prefix}.deb',
                ])
            for pattern in patterns:
                for match in root_path.glob(pattern):
                    try:
                        resolved = match.resolve()
                    except Exception as error:
                        captureException(None, source='start.py', context='except@3955')
                        print(f"[WARN:swallowed-exception] start.py:3474 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                        continue
                    if resolved.is_file() and resolved not in candidates:
                        candidates.append(resolved)
        candidates.sort(key=lambda item: (item.name, str(item)))
        return candidates

    def attemptBundledLinuxSystemPackageInstall(self, missing_requirements: list[dict[str, Any]]) -> bool:
        if not self.isLinux():
            return False
        extracted_any = False
        package_names: list[str] = []
        for requirement in list(missing_requirements or []):
            package_name = str(requirement.get('package', EMPTY_STRING) or EMPTY_STRING).strip()
            if package_name and package_name not in package_names:
                package_names.append(package_name)
        for package_name in package_names:
            for candidate in self.bundledLinuxPackageCandidates(package_name):
                try:
                    print(f'[TRACE:deps] extracting bundled linux package: {candidate.name}', file=sys.stderr, flush=True)
                    if extractDebianPackageToRuntime(candidate, LINUX_RUNTIME_DIR):
                        extracted_any = True
                except Exception as error:
                    captureException(None, source='start.py', context='except@3978')
                    print(f'[WARN:deps] Failed extracting bundled package {candidate.name}: {type(error).__name__}: {error}', file=sys.stderr, flush=True)
        return extracted_any

    def warnForMissingLinuxSystemRequirements(self, missing_requirements: list[dict[str, Any]]) -> None:
        if not self.isLinux() or not list(missing_requirements or []):
            return
        for requirement in list(missing_requirements or []):
            kind = str(requirement.get('kind', EMPTY_STRING) or EMPTY_STRING)
            name = str(requirement.get('name', EMPTY_STRING) or EMPTY_STRING)
            package_name = str(requirement.get('package', EMPTY_STRING) or EMPTY_STRING)
            required_text = 'required' if bool(requirement.get('required')) else 'optional'
            print(f'[WARN:deps] Missing Linux {kind} ({required_text}): {name}  package={package_name}', file=sys.stderr, flush=True)

    def installMissingDependencies(self) -> None:
        missing_modules = self.missingPythonModules(requiredOnly=True)
        optional_missing_modules = [item for item in self.missingPythonModules(requiredOnly=False) if item not in missing_modules]
        missing_system = self.missingLinuxSystemRequirements() if self.isLinux() else []
        if self.isLinux() and missing_system:
            self.warnForMissingLinuxSystemRequirements(missing_system)
            if self.attemptBundledLinuxSystemPackageInstall(missing_system):
                missing_system = self.missingLinuxSystemRequirements()
            if missing_system and self.attemptLinuxSystemPackageInstall(missing_system):
                missing_system = self.missingLinuxSystemRequirements()
        if missing_modules:
            module_names = ', '.join(module_name for module_name, _ in missing_modules)
            package_names = ', '.join(self.uniquePackagesForMissingModules(missing_modules))
            print(f'[WARN:deps] Missing Python modules: {module_names}', file=sys.stderr, flush=True)
            if package_names:
                print(f'[WARN:deps] Expected Python package source: {package_names}', file=sys.stderr, flush=True)
            self.installMissingPythonDependencies()
            missing_modules = self.missingPythonModules(requiredOnly=True)
            optional_missing_modules = [item for item in self.missingPythonModules(requiredOnly=False) if item not in missing_modules]
        if missing_system:
            self.warnForMissingLinuxSystemRequirements(missing_system)
        if missing_modules:
            module_names = ', '.join(module_name for module_name, _ in missing_modules)
            package_names = ', '.join(self.uniquePackagesForMissingModules(missing_modules))
            print(f'[WARN:deps] Still missing required Python modules: {module_names}', file=sys.stderr, flush=True)
            if package_names:
                print(f'[WARN:deps] Expected package source: {package_names}', file=sys.stderr, flush=True)
        if optional_missing_modules:
            module_names = ', '.join(module_name for module_name, _ in optional_missing_modules)
            package_names = ', '.join(self.uniquePackagesForMissingModules(optional_missing_modules))
            print(f'[WARN:deps] Optional Python modules missing (continuing): {module_names}', file=sys.stderr, flush=True)
            if package_names:
                print(f'[WARN:deps] Optional package source: {package_names}', file=sys.stderr, flush=True)




def missingRequiredModules(argv=None) -> list[tuple[str, str]]:
    return StartDependencyRegistry(argv).missingPythonModules(requiredOnly=True)


def uniquePackagesForMissingModules(missing_modules: list[tuple[str, str]]) -> list[str]:
    return StartDependencyRegistry.uniquePackagesForMissingModules(missing_modules)


def installMissingDependencies(argv=None) -> None:
    StartDependencyRegistry(argv).installMissingDependencies()


def verifyRequiredModulesOrDie(argv=None) -> None:
    missing_modules = missingRequiredModules(argv)
    if not missing_modules:
        return
    missing_names = ', '.join(module_name for module_name, _ in missing_modules)
    package_names = ', '.join(uniquePackagesForMissingModules(missing_modules))
    message = 'Missing Python modules: ' + missing_names
    if package_names:
        message += '\nExpected package(s): ' + package_names
    message += '\nstart.py attempted to install launcher-critical Python modules with the Linux system package manager when available. Install any remaining missing packages in the target runtime environment before launching again.'
    raise RuntimeError(message)


class PromptApplicationPackager:
    """Build Prompt into a distributable executable and exit."""

    EXE_METADATA = {
        'CompanyName': 'AcquisitionInvest LLC',
        'FileDescription': 'Prompt 1.0 - Desktop Prompt Workbench',
        'FileVersion': '1.0.0.0',
        'InternalName': 'Prompt',
        'LegalCopyright': 'AcquisitionInvest LLC © 2026',
        'LegalTrademarks': '',
        'OriginalFilename': 'Prompt.exe',
        'ProductName': 'Prompt',
        'ProductVersion': '1.0.0.0',
        'Comments': 'Prompt - LLM prompt, workflow, and doctype engine | http://www.trentontompkins.com | TrentTompkins@gmail.com | (724) 431-4207',
    }

    def __init__(self, root: Path, target_script: Path, argv=None):
        self.root = Path(root).resolve()  # noqa: nonconform
        self.targetScript = Path(target_script).resolve()  # noqa: nonconform
        self.argv = list(argv or [])
        # Final artifact output is the only thing allowed to live in dist/.
        # PyInstaller and Nuitka dump dependency trees while building, so their
        # scratch output must stay out of dist. Use %TEMP%/tmp for all builder
        # staging and copy only the finished EXE/ZIP/MSI/installer files back.
        self.distDir = self.root / 'dist'
        temp_base = Path(os.environ.get('PROMPT_BUILD_TEMP', '') or tempfile.gettempdir()).expanduser().resolve()
        root_hash = hashlib.sha1(str(self.root).encode('utf-8', errors='replace')).hexdigest()[:12]
        self.tempBuildRoot = temp_base / f'PromptBuild_{root_hash}'
        self.buildDir = self.tempBuildRoot / 'build'  # noqa: nonconform
        self.pyinstallerOutDir = self.tempBuildRoot / 'pyinstaller_dist'  # noqa: nonconform
        self.pyinstallerWorkDir = self.tempBuildRoot / 'pyinstaller_work'  # noqa: nonconform
        self.pyinstallerSpecDir = self.tempBuildRoot / 'pyinstaller_spec'  # noqa: nonconform
        self.nuitkaOutDir = self.tempBuildRoot / 'nuitka_dist'  # noqa: nonconform
        self.artifactDir = self.distDir  # noqa: nonconform
        self.backendStageDir = self.tempBuildRoot / 'backend_artifacts'  # noqa: nonconform
        self.selectedStageDir = self.tempBuildRoot / 'selected_backend'  # noqa: nonconform
        self.appName = 'Prompt'  # noqa: nonconform
        self.version = promptConfiguredVersion('1.0.0')
        self.dryRun = cliHasAnyFlag(self.argv, PACKAGE_DRY_RUN_FLAGS)  # noqa: nonconform
        self.log(f'Packager initialized root={self.root} final_dist={self.distDir} temp_build={self.tempBuildRoot} dry_run={self.dryRun}')

    def hasFlag(self, flags: set[str]) -> bool:
        return cliHasAnyFlag(self.argv, flags)

    def requestedBackend(self) -> str:
        if self.hasFlag(INSTALLER_FLAGS):
            return 'installer'
        if self.hasFlag(PYINSTALLER_FLAGS):
            return 'pyinstaller'
        if self.hasFlag(NIKITA_FLAGS):
            return 'nikita'
        return EMPTY_STRING

    def log(self, message: str) -> None:
        text = f'[PACKAGER] {message}'
        DebugLog.writeLine(text, level='PACKAGER', source='start.py', stream='stdout')
        print(text, flush=True)

    def warn(self, message: str) -> None:
        text = f'[WARN:packager] {message}'
        DebugLog.writeLine(text, level='WARN', source='start.py', stream='stderr')
        print(text, file=sys.stderr, flush=True)

    def die(self, message: str) -> None:
        print(f'[FATAL:packager] {message}', file=sys.stderr, flush=True)
        raise RuntimeError(message)

    def runCommand(self, cmd: list[str], *, label: str, timeout: int = 1800) -> int:
        self.log(f'{label}:BEGIN ' + ' '.join(shlex.quote(str(part)) for part in cmd))
        started = time.monotonic()
        proc = START_EXECUTION_LIFECYCLE.startProcess(
            f'Packager:{label}',
            [str(part) for part in cmd],
            cwd=str(self.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
        )
        last_output = time.monotonic()
        next_heartbeat = started + 10.0
        try:
            assert proc.stdout is not None
            while True:  # noqa: badcode reviewed detector-style finding
                line = proc.stdout.readline()
                now = time.monotonic()
                if line:
                    text = line.rstrip('\r\n')
                    if text.strip():
                        last_output = now
                        self.log(f'{label}:OUTPUT {text}')
                elif proc.poll() is not None:
                    break
                else:
                    time.sleep(0.25)  # noqa: badcode reviewed detector-style finding
                if now >= next_heartbeat:
                    self.log(f'{label}:HEARTBEAT pid={getattr(proc, "pid", 0)} elapsed={int(now-started)}s idle={int(now-last_output)}s')
                    next_heartbeat = now + 10.0
                if timeout and (now - started) > timeout:
                    raise subprocess.TimeoutExpired([str(part) for part in cmd], timeout)
            rc = int(proc.wait(timeout=5))
            self.log(f'{label}:EXIT code={rc} elapsed={int(time.monotonic()-started)}s')
            return rc
        except subprocess.TimeoutExpired:
            captureException(None, source='start.py', context=f'packager:{label}:timeout')
            proc.kill()
            self.die(f'{label} timed out after {timeout} seconds.')
        except KeyboardInterrupt:
            captureException(None, source='start.py', context=f'packager:{label}:keyboardinterrupt')
            proc.kill()
            raise
        return 1

    def ensureTargetExists(self) -> None:
        if not self.targetScript.exists():
            self.die(f'Missing target script: {self.targetScript}')

    def ensurePackageModule(self, module_name: str, package_name: str) -> None:
        python_executable = str(sys.executable or EMPTY_STRING)
        if canInterpreterImportModule(python_executable, module_name):
            return
        if self.dryRun:
            self.warn(f'Dry run: missing build module {module_name}; would install {package_name}')
            return
        self.warn(f'Missing build module {module_name}; attempting install: {package_name}')
        rc = 1
        retries_raw = str(os.environ.get('PROMPT_PIP_INSTALL_RETRIES', '2') or '2')
        try:
            retries = max(1, int(retries_raw))
        except ValueError:
            retries = 2
        timeout_raw = str(os.environ.get('PROMPT_PIP_INSTALL_TIMEOUT_SECONDS', '3600') or '3600')
        try:
            timeout_seconds = max(900, int(timeout_raw))
        except ValueError:
            timeout_seconds = 3600
        if package_name.lower() in {'pyside6', 'pyinstaller', 'nuitka'} and not os.environ.get('PROMPT_PIP_INSTALL_TIMEOUT_SECONDS'):
            timeout_seconds = 7200
        for attempt in range(1, retries + 1):
            rc = self.runCommand([python_executable, '-m', 'pip', 'install', package_name], label=f'install {package_name} attempt {attempt}/{retries}', timeout=timeout_seconds)
            if rc == 0:
                break
            if attempt < retries:
                self.warn(f'Install {package_name} failed/timed out; retrying so pip can reuse cached/resumed packages.')
        if rc != 0:
            self.die(f'Could not install {package_name}. Install it manually and rerun packaging.')
        if not canInterpreterImportModule(python_executable, module_name):
            self.die(f'{package_name} installed but {module_name} still cannot be imported.')

    def iconPath(self) -> Path | None:
        for name in ('icon.ico', 'favicon.ico'):
            candidate = self.root / name
            if candidate.exists() and candidate.is_file():
                return candidate.resolve()
        return None

    def metadata(self) -> dict[str, str]:
        """Build EXE + installer metadata from build_info.ini.

        Every value comes from build_info.ini[metadata]. The keys returned
        are the standard Win32 VERSIONINFO StringFileInfo names so they
        feed straight into writePyInstallerVersionFile() / the Nuitka,
        cx_Freeze, and PyApp version-info paths plus the NSIS/Inno/WiX
        installer scripts.

        The four `Custom*` keys carry the optional fields (author, license,
        website, phone) that the installer scripts and the custom-field
        section of the VERSIONINFO resource consume.
        """
        ensurePromptMetadataConfig()  # back-compat: still writes config.ini[metadata]
        version = promptBuildInfo('metadata', 'version', '') or promptConfiguredVersion('1.0.0')
        version4 = '.'.join(str(part) for part in promptVersionTuple4(version))
        app_name = promptBuildInfo('metadata', 'app_name', 'Prompt')
        company  = promptBuildInfo('metadata', 'company',  'Trenton Tompkins')
        copyright_s = promptBuildInfo('metadata', 'copyright', 'Trenton Tompkins')
        description = promptBuildInfo('metadata', 'description', f'{app_name} {version}')
        original_fn = promptBuildInfo('metadata', 'original_filename', f'{app_name}.exe')
        internal_nm = promptBuildInfo('metadata', 'internal_name', app_name)
        trademarks  = promptBuildInfo('metadata', 'trademarks', EMPTY_STRING)
        comments    = promptBuildInfo('metadata', 'comments',
                                      'https://trentontompkins.com  ·  (724) 431-5207')
        metadata = dict(self.EXE_METADATA)
        metadata.update({
            'CompanyName':      company,
            'FileDescription':  description,
            'FileVersion':      version4,
            'InternalName':     internal_nm,
            'LegalCopyright':   copyright_s,
            'LegalTrademarks':  trademarks,
            'OriginalFilename': original_fn,
            'ProductName':      app_name,
            'ProductVersion':   version4,
            'Comments':         comments,
            # Custom fields — emitted as additional StringStruct entries by
            # writePyInstallerVersionFile (and read by NSIS/Inno/WiX writers).
            'CustomAuthor':   promptBuildInfo('metadata_custom', 'author',   EMPTY_STRING),
            'CustomLicense':  promptBuildInfo('metadata_custom', 'license',  EMPTY_STRING),
            'CustomWebsite':  promptBuildInfo('metadata_custom', 'website',  EMPTY_STRING),
            'CustomGithub':   promptBuildInfo('metadata_custom', 'github',   EMPTY_STRING),
            'CustomPhone':    promptBuildInfo('metadata_custom', 'phone',    EMPTY_STRING),
            'CustomEmail':    promptBuildInfo('metadata_custom', 'email',    EMPTY_STRING),
            'CustomCodedBy':  promptBuildInfo('metadata_custom', 'coded_by', EMPTY_STRING),
            # Installer-only — consumed by NSIS/Inno/WiX writers, ignored
            # by the .exe VERSIONINFO emitter.
            'InstallerHelpUrl':    promptBuildInfo('installer', 'help_url',    EMPTY_STRING),
            'InstallerUpdateUrl':  promptBuildInfo('installer', 'update_url',  EMPTY_STRING),
            'InstallerPhone':      promptBuildInfo('installer', 'phone',       EMPTY_STRING),
            'InstallerPublisher':  promptBuildInfo('installer', 'publisher', company),
            'InstallerProductUuid':promptBuildInfo('installer', 'product_uuid', EMPTY_STRING),
            'InstallerUpgradeUuid':promptBuildInfo('installer', 'upgrade_uuid', EMPTY_STRING),
        })
        return metadata

    def windowsVersionInfoPath(self) -> Path:
        return self.buildDir / 'prompt_version_info.txt'

    def writePyInstallerVersionFile(self) -> Path:
        metadata = self.metadata()
        version_path = self.windowsVersionInfoPath()
        version_path.parent.mkdir(parents=True, exist_ok=True)

        def quoted(value: str) -> str:
            return repr(str(value or EMPTY_STRING) + '\0')

        file_tuple = promptVersionTuple4(str(metadata.get('FileVersion', '1.0.0.0')) )
        product_tuple = promptVersionTuple4(str(metadata.get('ProductVersion', '1.0.0.0')) )

        # Standard 10 VERSIONINFO fields (visible in Explorer > Properties).
        standard_keys = (
            'CompanyName', 'FileDescription', 'FileVersion', 'InternalName',
            'LegalCopyright', 'LegalTrademarks', 'OriginalFilename',
            'ProductName', 'ProductVersion', 'Comments',
        )
        # Custom string-struct entries — emitted alongside the standard set.
        # Microsoft's Properties dialog ignores them, but PowerShell
        # `(Get-Item …).VersionInfo`, Sigcheck, PEStudio, ResourceHacker, and
        # 7-Zip Properties all surface them. Blank values are skipped so we
        # don't pollute the resource with empties.
        custom_pairs = [
            ('Author',  metadata.get('CustomAuthor', EMPTY_STRING)),
            ('License', metadata.get('CustomLicense', EMPTY_STRING)),
            ('Website', metadata.get('CustomWebsite', EMPTY_STRING)),
            ('GitHub',  metadata.get('CustomGithub', EMPTY_STRING)),
            ('Phone',   metadata.get('CustomPhone', EMPTY_STRING)),
            ('Email',   metadata.get('CustomEmail', EMPTY_STRING)),
            ('CodedBy', metadata.get('CustomCodedBy', EMPTY_STRING)),
        ]
        custom_pairs = [(k, str(v or EMPTY_STRING)) for k, v in custom_pairs if v]

        struct_lines: list[str] = []
        for key in standard_keys:
            struct_lines.append(
                f"          StringStruct('{key}', {quoted(str(metadata.get(key, EMPTY_STRING) or EMPTY_STRING))}),"
            )
        for key, value in custom_pairs:
            struct_lines.append(f"          StringStruct('{key}', {quoted(value)}),")
        structs_block = '\n'.join(struct_lines)

        content = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={file_tuple},
    prodvers={product_tuple},
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
        [
{structs_block}
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
        File.writeText(version_path, content, encoding='utf-8')
        return version_path

    def dataDirectories(self) -> list[Path]:
        names = ('assets', 'fonts', 'generated', 'vendor', 'linux_runtime', 'debs')
        return [self.root / name for name in names if (self.root / name).exists()]

    def dataFiles(self) -> list[Path]:
        names: tuple[str, ...] = ()
        return [self.root / name for name in names if (self.root / name).exists()]

    def dataSeparator(self) -> str:
        return ';' if os.name == 'nt' else ':'

    def pyinstallerDataArg(self, directory: Path) -> str:
        return f'{directory}{self.dataSeparator()}{directory.name}'

    def pyinstallerDataFileArg(self, filePath: Path) -> str:
        return f'{filePath}{self.dataSeparator()}{filePath.name}'

    def builderOutputDir(self, backend: str) -> Path:
        key = str(backend or EMPTY_STRING).lower()
        if key in {'pyinstaller', 'py-installer'}:
            return self.pyinstallerOutDir
        if key in {'nikita', 'nuitka'}:
            return self.nuitkaOutDir
        return self.tempBuildRoot / f'{key or "unknown"}_dist'

    def cleanBackendScratch(self, backend: str) -> None:
        key = str(backend or EMPTY_STRING).lower()
        paths = [self.builderOutputDir(key)]
        if key in {'pyinstaller', 'py-installer'}:
            paths.extend([self.pyinstallerWorkDir, self.pyinstallerSpecDir])
        for path in paths:
            try:
                if path.exists():
                    shutil.rmtree(path, ignore_errors=True)
                    self.log(f'Removed temp builder scratch: {path}')
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                captureException(exc, source='start.py', context=f'packager.cleanBackendScratch:{key}', handled=True)
                self.warn(f'Could not reset temp builder scratch {path}: {type(exc).__name__}: {exc}')

    def finalOutputNames(self) -> set[str]:
        # The dist/ shelf is artifact-only. These are the only files allowed to
        # remain there after a successful build. Backend-specific zips/scripts
        # and selected_backend metadata belong in the temp build root.
        return {
            f'{self.appName}.exe',
            'PromptSetup.exe',
            'PromptSetup-Inno.exe',
            'PromptSetup-WiX.msi',
        }

    def legacyGeneratedOutputNames(self) -> set[str]:
        return {
            f'{self.appName}-pyinstaller.exe',
            f'{self.appName}-pyinstaller.zip',
            f'{self.appName}-nikita.exe',
            f'{self.appName}-nikita.zip',
            'PromptSetup.msi',
            'PromptInstaller.nsi',
            'PromptInstaller.iss',
            'PromptInstaller.wxs',
            'selected_backend.json',
            'OUTPUT_PATHS.txt',
            'latest.json',
            'md5s.txt',
            'debug.log',
            'run.log',
            'run_faults.log',
        }

    def cleanLegacyInTreeBuilderFolders(self) -> None:
        # v181: build/release staging belongs in temp, not beside app files and
        # not inside the final dist artifact folder. Remove old in-tree scratch
        # folders left by previous versions of the packager.
        for name in ('build', 'installer', 'release_upload'):
            candidate = self.root / name
            try:
                if candidate.exists() and candidate.is_dir() and not str(candidate.resolve()).startswith(str(self.tempBuildRoot.resolve())):
                    shutil.rmtree(candidate, ignore_errors=True)
                    self.log(f'Removed legacy in-tree builder folder: {candidate}')
            except OSError as exc:
                captureException(exc, source='start.py', context=f'packager.cleanLegacyInTreeBuilderFolders:{name}', handled=True)
                self.warn(f'Could not remove legacy builder folder {candidate}: {type(exc).__name__}: {exc}')

    def legacyBuilderJunkCandidates(self) -> list[Path]:
        # Obvious PyInstaller/Nuitka dependency output that must never remain in
        # the final artifact dist folder. This is deliberately conservative for
        # source-root=/dist mode: keep app source/data folders, remove generated
        # runtime packages and binary extension dumps.
        junk_names = {
            '_internal',
            'base_library.zip',
            'cryptography',
            'fontTools',
            'greenlet',
            'lxml',
            'pathops',
            'PySide6',
            'shiboken6',
            'sqlalchemy',
            'zopfli',
        }
        candidates = [self.distDir / name for name in sorted(junk_names)]
        for pattern in ('*.pyd', '*.dll', '*.so', '*.dylib', '*.manifest'):
            candidates.extend(sorted(self.distDir.glob(pattern)))
        return candidates

    def cleanBuildArtifacts(self) -> None:
        self.distDir.mkdir(parents=True, exist_ok=True)
        self.tempBuildRoot.mkdir(parents=True, exist_ok=True)
        self.buildDir.mkdir(parents=True, exist_ok=True)
        self.backendStageDir.mkdir(parents=True, exist_ok=True)
        self.log(f'Build scratch root is temp-only: {self.tempBuildRoot}')
        self.cleanLegacyInTreeBuilderFolders()
        self.cleanFlatOutputArtifacts()

    def cleanFlatOutputArtifacts(self) -> None:
        generated_names = set(self.finalOutputNames()) | set(self.legacyGeneratedOutputNames()) | {
            f'{self.appName}',
            f'{self.appName}.dist',
            f'{self.targetScript.stem}.exe',
            f'{self.targetScript.stem}',
            f'{self.targetScript.stem}.dist',
        }
        candidates = [self.distDir / name for name in sorted(generated_names)]
        candidates.extend(self.legacyBuilderJunkCandidates())
        seen_cleanup: set[Path] = set()
        for item in candidates:
            try:
                item = Path(item)  # noqa: badcode reviewed detector-style finding
                resolved = item.resolve() if item.exists() else item
            except OSError:
                resolved = item
            if resolved in seen_cleanup:
                continue
            seen_cleanup.add(resolved)
            if not item.exists():
                continue
            try:
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink()
                self.log(f'Removed generated flat output: {item}')
            except OSError as exc:
                captureException(None, source='start.py', context='packager.cleanFlatOutputArtifacts')
                self.warn(f'Could not remove generated flat output {item}: {type(exc).__name__}: {exc}')

    def copyBuiltArtifacts(self, backend: str) -> Path:
        self.distDir.mkdir(parents=True, exist_ok=True)
        self.backendStageDir.mkdir(parents=True, exist_ok=True)
        backend_dir = self.backendStageDir / backend
        if backend_dir.exists():
            shutil.rmtree(backend_dir, ignore_errors=True)
        backend_dir.mkdir(parents=True, exist_ok=True)

        candidates: list[Path] = []
        build_output = self.builderOutputDir(backend)
        direct_candidates = [
            build_output / self.appName,
            build_output / f'{self.appName}.exe',
            build_output / f'{self.appName}.dist',
            build_output / f'{self.targetScript.stem}.dist',
            build_output / self.targetScript.stem,
            build_output / f'{self.targetScript.stem}.exe',
        ]
        for candidate in direct_candidates:
            if candidate.exists() and candidate not in candidates:
                candidates.append(candidate)
        if backend.lower() in {'nikita', 'nuitka'} and build_output.exists():
            for pattern in (f'{self.appName}*.dist', f'{self.targetScript.stem}*.dist', '*.dist', f'{self.appName}*.exe', f'{self.targetScript.stem}*.exe'):
                for candidate in sorted(build_output.glob(pattern), key=lambda item: (0 if item.name.lower().startswith(self.appName.lower()) else 1, len(str(item)), str(item).lower())):
                    if candidate.exists() and candidate not in candidates:
                        candidates.append(candidate)
        if not candidates:
            seen = []
            if build_output.exists():
                seen = [str(item.relative_to(build_output)) for item in sorted(build_output.rglob('*'))[:80]]
            self.die(f'Build finished but no executable/folder was found under temp builder output {build_output}. Saw: {seen}')

        for candidate in candidates:
            target = backend_dir / candidate.name
            if candidate.is_dir():
                File.copytree(candidate, target, dirs_exist_ok=True)
            else:
                File.copy2(candidate, target)
            self.log(f'Copied artifact: {target}')

        return backend_dir

    def writeManifest(self, backend: str, artifact_path: Path, command: list[str]) -> None:
        manifest = {
            'app': self.appName,
            'version': self.version,
            'backend': backend,
            'target_script': str(self.targetScript),
            'artifact_path': str(artifact_path),
            'icon_path': str(self.iconPath() or EMPTY_STRING),
            'metadata': self.metadata(),
            'command': [str(item) for item in command],
            'created_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        File.writeText(artifact_path / 'build_manifest.json', json.dumps(manifest, indent=2), encoding='utf-8')

    def _bestExecutableInArtifact(self, artifact_path: Path) -> Path | None:
        path = Path(artifact_path)
        if path.is_file() and path.suffix.lower() == '.exe':
            return path
        if not path.exists():
            return None
        candidates = sorted(path.rglob('*.exe'), key=lambda item: (0 if item.name.lower() == f'{self.appName.lower()}.exe' else 1, len(str(item)), str(item).lower()))
        return candidates[0] if candidates else None

    def _zipArtifactFlat(self, artifact_path: Path, target: Path) -> Path | None:
        source = Path(artifact_path)
        if not source.exists():
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()
        if source.is_file():
            with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(source, source.name)
        else:
            shutil.make_archive(str(target.with_suffix('')), 'zip', root_dir=str(source))
        self.log(f'Flat zip output: {target}')
        return target if target.exists() else None

    def materializeBackendFlatOutfiles(self, backend: str, artifact_path: Path) -> list[Path]:
        # v184: /dist is no longer a backend comparison dump. Keep backend
        # outputs in the temp build root; only the selected final Prompt.exe is
        # allowed onto the artifact shelf.
        self.log(f'Backend {backend} artifact staged in temp only: {artifact_path}')
        return []

    def materializeSelectedExecutable(self, selected_source: Path | None) -> Path | None:
        if selected_source is None:
            return None
        exe = self._bestExecutableInArtifact(Path(selected_source))
        if exe is None or not exe.exists():
            self.warn(f'Selected backend has no executable to flatten: {selected_source}')
            return None
        target = self.distDir / f'{self.appName}.exe'
        self.logCreateBegin(target, source=str(exe))
        target.parent.mkdir(parents=True, exist_ok=True)
        File.copy2(exe, target)
        self.logCreateSuccess(target, label='selected executable')
        self.log(f'Flat selected executable output: {target}')
        return target

    def cleanupDistBuilderOutputs(self) -> None:
        keep_names = set(self.finalOutputNames())
        if not self.distDir.exists():
            return
        candidates = [item for item in list(self.distDir.iterdir()) if item.name not in keep_names]
        for item in candidates:
            if not item.exists() or item.name in keep_names:
                continue
            try:
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink()
                self.log(f'Removed non-flat builder output from dist: {item}')
            except OSError as exc:
                captureException(None, source='start.py', context='packager.cleanupDistBuilderOutputs')
                self.warn(f'Could not remove non-flat builder output {item}: {type(exc).__name__}: {exc}')


    def artifactSizeBytes(self, artifact_path: Path) -> int:
        path = Path(artifact_path)
        try:
            if path.is_file():
                return int(path.stat().st_size)
            total = 0
            for file_path in path.rglob('*'):
                if file_path.is_file():
                    try:
                        total += int(file_path.stat().st_size)
                    except OSError as exc:
                        captureException(None, source='start.py', context='except@4293')
                        self.warn(f'Could not size artifact file {file_path}: {type(exc).__name__}: {exc}')
            return int(total)
        except OSError as exc:
            captureException(None, source='start.py', context='except@4296')
            self.warn(f'Could not size artifact {artifact_path}: {type(exc).__name__}: {exc}')
        return 0

    def artifactMd5(self, artifact_path: Path) -> str:
        path = Path(artifact_path)
        if not path.exists() or not path.is_file():
            return EMPTY_STRING
        digest = hashlib.md5()
        with File.tracedOpen(path, 'rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def humanBytes(self, size: int) -> str:
        value = float(max(0, int(size or 0)))
        units = ('bytes', 'KB', 'MB', 'GB')
        index = 0
        while value >= 1024.0 and index < len(units) - 1:
            value /= 1024.0
            index += 1
        if index == 0:
            return f'{int(value)} bytes'
        return f'{value:.2f} {units[index]}'

    def artifactSummary(self, path: Path) -> str:
        target = Path(path)
        if not target.exists():
            return f'path={target} exists=0'
        size = target.stat().st_size if target.is_file() else self.artifactSizeBytes(target)
        md5 = self.artifactMd5(target) if target.is_file() else 'directory'
        return f'path={target} md5={md5} bytes={size} human={self.humanBytes(size)}'

    def logCreateBegin(self, artifact_path: Path, *, source: str = EMPTY_STRING) -> None:
        detail = f'Now creating {artifact_path}'
        if source:
            detail += f' from {source}'
        detail += '...'
        self.log(detail)

    def logCreateSuccess(self, artifact_path: Path, *, label: str = 'artifact') -> None:
        self.log(f'SUCCESS creating {label}: {self.artifactSummary(artifact_path)}')

    def logCreateFailure(self, artifact_path: Path, *, label: str = 'artifact', reason: str = EMPTY_STRING) -> None:
        suffix = f' reason={reason}' if reason else EMPTY_STRING
        self.warn(f'FAILED creating {label}: path={artifact_path}{suffix}')

    def verifyNsisUninstallerScript(self, script_path: Path) -> None:
        text = File.readText(script_path, encoding='utf-8', errors='replace') if Path(script_path).exists() else EMPTY_STRING
        required = ('WriteUninstaller', 'Section "Uninstall"', 'DeleteRegKey HKCU')
        missing = [token for token in required if token not in text]
        if missing:
            self.die(f'NSIS installer script is missing uninstaller support tokens: {missing} script={script_path}')
        self.log(f'UNINSTALLER VERIFIED: NSIS script writes $INSTDIR\\Uninstall.exe and Add/Remove Programs uninstall keys script={script_path}')

    def verifyOptionalInstallerUninstallers(self, inno_script: Path | None, wix_source: Path | None) -> None:
        if inno_script is not None and Path(inno_script).exists():
            text = File.readText(Path(inno_script), encoding='utf-8', errors='replace')
            if 'UninstallDisplayName=' not in text or '{uninstallexe}' not in text:
                self.die(f'Inno installer script is missing explicit uninstaller registration/shortcut support: {inno_script}')
            self.log(f'UNINSTALLER VERIFIED: Inno Setup will generate its standard uninstaller and Start Menu uninstall shortcut script={inno_script}')
        if wix_source is not None and Path(wix_source).exists():
            text = File.readText(Path(wix_source), encoding='utf-8', errors='replace')
            if '<Package ' not in text or 'UpgradeCode=' not in text:
                self.die(f'WiX MSI source is missing Package/UpgradeCode metadata needed for Windows uninstall/upgrade tracking: {wix_source}')
            self.log(f'UNINSTALLER VERIFIED: WiX/MSI package has Add/Remove Programs uninstall support source={wix_source}')

    def resetFullBuildScratch(self) -> None:
        if self.tempBuildRoot.exists():
            shutil.rmtree(self.tempBuildRoot, ignore_errors=True)
            self.log(f'Removed stale temp build root for full rebuild: {self.tempBuildRoot}')
        self.tempBuildRoot.mkdir(parents=True, exist_ok=True)

    def installerScriptDir(self) -> Path:
        return self.tempBuildRoot / 'installer_scripts'

    def backendArtifactRoot(self, backend: str) -> Path:
        return self.backendStageDir / str(backend or EMPTY_STRING)

    def backendArtifactExists(self, backend: str) -> bool:
        root = self.backendArtifactRoot(backend)
        if not root.exists():
            return False
        return any(item.is_file() or item.is_dir() for item in root.iterdir())

    def backendArtifactSize(self, backend: str) -> int:
        return self.artifactSizeBytes(self.backendArtifactRoot(backend))

    def ensureBackendArtifact(self, backend: str) -> Path:
        backend = str(backend or EMPTY_STRING).lower()
        if self.backendArtifactExists(backend):
            self.log(f'{backend} artifact already exists: {self.backendArtifactRoot(backend)}')
            return self.backendArtifactRoot(backend)
        if backend == 'pyinstaller':
            self.log('PyInstaller artifact missing; building it now.')
            rc = self.buildWithPyInstaller()
        elif backend in {'nikita', 'nuitka'}:
            self.log('Nikita/Nuitka artifact missing; building it now.')
            rc = self.buildWithNikita()
            backend = 'nikita'
        else:
            self.die(f'Unknown backend requested: {backend}')
            return self.backendArtifactRoot(backend)
        if int(rc) != 0:
            self.die(f'{backend} build failed with exit code {rc}')
        if not self.backendArtifactExists(backend):
            self.die(f'{backend} build completed but artifact folder is missing or empty: {self.backendArtifactRoot(backend)}')
        return self.backendArtifactRoot(backend)

    def buildBothBackendArtifacts(self) -> dict[str, Path]:
        self.log('Ensuring both executable backends exist before installer/push.')
        artifacts = {
            'pyinstaller': self.ensureBackendArtifact('pyinstaller'),
            'nikita': self.ensureBackendArtifact('nikita'),
        }
        for backend, artifact in artifacts.items():
            self.log(f'{backend} artifact size: {self.artifactSizeBytes(artifact)} bytes at {artifact}')
        return artifacts

    def selectSmallestBackendArtifact(self, artifacts: dict[str, Path] | None = None) -> Path:
        artifacts = dict(artifacts or {})
        if not artifacts:
            for backend in ('pyinstaller', 'nikita'):
                root = self.backendArtifactRoot(backend)
                if self.backendArtifactExists(backend):
                    artifacts[backend] = root
        if not artifacts:
            artifacts = self.buildBothBackendArtifacts()
        ranked = sorted(
            ((self.artifactSizeBytes(path), backend, Path(path)) for backend, path in artifacts.items()),
            key=lambda item: (item[0] <= 0, item[0], item[1]),
        )
        if not ranked or ranked[0][0] <= 0:
            self.die('Could not select a backend artifact because no backend has a measurable size.')
        size, backend, source = ranked[0]
        selected_dir = self.selectedStageDir
        if selected_dir.exists():
            shutil.rmtree(selected_dir, ignore_errors=True)
        selected_dir.mkdir(parents=True, exist_ok=True)
        copied_source = selected_dir / source.name
        if source.is_dir():
            File.copytree(source, copied_source, dirs_exist_ok=True)
        else:
            File.copy2(source, copied_source)
        manifest = {
            'selected_backend': backend,
            'selected_size_bytes': size,
            'source': str(source),
            'copied_source': str(copied_source),
            'all_backend_sizes': {name: self.artifactSizeBytes(path) for name, path in artifacts.items()},
            'icon_path': str(self.iconPath() or EMPTY_STRING),
            'metadata': self.metadata(),
            'created_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        selected_manifest = self.tempBuildRoot / 'selected_backend.json'
        File.writeText(selected_manifest, json.dumps(manifest, indent=2), encoding='utf-8')
        self.materializeSelectedExecutable(copied_source)
        self.log(f'Selected smaller backend for installer: {backend} ({size} bytes) -> {copied_source}')
        return copied_source

    def nsisScriptPath(self) -> Path:
        return self.installerScriptDir() / 'PromptInstaller.nsi'

    def installerOutputPath(self) -> Path:
        return self.distDir / 'PromptSetup.exe'

    def nsisSafe(self, value: str) -> str:
        return str(value or EMPTY_STRING).replace('$', '$$').replace('"', "$\\\"")

    def installerToolManager(self):
        from tools.installer_tools import InstallerToolManager
        return InstallerToolManager(self.root, logger=self.log, dry_run=self.dryRun)

    def ensureInstallerTools(self) -> dict[str, str]:
        self.log('Installer tool discovery/install begins. This only runs during --build.')
        manager = self.installerToolManager()
        return manager.ensure_all()

    def findMakeNsis(self) -> str:
        try:
            from tools.installer_tools import NsisInstallerTool
            found = NsisInstallerTool(self.root, logger=self.log, dry_run=True).discover()
            if found:
                return found
        except Exception as error:
            captureException(None, source='start.py', context='except@findMakeNsis')
            self.warn(f'NSIS discovery helper failed: {type(error).__name__}: {error}')
        candidates = [shutil.which('makensis'), shutil.which('makensis.exe')]
        if os.name == 'nt':
            candidates.extend([
                r'C:\Program Files (x86)\NSIS\makensis.exe',
                r'C:\Program Files\NSIS\makensis.exe',
            ])
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return str(Path(candidate).resolve())
        return EMPTY_STRING

    def findInnoIscc(self) -> str:
        try:
            from tools.installer_tools import InnoSetupInstallerTool
            found = InnoSetupInstallerTool(self.root, logger=self.log, dry_run=True).discover()
            if found:
                return found
        except Exception as error:
            captureException(None, source='start.py', context='except@findInnoIscc')
            self.warn(f'Inno Setup discovery helper failed: {type(error).__name__}: {error}')
        return EMPTY_STRING

    def findWix(self) -> str:
        try:
            from tools.installer_tools import WixInstallerTool
            found = WixInstallerTool(self.root, logger=self.log, dry_run=True).discover()
            if found:
                return found
        except Exception as error:
            captureException(None, source='start.py', context='except@findWix')
            self.warn(f'WiX discovery helper failed: {type(error).__name__}: {error}')
        return EMPTY_STRING

    def bundledApplicationSource(self) -> Path | None:
        candidates: list[Path] = []
        selected_dir = self.selectedStageDir
        if selected_dir.exists():
            candidates.extend([item for item in selected_dir.iterdir() if item.exists()])
        for backend in ('pyinstaller', 'nikita'):
            backend_root = self.backendArtifactRoot(backend)
            if backend_root.exists():
                candidates.append(backend_root)
                candidates.extend([item for item in backend_root.iterdir() if item.exists()])
        candidates.extend([
            self.distDir / self.appName,
            self.distDir / f'{self.appName}.exe',
            self.distDir / f'{self.appName}.dist',
            self.distDir / f'{self.targetScript.stem}.dist',
            self.distDir / self.targetScript.stem,
            self.distDir / f'{self.targetScript.stem}.exe',
        ])
        if self.distDir.exists():
            candidates.extend(sorted(self.distDir.glob('*.dist')))
            candidates.extend(sorted(self.distDir.glob('*.exe')))
        seen: set[Path] = set()
        for candidate in candidates:
            candidate = Path(candidate)  # noqa: badcode reviewed detector-style finding
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate.exists():
                return candidate.resolve()
        return None

    def installerDirectoryLooksLikeBundle(self, candidate: Path) -> bool:
        path = Path(candidate)
        if not path.exists() or not path.is_dir():
            return False
        if (path / '_internal').exists():
            return True
        if (path / f'{self.appName}.exe').exists() or (path / 'Prompt.exe').exists() or (path / 'prompt_app.exe').exists() or (path / 'start.exe').exists():
            return True
        if (path / 'assets' / 'assets.zip').exists() or (path / 'prompts').exists() or (path / 'workflows').exists():
            return True
        return False

    def installerContentRoot(self, source_path: Path) -> Path:
        source = Path(source_path)
        if source.is_file():
            return source
        preferred = source / self.appName
        if self.installerDirectoryLooksLikeBundle(preferred):
            return preferred
        prompt_named = source / 'Prompt'
        if self.installerDirectoryLooksLikeBundle(prompt_named):
            return prompt_named
        children = [child for child in source.iterdir()] if source.exists() and source.is_dir() else []
        bundle_children = [child for child in children if self.installerDirectoryLooksLikeBundle(child)]
        if len(bundle_children) == 1 and not self.installerDirectoryLooksLikeBundle(source):
            return bundle_children[0]
        if source.exists() and source.is_dir() and not (source / '_internal').exists():
            for name in (f'{self.appName}.exe', 'Prompt.exe', 'prompt_app.exe'):
                exe = source / name
                if exe.exists() and exe.is_file():
                    return exe
        return source

    def appExecutableRelativePath(self, source_path: Path) -> str:
        source = self.installerContentRoot(Path(source_path))
        if source.is_file():
            return source.name
        for name in (f'{self.appName}.exe', 'Prompt.exe', 'prompt_app.exe', 'start.exe'):
            candidate = source / name
            if candidate.exists():
                return str(candidate.relative_to(source)).replace('\\', '/')
        matches = sorted(source.rglob('*.exe'), key=lambda item: (0 if item.name.lower() == f'{self.appName.lower()}.exe' else 1, len(str(item)), str(item).lower()))
        if matches:
            return str(matches[0].relative_to(source)).replace('\\', '/')
        return f'{self.appName}.exe'

    def nsisFileInstallCommands(self, source_path: Path) -> str:
        source = self.installerContentRoot(Path(source_path))
        if source.is_file():
            return f'  File /oname=$INSTDIR\\{self.nsisSafe(source.name)} "{self.nsisSafe(str(source))}"'
        return f'  File /r "{self.nsisSafe(str(source / "*"))}"'

    def writeNsisInstallerScript(self, source_path: Path | None = None) -> Path:
        self.installerScriptDir().mkdir(parents=True, exist_ok=True)
        self.artifactDir.mkdir(parents=True, exist_ok=True)
        metadata = self.metadata()
        icon_path = self.iconPath()
        source = Path(source_path).resolve() if source_path is not None else None
        app_rel = self.appExecutableRelativePath(source) if source is not None else f'{self.appName}.exe'
        install_commands = self.nsisFileInstallCommands(source) if source is not None else '  ; Application files are added after building with --PyInstaller or --Nikita.'
        icon_lines = EMPTY_STRING
        if icon_path is not None:
            safe_icon = self.nsisSafe(str(icon_path))
            icon_lines = f'Icon "{safe_icon}"\nUninstallIcon "{safe_icon}"\n'
        else:
            self.warn('No icon.ico or favicon.ico found next to start.py; installer will use the NSIS default icon.')

        script_template = """Unicode true
!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"

Name "__PRODUCT_NAME__"
OutFile "__OUT_FILE__"
InstallDir "$LOCALAPPDATA\\Programs\\__APP_NAME__"
InstallDirRegKey HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\__APP_NAME__" "InstallLocation"
RequestExecutionLevel user

VIProductVersion "__PRODUCT_VERSION__"
VIAddVersionKey "ProductName" "__PRODUCT_NAME__"
VIAddVersionKey "CompanyName" "__COMPANY_NAME__"
VIAddVersionKey "LegalCopyright" "__COPYRIGHT__"
VIAddVersionKey "FileDescription" "__DESCRIPTION__"
VIAddVersionKey "FileVersion" "__FILE_VERSION__"
VIAddVersionKey "ProductVersion" "__PRODUCT_VERSION__"
VIAddVersionKey "Comments" "__COMMENTS__"
__ICON_LINES__
!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Section "Install"
  ; Prompt is installed per-user into LocalAppData so shipped MD/HTML/workflow files remain editable.
  ; Never install editable Prompt content under Program Files ACLs.
  SetOutPath "$INSTDIR"
__INSTALL_COMMANDS__
  ExecWait '"$SYSDIR\\attrib.exe" -R "$INSTDIR\\*.*" /S /D'
  ExecWait '"$SYSDIR\\attrib.exe" -R "$INSTDIR" /D'
  WriteUninstaller "$INSTDIR\\Uninstall.exe"
  SetShellVarContext current
  CreateDirectory "$SMPROGRAMS\\__APP_NAME__"
  CreateShortcut "$SMPROGRAMS\\__APP_NAME__\\__APP_NAME__.lnk" "$INSTDIR\\__APP_REL__"
  CreateShortcut "$SMPROGRAMS\\__APP_NAME__\\Uninstall.lnk" "$INSTDIR\\Uninstall.exe"
  CreateShortcut "$DESKTOP\\__APP_NAME__.lnk" "$INSTDIR\\__APP_REL__"
  WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\__APP_NAME__" "DisplayName" "__PRODUCT_NAME__"
  WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\__APP_NAME__" "DisplayVersion" "__PRODUCT_VERSION__"
  WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\__APP_NAME__" "Publisher" "__COMPANY_NAME__"
  WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\__APP_NAME__" "URLInfoAbout" "http://www.trentontompkins.com"
  WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\__APP_NAME__" "Contact" "TrentTompkins@gmail.com"
  WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\__APP_NAME__" "HelpTelephone" "(724) 431-4207"
  WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\__APP_NAME__" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\__APP_NAME__" "UninstallString" "$\\\"$INSTDIR\\Uninstall.exe$\\\""
  WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\__APP_NAME__" "QuietUninstallString" "$\\\"$INSTDIR\\Uninstall.exe$\\\" /S"
  WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\__APP_NAME__" "DisplayIcon" "$INSTDIR\\__APP_REL__"
  WriteRegDWORD HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\__APP_NAME__" "NoModify" 1
  WriteRegDWORD HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\__APP_NAME__" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\__APP_NAME__" "EstimatedSize" "$0"
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  Delete "$DESKTOP\\__APP_NAME__.lnk"
  RMDir /r "$SMPROGRAMS\\__APP_NAME__"
  ExecWait '"$SYSDIR\\attrib.exe" -R "$INSTDIR\\*.*" /S /D'
  ExecWait '"$SYSDIR\\attrib.exe" -R "$INSTDIR" /D'
  ${If} "$INSTDIR" != ""
    RMDir /r "$INSTDIR"
  ${EndIf}
  DeleteRegKey HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\__APP_NAME__"
SectionEnd
"""
        replacements = {
            '__PRODUCT_NAME__': self.nsisSafe(str(metadata.get('ProductName', self.appName) or self.appName)),
            '__APP_NAME__': self.nsisSafe(self.appName),
            '__OUT_FILE__': self.nsisSafe(str(self.installerOutputPath())),
            '__PRODUCT_VERSION__': self.nsisSafe(str(metadata.get('ProductVersion', self.version) or self.version)),
            '__FILE_VERSION__': self.nsisSafe(str(metadata.get('FileVersion', self.version) or self.version)),
            '__COMPANY_NAME__': self.nsisSafe(str(metadata.get('CompanyName', EMPTY_STRING) or EMPTY_STRING)),
            '__COPYRIGHT__': self.nsisSafe(str(metadata.get('LegalCopyright', EMPTY_STRING) or EMPTY_STRING)),
            '__DESCRIPTION__': self.nsisSafe(str(metadata.get('FileDescription', EMPTY_STRING) or EMPTY_STRING)),
            '__COMMENTS__': self.nsisSafe(str(metadata.get('Comments', EMPTY_STRING) or EMPTY_STRING)),
            '__ICON_LINES__': icon_lines.rstrip(),
            '__INSTALL_COMMANDS__': install_commands,
            '__APP_REL__': self.nsisSafe(app_rel.replace('/', '\\')),
        }
        script = script_template
        for key, value in replacements.items():
            script = script.replace(key, str(value))
        script_path = self.nsisScriptPath()
        File.writeText(script_path, script, encoding='utf-8')
        return script_path

    def buildInstaller(self) -> int:
        self.log('BUILD INSTALLERS BEGIN: rebuilding Prompt.exe plus NSIS, Inno, and WiX/MSI installers into ' + str(self.distDir))
        self.resetFullBuildScratch()
        self.cleanBuildArtifacts()
        artifacts: dict[str, Path] = {}
        tools = self.ensureInstallerTools()
        self.log('Installer tool status: ' + json.dumps({key: (value or 'MISSING') for key, value in tools.items()}, ensure_ascii=False))
        if self.dryRun:
            self.log('Installer dry-run: generating installer scripts without compiling executable backends.')
            source = self.bundledApplicationSource()
            if source is None:
                self.warn('Dry run: no real backend artifact exists; installer scripts will be generated with placeholder file sections.')
        else:
            artifacts = self.buildBothBackendArtifacts()
            source = self.selectSmallestBackendArtifact(artifacts)
        selected_source = source if 'source' in locals() else None
        script_path = self.writeNsisInstallerScript(selected_source)
        self.log(f'NSIS script prepared: {script_path}')
        self.verifyNsisUninstallerScript(script_path)
        try:
            self.log('INNO/WIX SCRIPT GENERATION BEGIN')
            from tools.installer_builders import build_optional_installers
            optional = build_optional_installers(
                root=self.root,
                artifact_dir=self.installerScriptDir(),
                output_dir=self.distDir,
                source_path=self.installerContentRoot(selected_source) if selected_source is not None else None,
                metadata=self.metadata(),
                app_name=self.appName,
                app_rel=self.appExecutableRelativePath(selected_source) if selected_source is not None else f'{self.appName}.exe',
                icon_path=self.iconPath(),
                dry_run=self.dryRun,
                logger=self.log,
            )
            self.log('INNO/WIX SCRIPT GENERATION RESULT: ' + json.dumps(optional, ensure_ascii=False, default=str))
            self.verifyOptionalInstallerUninstallers(Path(str(optional.get('inno_script') or EMPTY_STRING)) if optional.get('inno_script') else None, Path(str(optional.get('wix_source') or EMPTY_STRING)) if optional.get('wix_source') else None)
        except Exception as error:
            captureException(error, source='start.py', context='except@optional-installers', handled=self.dryRun)
            self.warn(f'Inno/WiX installer generation failed: {type(error).__name__}: {error}')
            if not self.dryRun:
                self.die(f'Full --build requires Inno and WiX installers to be created; stopping after failure: {error}')
        make_nsis = self.findMakeNsis()
        if self.dryRun:
            self.cleanupDistBuilderOutputs()
            self.log(f'Dry run selected; NSIS/Inno/WiX scripts generated but installers not executed: {script_path}')
            return 0
        if not make_nsis:
            self.warn('NSIS makensis.exe not found. NSIS script generated but NSIS installer was not built.')
            return 0
        self.logCreateBegin(self.installerOutputPath(), source=str(script_path))
        self.log('NSIS INSTALLER BUILD BEGIN output=' + str(self.installerOutputPath()))
        rc = self.runCommand([make_nsis, str(script_path)], label='NSIS installer build', timeout=1200)
        if rc != 0:
            self.die(f'NSIS installer build failed with exit code {rc}')
        if not self.installerOutputPath().exists():
            self.die(f'NSIS completed but installer was not found: {self.installerOutputPath()}')
        if not ReleaseArtifactValidator.is_valid(self.installerOutputPath()):
            self.die(f'NSIS output is not a valid Windows installer: {self.installerOutputPath()} validator={ReleaseArtifactValidator.describe(self.installerOutputPath())}')
        self.logCreateSuccess(self.installerOutputPath(), label='NSIS installer')
        for expected_name, expected_label in (('Prompt.exe', 'selected executable'), ('PromptSetup-Inno.exe', 'Inno installer'), ('PromptSetup-WiX.msi', 'WiX MSI installer')):
            expected_path = self.distDir / expected_name
            if not expected_path.exists():
                self.logCreateFailure(expected_path, label=expected_label, reason='expected full --build output missing')
                self.die(f'Full --build did not create required output: {expected_path}')
            self.logCreateSuccess(expected_path, label=expected_label)
        self.cleanupDistBuilderOutputs()
        self.log(f'Installer build complete: {self.installerOutputPath()} validator={ReleaseArtifactValidator.describe(self.installerOutputPath())}')
        return 0

    def buildWithPyInstaller(self) -> int:
        self.log('PYINSTALLER EXE BUILD BEGIN target=' + str(self.targetScript) + ' temp_output=' + str(self.pyinstallerOutDir) + ' final_dist=' + str(self.distDir))
        self.ensureTargetExists()
        self.ensurePackageModule('PyInstaller', 'pyinstaller')
        self.cleanBuildArtifacts()
        self.cleanBackendScratch('pyinstaller')
        self.log('PYINSTALLER:MODE onefile selected because /dist is artifact-only; do not copy a onedir EXE away from its _internal runtime folder')
        version_path = self.writePyInstallerVersionFile()
        cmd = [
            str(sys.executable),
            '-m',
            'PyInstaller',
            '--noconfirm',
            '--clean',
            '--onefile',
            '--distpath',
            str(self.pyinstallerOutDir),
            '--workpath',
            str(self.pyinstallerWorkDir),
            '--specpath',
            str(self.pyinstallerSpecDir),
            '--windowed',
            '--name',
            self.appName,
            '--version-file',
            str(version_path),
            str(self.targetScript),
        ]
        icon_path = self.iconPath()
        if icon_path is not None:
            cmd.extend(['--icon', str(icon_path)])
        else:
            self.warn('No icon.ico or favicon.ico found next to start.py; EXE will use the packager default icon.')
        for directory in self.dataDirectories():
            cmd.extend(['--add-data', self.pyinstallerDataArg(directory)])
        for filePath in self.dataFiles():
            cmd.extend(['--add-data', self.pyinstallerDataFileArg(filePath)])
        # v159: Qt WebEngine blank panes in installed EXEs are commonly caused by
        # PyInstaller missing Chromium helper/resources even though the Python
        # source runs. Collect the PySide6 runtime explicitly instead of relying
        # on implicit hooks.
        cmd.extend(['--collect-all', 'PySide6'])
        for module in (
            'PySide6.QtCore',
            'PySide6.QtGui',
            'PySide6.QtWidgets',
            'PySide6.QtPrintSupport',
            'PySide6.QtWebChannel',
            'PySide6.QtWebEngineCore',
            'PySide6.QtWebEngineWidgets',
        ):
            cmd.extend(['--hidden-import', module])
        if self.dryRun:
            self.logCreateBegin(self.distDir / f'{self.appName}-pyinstaller.exe', source='PyInstaller command')
            self.log('Dry run selected; PyInstaller command prepared but not executed. Final selected executable would be /dist/Prompt.exe only.')
            self.log(' '.join(shlex.quote(str(part)) for part in cmd))
            return 0
        self.logCreateBegin(self.distDir / f'{self.appName}-pyinstaller.exe', source='PyInstaller')
        rc = self.runCommand(cmd, label='PyInstaller build')
        if rc != 0:
            self.die(f'PyInstaller failed with exit code {rc}')
        self.log('PYINSTALLER EXE BUILD FINISHED; copying artifacts flat')
        artifact_path = self.copyBuiltArtifacts('pyinstaller')
        self.writeManifest('pyinstaller', artifact_path, cmd)
        for output in self.materializeBackendFlatOutfiles('pyinstaller', artifact_path):
            self.logCreateSuccess(output, label='PyInstaller output')
        self.cleanupDistBuilderOutputs()
        self.log(f'PyInstaller build complete: {artifact_path}')
        return 0

    def buildWithNikita(self) -> int:
        self.log('NIKITA/NUITKA EXE BUILD BEGIN target=' + str(self.targetScript) + ' temp_output=' + str(self.nuitkaOutDir) + ' final_dist=' + str(self.distDir))
        self.ensureTargetExists()
        self.ensurePackageModule('nuitka', 'nuitka')
        self.cleanBuildArtifacts()
        self.cleanBackendScratch('nikita')
        metadata = self.metadata()
        cmd = [
            str(sys.executable),
            '-m',
            'nuitka',
            '--standalone',
            '--onefile',
            '--assume-yes-for-downloads',
            '--enable-plugin=pyside6',
            '--include-package=sqlalchemy',
            '--include-module=sqlalchemy.orm.strategies',
            '--include-module=sqlalchemy.orm.strategy_options',
            '--include-module=sqlalchemy.orm.loading',
            '--include-module=sqlalchemy.orm.context',
            '--include-module=sqlalchemy.orm.properties',
            '--include-module=sqlalchemy.orm.dependency',
            '--include-module=sqlalchemy.sql.default_comparator',
            f'--output-dir={self.nuitkaOutDir}',
            f'--output-filename={self.appName}',
            f'--company-name={metadata.get("CompanyName", EMPTY_STRING)}',
            f'--product-name={metadata.get("ProductName", EMPTY_STRING)}',
            f'--file-version={metadata.get("FileVersion", EMPTY_STRING)}',
            f'--product-version={metadata.get("ProductVersion", EMPTY_STRING)}',
            f'--file-description={metadata.get("FileDescription", EMPTY_STRING)}',
            f'--copyright={metadata.get("LegalCopyright", EMPTY_STRING)}',
            f'--trademarks={metadata.get("LegalTrademarks", EMPTY_STRING)}',
            str(self.targetScript),
        ]
        icon_path = self.iconPath()
        if icon_path is not None:
            cmd.insert(-1, f'--windows-icon-from-ico={icon_path}')
        else:
            self.warn('No icon.ico or favicon.ico found next to start.py; EXE will use the packager default icon.')
        for directory in self.dataDirectories():
            cmd.append(f'--include-data-dir={directory}={directory.name}')
        for filePath in self.dataFiles():
            cmd.append(f'--include-data-files={filePath}={filePath.name}')
        if self.dryRun:
            self.logCreateBegin(self.distDir / f'{self.appName}-nikita.exe', source='Nuitka/Nikita command')
            self.log('Dry run selected; Nikita/Nuitka command prepared but not executed. Backend output stays temp-only; final selected executable is /dist/Prompt.exe.')
            self.log(' '.join(shlex.quote(str(part)) for part in cmd))
            return 0
        self.logCreateBegin(self.distDir / f'{self.appName}-nikita.exe', source='Nuitka/Nikita')
        rc = self.runCommand(cmd, label='Nikita/Nuitka build')
        if rc != 0:
            self.die(f'Nikita/Nuitka failed with exit code {rc}')
        self.log('NIKITA/NUITKA EXE BUILD FINISHED; copying artifacts flat')
        artifact_path = self.copyBuiltArtifacts('nikita')
        self.writeManifest('nikita', artifact_path, cmd)
        for output in self.materializeBackendFlatOutfiles('nikita', artifact_path):
            self.logCreateSuccess(output, label='Nuitka/Nikita output')
        self.cleanupDistBuilderOutputs()
        self.log(f'Nikita/Nuitka build complete: {artifact_path}')
        return 0

    def run(self) -> int:
        ensurePromptMetadataConfig()
        backend = self.requestedBackend()
        self.log(f'PACKAGER RUN requested_backend={backend or "installer"} argv={self.argv}')
        if backend == 'pyinstaller':
            return self.buildWithPyInstaller()
        if backend == 'nikita':
            return self.buildWithNikita()
        # Plain --build or --build --installer means full installer pipeline.
        return self.buildInstaller()



def vultureRequested(argv=None) -> bool:
    return bool(readCliOption(argv, VULTURE_FLAGS, takesValue=False, knownAliases=START_VALID_CLI_ALIASES).get('present'))


def vultureVendorRoot() -> Path:
    return BASE_DIR / 'vendor' / 'vulture'


def vultureReportOutputPath(argv=None) -> Path:
    option = readCliOption(argv, VULTURE_OUTPUT_FLAGS, takesValue=True, knownAliases=START_VALID_CLI_ALIASES)
    value = str(option.get('value') or EMPTY_STRING).strip()
    if value:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = BASE_DIR / candidate
        return candidate
    return BASE_DIR / 'Vulture.txt'


def vultureMinConfidence(argv=None) -> int:
    option = readCliOption(argv, VULTURE_MIN_CONFIDENCE_FLAGS, takesValue=True, knownAliases=START_VALID_CLI_ALIASES)
    value = str(option.get('value') or EMPTY_STRING).strip()
    try:
        return max(0, min(100, int(float(value)))) if value else 60
    except Exception as error:
        captureException(None, source='start.py', context='except@4710')
        print(f"[WARN:swallowed-exception] start.py:4083 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        return 60


def _appendVultureTarget(targets: list[Path], candidate: Path | str) -> None:
    try:
        path = candidate if isinstance(candidate, Path) else Path(str(candidate or EMPTY_STRING).strip())
        if not path.is_absolute():
            path = BASE_DIR / path
        if not path.exists():
            return
        resolved = path.resolve()
        if resolved not in targets:
            targets.append(resolved)
    except Exception as error:
        captureException(None, source='start.py', context='except@4725')
        print(f"[WARN:swallowed-exception] start.py:4097 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        return


def vultureTargetTokens(argv=None) -> list[str]:
    tokens = [str(token or EMPTY_STRING).strip() for token in list(argv or []) if str(token or EMPTY_STRING).strip()]
    outputAliases = {str(alias or EMPTY_STRING).strip().lower() for alias in VULTURE_OUTPUT_FLAGS}
    confidenceAliases = {str(alias or EMPTY_STRING).strip().lower() for alias in VULTURE_MIN_CONFIDENCE_FLAGS}
    valueAliases = outputAliases | confidenceAliases
    targets: list[str] = []
    skipNext = False
    for token in tokens:
        if skipNext:
            skipNext = False
            continue
        raw = stripCliValueQuotes(token)
        lowered = raw.lower()
        matchedValueAlias = False
        for alias in valueAliases:
            if lowered == alias:
                matchedValueAlias = True
                skipNext = True
                break
            if lowered.startswith(alias + '=') or lowered.startswith(alias + ':'):
                matchedValueAlias = True
                break
        if matchedValueAlias:
            continue
        if lowered in VULTURE_FLAGS:
            continue
        # Normal CLI switches are not Vulture targets.  Absolute Unix paths are
        # allowed only when they already exist.
        candidatePath = Path(raw) if raw else Path()
        absoluteExisting = raw.startswith('/') and candidatePath.exists()
        if cliTokenLooksLikeOption(raw, START_VALID_CLI_ALIASES) and not absoluteExisting:
            continue
        candidate = candidatePath if candidatePath.is_absolute() else BASE_DIR / candidatePath
        if candidate.exists() and (candidate.is_dir() or candidate.suffix.lower() == '.py'):
            targets.append(raw)
        elif raw.lower().endswith('.py'):
            targets.append(raw)
    return targets


def vultureScanTargets(argv=None) -> list[Path]:
    targets: list[Path] = []
    # Always scan the launcher first, then the exact Python file it bootstraps.
    _appendVultureTarget(targets, BASE_DIR / 'start.py')
    _appendVultureTarget(targets, GTP_PATH)
    for raw in vultureTargetTokens(argv):
        _appendVultureTarget(targets, raw)
    return targets


def runVultureIfRequested(argv=None) -> int | None:
    tokens = list(argv or [])
    if not vultureRequested(tokens):
        return None
    report_path = vultureReportOutputPath(tokens)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    vendor_root = vultureVendorRoot()
    targets = vultureScanTargets(tokens)
    header = [
        'Prompt vendored Vulture report',
        f'Generated: {datetime.datetime.now(datetime.timezone.utc).isoformat()}',
        f'Vendored Vulture root: {vendor_root}',
        f'Min confidence: {vultureMinConfidence(tokens)}',
        'Targets: ' + (', '.join(str(path.relative_to(BASE_DIR)) for path in targets) if targets else '(none)'),
        '',
    ]
    if not vendor_root.exists() or not (vendor_root / 'vulture' / '__main__.py').exists():
        File.writeText(report_path, '\n'.join(header + ['ERROR: vendor/vulture is missing or incomplete.', '']), encoding='utf-8')
        print(f'[VULTURE] Missing vendored Vulture. Report written: {report_path}', file=sys.stderr, flush=True)
        return 2
    if not targets:
        File.writeText(report_path, '\n'.join(header + ['ERROR: no Python targets found.', '']), encoding='utf-8')
        print(f'[VULTURE] No Python targets found. Report written: {report_path}', file=sys.stderr, flush=True)
        return 2
    try:
        runner_path = BASE_DIR / 'tools' / 'run_vulture.py'
        if not runner_path.exists():
            File.writeText(report_path, '\n'.join(header + ['ERROR: tools/run_vulture.py is missing.', '']), encoding='utf-8')
            print(f'[VULTURE] Missing runner. Report written: {report_path}', file=sys.stderr, flush=True)
            return 2
        runner_args = ['--root', str(BASE_DIR), '--output', str(report_path), '--min-confidence', str(vultureMinConfidence(tokens)), *[str(path) for path in targets]]
        existingPath = list(sys.path)
        try:
            if str(vendor_root) not in sys.path:
                sys.path.insert(0, str(vendor_root))
            if str(BASE_DIR) not in sys.path:
                sys.path.insert(0, str(BASE_DIR))
            spec = importlib.util.spec_from_file_location('prompt_vendored_vulture_runner', str(runner_path))
            if spec is None or spec.loader is None:
                raise RuntimeError('could not load tools/run_vulture.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            exit_code = int(module.main(runner_args) or 0)
        finally:
            sys.path[:] = existingPath  # monkeypatch-ok: restore temporary Vulture import path mutation.
        return 0 if exit_code in (0, 3) else exit_code
    except Exception as error:
        captureException(None, source='start.py', context='except@4826')
        File.writeText(report_path, '\n'.join(header + [f'ERROR: {type(error).__name__}: {error}', traceback.format_exc(), '']), encoding='utf-8')
        print(f'[VULTURE] Failed. Report written: {report_path}', file=sys.stderr, flush=True)
        return 1


def buildRequested(argv=None) -> bool:
    return bool(readCliOption(argv, BUILD_FLAGS, takesValue=False, knownAliases=START_VALID_CLI_ALIASES).get('present'))


def packagingFlagRequested(argv=None) -> bool:
    tokens = normalizedCliTokens(argv)
    all_flags = PYINSTALLER_FLAGS.union(NIKITA_FLAGS).union(INSTALLER_FLAGS)
    return any(str(token or EMPTY_STRING).lower().split('=', 1)[0].split(':', 1)[0] in all_flags for token in tokens)


def packagingRequested(argv=None) -> bool:
    return buildRequested(argv)


class PromptBuildBackgroundProcess:
    """Starts the build pipeline as its own process so the GUI can keep running."""

    def __init__(self, root: Path, argv=None):
        self.root = Path(root).resolve()  # noqa: nonconform
        self.argv = list(argv or [])
        self.logPath = self.root / 'logs' / 'build_pipeline.log'

    def log(self, message: str) -> None:
        text = f'[BUILD-BACKGROUND] {message}'
        print(text, flush=True)
        try:
            self.logPath.parent.mkdir(parents=True, exist_ok=True)
            with File.tracedOpen(self.logPath, 'a', encoding='utf-8', errors='replace') as handle:
                handle.write(f'{datetime.datetime.now().isoformat(timespec="seconds")} {text}\n')
        except Exception as error:  # swallow-ok: launch logging must not block app startup
            captureException(error, source='start.py', context='build-background-log')

    def start(self) -> int:
        runner = self.root / 'tools' / 'run_prompt_release.py'
        if not runner.exists():
            print(f'[BUILD:FAILED] Missing release runner: {runner}', file=sys.stderr, flush=True)
            return 2
        cmd = [str(sys.executable or 'python'), '-m', 'tools.run_prompt_release', '--root', str(self.root), *[str(item) for item in self.argv]]
        env = dict(os.environ)
        env['PROMPT_RELEASE_ROOT'] = str(self.root)
        env['PROMPT_RELEASE_BACKGROUND'] = '1'
        env['PROMPT_RUN_LOG_ROOT'] = str(self.root)
        env['PROMPT_RUN_LOG'] = str(promptRunLogPath())
        self.log('START command=' + ' '.join(shlex.quote(part) for part in cmd))
        appendRunLog('[BUILD-BACKGROUND] pipe child stdout/stderr to ' + str(promptRunLogPath()))
        run_log_handle = openRunLogStream()
        proc = START_EXECUTION_LIFECYCLE.startProcess(
            'Prompt build pipeline',
            cmd,
            cwd=str(self.root),
            stdout=run_log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
        )
        setattr(proc, '_prompt_run_log_handle', run_log_handle)
        self.log(f'STARTED pid={getattr(proc, "pid", 0)} log={self.logPath} run_log={promptRunLogPath()}')
        self.log('The main application will continue launching while the build pipeline writes progress to run.log and logs/build_pipeline.log.')
        return 0


def runPackagingIfRequested(argv=None) -> int | None:
    tokens = list(argv or [])
    if packagingFlagRequested(tokens) and not buildRequested(tokens):
        print('[BUILD:BLOCKED] Packaging flags were ignored because --build was not supplied. Use --build --installer, --build --pyinstaller, or --build --nikita.', file=sys.stderr, flush=True)
        return 2
    if not packagingRequested(tokens):
        return None
    DebugLog.stage('BUILD REQUEST DETECTED', 'running foreground packager; GUI child launch is blocked for this command', source='start.py')
    try:
        return int(PromptApplicationPackager(BASE_DIR, GTP_PATH, tokens).run() or 0)
    except BaseException as error:
        captureException(error, source='start.py', context='runPackagingIfRequested', handled=False, extra='foreground packager failed')
        print(f'[BUILD:FAILED] {type(error).__name__}: {error}', file=sys.stderr, flush=True)
        return 1


class ReleaseArtifactValidator:
    """Rejects dry-run placeholder files before installer/push can ship them."""

    @staticmethod
    def describe(path: Path) -> str:
        try:
            head = path.read_bytes()[:16]
        except Exception as exc:
            captureException(None, source='start.py', context='except@4852')
            return f'unreadable:{type(exc).__name__}:{exc}'
        suffix = path.suffix.lower()
        if suffix == '.exe':
            return 'pe-mz' if head.startswith(b'MZ') else 'invalid-exe-missing-MZ'
        if suffix == '.msi':
            return 'msi-ole' if head.startswith(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1') else 'invalid-msi-missing-OLE-header'
        if suffix == '.zip':
            try:
                return 'zip-ok' if zipfile.is_zipfile(path) else 'invalid-zip-central-directory'
            except Exception as exc:
                captureException(None, source='start.py', context='except@4862')
                return f'invalid-zip:{type(exc).__name__}:{exc}'
        return 'metadata'

    @staticmethod
    def is_valid(path: Path) -> bool:
        if not path.exists() or not path.is_file() or path.stat().st_size <= 0:
            return False
        suffix = path.suffix.lower()
        if suffix == '.exe':
            return path.read_bytes()[:2] == b'MZ'
        if suffix == '.msi':
            return path.read_bytes()[:8] == b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
        if suffix == '.zip':
            return zipfile.is_zipfile(path)
        if suffix in {'.json', '.nsi'}:
            return True
        return False


class WinSCPClient:
    """Traceable WinSCP scripting wrapper for Prompt release upload.

    This intentionally uses WinSCP.com with /script, /log, /loglevel=1,
    and /nointeractiveinput so failures are visible in console and log files.
    """

    def __init__(self, *, root: Path, session: str, remote_path: str, winscp_ini: str = EMPTY_STRING, dry_run: bool = False, logger=None, warner=None, die=None, run_command=None, file_md5=None):
        self.root = Path(root).resolve()  # noqa: nonconform
        self.session = (str(session or 'vps').strip() or 'vps')  # noqa: nonconform
        self.remote_path = str(remote_path or '/home/trentontompkins.com/prompt').strip() or '/home/trentontompkins.com/prompt'  # noqa: nonconform
        self.winscp_ini = str(winscp_ini or EMPTY_STRING).strip()  # noqa: nonconform
        self.dry_run = bool(dry_run)  # noqa: nonconform
        self._log = logger or (lambda message: print(f'[PROMPT-RELEASE] {message}', flush=True))
        self._warn = warner or (lambda message: print(f'[WARN:PROMPT-RELEASE] {message}', file=sys.stderr, flush=True))
        self._die = die or (lambda message: (_ for _ in ()).throw(RuntimeError(message)))
        self._run_command = run_command
        self._file_md5 = file_md5 or safeFileMd5Hex
        self.resolved_path = EMPTY_STRING  # noqa: nonconform

    def log(self, message: str) -> None:
        self._log(f'WINSCP:{message}')

    def warn(self, message: str) -> None:
        self._warn(f'WINSCP:{message}')

    def die(self, message: str) -> None:
        self._die(f'WINSCP:{message}')

    @staticmethod
    def _decode_session_name(raw: str) -> str:
        return urllib.parse.unquote(str(raw or EMPTY_STRING).replace('\\', '/'))

    def candidate_paths(self) -> list[str]:
        candidates: list[str] = []
        for name in ('winscp.com', 'WinSCP.com', 'winscp.exe', 'WinSCP.exe'):
            result = shutil.which(name)
            self.log(f'FIND:PATH name={name} result={result or EMPTY_STRING}')
            if result:
                candidates.append(result)
        for env_name in ('WINSCP_COM', 'WINSCP', 'WINSCP_EXE', 'WINSCP_PATH', 'WINSCP_HOME', 'WINSCP_ROOT'):
            raw = os.environ.get(env_name, EMPTY_STRING)
            self.log(f'FIND:ENV name={env_name} value={raw}')
            if not raw:
                continue
            value = Path(raw)
            candidates.extend([str(value), str(value / 'WinSCP.com'), str(value / 'winscp.com'), str(value / 'WinSCP.exe'), str(value / 'winscp.exe')])
        if os.name == 'nt':
            for root in (os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'), os.environ.get('ProgramFiles', r'C:\Program Files'), os.environ.get('LOCALAPPDATA', EMPTY_STRING)):
                if root:
                    candidates.extend([
                        str(Path(root) / 'WinSCP' / 'WinSCP.com'),
                        str(Path(root) / 'WinSCP' / 'winscp.com'),
                        str(Path(root) / 'WinSCP' / 'WinSCP.exe'),
                        str(Path(root) / 'Programs' / 'WinSCP' / 'WinSCP.com'),
                        str(Path(root) / 'Programs' / 'WinSCP' / 'winscp.com'),
                    ])
        unique: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate or EMPTY_STRING).lower()
            if key and key not in seen:
                seen.add(key)
                unique.append(candidate)
        self.log(f'FIND:CANDIDATES count={len(unique)}')
        return unique

    def discover(self) -> str:
        self.log('FIND:BEGIN preferred=winscp.com')
        for candidate in self.candidate_paths():
            path = Path(candidate)
            exists = path.exists()
            kind = 'file' if exists and path.is_file() else ('dir' if exists and path.is_dir() else 'missing')
            self.log(f'FIND:CANDIDATE exists={exists} kind={kind} path={path}')
            if exists and path.is_file():
                resolved = str(path.resolve())
                if path.name.lower() == 'winscp.exe':
                    com = path.with_name('WinSCP.com')
                    self.log(f'FIND:EXE-CHECK companion_com={com} exists={com.exists()}')
                    if com.exists():
                        resolved = str(com.resolve())
                self.resolved_path = resolved
                self.log(f'FIND:SELECTED path={resolved}')
                self.probe_version(resolved)
                return resolved
        self.warn('FIND:FAILED WinSCP.com was not found in PATH/env/Program Files/LocalAppData')
        return EMPTY_STRING

    def probe_version(self, winscp_path: str) -> None:
        if self.dry_run:
            self.log('VERSION:SKIP reason=dry-run')
            return
        for args in ([winscp_path, '/info'], [winscp_path, '/help']):
            try:
                self.log('VERSION:COMMAND ' + json.dumps([str(x) for x in args]))
                proc = START_EXECUTION_LIFECYCLE.startProcess(
                    'WinSCP version probe',
                    [str(x) for x in args],
                    cwd=str(self.root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                )
                output, _stderr = proc.communicate(timeout=20)  # lifecycle-bypass-ok main-thread-ok winscp-cli-probe-with-timeout
                output = str(output or EMPTY_STRING).strip()
                first_lines = ' | '.join(output.splitlines()[:8])
                self.log(f'VERSION:RESULT exit_code={getattr(proc, "returncode", EMPTY_STRING)} preview={first_lines}')
                if output:
                    break
            except Exception as exc:
                captureException(None, source='start.py', context='except@4993')
                self.warn(f'VERSION:FAILED command={args[1]} {type(exc).__name__}: {exc}')

    def _session_registry_roots(self):
        try:
            import winreg  # type: ignore[import-not-found]
        except Exception as exc:
            captureException(None, source='start.py', context='except@4999')
            self.warn(f'SESSION:REGISTRY:IMPORT-FAILED {type(exc).__name__}: {exc}')
            return []
        return [
            ('HKCU', getattr(winreg, 'HKEY_CURRENT_USER'), r'Software\Martin Prikryl\WinSCP 2\Sessions'),
            ('HKCU', getattr(winreg, 'HKEY_CURRENT_USER'), r'Software\WOW6432Node\Martin Prikryl\WinSCP 2\Sessions'),
            ('HKLM', getattr(winreg, 'HKEY_LOCAL_MACHINE'), r'Software\Martin Prikryl\WinSCP 2\Sessions'),
            ('HKLM', getattr(winreg, 'HKEY_LOCAL_MACHINE'), r'Software\WOW6432Node\Martin Prikryl\WinSCP 2\Sessions'),
        ]

    def registry_stored_sessions(self) -> list[str]:
        details = self.registry_session_details()
        return sorted({str(info.get('name') or EMPTY_STRING) for info in details.values() if str(info.get('name') or EMPTY_STRING)}, key=lambda value: value.lower())

    def registry_session_details(self) -> dict[str, dict[str, Any]]:
        sessions: dict[str, dict[str, Any]] = {}
        if os.name != 'nt':
            self.log('SESSION:REGISTRY:SKIP reason=not-windows')
            return sessions
        try:
            import winreg  # type: ignore[import-not-found]
        except Exception as exc:
            captureException(None, source='start.py', context='except@5020')
            self.warn(f'SESSION:REGISTRY:IMPORT-FAILED {type(exc).__name__}: {exc}')
            return sessions
        for root_name, root_key, subkey in self._session_registry_roots():
            location = root_name + '\\' + subkey
            try:
                with winreg.OpenKey(root_key, subkey) as key:
                    count = winreg.QueryInfoKey(key)[0]
                    self.log(f'SESSION:REGISTRY:READ location={location} count={count}')
                    for index in range(count):
                        raw = str(winreg.EnumKey(key, index))
                        name = self._decode_session_name(raw)
                        child_subkey = subkey + '\\' + raw
                        info: dict[str, Any] = {'name': name, 'raw': raw, 'location': location, 'source': 'registry'}
                        try:
                            with winreg.OpenKey(root_key, child_subkey) as child:
                                for field in ('HostName', 'UserName', 'PortNumber', 'FSProtocol', 'RemoteDirectory', 'PublicKeyFile', 'PingType'):
                                    try:
                                        value, _kind = winreg.QueryValueEx(child, field)
                                        info[field] = value
                                    except FileNotFoundError:
                                        captureException(None, source='start.py', context='except@5040')
                                        info[field] = EMPTY_STRING
                                    except OSError as exc:
                                        captureException(None, source='start.py', context='except@5042')
                                        info[field + 'ReadError'] = f'{type(exc).__name__}: {exc}'
                                for secret_field in ('Password', 'Passphrase', 'ProxyPassword'):
                                    try:
                                        secret_value, _kind = winreg.QueryValueEx(child, secret_field)
                                        info[secret_field + 'Present'] = bool(str(secret_value or EMPTY_STRING))
                                    except FileNotFoundError:
                                        captureException(None, source='start.py', context='except@5048')
                                        info[secret_field + 'Present'] = False
                                    except OSError:
                                        captureException(None, source='start.py', context='except@5050')
                                        info[secret_field + 'Present'] = False
                        except Exception as exc:
                            captureException(None, source='start.py', context='except@5052')
                            info['read_error'] = f'{type(exc).__name__}: {exc}'
                        sessions[name.lower()] = info
                        self.log(
                            'SESSION:STORED '
                            f'name={name} raw={raw} source=registry location={location} '
                            f'host={info.get("HostName", EMPTY_STRING)} user={info.get("UserName", EMPTY_STRING)} '
                            f'port={info.get("PortNumber", EMPTY_STRING)} protocol={info.get("FSProtocol", EMPTY_STRING)} '
                            f'remote={info.get("RemoteDirectory", EMPTY_STRING)} '
                            f'password_saved={bool(info.get("PasswordPresent"))} passphrase_saved={bool(info.get("PassphrasePresent"))}'
                        )
            except FileNotFoundError:
                captureException(None, source='start.py', context='except@5063')
                self.log(f'SESSION:REGISTRY:MISS location={location}')
            except Exception as exc:
                captureException(None, source='start.py', context='except@5065')
                self.warn(f'SESSION:REGISTRY:FAILED location={location} {type(exc).__name__}: {exc}')
        return sessions

    def log_selected_session_details(self, session: str) -> None:
        if '://' in str(session or EMPTY_STRING):
            self.log('SESSION:DETAILS source=url password_read=never')
            return
        details = self.registry_session_details()
        info = details.get(str(session or EMPTY_STRING).lower())
        if not info:
            self.warn(f'SESSION:DETAILS:MISSING name={session}')
            return
        self.log(
            'SESSION:DETAILS '
            f'name={info.get("name", session)} host={info.get("HostName", EMPTY_STRING)} '
            f'user={info.get("UserName", EMPTY_STRING)} port={info.get("PortNumber", EMPTY_STRING)} '
            f'protocol={info.get("FSProtocol", EMPTY_STRING)} remote_default={info.get("RemoteDirectory", EMPTY_STRING)} '
            f'password_saved={bool(info.get("PasswordPresent"))} password_value_logged=false password_value_decoded=false'
        )

    def select_session(self) -> str:
        configured = str(self.session or 'vps').strip() or 'vps'
        if '://' in configured.lower():
            self.log(f'SESSION:SELECTED reason=url value={configured}')
            return configured
        stored = self.registry_stored_sessions()
        self.log(f'SESSION:VALIDATE configured={configured} stored_count={len(stored)}')
        self.log('SESSION:CANDIDATES ' + json.dumps([configured]))
        if not stored:
            self.log(f'SESSION:SELECTED name={configured} reason=no-registry-session-list-available')
            return configured
        if configured in stored:
            self.log(f'SESSION:SELECTED name={configured} reason=exact-match')
            return configured
        lower_map = {name.lower(): name for name in stored}
        if configured.lower() in lower_map:
            selected = lower_map[configured.lower()]
            self.warn(f'SESSION:CASE-MISMATCH configured={configured} selected={selected}')
            return selected
        self.warn('SESSION:NOT-FOUND configured=' + configured + ' available=' + ','.join(stored))
        return configured

    def script_path(self) -> Path:
        return self.root / 'winscp_prompt_push.txt'

    def log_path(self) -> Path:
        return self.root / 'winscp_prompt_push.log'

    def xml_log_path(self) -> Path:
        return self.root / 'winscp_prompt_push.xml'

    def transcript_path(self) -> Path:
        return self.root / 'winscp_prompt_push.transcript.txt'

    def failure_summary_path(self) -> Path:
        return self.root / 'winscp_prompt_push.failure.txt'

    def append_transcript(self, label: str, text: str = EMPTY_STRING) -> None:
        try:
            stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
            path = self.transcript_path()
            with File.tracedOpen(path, 'a', encoding='utf-8') as handle:
                handle.write(f'[{stamp}] {label}')
                if text:
                    handle.write(' ' + str(text).replace('\r', '\n'))
                handle.write('\n')
        except Exception as exc:
            captureException(None, source='start.py', context='except@5132')
            self.warn(f'TRANSCRIPT:WRITE-FAILED {type(exc).__name__}: {exc}')

    def reset_trace_files(self) -> None:
        for path in (self.transcript_path(), self.failure_summary_path(), self.log_path(), self.xml_log_path(), self.script_path()):
            try:
                if path.exists():
                    path.unlink()
                    self.log(f'TRACE-FILE:DELETE path={path}')
            except Exception as exc:
                captureException(None, source='start.py', context='except@5141')
                self.warn(f'TRACE-FILE:DELETE-FAILED path={path} {type(exc).__name__}: {exc}')

    def tail_file(self, path: Path, *, lines: int = 160) -> str:
        try:
            if not path.exists():
                return f'<missing {path}>'
            text = File.readText(path, encoding='utf-8', errors='replace')
            return '\n'.join(text.splitlines()[-int(lines):])
        except Exception as exc:
            captureException(None, source='start.py', context='except@5150')
            return f'<tail failed {path}: {type(exc).__name__}: {exc}>'

    def write_failure_summary(self, *, exit_code: int, command: list[str], output: str, elapsed: float) -> None:
        try:
            lines = [
                'Prompt WinSCP Push Failure Summary',
                f'utc={datetime.datetime.now(datetime.timezone.utc).isoformat()}',
                f'exit_code={exit_code}',
                f'elapsed={elapsed:.3f}s',
                f'cwd={self.root}',
                'command_json=' + json.dumps([str(item) for item in command]),
                'command_shell=' + ' '.join(shlex.quote(str(item)) for item in command),
                f'script={self.script_path()}',
                f'log={self.log_path()}',
                f'xml_log={self.xml_log_path()}',
                '',
                '--- script ---',
                self.tail_file(self.script_path(), lines=300),
                '',
                '--- WinSCP stdout/stderr ---',
                str(output or EMPTY_STRING),
                '',
                '--- WinSCP log tail ---',
                self.tail_file(self.log_path(), lines=240),
                '',
                '--- WinSCP XML log tail ---',
                self.tail_file(self.xml_log_path(), lines=240),
            ]
            File.writeText(self.failure_summary_path(), '\n'.join(lines) + '\n', encoding='utf-8')
            self.log(f'PUSH:FAILURE-SUMMARY path={self.failure_summary_path()}')
        except Exception as exc:
            captureException(None, source='start.py', context='except@5181')
            self.warn(f'PUSH:FAILURE-SUMMARY:WRITE-FAILED {type(exc).__name__}: {exc}')

    def write_script(self, files: list[Path], release_dir: Path) -> Path:
        session = self.select_session()
        self.session = session
        self.log_selected_session_details(session)
        script_path = self.script_path()
        remote_root = self.remote_path.rstrip('/')
        lines = [
            'option echo on',
            'option batch abort',
            'option confirm off',
            f'echo PROMPT-PUSH begin session={session} remote={remote_root}',
            f'echo PROMPT-PUSH open saved session {session}',
            f'open "{session}"',
            'echo PROMPT-PUSH connected; printing remote working directory before mkdir',
            'pwd',
            'option batch continue',
            f'echo PROMPT-PUSH ensure remote directory {remote_root}',
            f'mkdir "{remote_root}"',
            'option batch abort',
            f'echo PROMPT-PUSH cd remote directory {remote_root}',
            f'cd "{remote_root}"',
            'echo PROMPT-PUSH remote working directory after cd',
            'pwd',
            f'echo PROMPT-PUSH lcd local release directory {Path(release_dir).resolve()}',
            f'lcd "{Path(release_dir).resolve()}"',
            'echo PROMPT-PUSH local working directory after lcd',
            'lpwd',
        ]
        self.log(f'PUSH:SCRIPT:BEGIN path={script_path} session={session} remote={remote_root} files={len(files)}')
        for file_path in files:
            staged = Path(release_dir) / file_path.name
            local = str(staged.resolve())
            local_name = staged.name
            remote = remote_root + '/' + file_path.name
            size = staged.stat().st_size if staged.exists() else 0
            digest = self._file_md5(staged) if staged.exists() else EMPTY_STRING
            validator = ReleaseArtifactValidator.describe(staged)
            self.log(f'PUSH:FILE name={file_path.name} local={local} remote={remote} size={size} md5={digest} validator={validator}')
            lines.append(f'echo PROMPT-PUSH put begin name={file_path.name} bytes={size} md5={digest}')
            lines.append(f'echo PROMPT-PUSH put local name {local_name}')
            lines.append(f'echo PROMPT-PUSH put remote {remote}')
            lines.append(f'put -nopreservetime -transfer=binary "{local_name}" "{remote}"')
            lines.append(f'echo PROMPT-PUSH verify remote file {remote}')
            lines.append(f'ls "{remote}"')
        lines.append(f'echo PROMPT-PUSH final remote listing {remote_root}')
        lines.append('ls')
        lines.append('echo PROMPT-PUSH script complete; exiting WinSCP')
        lines.append('exit')
        File.writeText(script_path, '\n'.join(lines) + '\n', encoding='utf-8')
        self.log(f'PUSH:SCRIPT:WRITE path={script_path} bytes={script_path.stat().st_size if script_path.exists() else 0}')
        self.append_transcript('SCRIPT:WRITE', f'path={script_path} bytes={script_path.stat().st_size if script_path.exists() else 0}')
        for index, line in enumerate(lines, start=1):
            self.log(f'PUSH:SCRIPT-LINE:{index:03d} {line}')
            self.append_transcript(f'SCRIPT-LINE:{index:03d}', line)
        return script_path

    def command(self, winscp: str, script_path: Path, log_path: Path) -> list[str]:
        xml_log_path = self.xml_log_path()
        command = [winscp, f'/log={log_path}', f'/xmllog={xml_log_path}', '/xmlgroups', '/loglevel=2', '/nointeractiveinput', f'/script={script_path}']
        if self.winscp_ini:
            command.insert(1, f'/ini={self.winscp_ini}')
        self.log('PUSH:COMMAND:VERBATIM ' + json.dumps([str(item) for item in command]))
        self.log('PUSH:COMMAND:SHELL ' + ' '.join(shlex.quote(str(item)) for item in command))
        self.log(f'PUSH:COMMAND:CONTEXT cwd={self.root} script_exists={script_path.exists()} script_bytes={script_path.stat().st_size if script_path.exists() else 0} log_path={log_path} xml_log_path={xml_log_path} transcript={self.transcript_path()} dry_run={self.dry_run}')
        self.append_transcript('COMMAND:JSON', json.dumps([str(item) for item in command]))
        self.append_transcript('COMMAND:SHELL', ' '.join(shlex.quote(str(item)) for item in command))
        return command

    def _run_winscp_command(self, command: list[str], *, timeout: int = 1200) -> int:
        self.log('PROCESS:START ' + json.dumps([str(item) for item in command]))
        self.log('PROCESS:START-SHELL ' + ' '.join(shlex.quote(str(item)) for item in command))
        self.append_transcript('PROCESS:START', json.dumps([str(item) for item in command]))
        started = time.time()
        try:
            proc = START_EXECUTION_LIFECYCLE.startProcess(
                'WinSCP upload',
                [str(item) for item in command],
                cwd=str(self.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
            )
        except Exception as exc:
            captureException(None, source='start.py', context='except@5268')
            self.warn(f'PROCESS:LIFECYCLE-START-FAILED {type(exc).__name__}: {exc}')
            return 125
        try:
            output, _stderr = proc.communicate(timeout=timeout)  # lifecycle-bypass-ok main-thread-ok winscp-cli-run-with-timeout
            output_text = str(output or EMPTY_STRING)
            if output_text.strip():
                self.log('PROCESS:RAW-OUTPUT:BEGIN')
                for line in output_text.splitlines():
                    self.log('PROCESS:OUT ' + line)
                    self.append_transcript('PROCESS:OUT', line)
                self.log('PROCESS:RAW-OUTPUT:END')
            else:
                self.warn('PROCESS:NO-OUTPUT WinSCP returned no stdout/stderr; inspect /log and /xmllog files')
                self.append_transcript('PROCESS:NO-OUTPUT')
            exit_code = int(getattr(proc, 'returncode', 1) if getattr(proc, 'returncode', None) is not None else 1)
            elapsed = time.time() - started
            self.log(f'PROCESS:END exit_code={exit_code} elapsed={elapsed:.3f}s')
            self.append_transcript('PROCESS:END', f'exit_code={exit_code} elapsed={elapsed:.3f}s')
            if exit_code != 0:
                self.write_failure_summary(exit_code=exit_code, command=command, output=output_text, elapsed=elapsed)
            return exit_code
        except subprocess.TimeoutExpired:
            captureException(None, source='start.py', context='except@5290')
            self.warn(f'PROCESS:TIMEOUT seconds={timeout}; killing WinSCP process')
            try:
                proc.kill()
                output, _stderr = proc.communicate(timeout=10)  # lifecycle-bypass-ok main-thread-ok winscp-cli-drain-with-timeout
                output_text = str(output or EMPTY_STRING)
                for line in output_text.splitlines():
                    self.log('PROCESS:OUT-AFTER-KILL ' + line)
                    self.append_transcript('PROCESS:OUT-AFTER-KILL', line)
                self.write_failure_summary(exit_code=124, command=command, output=output_text, elapsed=time.time() - started)
            except Exception as exc:
                captureException(None, source='start.py', context='except@5300')
                self.warn(f'PROCESS:KILL-FAILED {type(exc).__name__}: {exc}')
            return 124

    def upload(self, files: list[Path], release_dir: Path) -> None:
        self.log(f'PUSH:UPLOAD:BEGIN files={len(files)} release_dir={release_dir} remote={self.remote_path} session={self.session}')
        self.reset_trace_files()
        self.append_transcript('UPLOAD:BEGIN', f'files={len(files)} release_dir={release_dir} remote={self.remote_path} session={self.session}')
        for index, path in enumerate(files, start=1):
            staged = Path(release_dir) / Path(path).name
            self.log(f'PUSH:UPLOAD-FILE:{index:03d} original={path} staged={staged} staged_exists={staged.exists()} staged_size={staged.stat().st_size if staged.exists() else 0} staged_md5={self._file_md5(staged) if staged.exists() else EMPTY_STRING}')
        winscp = self.discover()
        script_path = self.write_script(files, release_dir)
        log_path = self.log_path()
        command = self.command(winscp or 'WinSCP.com', script_path, log_path)
        if self.dry_run:
            self.log('PUSH:DRY-RUN WinSCP command not executed')
            return
        if not winscp:
            self.die('WinSCP.com not found. Install WinSCP or add WinSCP.com to PATH.')
        started = time.time()
        rc = self._run_winscp_command(command, timeout=1200)
        elapsed = time.time() - started
        xml_log_path = self.xml_log_path()
        self.log(f'PUSH:RESULT exit_code={rc} elapsed={elapsed:.3f}s log={log_path} log_exists={log_path.exists()} log_size={log_path.stat().st_size if log_path.exists() else 0} xml_log={xml_log_path} xml_exists={xml_log_path.exists()} xml_size={xml_log_path.stat().st_size if xml_log_path.exists() else 0}')
        for label, path in (('WINSCP-LOG', log_path), ('WINSCP-XMLLOG', xml_log_path), ('WINSCP-TRANSCRIPT', self.transcript_path())):
            if path.exists():
                try:
                    tail = '\n'.join(File.readText(path, encoding='utf-8', errors='replace').splitlines()[-180:])
                    self.log(f'PUSH:{label}:TAIL\n' + tail)
                except Exception as exc:
                    captureException(None, source='start.py', context='except@5330')
                    self.warn(f'PUSH:{label}:TAIL-FAILED {type(exc).__name__}: {exc}')
        if int(rc) != 0:
            self.die(f'WinSCP upload failed with exit code {rc}. See {log_path}, {xml_log_path}, {self.transcript_path()}, and {self.failure_summary_path()}')


class PromptReleasePublisher:
    """Outfile-first publisher: push existing EXE/MSI/ZIP artifacts before building anything."""

    DEFAULT_WINSCP_SESSION = 'vps'
    DEFAULT_REMOTE_PATH = '/home/trentontompkins.com/prompt'
    MD5_STATE_FILENAME = 'md5s.txt'
    RELEASE_DIR_NAME = 'release_upload'  # stored under temp build root, not in the repo/dist tree

    def __init__(self, root: Path, argv=None):
        self.root = Path(root).resolve()  # noqa: nonconform
        self.argv = list(argv or [])
        self.packager = PromptApplicationPackager(self.root, GTP_PATH, self.argv)  # noqa: nonconform
        self.releaseDir = self.packager.tempBuildRoot / self.RELEASE_DIR_NAME  # noqa: nonconform
        self.md5Path = self.root / self.MD5_STATE_FILENAME  # noqa: nonconform
        self.dryRun = cliHasAnyFlag(self.argv, PACKAGE_DRY_RUN_FLAGS)  # noqa: nonconform
        self.config = self.loadConfig()  # noqa: nonconform
        self.winscpSession = (str(self.config.get('winscp_session') or self.DEFAULT_WINSCP_SESSION).strip() or self.DEFAULT_WINSCP_SESSION).lower()  # noqa: nonconform
        self.remotePath = str(self.config.get('remote_path') or self.DEFAULT_REMOTE_PATH).strip() or self.DEFAULT_REMOTE_PATH  # noqa: nonconform
        self.winscpIni = str(self.config.get('winscp_ini') or EMPTY_STRING).strip()  # noqa: nonconform

    def log(self, message: str) -> None:
        print(f'[PROMPT-RELEASE] {message}', flush=True)

    def warn(self, message: str) -> None:
        print(f'[WARN:PROMPT-RELEASE] {message}', file=sys.stderr, flush=True)

    def die(self, message: str) -> None:
        print(f'[FATAL:PROMPT-RELEASE] {message}', file=sys.stderr, flush=True)
        raise RuntimeError(message)

    def distDir(self) -> Path:
        return self.packager.distDir

    def loadConfig(self) -> dict[str, str]:
        config_path = self.root / 'config.ini'
        values: dict[str, str] = {}
        if not config_path.exists():
            self.warn(f'CONFIG:MISSING path={config_path}')
            return values
        section = EMPTY_STRING
        try:
            for raw_line in File.readText(config_path, encoding='utf-8', errors='replace').splitlines():
                line = str(raw_line or EMPTY_STRING).strip()
                if not line or line.startswith(('#', ';')):
                    continue
                if line.startswith('[') and line.endswith(']'):
                    section = line.strip('[]').strip().lower()
                    continue
                if section == 'push' and '=' in line:
                    key, value = line.split('=', 1)
                    values[key.strip().lower()] = value.strip()
            self.log(f'CONFIG:READ path={config_path} push_keys={sorted(values.keys())}')
        except Exception as exc:
            captureException(None, source='start.py', context='except@5385')
            self.warn(f'CONFIG:READ:FAILED path={config_path} {type(exc).__name__}: {exc}')
        return values

    def fileMd5(self, path: Path) -> str:
        return safeFileMd5Hex(path)

    def artifactValidatorLabel(self, path: Path) -> str:
        return ReleaseArtifactValidator.describe(path)

    def isValidReleaseArtifact(self, path: Path) -> bool:
        try:
            valid = ReleaseArtifactValidator.is_valid(path)
        except Exception as exc:
            captureException(None, source='start.py', context='except@5398')
            self.warn(f'OUTFILE:VALIDATE:FAILED file={path} {type(exc).__name__}: {exc}')
            return False
        self.log(f'OUTFILE:VALIDATE file={path} validator={self.artifactValidatorLabel(path)} valid={valid}')
        return bool(valid)

    def explicitBuildRequested(self) -> bool:
        return buildRequested(self.argv)

    def _candidateOutfilePaths(self) -> list[Path]:
        dist = self.distDir()
        return [
            dist / 'Prompt.exe',
            dist / 'PromptSetup.exe',
            dist / 'PromptSetup-Inno.exe',
            dist / 'PromptSetup-WiX.msi',
        ]

    def _recursiveOutfileCandidates(self) -> list[Path]:
        roots = [self.distDir()]
        files: list[Path] = []
        for root in roots:
            if not root.exists():
                continue
            for pattern in ('*.exe', '*.msi', '*.zip'):
                files.extend(sorted(root.rglob(pattern)))
        return files

    def _copyBackendExeOutfile(self, backend: str) -> Path | None:
        root = self.packager.backendArtifactRoot(backend)
        if not root.exists():
            return None
        exe_candidates = sorted(root.rglob('*.exe'), key=lambda item: (0 if item.name.lower() == 'prompt.exe' else 1, len(str(item)), str(item).lower()))
        if not exe_candidates:
            self.warn(f'OUTFILE:BACKEND:EXE-MISSING backend={backend} root={root}')
            return None
        target = self.distDir() / f'Prompt-{backend}.exe'
        target.parent.mkdir(parents=True, exist_ok=True)
        File.copy2(exe_candidates[0], target)
        self.log(f'OUTFILE:MATERIALIZE backend={backend} source={exe_candidates[0]} target={target}')
        return target

    def _zipBackendOutfile(self, backend: str) -> Path | None:
        root = self.packager.backendArtifactRoot(backend)
        if not root.exists() or not any(root.iterdir()):
            return None
        target = self.distDir() / f'Prompt-{backend}.zip'
        newest_source = max((item.stat().st_mtime for item in root.rglob('*') if item.exists()), default=0)
        if target.exists() and target.stat().st_mtime >= newest_source:
            self.log(f'OUTFILE:ZIP:SKIP backend={backend} reason=current target={target}')
            return target
        if target.exists():
            target.unlink()
        self.log(f'OUTFILE:ZIP:BEGIN backend={backend} source={root} target={target}')
        shutil.make_archive(str(target.with_suffix('')), 'zip', root_dir=str(root))
        self.log(f'OUTFILE:ZIP:DONE backend={backend} target={target} size={target.stat().st_size if target.exists() else 0}')
        return target if target.exists() else None

    def materializeExistingBackendOutfiles(self) -> list[Path]:
        files: list[Path] = []
        for backend in ('pyinstaller', 'nikita'):
            exe = self._copyBackendExeOutfile(backend)
            if exe is not None:
                files.append(exe)
            zip_path = self._zipBackendOutfile(backend)
            if zip_path is not None:
                files.append(zip_path)
        return files

    def inventoryOutfiles(self, *, materialize: bool = True) -> list[Path]:
        self.log('OUTFILE:INVENTORY:BEGIN')
        if materialize:
            self.materializeExistingBackendOutfiles()
        candidates = self._candidateOutfilePaths() + self._recursiveOutfileCandidates()
        deduped: list[Path] = []
        seen: set[Path] = set()
        allowed_suffixes = {'.exe', '.msi', '.zip', '.nsi', '.json'}
        for candidate in candidates:
            path = Path(candidate)
            if path in seen:
                continue
            seen.add(path)
            exists = path.exists() and path.is_file()
            size = path.stat().st_size if exists else 0
            digest = self.fileMd5(path) if exists and path.suffix.lower() in allowed_suffixes else EMPTY_STRING
            self.log(f'OUTFILE:CHECK exists={exists} file={path} size={size} md5={digest}')
            if exists and size > 0 and path.suffix.lower() in allowed_suffixes:
                if self.isValidReleaseArtifact(path):
                    deduped.append(path)
                else:
                    self.warn(f'OUTFILE:SKIP:INVALID file={path} validator={self.artifactValidatorLabel(path)} size={size}')
                    try:
                        head = path.read_bytes()[:256]
                        generated_parent = path.parent.name.lower() in {'dist', 'release_upload'}
                        if generated_parent and b'DRY-RUN ' + b'placeholder' in head and path.suffix.lower() in {'.exe', '.msi', '.zip'}:
                            path.unlink()
                            self.warn(f'OUTFILE:DELETE:PLACEHOLDER file={path}')
                    except Exception as exc:
                        captureException(None, source='start.py', context='except@5511')
                        self.warn(f'OUTFILE:DELETE:PLACEHOLDER-FAILED file={path} {type(exc).__name__}: {exc}')
        # Prefer uploadable release payloads. Keep script/json metadata too, but only after real artifacts.
        real = [p for p in deduped if p.suffix.lower() in {'.exe', '.msi', '.zip'}]
        meta = [p for p in deduped if p.suffix.lower() in {'.nsi', '.json'}]
        ordered = sorted(real, key=lambda p: (p.name.lower(), str(p).lower())) + sorted(meta, key=lambda p: (p.name.lower(), str(p).lower()))
        self.log('OUTFILE:INVENTORY:DONE count=' + str(len(ordered)) + ' names=' + ','.join(path.name for path in ordered))
        return ordered

    def buildMissingOutfiles(self) -> None:
        self.log('BUILD:RUN reason=no-existing-outfiles-or-explicit-build command=NSIS/PyInstaller if needed')
        rc = self.packager.buildInstaller()
        if int(rc) != 0:
            self.die(f'Installer build failed with exit code {rc}')

    def loadMd5State(self) -> dict[str, str]:
        state: dict[str, str] = {}
        if not self.md5Path.exists():
            return state
        try:
            for raw_line in File.readText(self.md5Path, encoding='utf-8', errors='replace').splitlines():
                line = str(raw_line or EMPTY_STRING).strip()
                if not line or line.startswith('#'):
                    continue
                if '  ' in line:
                    digest, rel = line.split('  ', 1)
                elif '\t' in line:
                    digest, rel = line.split('\t', 1)
                else:
                    continue
                digest = str(digest or EMPTY_STRING).strip().lower()
                rel = str(rel or EMPTY_STRING).strip().replace('\\', '/')
                if digest and rel:
                    state[rel] = digest
        except Exception as exc:
            captureException(None, source='start.py', context='except@5545')
            self.warn(f'MD5:READ:FAILED file={self.md5Path} {type(exc).__name__}: {exc}')
        return state

    def writeMd5State(self, state: dict[str, str]) -> None:
        lines = ['# Prompt release MD5 state', f'# Updated UTC: {datetime.datetime.now(datetime.timezone.utc).isoformat()}']
        for rel in sorted(state):
            lines.append(f'{state[rel]}  {rel}')
        File.writeText(self.md5Path, '\n'.join(lines) + '\n', encoding='utf-8')
        self.log(f'MD5:WRITE path={self.md5Path} count={len(state)}')

    def prepareReleaseDirectory(self, files: list[Path]) -> dict[str, str]:
        if self.releaseDir.exists():
            shutil.rmtree(self.releaseDir, ignore_errors=True)
        self.releaseDir.mkdir(parents=True, exist_ok=True)
        md5_state: dict[str, str] = {}
        for file_path in files:
            target = self.releaseDir / file_path.name
            if Path(file_path).resolve() != target.resolve():
                File.copy2(file_path, target)
            digest = self.fileMd5(target)
            md5_state[target.name] = digest
            self.log(f'RELEASE:STAGE name={target.name} size={target.stat().st_size if target.exists() else 0} md5={digest} source={file_path} target={target}')
        md5_lines = ['# Prompt server release MD5s', f'# Updated UTC: {datetime.datetime.now(datetime.timezone.utc).isoformat()}']
        for name in sorted(md5_state):
            md5_lines.append(f'{md5_state[name]}  {name}')
        File.writeText(self.releaseDir / self.MD5_STATE_FILENAME, '\n'.join(md5_lines) + '\n', encoding='utf-8')
        latest = {'product': 'Prompt', 'company': 'AcquisitionInvest LLC', 'version': self.packager.version, 'updated_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'files': [{'name': name, 'md5': md5_state[name]} for name in sorted(md5_state)]}
        File.writeText(self.releaseDir / 'latest.json', json.dumps(latest, indent=2), encoding='utf-8')
        md5_state[self.MD5_STATE_FILENAME] = self.fileMd5(self.releaseDir / self.MD5_STATE_FILENAME)
        md5_state['latest.json'] = self.fileMd5(self.releaseDir / 'latest.json')
        return md5_state

    def uploadWithWinscp(self, files: list[Path]) -> None:
        client = WinSCPClient(
            root=self.root,
            session=self.winscpSession,
            remote_path=self.remotePath,
            winscp_ini=self.winscpIni,
            dry_run=self.dryRun,
            logger=self.log,
            warner=self.warn,
            die=self.die,
            run_command=self.packager.runCommand,
            file_md5=self.fileMd5,
        )
        client.upload(files, self.releaseDir)

    def run(self) -> int:
        self.log(f'PUSH:BEGIN root={self.root} remote={self.remotePath} session={self.winscpSession} dry_run={self.dryRun} argv={json.dumps([str(a) for a in self.argv])}')
        files = self.inventoryOutfiles(materialize=True)
        if self.explicitBuildRequested():
            self.log(f'BUILD:NEEDED reason=explicit-build-requested existing_count={len(files)}')
            self.buildMissingOutfiles()
            files = self.inventoryOutfiles(materialize=True)
        else:
            self.log('BUILD:SKIP reason=no-build-flag count=' + str(len(files)))
        uploadable = []
        for path in files:
            if path.suffix.lower() not in {'.exe', '.msi', '.zip'}:
                continue
            if self.isValidReleaseArtifact(path):
                uploadable.append(path)
            else:
                self.warn(f'PUSH:SKIP:INVALID-UPLOADABLE file={path} validator={self.artifactValidatorLabel(path)}')
        if not uploadable:
            self.die('No uploadable release outfiles found. Expected .exe, .msi, or .zip directly under dist/.')
        current = self.prepareReleaseDirectory(uploadable)
        self.log('PUSH:FILES ' + ', '.join(f'{path.name}:{path.stat().st_size if path.exists() else 0}:{self.fileMd5(path)}' for path in uploadable))
        self.uploadWithWinscp(uploadable)
        self.writeMd5State(current)
        File.writeText(self.root / 'last_update_check.txt', datetime.datetime.now(datetime.timezone.utc).isoformat() + '\n', encoding='utf-8')
        self.log(f'PUSH:COMPLETE md5_state={self.md5Path}')
        return 0


def pushRequested(argv=None) -> bool:
    return cliHasAnyFlag(argv, PUSH_FLAGS)


def runPushIfRequested(argv=None) -> int | None:
    tokens = list(argv or [])
    if not pushRequested(tokens):
        return None
    return int(PromptReleasePublisher(BASE_DIR, tokens).run())



class PromptWeeklyUpdateChecker:
    """Client-side weekly updater; publishing remains gated behind --push."""

    UPDATE_BASE_URL = 'http://www.trentontompkins.com/prompt'
    UPDATE_URL = UPDATE_BASE_URL + '/latest.json'
    CHECK_FILE = 'last_update_check.txt'
    LOCAL_RELEASE_FILE = 'local_release_md5.txt'
    DOWNLOAD_DIR_NAME = 'updates'
    INSTALLER_NAME = 'PromptSetup.exe'
    CHECK_INTERVAL_SECONDS = 7 * 24 * 60 * 60

    def __init__(self, root: Path):
        self.root = Path(root).resolve()  # noqa: nonconform
        self.checkPath = self.root / self.CHECK_FILE  # noqa: nonconform
        self.localReleasePath = self.root / self.LOCAL_RELEASE_FILE  # noqa: nonconform
        self.downloadDir = self.root / self.DOWNLOAD_DIR_NAME  # noqa: nonconform

    def log(self, message: str) -> None:
        print(f'[UPDATE] {message}', flush=True)

    def warn(self, message: str) -> None:
        print(f'[WARN:update] {message}', file=sys.stderr, flush=True)

    def lastCheckTimestamp(self) -> float:
        if not self.checkPath.exists():
            return 0.0
        try:
            raw = File.readText(self.checkPath, encoding='utf-8', errors='replace').strip()
            if not raw:
                return 0.0
            try:
                return float(raw)
            except Exception as error:
                captureException(None, source='start.py', context='except@5666')
                print(f"[WARN:swallowed-exception] start.py:4457 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                parsed = datetime.datetime.fromisoformat(raw.replace('Z', '+00:00'))
                return parsed.timestamp()
        except Exception as error:
            captureException(None, source='start.py', context='except@5670')
            print(f"[WARN:swallowed-exception] start.py:4460 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return 0.0

    def shouldCheck(self) -> bool:
        return (time.time() - float(self.lastCheckTimestamp() or 0.0)) >= self.CHECK_INTERVAL_SECONDS

    def saveCheckTimestamp(self) -> None:
        try:
            File.writeText(self.checkPath, str(time.time()) + '\n', encoding='utf-8')
        except Exception as error:
            captureException(None, source='start.py', context='except@5680')
            print(f"[WARN:swallowed-exception] start.py:4469 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass

    def loadLocalReleaseMd5(self) -> str:
        try:
            return File.readText(self.localReleasePath, encoding='utf-8', errors='replace').strip().lower()
        except Exception as error:
            captureException(None, source='start.py', context='except@5687')
            print(f"[WARN:swallowed-exception] start.py:4475 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return EMPTY_STRING

    def saveLocalReleaseMd5(self, digest: str) -> None:
        try:
            File.writeText(self.localReleasePath, str(digest or EMPTY_STRING).strip().lower() + '\n', encoding='utf-8')
        except Exception as error:
            captureException(None, source='start.py', context='except@5694')
            print(f"[WARN:swallowed-exception] start.py:4481 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass

    def fetchLatestMetadata(self) -> dict[str, Any]:
        with urllib.request.urlopen(self.UPDATE_URL, timeout=8) as response:
            payload = response.read(1024 * 512).decode('utf-8', errors='replace')
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ValueError('latest.json did not contain an object')
        return data

    def installerEntry(self, latest: dict[str, Any]) -> dict[str, str]:
        files = latest.get('files', [])
        if not isinstance(files, list):
            return {}
        for entry in files:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get('name', EMPTY_STRING) or EMPTY_STRING).strip()
            digest = str(entry.get('md5', EMPTY_STRING) or EMPTY_STRING).strip().lower()
            if name.lower() == self.INSTALLER_NAME.lower() and digest:
                return {'name': name, 'md5': digest}
        for entry in files:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get('name', EMPTY_STRING) or EMPTY_STRING).strip()
            digest = str(entry.get('md5', EMPTY_STRING) or EMPTY_STRING).strip().lower()
            if name.lower().endswith('.exe') and digest:
                return {'name': name, 'md5': digest}
        return {}

    def downloadInstaller(self, entry: dict[str, str]) -> Path:
        name = str(entry.get('name', self.INSTALLER_NAME) or self.INSTALLER_NAME).strip() or self.INSTALLER_NAME
        url = self.UPDATE_BASE_URL.rstrip('/') + '/' + urllib.parse.quote(name)
        self.downloadDir.mkdir(parents=True, exist_ok=True)
        target = self.downloadDir / name
        temp_target = self.downloadDir / (name + '.download')
        self.log(f'Downloading update installer: {url}')
        with urllib.request.urlopen(url, timeout=60) as response:
            with File.tracedOpen(temp_target, 'wb') as handle:
                File.copyFileObj(response, handle)
        expected = str(entry.get('md5', EMPTY_STRING) or EMPTY_STRING).strip().lower()
        actual = safeFileMd5Hex(temp_target).lower()
        if expected and actual != expected:
            try:
                temp_target.unlink()
            except Exception as error:
                captureException(None, source='start.py', context='except@5741')
                print(f"[WARN:swallowed-exception] start.py:4527 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
            raise ValueError(f'Installer MD5 mismatch. expected={expected} actual={actual}')
        if target.exists():
            try:
                target.unlink()
            except Exception as error:
                captureException(None, source='start.py', context='except@5748')
                print(f"[WARN:swallowed-exception] start.py:4533 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
        temp_target.rename(target)
        self.saveLocalReleaseMd5(actual)
        return target

    def launchInstaller(self, installer_path: Path) -> None:
        installer = Path(installer_path).resolve()
        if not installer.exists():
            raise FileNotFoundError(str(installer))
        auto_launch = str(os.environ.get('PROMPT_AUTO_LAUNCH_UPDATE_INSTALLER', '') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
        if not auto_launch:
            self.log(f'Update installer downloaded but not launched automatically: {installer}')
            self.log('Set PROMPT_AUTO_LAUNCH_UPDATE_INSTALLER=1 to launch update installers from startup; otherwise run it manually/elevated when ready.')
            return
        self.log(f'Launching update installer: {installer}')
        if os.name == 'nt':
            # NSIS/MSI installers frequently require elevation.  ShellExecute
            # with runas is the correct Windows route for an elevated launch;
            # plain subprocess.CreateProcess raises WinError 740 and pollutes
            # debug.log during normal startup.  Use it only when explicitly
            # opted in.
            try:
                import ctypes
                rc = ctypes.windll.shell32.ShellExecuteW(None, 'runas', str(installer), None, str(installer.parent), 1)
                if int(rc) <= 32:
                    raise OSError(f'ShellExecuteW returned {rc}')
                self.log(f'Update installer elevation requested via ShellExecute/runas: {installer}')
                return
            except Exception as error:
                captureException(error, source='start.py', context='weekly-update-shell-execute-runas', handled=True)
                self.warn(f'Could not launch update installer elevated; run manually: {installer} ({type(error).__name__}: {error})')
                return
        run_log_handle = openRunLogStream()
        proc = START_EXECUTION_LIFECYCLE.startProcess('PromptUpdaterInstaller', [str(installer)], cwd=str(installer.parent), stdout=run_log_handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        setattr(proc, '_prompt_run_log_handle', run_log_handle)

    def check(self) -> None:
        if not self.shouldCheck():
            return
        self.saveCheckTimestamp()
        try:
            latest = self.fetchLatestMetadata()
            version = str(latest.get('version', EMPTY_STRING) or EMPTY_STRING).strip()
            updated = str(latest.get('updated_utc', EMPTY_STRING) or EMPTY_STRING).strip()
            entry = self.installerEntry(latest)
            if not entry:
                self.log(f'Latest metadata found but no installer entry is listed. version={version or "unknown"} updated={updated or "unknown"}')
                return
            remote_md5 = str(entry.get('md5', EMPTY_STRING) or EMPTY_STRING).strip().lower()
            local_md5 = self.loadLocalReleaseMd5()
            if remote_md5 and local_md5 == remote_md5:
                self.log(f'Prompt is up to date. version={version or "unknown"}')
                return
            self.log(f'Update available. version={version or "unknown"} updated={updated or "unknown"}')
            installer = self.downloadInstaller(entry)
            self.launchInstaller(installer)
        except Exception as exc:
            captureException(None, source='start.py', context='except@5791')
            self.warn(f'Weekly update check/install failed: {type(exc).__name__}: {exc}')


def runWeeklyUpdateCheckIfDue(argv=None) -> None:
    tokens = list(argv or [])
    if pushRequested(tokens) or packagingRequested(tokens) or deployMonitorRequested(tokens) or deployOnceRequested(tokens) or proxyDaemonRequested(tokens) or gitRequested(tokens):
        return
    PromptWeeklyUpdateChecker(BASE_DIR).check()



def missingLinuxSystemRequirements(argv=None) -> list[dict[str, Any]]:
    return StartDependencyRegistry(argv).missingLinuxSystemRequirements()


def verifyLinuxSystemRequirementsOrDie(argv=None) -> None:
    missing_requirements = missingLinuxSystemRequirements(argv)
    required_missing = [item for item in list(missing_requirements or []) if bool(item.get('required'))]
    if not required_missing:
        return
    lines = ['Missing required Linux system dependencies:']
    for requirement in required_missing:
        kind = str(requirement.get('kind', EMPTY_STRING) or EMPTY_STRING)
        name = str(requirement.get('name', EMPTY_STRING) or EMPTY_STRING)
        package_name = str(requirement.get('package', EMPTY_STRING) or EMPTY_STRING)
        line = f'- {kind}: {name} (required)'
        if package_name:
            line += f'  package={package_name}'
        lines.append(line)
    lines.append('start.py attempted to install launcher-critical Linux packages when possible. Install any remaining missing system packages before launching again.')
    raise RuntimeError('\n'.join(lines))

def safeRepr(value: Any, limit: int = 400) -> str:
    try:
        rendered = repr(value)
    except BaseException as outer_error:
        captureException(None, source='start.py', context='except@5827')
        print(f"[WARN:swallowed-exception] start.py:4611 {type(outer_error).__name__}: {outer_error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        try:
            detail = str(outer_error)
        except Exception as detail_error:
            captureException(None, source='start.py', context='except@5831')
            print(f"[WARN:swallowed-exception] start.py:4614 {type(detail_error).__name__}: {detail_error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            try:
                detail = repr(getattr(outer_error, 'args', ()))
            except Exception as args_error:
                captureException(None, source='start.py', context='except@5835')
                print(f"[WARN:swallowed-exception] start.py:4617 {type(args_error).__name__}: {args_error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                detail = 'unprintable error'
        rendered = f"<repr failed: {type(outer_error).__name__}: {detail}>"
    if len(rendered) > limit:
        rendered = rendered[:limit] + '…'
    return rendered

_DEBUGGER_QT_IMPORTS_READY = False
_DEBUGGER_QT_IMPORT_ERROR = None
_DEBUGGER_QT_APP = None

# Pyright-visible placeholders for optional debugger Qt symbols. ensureDebuggerQtImports()
# replaces these globals with real PySide6 objects before any debugger UI is opened.
QObject: Any = None
QEventLoop: Any = None
QTimer: Any = None
QUrl: Any = None
Qt: Any = None
QAction: Any = None
QFont: Any = None
QBrush: Any = None
QColor: Any = None
QApplication: Any = None
QCheckBox: Any = None
QDialog: Any = None
QFileDialog: Any = None
QDoubleSpinBox: Any = None
QFormLayout: Any = None
QHBoxLayout: Any = None
QLabel: Any = None
QLineEdit: Any = None
QMenu: Any = None
QMessageBox: Any = None
QPlainTextEdit: Any = None
QPushButton: Any = None
QTabWidget: Any = None
QTreeWidget: Any = None
QTreeWidgetItem: Any = None
QVBoxLayout: Any = None
QWidget: Any = None
QPrintDialog: Any = None
QPrinter: Any = None
QWebEngineView: Any = None
QWebEnginePage: Any = None


def ensureDebuggerQtImports() -> bool:
    global _DEBUGGER_QT_IMPORTS_READY, _DEBUGGER_QT_IMPORT_ERROR
    if _DEBUGGER_QT_IMPORTS_READY:
        return True
    if _DEBUGGER_QT_IMPORT_ERROR is not None:
        return False
    try:
        from PySide6.QtCore import QObject, QEventLoop, QTimer, QUrl, Qt
        from PySide6.QtGui import QAction, QBrush, QColor, QFont
        from PySide6.QtWidgets import QApplication, QCheckBox, QDialog, QFileDialog, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget
        from PySide6.QtPrintSupport import QPrintDialog, QPrinter
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtWebEngineCore import QWebEnginePage
        globals().update({
            'QObject': QObject,
            'QEventLoop': QEventLoop,
            'QTimer': QTimer,
            'QUrl': QUrl,
            'Qt': Qt,
            'QAction': QAction,
            'QBrush': QBrush,
            'QColor': QColor,
            'QFont': QFont,
            'QApplication': QApplication,
            'QCheckBox': QCheckBox,
            'QDialog': QDialog,
            'QFileDialog': QFileDialog,
            'QDoubleSpinBox': QDoubleSpinBox,
            'QFormLayout': QFormLayout,
            'QHBoxLayout': QHBoxLayout,
            'QLabel': QLabel,
            'QLineEdit': QLineEdit,
            'QMenu': QMenu,
            'QMessageBox': QMessageBox,
            'QPlainTextEdit': QPlainTextEdit,
            'QPushButton': QPushButton,
            'QTabWidget': QTabWidget,
            'QTreeWidget': QTreeWidget,
            'QTreeWidgetItem': QTreeWidgetItem,
            'QVBoxLayout': QVBoxLayout,
            'QWidget': QWidget,
            'QPrintDialog': QPrintDialog,
            'QPrinter': QPrinter,
            'QWebEngineView': QWebEngineView,
            'QWebEnginePage': QWebEnginePage,
        })
        _DEBUGGER_QT_IMPORTS_READY = True
        return True
    except Exception as error:
        captureException(None, source='start.py', context='except@5930')
        print(f"[WARN:swallowed-exception] start.py:4711 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        _DEBUGGER_QT_IMPORT_ERROR = error
        return False


def debuggerQtUnavailableText() -> str:
    error = _DEBUGGER_QT_IMPORT_ERROR
    if error is None:
        return '[PromptDebugger] Qt WebEngine editor unavailable.'
    return f'[PromptDebugger] Qt WebEngine editor unavailable: {type(error).__name__}: {error}'


def ensureDebuggerQtApplication():
    global _DEBUGGER_QT_APP
    if not ensureDebuggerQtImports():
        return None
    app = QApplication.instance()
    if app is None:
        app = QApplication(['PromptDebugger'])
        _DEBUGGER_QT_APP = app
    elif _DEBUGGER_QT_APP is None:
        _DEBUGGER_QT_APP = app
    return app


def debuggerEmbeddedLibraryId(name: str):
    library_id = getattr(TrioDesktopEmbeddedData, 'EmbeddedLibraryId', None)
    if library_id is None:
        return None
    normalized = str(name or EMPTY_STRING).strip().upper()
    if not normalized:
        return None
    return getattr(library_id, normalized, None)


def debuggerEmbeddedBlob(library_class_name: str, library_key) -> str:
    try:
        library_class = getattr(TrioDesktopEmbeddedData, str(library_class_name or EMPTY_STRING).strip(), None)
        libraries = getattr(library_class, 'LIBRARIES', {}) if library_class is not None else {}
        raw = libraries.get(library_key)
        if raw is None:
            return EMPTY_STRING
        if isinstance(raw, str):
            return base64.b64decode(raw.encode('ascii')).decode('utf-8', errors='replace')
        if isinstance(raw, (bytes, bytearray, memoryview)):
            return base64.b64decode(bytes(raw)).decode('utf-8', errors='replace')
    except Exception as error:
        captureException(None, source='start.py', context='except@5977')
        print(f"[WARN:swallowed-exception] start.py:4757 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        pass
    return EMPTY_STRING


def debuggerCodeEditorInlineScript(text: str) -> str:
    return str(text or EMPTY_STRING).replace('</script>', '<' + '\\/' + 'script>')


def debuggerCodeEditorPrismAssetsPresent() -> bool:
    required = [
        ('StylesheetLibraries', debuggerEmbeddedLibraryId('PRISM_THEME_CSS')),
        ('JavascriptLibraries', debuggerEmbeddedLibraryId('PRISM_CORE_JS')),
        ('JavascriptLibraries', debuggerEmbeddedLibraryId('PRISM_PYTHON_JS')),
    ]
    return all(bool(key) and bool(debuggerEmbeddedBlob(group, key)) for group, key in required)


def debuggerCodeEditorNormalizeLanguage(language_hint: str = EMPTY_STRING, source_text: str = EMPTY_STRING) -> dict:
    raw = str(language_hint or EMPTY_STRING).strip().lower()
    source = str(source_text or EMPTY_STRING)
    extension = raw.rsplit('.', 1)[-1] if '.' in raw else raw
    aliases = {
        'py': ('python', 'Python'),
        'python': ('python', 'Python'),
        'ps1': ('powershell', 'PowerShell'),
        'powershell': ('powershell', 'PowerShell'),
        'php': ('php', 'PHP'),
        'html': ('markup', 'HTML'),
        'xml': ('markup', 'XML'),
        'svg': ('markup', 'SVG'),
        'js': ('javascript', 'JavaScript'),
        'javascript': ('javascript', 'JavaScript'),
        'css': ('css', 'CSS'),
        'txt': ('none', 'Text'),
    }
    if extension in aliases:
        prism_language, label = aliases[extension]
        return {'requested': extension, 'prism': prism_language, 'label': label}
    low_sample = source.lstrip().lower()
    if re.search(r'(^|\n)\s*(def|class|import|from)\s+', source):
        return {'requested': 'python', 'prism': 'python', 'label': 'Python'}
    if re.search(r'(^|\n)\s*(param\s*\(|\$[A-Za-z_][\w]*\s*=|Get-[A-Za-z]|Set-[A-Za-z])', source, re.IGNORECASE):
        return {'requested': 'powershell', 'prism': 'powershell', 'label': 'PowerShell'}
    if '<?php' in low_sample[:120]:
        return {'requested': 'php', 'prism': 'php', 'label': 'PHP'}
    if low_sample.startswith('<?xml'):
        return {'requested': 'xml', 'prism': 'markup', 'label': 'XML'}
    if low_sample.startswith('<!doctype html') or '<html' in low_sample[:500]:
        return {'requested': 'html', 'prism': 'markup', 'label': 'HTML'}
    return {'requested': 'txt', 'prism': 'none', 'label': 'Text'}


def debuggerCodeEditorBaseUrl():
    if not ensureDebuggerQtImports():
        return None
    return QUrl()


def debuggerCodeEditorHtmlShell(title: str, language_label: str, read_only: bool) -> str:
    escaped_title = html.escape(str(title or 'Debugger Code Editor'), quote=False)
    escaped_label = html.escape(str(language_label or 'Code'), quote=False)
    editable_boolean = 'false' if bool(read_only) else 'true'
    prism_css = debuggerEmbeddedBlob('StylesheetLibraries', debuggerEmbeddedLibraryId('PRISM_THEME_CSS'))
    prism_core = debuggerCodeEditorInlineScript(debuggerEmbeddedBlob('JavascriptLibraries', debuggerEmbeddedLibraryId('PRISM_CORE_JS')))
    prism_python = debuggerCodeEditorInlineScript(debuggerEmbeddedBlob('JavascriptLibraries', debuggerEmbeddedLibraryId('PRISM_PYTHON_JS')))
    prism_powershell = debuggerCodeEditorInlineScript(debuggerEmbeddedBlob('JavascriptLibraries', debuggerEmbeddedLibraryId('PRISM_POWERSHELL_JS')))
    prism_php = debuggerCodeEditorInlineScript(debuggerEmbeddedBlob('JavascriptLibraries', debuggerEmbeddedLibraryId('PRISM_PHP_JS')))
    template = '''<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{escaped_title}</title>
<meta http-equiv="Content-Security-Policy" content="default-src 'unsafe-inline' data: blob:; img-src data: file:; style-src 'unsafe-inline' data:; script-src 'unsafe-inline' data:; font-src data: file:;">
<style>{prism_css}</style>
<style>
html, body {{ margin:0; padding:0; width:100%; height:100%; background:#10151c; color:#e8edf2; overflow:hidden; font-family:Consolas, 'Courier New', monospace; }}
body {{ display:flex; flex-direction:column; }}
#wrap {{ display:flex; flex-direction:column; width:100%; height:100%; }}
#header {{ flex:0 0 auto; display:flex; justify-content:space-between; align-items:center; gap:12px; padding:10px 14px; border-bottom:1px solid rgba(255,255,255,.12); background:#141b24; color:#d8e1ea; font-size:13px; }}
#title {{ font-weight:700; }}
#language {{ opacity:.88; font-weight:700; }}
#scroll {{ flex:1 1 auto; overflow:auto; }}
#editor-shell {{ display:flex; min-height:100%; }}
#line-gutter {{ flex:0 0 64px; margin:0; padding:14px 10px 14px 0; text-align:right; color:rgba(230,236,242,.42); background:#0d1218; border-right:1px solid rgba(255,255,255,.08); user-select:none; white-space:pre; line-height:1.45; font-size:13px; }}
#code-wrap {{ flex:1 1 auto; overflow:auto; }}
pre[class*="language-"] {{ margin:0; min-height:100%; border-radius:0; background:#10151c !important; font-size:13px; line-height:1.45; padding:14px; }}
pre.language-none {{ margin:0; min-height:100%; border-radius:0; background:#10151c !important; font-size:13px; line-height:1.45; padding:14px; }}
code[class*="language-"] {{ display:block; min-height:100%; outline:none; white-space:pre; }}
code.language-none {{ display:block; min-height:100%; outline:none; white-space:pre; }}
.token.keyword, .token.atrule, .token.important {{ font-weight:700; }}
.token.tag, .token.selector, .token.function, .token.class-name {{ font-weight:600; }}
.token.comment, .token.prolog, .token.doctype, .token.cdata {{ font-style:italic; }}
</style>
<script>window.Prism = window.Prism || {{}}; window.Prism.manual = true;</script>
<script>{prism_core}</script>
<script>{prism_python}</script>
<script>{prism_powershell}</script>
<script>{prism_php}</script>
<script>
(function() {{
    function codeNode() {{ return document.getElementById('code-source'); }}
    function preNode() {{ return document.getElementById('code-pre'); }}
    function gutterNode() {{ return document.getElementById('line-gutter'); }}
    function labelNode() {{ return document.getElementById('language'); }}
    function updateLineNumbers(textValue) {{
        var text = (textValue || '').toString();
        var count = Math.max(1, text.split('\n').length);
        var rows = [];
        for (var i = 1; i <= count; i += 1) rows.push(String(i));
        var gutter = gutterNode();
        if (gutter) gutter.textContent = rows.join('\n');
    }}
    function languageClass(prismLanguage) {{
        var safeLanguage = (prismLanguage || 'none').toString().trim() || 'none';
        return safeLanguage === 'none' ? 'language-none' : ('language-' + safeLanguage);
    }}
    function reportClientError(prefix, value) {{
        try {{ console.error('[' + prefix + '] ' + String(value || '')); }} catch (_ignored) {{}}
    }}
    window.onerror = function(message, source, lineno, colno, error) {{
        reportClientError('JS', String(message || '') + ' @ ' + String(source || '') + ':' + String(lineno || 0) + ':' + String(colno || 0));
        if (error && error.stack) reportClientError('JS-STACK', error.stack);
        return false;
    }};
    window.addEventListener('unhandledrejection', function(event) {{
        var reason = event && event.reason ? event.reason : 'Unhandled rejection';
        reportClientError('JS-PROMISE', reason && reason.stack ? reason.stack : reason);
    }});
    window.debuggerCodeEditorSetSource = function(text, prismLanguage, languageLabel, editable) {{
        var code = codeNode();
        var pre = preNode();
        if (!code || !pre) return false;
        var cssClass = languageClass(prismLanguage);
        code.className = cssClass;
        pre.className = cssClass;
        code.textContent = (text || '').toString();
        code.setAttribute('contenteditable', editable ? 'true' : 'false');
        var label = labelNode();
        if (label) label.textContent = (languageLabel || prismLanguage || 'Code').toString();
        updateLineNumbers(code.textContent || '');
        if (window.Prism && typeof window.Prism.highlightElement === 'function' && cssClass !== 'language-none') {{
            window.Prism.highlightElement(code);
        }}
        return true;
    }};
    window.debuggerCodeEditorGetSource = function() {{
        var code = codeNode();
        return code ? (code.textContent || '') : '';
    }};
    document.addEventListener('input', function(event) {{
        var code = codeNode();
        if (!code || event.target !== code) return;
        updateLineNumbers(code.textContent || '');
        if (window.Prism && typeof window.Prism.highlightElement === 'function' && code.className !== 'language-none') {{
            window.Prism.highlightElement(code);
        }}
    }});
    document.addEventListener('DOMContentLoaded', function() {{
        window.debuggerCodeEditorSetSource('', 'none', 'Text', {editable_boolean});
    }});
}})();
</script>
</head>
<body>
<div id="wrap">
  <div id="header"><div id="title">{escaped_title}</div><div id="language">{escaped_label}</div></div>
  <div id="scroll"><div id="editor-shell"><pre id="line-gutter">1</pre><div id="code-wrap"><pre id="code-pre" class="language-none"><code id="code-source" class="language-none" contenteditable="{editable_boolean}" spellcheck="false"></code></pre></div></div></div>
</div>
</body>
</html>'''
    return template.format(escaped_title=escaped_title, escaped_label=escaped_label, prism_css=prism_css, prism_core=prism_core, prism_python=prism_python, prism_powershell=prism_powershell, prism_php=prism_php, editable_boolean=editable_boolean)


def debuggerWebMessageKind(message: str = EMPTY_STRING, source_id: str = EMPTY_STRING) -> str:
    text = str(message or EMPTY_STRING).strip().lower()
    source = str(source_id or EMPTY_STRING).strip().lower()
    payload = f'{source} {text}'
    if any(token in payload for token in ('css', '.css')):
        return 'CSS'
    if any(token in payload for token in ('javascript', '.js', ' uncaught ', 'syntaxerror', 'referenceerror', 'typeerror', '[js', '[js-stack', '[js-promise')):
        return 'JS'
    if any(token in payload for token in ('<svg', ' svg ', '.svg')):
        return 'SVG'
    if any(token in payload for token in ('xml', '.xml')):
        return 'XML'
    if any(token in payload for token in ('html', '.html', '<!doctype', '<body', '<div')):
        return 'HTML'
    return 'WEB'


# Do not import/open optional Qt debugger objects during module import.
# The real Prompt GUI child owns its own Qt startup; debugger Qt helpers are lazy.
DebuggerWebEnginePageBase = cast(type, object)
DebuggerWebEngineViewBase = cast(type, object)
DebuggerDialogBase = cast(type, object)


class DebuggerWebEnginePage(DebuggerWebEnginePageBase):
    def __init__(self, debugger, parent=None):
        if ensureDebuggerQtImports() and QWebEnginePage is not None:
            super().__init__(parent)
        self.debugger = debugger  # noqa: nonconform
        for signal_name, handler_name in (
            ('loadFinished', '_onLoadFinished'),
            ('renderProcessTerminated', '_onRenderProcessTerminated'),
            ('loadingChanged', '_onLoadingChanged'),
        ):
            try:
                getattr(self, signal_name).connect(getattr(self, handler_name))
            except Exception as error:
                captureException(None, source='start.py', context='except@6186')
                print(f"[WARN:swallowed-exception] start.py:4965 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass

    def report(self, kind: str, message: str, detail: str = EMPTY_STRING, source_id: str = EMPTY_STRING, line_number: int | None = None):
        text = f'[WEBENGINE:{str(kind or "WEB").upper()}] {str(message or EMPTY_STRING)}'
        if source_id:
            text += f'  source={source_id}'
        if line_number is not None:
            text += f'  line={int(line_number or 0)}'
        if detail:
            text += f'  {detail}'
        try:
            print(text, file=sys.stderr, flush=True)
        except Exception as error:
            captureException(None, source='start.py', context='except@6200')
            print(f"[WARN:swallowed-exception] start.py:4978 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        try:
            if self.debugger is not None:
                self.debugger.emit(text, getattr(self.debugger, 'exceptionPath', None))
        except Exception as error:
            captureException(None, source='start.py', context='except@6206')
            print(f"[WARN:swallowed-exception] start.py:4983 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass

    def javaScriptConsoleMessage(self, level, message, lineNumber, sourceID):
        self.report(debuggerWebMessageKind(message, sourceID), str(message or EMPTY_STRING), source_id=str(sourceID or EMPTY_STRING), line_number=int(lineNumber or 0))
        try:
            return super().javaScriptConsoleMessage(level, message, lineNumber, sourceID)
        except Exception as error:
            captureException(None, source='start.py', context='except@6214')
            print(f"[WARN:swallowed-exception] start.py:4990 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return None

    def _onLoadFinished(self, ok):
        if not bool(ok):
            self.report('LOAD', 'load finished false')

    def _onLoadingChanged(self, loading_info):
        try:
            error_string = str(getattr(loading_info, 'errorString', lambda: EMPTY_STRING)() or EMPTY_STRING)
            status = str(getattr(loading_info, 'status', lambda: EMPTY_STRING)() or EMPTY_STRING)
            if error_string:
                self.report('LOAD', error_string, detail=f'status={status}')
        except Exception as error:
            captureException(None, source='start.py', context='except@6228')
            print(f"[WARN:swallowed-exception] start.py:5003 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            self.report('LOAD', f'loadingChanged failure: {type(error).__name__}: {error}')

    def _onRenderProcessTerminated(self, termination_status, exit_code):
        self.report('RENDER', 'render process terminated', detail=f'status={termination_status} exitCode={int(exit_code or 0)}')


class DebuggerWebEngineView(DebuggerWebEngineViewBase):
    def __init__(self, debugger, parent=None):
        if ensureDebuggerQtImports() and QWebEngineView is not None:
            super().__init__(parent)
        self.debugger = debugger  # noqa: nonconform
        self.debuggerPage = DebuggerWebEnginePage(debugger=debugger, parent=self)  # noqa: nonconform
        try:
            self.setPage(self.debuggerPage)
        except Exception as error:
            captureException(None, source='start.py', context='except@6244')
            print(f"[WARN:swallowed-exception] start.py:5018 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass

    def contextMenuEvent(self, event):
        try:
            menu = self.createStandardContextMenu()
            if menu is None:
                return
            menu.addSeparator()
            view_source_action = QAction(Localization.text('action.view_source'), menu)
            view_source_action.triggered.connect(self.showViewSource)
            menu.addAction(view_source_action)
            Lifecycle.runQtBlockingCall(menu, event.globalPos(), phase_name='start.source.context-menu')
            return
        except Exception as error:
            captureException(None, source='start.py', context='except@6259')
            try:
                print(f'[WEBENGINE:MENU] {type(error).__name__}: {error}', file=sys.stderr, flush=True)
            except Exception as error:
                captureException(None, source='start.py', context='except@6262')
                print(f"[WARN:swallowed-exception] start.py:5035 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
        try:
            return super().contextMenuEvent(event)
        except Exception as error:
            captureException(None, source='start.py', context='except@6267')
            print(f"[WARN:swallowed-exception] start.py:5039 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return None

    def showViewSource(self):
        page = getattr(self, 'page', lambda: None)()
        if page is None or not hasattr(page, 'toHtml'):
            return
        def _opened(source_text: str):
            dialog = DebuggerCodeDialog(debugger=self.debugger, title=Localization.text('action.view_source').replace('📄 ', ''), sourceText=str(source_text or EMPTY_STRING), languageHint='html', readOnly=True)
            Lifecycle.runQtBlockingCall(dialog, phase_name='start.source-dialog')
        try:
            page.toHtml(_opened)
        except Exception as error:
            captureException(None, source='start.py', context='except@6280')
            try:
                print(f'[WEBENGINE:VIEW-SOURCE] {type(error).__name__}: {error}', file=sys.stderr, flush=True)
            except Exception as error:
                captureException(None, source='start.py', context='except@6283')
                print(f"[WARN:swallowed-exception] start.py:5054 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass


class DebuggerCodeDialog(DebuggerDialogBase):
    def __init__(self, debugger, title: str = EMPTY_STRING, sourceText: str = EMPTY_STRING, languageHint: str = EMPTY_STRING, readOnly: bool = True, submitLabel: str = 'Submit', showSubmit: bool = False, intervalSeconds: float | None = None, showInterval: bool = False):
        if not ensureDebuggerQtImports():
            raise RuntimeError(debuggerQtUnavailableText())
        super().__init__(None)
        self.debugger = debugger  # noqa: nonconform
        self.editorTitle = str(title or 'Code Editor').strip() or 'Code Editor'  # noqa: nonconform
        self.languageHint = str(languageHint or EMPTY_STRING).strip()  # noqa: nonconform
        self.readOnly = bool(readOnly)  # noqa: nonconform
        self.lastPayload = debuggerCodeEditorNormalizeLanguage(self.languageHint, sourceText)  # noqa: nonconform
        self.lastKnownText = str(sourceText or EMPTY_STRING)  # noqa: nonconform
        self.browser = None
        self.editor = None
        self.intervalSpin = None
        self.showSubmit = bool(showSubmit)  # noqa: nonconform
        self.setModal(True)
        self.setWindowTitle(self.editorTitle)
        try:
            self.resize(1120, 760)
        except Exception as error:
            captureException(None, source='start.py', context='except@6307')
            print(f"[WARN:swallowed-exception] start.py:5077 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)
        if showInterval:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)
            row.addWidget(QLabel(Localization.text('label.interval_seconds'), self))
            self.intervalSpin = QDoubleSpinBox(self)  # noqa: nonconform
            self.intervalSpin.setDecimals(2)
            self.intervalSpin.setSingleStep(0.05)
            self.intervalSpin.setMinimum(0.05)
            self.intervalSpin.setMaximum(86400.0)
            self.intervalSpin.setValue(max(0.05, float(intervalSeconds or 1.0)))
            row.addWidget(self.intervalSpin)
            row.addStretch(1)
            root.addLayout(row)
        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 0, 0, 0)
        button_row.setSpacing(8)
        print_button = QPushButton(Localization.text('button.print'), self)
        save_button = QPushButton(Localization.text('button.save'), self)
        pdf_button = QPushButton(Localization.text('button.save_pdf'), self)
        print_button.clicked.connect(self.printDocument)
        save_button.clicked.connect(self.saveDocument)
        pdf_button.clicked.connect(self.saveDocumentAsPdf)
        button_row.addWidget(print_button)
        button_row.addWidget(save_button)
        button_row.addWidget(pdf_button)
        button_row.addStretch(1)
        if self.showSubmit:
            submit_button = QPushButton(str(submitLabel or 'Submit'), self)
            submit_button.clicked.connect(self.accept)
            button_row.addWidget(submit_button)
            cancel_button = QPushButton(Localization.text('button.cancel'), self)
            cancel_button.clicked.connect(self.reject)
            button_row.addWidget(cancel_button)
        else:
            close_button = QPushButton(Localization.text('button.close'), self)
            close_button.clicked.connect(self.accept)
            button_row.addWidget(close_button)
        root.addLayout(button_row)
        use_web = bool(debuggerCodeEditorPrismAssetsPresent())
        if use_web:
            self.browser = DebuggerWebEngineView(debugger=debugger, parent=self)  # noqa: nonconform
            root.addWidget(self.browser, 1)
            html_shell = debuggerCodeEditorHtmlShell(self.editorTitle, str(self.lastPayload.get('label', 'Code') or 'Code'), self.readOnly)
            try:
                self.browser.setHtml(html_shell, debuggerCodeEditorBaseUrl())
            except TypeError:
                captureException(None, source='start.py', context='except@6359')
                self.browser.setHtml(html_shell)
            try:
                self.browser.loadFinished.connect(self._onBrowserLoadFinished)
            except Exception as error:
                captureException(None, source='start.py', context='except@6363')
                print(f"[WARN:swallowed-exception] start.py:5132 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
        else:
            self.editor = QPlainTextEdit(self)  # noqa: nonconform
            self.editor.setReadOnly(self.readOnly)
            try:
                self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            except Exception as error:
                captureException(None, source='start.py', context='except@6371')
                print(f"[WARN:swallowed-exception] start.py:5139 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
            try:
                self.editor.setFont(QFont('Courier New', 10))
            except Exception as error:
                captureException(None, source='start.py', context='except@6376')
                print(f"[WARN:swallowed-exception] start.py:5143 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
            self.editor.setPlainText(self.lastKnownText)
            root.addWidget(self.editor, 1)

    def _onBrowserLoadFinished(self, ok):
        if not bool(ok) or self.browser is None:
            return
        payload = dict(self.lastPayload or {})
        script = 'window.debuggerCodeEditorSetSource({text}, {prism}, {label}, {editable});'.format(
            text=json.dumps(str(self.lastKnownText or EMPTY_STRING)),
            prism=json.dumps(str(payload.get('prism', 'none') or 'none')),
            label=json.dumps(str(payload.get('label', 'Code') or 'Code')),
            editable='false' if bool(self.readOnly) else 'true',
        )
        try:
            self.browser.page().runJavaScript(script)
        except Exception as error:
            captureException(None, source='start.py', context='except@6394')
            print(f"[WARN:swallowed-exception] start.py:5160 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass

    def currentSourceText(self) -> str:
        if self.editor is not None:
            return str(self.editor.toPlainText() or EMPTY_STRING)
        page = self.browser.page() if self.browser is not None and hasattr(self.browser, 'page') else None
        if page is None or not hasattr(page, 'runJavaScript'):
            return str(self.lastKnownText or EMPTY_STRING)
        result = {'text': str(self.lastKnownText or EMPTY_STRING)}
        loop = QEventLoop()
        def _done(value):
            result['text'] = str(value or EMPTY_STRING)
            try:
                loop.quit()
            except Exception as error:
                captureException(None, source='start.py', context='except@6410')
                print(f"[WARN:swallowed-exception] start.py:5175 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
        try:
            page.runJavaScript('window.debuggerCodeEditorGetSource();', _done)
            timeout_timer = QTimer(self)
            timeout_timer.setSingleShot(True)
            timeout_timer.setInterval(1500)
            timeout_timer.timeout.connect(loop.quit)
            timeout_timer.start()
            Lifecycle.runQtBlockingCall(loop, phase_name='start.qt-event-loop')
            try:
                timeout_timer.stop()
                timeout_timer.deleteLater()
            except Exception as error:
                captureException(None, source='start.py', context='except@6424')
                print(f"[WARN:swallowed-exception] start.py:5188 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
        except Exception as error:
            captureException(None, source='start.py', context='except@6427')
            print(f"[WARN:swallowed-exception] start.py:5190 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return str(self.lastKnownText or EMPTY_STRING)
        self.lastKnownText = str(result.get('text', self.lastKnownText) or EMPTY_STRING)
        return self.lastKnownText

    def intervalValue(self) -> float | None:
        if self.intervalSpin is None:
            return None
        try:
            return float(self.intervalSpin.value())
        except Exception as error:
            captureException(None, source='start.py', context='except@6438')
            print(f"[WARN:swallowed-exception] start.py:5200 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return None

    def defaultStem(self) -> str:
        stem = re.sub(r'[^A-Za-z0-9._-]+', '_', str(self.editorTitle or 'debugger_code')).strip('._')
        return stem or 'debugger_code'

    def suggestedSourcePath(self) -> Path:
        key = str((self.lastPayload or {}).get('requested', 'txt') or 'txt').lower()
        suffix = {'python': '.py', 'powershell': '.ps1', 'php': '.php', 'html': '.html', 'xml': '.xml', 'svg': '.svg', 'javascript': '.js', 'css': '.css'}.get(key, '.txt')
        return Path.cwd() / f'{self.defaultStem()}{suffix}'

    def saveDocument(self):
        chosen, _ = QFileDialog.getSaveFileName(self, 'Save', str(self.suggestedSourcePath()), 'All Files (*.*)')
        if not chosen:
            return
        try:
            File.writeText(Path(chosen), self.currentSourceText(), encoding='utf-8')
        except Exception as error:
            captureException(None, source='start.py', context='except@6457')
            print(f"[WARN:swallowed-exception] start.py:5218 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            if self.debugger is not None:
                self.debugger.emit(f'[PromptDebugger] Save failed: {type(error).__name__}: {error}', getattr(self.debugger, 'exceptionPath', None))

    def saveDocumentAsPdf(self):
        page = self.browser.page() if self.browser is not None and hasattr(self.browser, 'page') else None
        if page is None or not hasattr(page, 'printToPdf'):
            return
        chosen, _ = QFileDialog.getSaveFileName(self, 'Save PDF', str(Path.cwd() / f'{self.defaultStem()}.pdf'), 'PDF Files (*.pdf)')
        if not chosen:
            return
        try:
            page.printToPdf(str(chosen))
        except Exception as error:
            captureException(None, source='start.py', context='except@6471')
            print(f"[WARN:swallowed-exception] start.py:5231 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            if self.debugger is not None:
                self.debugger.emit(f'[PromptDebugger] Save PDF failed: {type(error).__name__}: {error}', getattr(self.debugger, 'exceptionPath', None))

    def printDocument(self):
        page = self.browser.page() if self.browser is not None and hasattr(self.browser, 'page') else None
        if page is None or not hasattr(self.browser, 'print'):
            return
        try:
            printer = QPrinter(QPrinter.PrinterMode.HighResolution)
            dialog = QPrintDialog(printer, self)
            if Lifecycle.runQtBlockingCall(dialog, phase_name='start.print-dialog') != QDialog.DialogCode.Accepted:
                return
            cast(Any, self.browser).print(printer)
        except Exception as error:
            captureException(None, source='start.py', context='except@6486')
            print(f"[WARN:swallowed-exception] start.py:5245 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            if self.debugger is not None:
                self.debugger.emit(f'[PromptDebugger] Print failed: {type(error).__name__}: {error}', getattr(self.debugger, 'exceptionPath', None))




class GitAuthDialog(DebuggerDialogBase):
    def __init__(self, debugger=None, parent=None, repoRoot: str | Path = BASE_DIR):
        if not ensureDebuggerQtImports():
            raise RuntimeError(debuggerQtUnavailableText())
        super().__init__(parent)
        self.debugger = debugger  # noqa: nonconform
        self.repoRoot = Path(repoRoot or BASE_DIR)  # noqa: nonconform
        self.setModal(True)
        self.setWindowTitle(Localization.text('dialog.github_auth'))
        try:
            self.resize(560, 320)
        except Exception as error:
            captureException(None, source='start.py', context='except@6505')
            print(f"[WARN:swallowed-exception] start.py:5263 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)
        info = QLabel(Localization.text('github.auth_info'), self)
        try:
            info.setWordWrap(True)
        except Exception as error:
            captureException(None, source='start.py', context='except@6514')
            print(f"[WARN:swallowed-exception] start.py:5271 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        root.addWidget(info)
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(8)
        self.authorNameEdit = QLineEdit(self)  # noqa: nonconform
        self.authorEmailEdit = QLineEdit(self)  # noqa: nonconform
        self.usernameEdit = QLineEdit(self)  # noqa: nonconform
        self.passwordEdit = QLineEdit(self)  # noqa: nonconform
        self.passwordEdit.setEchoMode(QLineEdit.EchoMode.Password)
        self.hostEdit = QLineEdit(self)  # noqa: nonconform
        self.hostEdit.setText(Localization.text('github.default_host'))
        self.remoteEdit = QLineEdit(self)  # noqa: nonconform
        self.remoteEdit.setText(Localization.text('github.default_remote'))
        self.setupGitCheck = QCheckBox(Localization.text('github.setup_git'), self)  # noqa: nonconform
        self.setupGitCheck.setChecked(True)
        existing_name = debuggerGitConfigValue('user.name', repo_root=self.repoRoot)
        existing_email = debuggerGitConfigValue('user.email', repo_root=self.repoRoot)
        if existing_name:
            self.authorNameEdit.setText(existing_name)
        if existing_email:
            self.authorEmailEdit.setText(existing_email)
        form.addRow(Localization.text('github.author_name'), self.authorNameEdit)
        form.addRow(Localization.text('github.author_email'), self.authorEmailEdit)
        form.addRow(Localization.text('github.username'), self.usernameEdit)
        form.addRow(Localization.text('github.password_token'), self.passwordEdit)
        form.addRow(Localization.text('github.host'), self.hostEdit)
        form.addRow(Localization.text('github.remote'), self.remoteEdit)
        root.addLayout(form)
        root.addWidget(self.setupGitCheck)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        submit = QPushButton(Localization.text('button.save_auth'), self)
        cancel = QPushButton(Localization.text('button.cancel'), self)
        submit.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(submit)
        buttons.addWidget(cancel)
        root.addLayout(buttons)

    def payload(self) -> dict[str, Any]:
        return {
            'author_name': str(self.authorNameEdit.text() or EMPTY_STRING).strip(),
            'author_email': str(self.authorEmailEdit.text() or EMPTY_STRING).strip(),
            'username': str(self.usernameEdit.text() or EMPTY_STRING).strip(),
            'password': str(self.passwordEdit.text() or EMPTY_STRING),
            'host': str(self.hostEdit.text() or 'github.com').strip() or 'github.com',
            'remote': str(self.remoteEdit.text() or 'origin').strip() or 'origin',
            'setup_git': bool(self.setupGitCheck.isChecked()),
        }


def debuggerAstNodeLabel(node, field_name: str = EMPTY_STRING, list_index: int | None = None) -> str:
    prefix = EMPTY_STRING
    if field_name:
        prefix = f'{field_name}: '
    if list_index is not None:
        prefix += f'[{int(list_index)}] '
    node_type = type(node).__name__
    details = []
    try:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            details.append(str(getattr(node, 'name', EMPTY_STRING) or EMPTY_STRING))
        elif isinstance(node, ast.Name):
            details.append(str(getattr(node, 'id', EMPTY_STRING) or EMPTY_STRING))
        elif isinstance(node, ast.arg):
            details.append(str(getattr(node, 'arg', EMPTY_STRING) or EMPTY_STRING))
        elif isinstance(node, ast.Attribute):
            details.append(str(getattr(node, 'attr', EMPTY_STRING) or EMPTY_STRING))
        elif isinstance(node, ast.Constant):
            details.append(safeRepr(getattr(node, 'value', None), limit=48))
    except Exception as error:
        captureException(None, source='start.py', context='except@6587')
        print(f"[WARN:swallowed-exception] start.py:5343 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        pass
    start = int(getattr(node, 'lineno', 0) or 0)
    end = int(getattr(node, 'end_lineno', start) or start)
    line_text = EMPTY_STRING
    if start > 0:
        line_text = f' lines {start}-{end}'
    detail_text = f" {' '.join(part for part in details if part)}" if any(details) else EMPTY_STRING
    return f'{prefix}{node_type}{detail_text}{line_text}'.strip()


def debuggerAstNodeToPython(node, source_text: str) -> str:
    try:
        if isinstance(node, ast.Module):
            return str(source_text or EMPTY_STRING)
        segment = ast.get_source_segment(str(source_text or EMPTY_STRING), node)
        if segment:
            return str(segment)
    except Exception as error:
        captureException(None, source='start.py', context='except@6606')
        print(f"[WARN:swallowed-exception] start.py:5361 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        pass
    try:
        return ast.unparse(node) + "\n"
    except Exception as error:
        captureException(None, source='start.py', context='except@6611')
        print(f"[WARN:swallowed-exception] start.py:5365 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        return ast.dump(node, indent=2, include_attributes=True) + "\n"
def debuggerAstNodeToXmlElement(node):
    import xml.etree.ElementTree as ET

    def decorate(element, ast_node) -> None:
        for name in ('lineno', 'end_lineno', 'col_offset', 'end_col_offset'):
            try:
                value = getattr(ast_node, name, None)
            except Exception as error:
                captureException(None, source='start.py', context='except@6620')
                print(f"[WARN:swallowed-exception] start.py:5373 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                value = None
            if value not in (None, EMPTY_STRING):
                element.set(name, str(value))

    root = ET.Element(type(node).__name__)
    decorate(root, node)
    stack: list[tuple[object, object]] = [(node, root)]
    while stack:
        current_node, current_element = stack.pop()
        child_pairs: list[tuple[object, object]] = []
        for field_name, value in ast.iter_fields(current_node):
            if isinstance(value, ast.AST):
                child = ET.SubElement(current_element, type(value).__name__)
                child.set('field', str(field_name))
                decorate(child, value)
                child_pairs.append((value, child))
            elif isinstance(value, list):
                list_element = ET.SubElement(current_element, 'List', field=str(field_name))
                for index, item in enumerate(value):
                    if isinstance(item, ast.AST):
                        child = ET.SubElement(list_element, type(item).__name__)
                        child.set('index', str(index))
                        decorate(child, item)
                        child_pairs.append((item, child))
                    else:
                        ET.SubElement(list_element, 'Item', index=str(index), value=safeRepr(item, limit=120))
            else:
                ET.SubElement(current_element, 'Field', name=str(field_name), value=safeRepr(value, limit=240))
        stack.extend(reversed(child_pairs))
    return root


def debuggerAstNodeToXml(node) -> str:
    import xml.etree.ElementTree as ET
    root = debuggerAstNodeToXmlElement(node)
    try:
        ET.indent(root)
    except Exception as error:
        captureException(None, source='start.py', context='except@6649')
        print(f"[WARN:swallowed-exception] start.py:5401 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        pass
    return ET.tostring(root, encoding='unicode')


def debuggerAstNodeToHtml(node, source_text: str, title: str = EMPTY_STRING) -> str:
    xml_text = debuggerAstNodeToXml(node)
    python_text = debuggerAstNodeToPython(node, source_text)
    title_text = str(title or debuggerAstNodeLabel(node) or 'AST Node')
    return """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
body {{ font-family: Segoe UI, Arial, sans-serif; margin: 16px; background:#111; color:#eee; }}
h1 {{ font-size: 18px; margin: 0 0 12px 0; }}
h2 {{ font-size: 14px; margin: 18px 0 8px 0; color:#9fd3ff; }}
pre {{ white-space: pre-wrap; word-break: break-word; background:#1c1c1c; border:1px solid #333; border-radius:8px; padding:12px; }}
</style>
</head>
<body>
<h1>{title}</h1>
<h2>Python</h2>
<pre>{python}</pre>
<h2>XML</h2>
<pre>{xml}</pre>
</body>
</html>
""".format(title=html.escape(title_text), python=html.escape(python_text), xml=html.escape(xml_text))


class DebuggerAstDialog(DebuggerDialogBase):
    def __init__(self, debugger, filePath: str, sourceText: str, focusLine: int = 0, astText: str = EMPTY_STRING):
        if not ensureDebuggerQtImports():
            raise RuntimeError(debuggerQtUnavailableText())
        super().__init__(None)
        self.debugger = debugger  # noqa: nonconform
        self.filePath = str(filePath or EMPTY_STRING)  # noqa: nonconform
        self.sourceText = str(sourceText or EMPTY_STRING)  # noqa: nonconform
        self.focusLine = int(focusLine or 0)  # noqa: nonconform
        self.astText = str(astText or EMPTY_STRING)  # noqa: nonconform
        self.browser = None
        self.tree = None
        try:
            self.treeData = ast.parse(self.sourceText or EMPTY_STRING, filename=self.filePath or EMPTY_STRING)
            self.parseErrorText = EMPTY_STRING
        except Exception as error:
            captureException(None, source='start.py', context='except@6697')
            print(f"[WARN:swallowed-exception] start.py:5448 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            self.treeData = None  # noqa: nonconform
            self.parseErrorText = f'# AST parse failed: {type(error).__name__}: {error}\n'  # noqa: nonconform
        self.setModal(True)
        self.setWindowTitle(f'Generate AST Tree — {Path(self.filePath).name}')
        try:
            self.resize(1220, 820)
        except Exception as error:
            captureException(None, source='start.py', context='except@6705')
            print(f"[WARN:swallowed-exception] start.py:5455 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)
        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 0, 0, 0)
        button_row.setSpacing(8)
        print_button = QPushButton(Localization.text('button.print'), self)
        save_button = QPushButton(Localization.text('button.save'), self)
        pdf_button = QPushButton(Localization.text('button.save_pdf'), self)
        close_button = QPushButton(Localization.text('button.close'), self)
        print_button.clicked.connect(self.printCurrentTab)
        save_button.clicked.connect(self.saveCurrentTab)
        pdf_button.clicked.connect(self.saveCurrentTabAsPdf)
        close_button.clicked.connect(self.accept)
        button_row.addWidget(print_button)
        button_row.addWidget(save_button)
        button_row.addWidget(pdf_button)
        button_row.addStretch(1)
        button_row.addWidget(close_button)
        root.addLayout(button_row)
        self.tabs = QTabWidget(self)  # noqa: nonconform
        root.addWidget(self.tabs, 1)
        self.tree = QTreeWidget(self)  # noqa: nonconform
        self.tree.setHeaderLabels(['AST Node'])
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.openTreeContextMenu)
        try:
            self.tree.itemDoubleClicked.connect(self.expandTreeBranch)
        except Exception as error:
            captureException(None, source='start.py', context='except@6736')
            print(f"[WARN:swallowed-exception] start.py:5485 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        self.tabs.addTab(self.tree, 'AST Tree')
        self.browser = DebuggerWebEngineView(debugger=debugger, parent=self)  # noqa: nonconform
        html_shell = debuggerCodeEditorHtmlShell('AST Source', 'Python', True)
        try:
            self.browser.setHtml(html_shell, debuggerCodeEditorBaseUrl())
        except TypeError:
            captureException(None, source='start.py', context='except@6744')
            self.browser.setHtml(html_shell)
        try:
            self.browser.loadFinished.connect(self._onBrowserLoadFinished)
        except Exception as error:
            captureException(None, source='start.py', context='except@6748')
            print(f"[WARN:swallowed-exception] start.py:5496 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        self.tabs.addTab(self.browser, 'AST Source')
        self.buildTreeUi()
        try:
            self.tabs.setCurrentIndex(0)
        except Exception as error:
            captureException(None, source='start.py', context='except@6755')
            print(f"[WARN:swallowed-exception] start.py:5502 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass

    def _onBrowserLoadFinished(self, ok):
        if not bool(ok) or self.browser is None:
            return
        script = 'window.debuggerCodeEditorSetSource({text}, {prism}, {label}, false);'.format(
            text=json.dumps(str(self.astText or self.parseErrorText or EMPTY_STRING)),
            prism=json.dumps('python'),
            label=json.dumps('Python'),
        )
        try:
            self.browser.page().runJavaScript(script)
        except Exception as error:
            captureException(None, source='start.py', context='except@6769')
            print(f"[WARN:swallowed-exception] start.py:5515 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass

    def buildTreeUi(self):
        cast(Any, self.tree).clear()
        if self.treeData is None:
            item = QTreeWidgetItem([str(self.parseErrorText or '# AST unavailable')])
            cast(Any, self.tree).addTopLevelItem(item)
            return
        root_item = self.makeAstItem(self.treeData, field_name='root')
        cast(Any, self.tree).addTopLevelItem(root_item)
        try:
            cast(Any, self.tree).expandToDepth(2)
        except Exception as error:
            captureException(None, source='start.py', context='except@6783')
            print(f"[WARN:swallowed-exception] start.py:5528 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        if self.focusLine > 0:
            self.selectFocusedNode(root_item)

    def makeAstItem(self, node, field_name: str = EMPTY_STRING, list_index: int | None = None):  # norecurse: intentional finite AST tree item construction
        item = QTreeWidgetItem([debuggerAstNodeLabel(node, field_name=field_name, list_index=list_index)])
        item.setData(0, Qt.ItemDataRole.UserRole, node)
        start = int(getattr(node, 'lineno', 0) or 0)
        end = int(getattr(node, 'end_lineno', start) or start)
        if self.focusLine and start and start <= self.focusLine <= end:
            try:
                item.setForeground(0, QBrush(QColor('#7fd4ff')))
            except Exception as error:
                captureException(None, source='start.py', context='except@6797')
                print(f"[WARN:swallowed-exception] start.py:5541 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
        for field_name_value, value in ast.iter_fields(node):
            if isinstance(value, ast.AST):
                item.addChild(self.makeAstItem(value, field_name=field_name_value))
            elif isinstance(value, list):
                list_item = QTreeWidgetItem([f'{field_name_value} [{len(value)}]'])
                for index, child in enumerate(value):
                    if isinstance(child, ast.AST):
                        list_item.addChild(self.makeAstItem(child, field_name=field_name_value, list_index=index))
                    else:
                        list_item.addChild(QTreeWidgetItem([f'[{index}] = {safeRepr(child, limit=120)}']))
                item.addChild(list_item)
            else:
                item.addChild(QTreeWidgetItem([f'{field_name_value} = {safeRepr(value, limit=120)}']))
        return item

    def selectFocusedNode(self, root_item):
        stack = [root_item]
        while stack:
            item = stack.pop()
            node = item.data(0, Qt.ItemDataRole.UserRole)
            if node is not None:
                start = int(getattr(node, 'lineno', 0) or 0)
                end = int(getattr(node, 'end_lineno', start) or start)
                if start and start <= self.focusLine <= end:
                    try:
                        cast(Any, self.tree).setCurrentItem(item)
                        item.setExpanded(True)
                    except Exception as error:
                        captureException(None, source='start.py', context='except@6827')
                        print(f"[WARN:swallowed-exception] start.py:5570 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                        pass
                    return
            for index in range(item.childCount() - 1, -1, -1):
                stack.append(item.child(index))

    def expandTreeBranch(self, item, _column):
        try:
            item.setExpanded(not item.isExpanded())
        except Exception as error:
            captureException(None, source='start.py', context='except@6837')
            print(f"[WARN:swallowed-exception] start.py:5579 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass

    def currentSourceText(self) -> str:
        return str(self.astText or self.parseErrorText or EMPTY_STRING)

    def defaultStem(self) -> str:
        stem = re.sub(r'[^A-Za-z0-9._-]+', '_', Path(self.filePath or 'ast_tree').stem).strip('._')
        return stem or 'ast_tree'

    def saveCurrentTab(self):
        if self.tabs.currentIndex() == 0:
            chosen, _ = QFileDialog.getSaveFileName(self, 'Save AST Tree', str(Path.cwd() / f'{self.defaultStem()}_tree.txt'), 'Text Files (*.txt);;All Files (*.*)')
            if not chosen:
                return
            try:
                File.writeText(Path(chosen), self.currentSourceText(), encoding='utf-8')
            except Exception as error:
                captureException(None, source='start.py', context='except@6855')
                print(f"[WARN:swallowed-exception] start.py:5596 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                if self.debugger is not None:
                    self.debugger.emit(f'[PromptDebugger] Save AST tree failed: {type(error).__name__}: {error}', getattr(self.debugger, 'exceptionPath', None))
            return
        chosen, _ = QFileDialog.getSaveFileName(self, 'Save AST Source', str(Path.cwd() / f'{self.defaultStem()}_ast.py'), 'Python Files (*.py);;Text Files (*.txt);;All Files (*.*)')
        if not chosen:
            return
        try:
            File.writeText(Path(chosen), self.currentSourceText(), encoding='utf-8')
        except Exception as error:
            captureException(None, source='start.py', context='except@6865')
            print(f"[WARN:swallowed-exception] start.py:5605 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            if self.debugger is not None:
                self.debugger.emit(f'[PromptDebugger] Save AST source failed: {type(error).__name__}: {error}', getattr(self.debugger, 'exceptionPath', None))

    def saveCurrentTabAsPdf(self):
        page = self.browser.page() if self.browser is not None and hasattr(self.browser, 'page') else None
        if page is None or not hasattr(page, 'printToPdf'):
            return
        chosen, _ = QFileDialog.getSaveFileName(self, 'Save PDF', str(Path.cwd() / f'{self.defaultStem()}_ast.pdf'), 'PDF Files (*.pdf)')
        if not chosen:
            return
        try:
            page.printToPdf(str(chosen))
        except Exception as error:
            captureException(None, source='start.py', context='except@6879')
            print(f"[WARN:swallowed-exception] start.py:5618 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            if self.debugger is not None:
                self.debugger.emit(f'[PromptDebugger] Save PDF failed: {type(error).__name__}: {error}', getattr(self.debugger, 'exceptionPath', None))

    def printCurrentTab(self):
        page = self.browser.page() if self.browser is not None and hasattr(self.browser, 'page') else None
        if page is None or not hasattr(self.browser, 'print'):
            return
        try:
            printer = QPrinter(QPrinter.PrinterMode.HighResolution)
            dialog = QPrintDialog(printer, self)
            if Lifecycle.runQtBlockingCall(dialog, phase_name='start.print-dialog') != QDialog.DialogCode.Accepted:
                return
            cast(Any, self.browser).print(printer)
        except Exception as error:
            captureException(None, source='start.py', context='except@6894')
            print(f"[WARN:swallowed-exception] start.py:5632 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            if self.debugger is not None:
                self.debugger.emit(f'[PromptDebugger] Print failed: {type(error).__name__}: {error}', getattr(self.debugger, 'exceptionPath', None))

    def openTreeContextMenu(self, point):
        item = self.tree.itemAt(point) if self.tree is not None else None
        if item is None:
            return
        node = item.data(0, Qt.ItemDataRole.UserRole)
        if node is None:
            return
        menu = QMenu(self)
        save_python = QAction(Localization.text('action.save_node_python'), menu)
        save_html = QAction(Localization.text('action.save_node_html'), menu)
        save_xml = QAction(Localization.text('action.save_node_xml'), menu)
        save_python.triggered.connect(lambda: self.saveAstNode(node, 'python', str(item.text(0) or 'AST Node')))
        save_html.triggered.connect(lambda: self.saveAstNode(node, 'html', str(item.text(0) or 'AST Node')))
        save_xml.triggered.connect(lambda: self.saveAstNode(node, 'xml', str(item.text(0) or 'AST Node')))
        menu.addAction(save_python)
        menu.addAction(save_html)
        menu.addAction(save_xml)
        Lifecycle.runQtBlockingCall(menu, cast(Any, self.tree).viewport().mapToGlobal(point), phase_name='start.ast-tree.context-menu')

    def saveAstNode(self, node, target_format: str, title_text: str):
        format_key = str(target_format or 'python').strip().lower()
        if format_key == 'python':
            suffix = '.py'
            payload = debuggerAstNodeToPython(node, self.sourceText)
            filters = 'Python Files (*.py);;All Files (*.*)'
        elif format_key == 'html':
            suffix = '.html'
            payload = debuggerAstNodeToHtml(node, self.sourceText, title=str(title_text or 'AST Node'))
            filters = 'HTML Files (*.html);;All Files (*.*)'
        else:
            suffix = '.xml'
            payload = debuggerAstNodeToXml(node)
            filters = 'XML Files (*.xml);;All Files (*.*)'
        safe_title = re.sub(r'[^A-Za-z0-9._-]+', '_', str(title_text or 'node')).strip('._') or 'node'
        chosen, _ = QFileDialog.getSaveFileName(self, f'Save Node as {format_key.upper()}', str(Path.cwd() / f'{self.defaultStem()}_{safe_title}{suffix}'), filters)
        if not chosen:
            return
        try:
            File.writeText(Path(chosen), str(payload or EMPTY_STRING), encoding='utf-8')
        except Exception as error:
            captureException(None, source='start.py', context='except@6938')
            print(f"[WARN:swallowed-exception] start.py:5675 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            if self.debugger is not None:
                self.debugger.emit(f'[PromptDebugger] Save node failed: {type(error).__name__}: {error}', getattr(self.debugger, 'exceptionPath', None))
class DebuggerDatabase:
    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)

    """Debugger-side heartbeat/snapshot database helper.

    This keeps the launcher on one SQLite connection path with one warning path.
    The debugger and child app are separate processes, so they cannot share one
    live Python DB object. They *can* share one database file/schema, and this
    helper makes the debugger side use one connection lifecycle and one query
    error policy.
    """
    def __init__(self, owner):
        self.owner = owner  # noqa: nonconform
        self.path: Path | None = None
        self.connectionPath: str = EMPTY_STRING
        self.engine = None  # noqa: nonconform
        self.SessionFactory = None  # noqa: nonconform

    def backend(self) -> str:
        return str(os.environ.get('TRIO_DB_BACKEND', 'sqlite') or 'sqlite').strip().lower()


    def resolveSqlitePath(self) -> str:
        if self.connectionPath:
            return str(self.connectionPath)
        env_path = str(os.environ.get('TRIO_SQLITE_PATH', EMPTY_STRING) or EMPTY_STRING).strip()
        if env_path:
            self.connectionPath = env_path
            return env_path
        appdata = Path(os.environ.get('APPDATA') or (Path.home() / 'AppData' / 'Roaming'))
        db_dir = appdata / 'TrioDesktop'
        db_dir.mkdir(parents=True, exist_ok=True)
        self.connectionPath = str(db_dir / 'triodesktop.db')
        os.environ['TRIO_SQLITE_PATH'] = self.connectionPath
        return self.connectionPath

    def close(self):
        try:
            if self.engine is not None:
                self.engine.dispose()
        except Exception as error:
            captureException(None, source='start.py', context='except@6983')
            print(f"[WARN:swallowed-exception] start.py:5718 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        self.engine = None
        self.SessionFactory = None

    def open(self):
        if not HAS_SQLALCHEMY:
            raise RuntimeError(f'SQLAlchemy is required for debugger database access: {SQLALCHEMY_IMPORT_ERROR!r}')
        if self.SessionFactory is not None:
            return self.SessionFactory
        path = self.resolveSqlitePath()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = Path(path)
        self.engine = create_engine(f'sqlite:///{self.path}', future=True)
        self.SessionFactory = sessionmaker(bind=self.engine, future=True)
        StartOrmBase.metadata.create_all(self.engine)
        return self.SessionFactory

    def run(self, operation_label: str, callback, commit: bool = False):  # noqa: nonconform reviewed return contract
        try:
            SessionFactory = self.open()  # file-io-ok
            with SessionFactory() as session:
                result = callback(session)
                if commit:
                    session.commit()
                return result
        except Exception as error:
            captureException(None, source='start.py', context='except@7010')
            print(f"[WARN:swallowed-exception] start.py:5744 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            try:
                self.owner.emit(f'[DB:{operation_label}] {type(error).__name__}: {error}', getattr(self.owner, 'exceptionPath', None))
            except Exception as error:
                captureException(None, source='start.py', context='except@7014')
                print(f"[WARN:swallowed-exception] start.py:5747 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
            return None









    def writeHeartbeatRow(self, source: str = 'start.py', event_kind: str = 'heartbeat', reason: str = EMPTY_STRING, caller: str = EMPTY_STRING, phase: str = EMPTY_STRING, pid: int = 0, stack_trace: str = EMPTY_STRING, var_dump: str = EMPTY_STRING, process_snapshot: str = EMPTY_STRING, timestamp: float = 0.0, exec_code: str = EMPTY_STRING, exec_is_file: int = 0, cron_code: str = EMPTY_STRING, cron_is_file: int = 0, cron_interval_seconds: float = 0.0, processed: int = 0):
        stamp = float(timestamp or time.time())
        created = datetime.datetime.fromtimestamp(stamp).isoformat(sep=' ', timespec='microseconds')
        def writer(session):
            row = DebuggerHeartbeatRecord(
                created=created,
                heartbeatMicrotime=stamp,
                source=str(source or EMPTY_STRING),
                eventKind=str(event_kind or EMPTY_STRING),
                reason=str(reason or EMPTY_STRING),
                caller=str(caller or EMPTY_STRING),
                phase=str(phase or EMPTY_STRING),
                pid=int(pid or 0),
                stackTrace=str(stack_trace or EMPTY_STRING),
                varDump=str(var_dump or EMPTY_STRING),
                processSnapshot=str(process_snapshot or EMPTY_STRING),
                execCode=str(exec_code or EMPTY_STRING),
                execIsFile=int(exec_is_file or 0),
                cronCode=str(cron_code or EMPTY_STRING),
                cronIsFile=int(cron_is_file or 0),
                cronIntervalSeconds=float(cron_interval_seconds or 0.0),
                processed=int(processed or 0),
            )
            session.add(row)
            session.flush()
            return int(row.id or 0)
        return int(self.run('writeHeartbeatRow', writer, commit=True) or 0)

    def writeTrafficRow(self, headers_text: str = EMPTY_STRING, data_text: str = EMPTY_STRING, status_text: str = EMPTY_STRING, error_text: str = EMPTY_STRING, length_value: int = 0, destination_text: str = EMPTY_STRING, roundtrip_microtime: float = 0.0, processed: int = 0, timestamp: float = 0.0, caller_text: str = EMPTY_STRING, response_preview_text: str = EMPTY_STRING):
        stamp = float(timestamp or time.time())
        created = datetime.datetime.fromtimestamp(stamp).isoformat(sep=' ', timespec='microseconds')
        def writer(session):
            row = DebuggerTrafficRecord(created=created, headers=str(headers_text or EMPTY_STRING), data=str(data_text or EMPTY_STRING), status=str(status_text or EMPTY_STRING), error=str(error_text or EMPTY_STRING), length=int(length_value or 0) if str(length_value or EMPTY_STRING).strip() else None, destination=str(destination_text or EMPTY_STRING), roundtripMicrotime=float(roundtrip_microtime or 0.0), processed=int(processed or 0), caller=str(caller_text or EMPTY_STRING), responsePreview=str(response_preview_text or EMPTY_STRING))
            session.add(row)
            session.flush()
            return int(row.id or 0)
        return int(self.run('writeTrafficRow', writer, commit=True) or 0)

    def _nowIso(self, stamp: float | None = None) -> str:
        return datetime.datetime.fromtimestamp(float(stamp or time.time())).isoformat(sep=' ', timespec='microseconds')

    def _coerceTimestampFloat(self, value, *, label: str = 'timestamp') -> float:
        if value is None:
            return 0.0
        if isinstance(value, (int, float)):
            return float(value or 0.0)
        raw = str(value or EMPTY_STRING).strip()
        if not raw:
            return 0.0
        try:
            return float(raw)
        except (TypeError, ValueError) as numeric_error:
            # Not an error by itself: DB rows commonly store ISO text such as
            # "2026-05-02 13:43:07.615896". Persist the coercion branch as
            # handled diagnostic data, then try ISO parsing before warning.
            DebugLog.saveExceptionFallback(numeric_error, source='start.py', context='_coerceTimestampFloat:numeric-parse', handled=True, extra=f'label={label} raw={raw!r}')
        try:
            parsed = datetime.datetime.fromisoformat(raw.replace('Z', '+00:00'))
            return parsed.timestamp()
        except Exception as exc:
            captureException(None, source='start.py', context='coerce-timestamp-failed')
            print(f"[WARN:debugger-db] COERCE-TIMESTAMP-FAILED label={label} value={raw!r} {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            return 0.0

    def _coerceNumberFloat(self, value, *, label: str = 'number') -> float:
        if value is None:
            return 0.0
        if isinstance(value, (int, float)):
            return float(value or 0.0)
        raw = str(value or EMPTY_STRING).strip()
        if not raw:
            return 0.0
        try:
            return float(raw)
        except Exception as exc:
            captureException(None, source='start.py', context='except@7097')
            print(f"[WARN:debugger-db] COERCE-FLOAT-FAILED label={label} value={raw!r} {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            return 0.0

    def insertProcessRecord(self, source: str = 'start.py', phase_key: str = EMPTY_STRING, phase_name: str = EMPTY_STRING, process_name: str = EMPTY_STRING, kind: str = 'process', pid: int = 0, status: str = 'running', started_at: float = 0.0, ttl_seconds: float = 0.0, command: str = EMPTY_STRING, metadata: str = EMPTY_STRING, processed: int = 0) -> int:
        stamp = float(started_at or time.time())
        def writer(session):
            row = DebuggerProcessRecord(
                created=self._nowIso(stamp),
                updated=self._nowIso(),
                source=str(source or EMPTY_STRING),
                phaseKey=str(phase_key or EMPTY_STRING),
                phaseName=str(phase_name or EMPTY_STRING),
                processName=str(process_name or EMPTY_STRING),
                kind=str(kind or 'process'),
                pid=int(pid or 0),
                status=str(status or 'running'),
                startedAt=stamp,
                endedAt=0.0,
                ttlSeconds=float(ttl_seconds or 0.0),
                exitCode=None,
                errorType=EMPTY_STRING,
                errorMessage=EMPTY_STRING,
                tracebackText=EMPTY_STRING,
                faultReason=EMPTY_STRING,
                command=str(command or EMPTY_STRING),
                metadataText=str(metadata or EMPTY_STRING),
                processed=int(processed or 0),
            )
            session.add(row)
            session.flush()
            return int(row.id or 0)
        return int(self.run('insertProcessRecord', writer, commit=True) or 0)

    def updateProcessRecord(self, row_id: int, status: str | None = None, ended_at: float | None = None, exit_code: int | None = None, error_type: str = EMPTY_STRING, error_message: str = EMPTY_STRING, traceback_text: str = EMPTY_STRING, fault_reason: str = EMPTY_STRING, processed: int | None = None, metadata: str | None = None) -> bool:
        target_id = int(row_id or 0)
        if target_id <= 0:
            return False
        def writer(session):
            row = session.get(DebuggerProcessRecord, target_id)
            if row is None:
                return False
            row.updated = self._nowIso()
            if status is not None:
                row.status = str(status or EMPTY_STRING)
            if ended_at is not None:
                row.endedAt = float(ended_at or 0.0)
            if exit_code is not None:
                row.exitCode = int(exit_code or 0)
            if error_type:
                row.errorType = str(error_type or EMPTY_STRING)
            if error_message:
                row.errorMessage = str(error_message or EMPTY_STRING)
            if traceback_text:
                row.tracebackText = str(traceback_text or EMPTY_STRING)
            if fault_reason:
                row.faultReason = str(fault_reason or EMPTY_STRING)
            if processed is not None:
                row.processed = int(processed or 0)
            if metadata is not None:
                row.metadataText = str(metadata or EMPTY_STRING)
            return True
        return bool(self.run('updateProcessRecord', writer, commit=True))

    def readProcessRowsByStatus(self, statuses, limit: int = 50, include_processed: bool = False):
        wanted = [str(value or EMPTY_STRING).strip() for value in list(statuses or []) if str(value or EMPTY_STRING).strip()]
        if not wanted:
            return []
        def reader(session):
            query = session.query(DebuggerProcessRecord).filter(DebuggerProcessRecord.status.in_(wanted))
            if not include_processed:
                query = query.filter((DebuggerProcessRecord.processed.is_(None)) | (DebuggerProcessRecord.processed == 0))
            rows = query.order_by(DebuggerProcessRecord.id.asc()).limit(max(1, int(limit or 50))).all()
            return [{
                'id': int(row.id or 0),
                'created': str(row.created or EMPTY_STRING),
                'updated': str(row.updated or EMPTY_STRING),
                'source': str(row.source or EMPTY_STRING),
                'phase_key': str(row.phaseKey or EMPTY_STRING),
                'phase_name': str(row.phaseName or EMPTY_STRING),
                'process_name': str(row.processName or EMPTY_STRING),
                'kind': str(row.kind or EMPTY_STRING),
                'pid': int(row.pid or 0),
                'status': str(row.status or EMPTY_STRING),
                'started_at': self._coerceTimestampFloat(row.startedAt, label='readProcessRowsByStatus.startedAt'),
                'ended_at': self._coerceTimestampFloat(row.endedAt, label='readProcessRowsByStatus.endedAt'),
                'ttl_seconds': self._coerceNumberFloat(row.ttlSeconds, label='readProcessRowsByStatus.ttlSeconds'),
                'exit_code': row.exitCode,
                'error_type': str(row.errorType or EMPTY_STRING),
                'error_message': str(row.errorMessage or EMPTY_STRING),
                'traceback_text': str(row.tracebackText or EMPTY_STRING),
                'fault_reason': str(row.faultReason or EMPTY_STRING),
                'command': str(row.command or EMPTY_STRING),
                'metadata': str(row.metadataText or EMPTY_STRING),
                'processed': int(row.processed or 0),
            } for row in rows]
        return list(self.run('readProcessRowsByStatus', reader, commit=False) or [])

    def readErroredProcessRows(self, limit: int = 20):
        return self.readProcessRowsByStatus(['errored', 'faulted', 'exception', 'failed'], limit=limit, include_processed=False)

    def markProcessRowsProcessed(self, row_ids) -> int:
        ids = [int(value) for value in list(row_ids or []) if str(value or EMPTY_STRING).strip()]
        if not ids:
            return 0
        def writer(session):
            count = 0
            for row in session.query(DebuggerProcessRecord).filter(DebuggerProcessRecord.id.in_(ids)).all():
                row.processed = 1
                row.updated = self._nowIso()
                count += 1
            return count
        return int(self.run('markProcessRowsProcessed', writer, commit=True) or 0)

    def expireOverdueProcessRows(self) -> list[dict]:
        now = time.time()
        def reader(session):
            rows = session.query(DebuggerProcessRecord).filter(DebuggerProcessRecord.status.in_(['registered', 'pending', 'running'])).all()
            expired = []
            for row in rows:
                ttl = self._coerceNumberFloat(row.ttlSeconds, label='expireOverdueProcessRows.ttlSeconds')
                started = self._coerceTimestampFloat(row.startedAt, label='expireOverdueProcessRows.startedAt')
                try:
                    if started > 0 and not isinstance(row.startedAt, (int, float)):
                        row.startedAt = started
                        row.updated = self._nowIso()
                except Exception as normalize_error:
                    captureException(None, source='start.py', context='except@7223')
                    print(f"[WARN:debugger-db] STARTED-AT-NORMALIZE-FAILED id={getattr(row, 'id', 0)} {type(normalize_error).__name__}: {normalize_error}", file=sys.stderr, flush=True)
                if ttl > 0 and started > 0 and now - started > ttl:
                    row.status = 'errored'
                    row.endedAt = now
                    row.errorMessage = f'ttl_expired:{row.phaseKey}:{row.processName}'
                    row.faultReason = row.errorMessage
                    row.updated = self._nowIso()
                    row.processed = 0
                    expired.append({'id': int(row.id or 0), 'pid': int(row.pid or 0), 'process_name': str(row.processName or EMPTY_STRING), 'fault_reason': str(row.faultReason or EMPTY_STRING)})
            return expired
        return list(self.run('expireOverdueProcessRows', reader, commit=True) or [])

    def readUnprocessedTrafficRows(self, limit: int = 250):
        def reader(session):
            rows = session.query(DebuggerTrafficRecord).filter((DebuggerTrafficRecord.processed.is_(None)) | (DebuggerTrafficRecord.processed == 0)).order_by(DebuggerTrafficRecord.id.asc()).limit(max(1, int(limit or 250))).all()
            return [{'id': int(row.id or 0), 'created': str(row.created or EMPTY_STRING), 'headers': str(row.headers or EMPTY_STRING), 'data': str(row.data or EMPTY_STRING), 'status': str(row.status or EMPTY_STRING), 'error': str(row.error or EMPTY_STRING), 'length': row.length, 'destination': str(row.destination or EMPTY_STRING), 'roundtrip_microtime': float(row.roundtripMicrotime or 0.0), 'processed': int(row.processed or 0), 'caller': str(row.caller or EMPTY_STRING), 'response_preview': str(row.responsePreview or EMPTY_STRING)} for row in rows]
        return list(self.run('readUnprocessedTrafficRows', reader, commit=False) or [])

    def markTrafficProcessed(self, row_ids):
        ids = [int(value) for value in list(row_ids or []) if str(value or EMPTY_STRING).strip()]
        if not ids:
            return 0
        def writer(session):
            count = 0
            for row in session.query(DebuggerTrafficRecord).filter(DebuggerTrafficRecord.id.in_(ids)).all():
                row.processed = 1
                count += 1
            return count
        return int(self.run('markTrafficProcessed', writer, commit=True) or 0)

    def updateTrafficRow(self, row_id: int, status_text: str = EMPTY_STRING, error_text: str = EMPTY_STRING, length_value: int = 0, roundtrip_microtime: float = 0.0, processed: int | None = None, headers_text: str | None = None, data_text: str | None = None, caller_text: str | None = None, response_preview_text: str | None = None):
        target_id = int(row_id or 0)
        if target_id <= 0:
            return 0
        def writer(session):
            row = session.get(DebuggerTrafficRecord, target_id)
            if row is None:
                return 0
            row.status = str(status_text or EMPTY_STRING)
            row.error = str(error_text or EMPTY_STRING)
            row.length = int(length_value or 0) if str(length_value or EMPTY_STRING).strip() else None
            row.roundtripMicrotime = float(roundtrip_microtime or 0.0)
            if processed is not None:
                row.processed = int(processed or 0)
            if headers_text is not None:
                row.headers = str(headers_text or EMPTY_STRING)
            if data_text is not None:
                row.data = str(data_text or EMPTY_STRING)
            if caller_text is not None:
                row.caller = str(caller_text or EMPTY_STRING)
            if response_preview_text is not None:
                row.responsePreview = str(response_preview_text or EMPTY_STRING)
            return 1
        return int(self.run('updateTrafficRow', writer, commit=True) or 0)

    def readUnprocessedFaultRows(self, limit: int = 20, min_id: int = 0):
        def reader(session):
            rows = session.query(DebuggerFaultRecord).filter(((DebuggerFaultRecord.processed.is_(None)) | (DebuggerFaultRecord.processed == 0)) & (DebuggerFaultRecord.id > int(min_id or 0))).order_by(DebuggerFaultRecord.id.asc()).limit(max(1, int(limit or 20))).all()
            return [{'id': int(row.id or 0), 'created': str(row.created or EMPTY_STRING), 'source': str(row.source or EMPTY_STRING), 'reason': str(row.reason or EMPTY_STRING), 'caller': str(row.caller or EMPTY_STRING), 'stack_trace': str(row.stackTrace or EMPTY_STRING), 'var_dump': str(row.varDump or EMPTY_STRING), 'process_snapshot': str(row.processSnapshot or EMPTY_STRING), 'thread': str(row.thread or EMPTY_STRING), 'pid': int(row.pid or 0), 'processed': int(row.processed or 0)} for row in rows]
        return list(self.run('readUnprocessedFaultRows', reader, commit=False) or [])

    def readUnprocessedHeartbeatRows(self, limit: int = 100, min_id: int = 0):
        def reader(session):
            rows = session.query(DebuggerHeartbeatRecord).filter(((DebuggerHeartbeatRecord.processed.is_(None)) | (DebuggerHeartbeatRecord.processed == 0)) & (DebuggerHeartbeatRecord.source.in_(childDebuggerSourceNames())) & (DebuggerHeartbeatRecord.eventKind.in_(['heartbeat', 'process'])) & (DebuggerHeartbeatRecord.id > int(min_id or 0))).order_by(DebuggerHeartbeatRecord.id.asc()).limit(max(1, int(limit or 100))).all()
            return [{'id': int(row.id or 0), 'created': str(row.created or EMPTY_STRING), 'heartbeat_microtime': float(row.heartbeatMicrotime or 0.0), 'source': str(row.source or EMPTY_STRING), 'event_kind': str(row.eventKind or EMPTY_STRING), 'reason': str(row.reason or EMPTY_STRING), 'caller': str(row.caller or EMPTY_STRING), 'phase': str(row.phase or EMPTY_STRING), 'pid': int(row.pid or 0), 'processed': int(row.processed or 0)} for row in rows]
        return list(self.run('readUnprocessedHeartbeatRows', reader, commit=False) or [])

    def markHeartbeatProcessed(self, row_ids):
        ids = [int(value) for value in list(row_ids or []) if str(value or EMPTY_STRING).strip()]
        if not ids:
            return 0
        def writer(session):
            count = 0
            for row in session.query(DebuggerHeartbeatRecord).filter(DebuggerHeartbeatRecord.id.in_(ids)).all():
                row.processed = 1
                count += 1
            return count
        return int(self.run('markHeartbeatProcessed', writer, commit=True) or 0)

    def markFaultProcessed(self, row_ids):
        ids = [int(value) for value in list(row_ids or []) if str(value or EMPTY_STRING).strip()]
        if not ids:
            return 0
        def writer(session):
            count = 0
            for row in session.query(DebuggerFaultRecord).filter(DebuggerFaultRecord.id.in_(ids)).all():
                row.processed = 1
                count += 1
            return count
        return int(self.run('markFaultProcessed', writer, commit=True) or 0)

    def readRecentExceptionRows(self, limit: int = 20, include_processed: bool = True, min_id: int = 0):
        def reader(session):
            query = session.query(DebuggerExceptionRecord).filter(DebuggerExceptionRecord.id > int(min_id or 0))
            if not include_processed:
                query = query.filter((DebuggerExceptionRecord.processed.is_(None)) | (DebuggerExceptionRecord.processed == 0))
            rows = query.order_by(DebuggerExceptionRecord.id.desc()).limit(max(1, int(limit or 20))).all()
            return [{'id': int(row.id or 0), 'created': str(row.created or EMPTY_STRING), 'source': str(row.source or EMPTY_STRING), 'context': str(row.context or EMPTY_STRING), 'type_name': str(row.typeName or EMPTY_STRING), 'message': str(row.message or EMPTY_STRING), 'traceback_text': str(row.tracebackText or EMPTY_STRING), 'source_context': str(row.sourceContext or EMPTY_STRING), 'thread': str(row.thread or EMPTY_STRING), 'pid': int(row.pid or 0), 'handled': int(row.handled or 0), 'processed': int(row.processed or 0)} for row in rows]
        return list(self.run('readRecentExceptionRows', reader, commit=False) or [])

    def readUnprocessedUnhandledExceptionRows(self, limit: int = 20, min_id: int = 0):
        rows = list(self.readRecentExceptionRows(limit=limit, include_processed=False, min_id=min_id) or [])
        return [row for row in rows if int((0 if row.get('handled', None) is None else row.get('handled', 0)) or 0) == 0]

    def markExceptionProcessed(self, row_ids):
        ids = [int(value) for value in list(row_ids or []) if str(value or EMPTY_STRING).strip()]
        if not ids:
            return 0
        def writer(session):
            count = 0
            for row in session.query(DebuggerExceptionRecord).filter(DebuggerExceptionRecord.id.in_(ids)).all():
                row.processed = 1
                count += 1
            return count
        return int(self.run('markExceptionProcessed', writer, commit=True) or 0)

    def readLatestDebuggerRowIds(self) -> dict[str, int]:
        baseline = {'heartbeat': 0, 'faults': 0, 'exceptions': 0}
        def reader(session):
            return {
                'heartbeat': int(getattr(session.query(DebuggerHeartbeatRecord).order_by(DebuggerHeartbeatRecord.id.desc()).first(), 'id', 0) or 0),
                'faults': int(getattr(session.query(DebuggerFaultRecord).order_by(DebuggerFaultRecord.id.desc()).first(), 'id', 0) or 0),
                'exceptions': int(getattr(session.query(DebuggerExceptionRecord).order_by(DebuggerExceptionRecord.id.desc()).first(), 'id', 0) or 0),
            }
        result = self.run('readLatestDebuggerRowIds', reader, commit=False)
        if isinstance(result, dict):
            return {key: int(result.get(key, 0) or 0) for key in baseline.keys()}
        return baseline

    def saveSnapshot(self, info: dict[str, Any] | None, stack_text: str, var_text: str):
        info = dict(info or {})
        snapshot_text = str(info.get('process_snapshot', EMPTY_STRING) or info.get('snapshot', EMPTY_STRING) or EMPTY_STRING)
        row_id = self.writeHeartbeatRow(source=childDebuggerTargetLabel(), event_kind='snapshot', reason=str(info.get('type_name', 'snapshot') or 'snapshot'), caller='PromptDebugger', phase='SNAPSHOT', pid=int(info.get('pid', 0) or 0), stack_trace=str(stack_text or EMPTY_STRING), var_dump=str(var_text or EMPTY_STRING), process_snapshot=snapshot_text, timestamp=float(info.get('timestamp') or time.time()))
        return int(row_id or 0)


    def readLatestSnapshotRow(self):
        def reader(session):
            rows = session.query(DebuggerHeartbeatRecord).order_by(DebuggerHeartbeatRecord.id.desc()).limit(250).all()
            for row in rows:
                if str(row.stackTrace or EMPTY_STRING) or str(row.varDump or EMPTY_STRING) or str(row.processSnapshot or EMPTY_STRING):
                    return row
            return None
        return self.run('readLatestSnapshotRow', reader, commit=False)

    def latestSnapshotRowId(self) -> int:
        row = self.readLatestSnapshotRow()
        try:
            return int(getattr(row, 'id', 0) or 0)
        except Exception as error:
            captureException(None, source='start.py', context='except@7373')
            print(f"[WARN:swallowed-exception] start.py:6099 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return 0

    def waitForSnapshotRowAfter(self, after_id: int = 0, timeout: float = 1.0, poll_interval: float = 0.05):
        deadline = time.time() + max(0.05, float(timeout or 0.0))
        last_row = None
        while time.time() < deadline:
            row = self.readLatestSnapshotRow()
            last_row = row
            try:
                if row is not None and int(getattr(row, 'id', 0) or 0) > int(after_id or 0):
                    return row
            except Exception as error:
                captureException(None, source='start.py', context='except@7386')
                print(f"[WARN:swallowed-exception] start.py:6111 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
            time.sleep(max(0.01, float(poll_interval or 0.05)))
        return last_row

    def formatSnapshotRow(self, row) -> str:
        if row is None:
            return '[PromptDebugger] No child snapshot found.'
        parts = ['=' * 62, '  Latest Child Snapshot', '=' * 62]
        for key, label in [('created', 'Created'), ('reason', 'Reason'), ('caller', 'Caller'), ('phase', 'Phase'), ('pid', 'PID')]:
            value = getattr(row, key, EMPTY_STRING)
            parts.append(f'  {label}: {value}')
        stack_text = str(getattr(row, 'stackTrace', EMPTY_STRING) or EMPTY_STRING)
        var_text = str(getattr(row, 'varDump', EMPTY_STRING) or EMPTY_STRING)
        proc_text = str(getattr(row, 'processSnapshot', EMPTY_STRING) or EMPTY_STRING)
        if stack_text:
            parts.extend([EMPTY_STRING, '[Stack Trace]', stack_text])
        if var_text:
            parts.extend([EMPTY_STRING, '[Var Dump]', var_text])
        if proc_text:
            parts.extend([EMPTY_STRING, '[Process Snapshot]', proc_text])
        parts.append('=' * 62)
        return '\n'.join(parts)

    def readLatestSnapshot(self) -> str:
        return self.formatSnapshotRow(self.readLatestSnapshotRow())



    def runDebuggerSqlite(self, operation_label: str, callback, commit: bool = False):
        return self.run(operation_label, callback, commit=commit)

    def formatSnapshotHistoryEntry(self, snapshot_text: str = EMPTY_STRING, timestamp: float = 0.0) -> tuple[str, str]:
        payload = str(snapshot_text or EMPTY_STRING).replace('\r\n', '\n').replace('\r', '\n').strip('\n')
        digest = hashlib.md5(payload.encode('utf-8', 'replace')).hexdigest()
        stamp = datetime.datetime.fromtimestamp(float(timestamp or time.time()))
        header = f"{digest} {stamp.strftime('%Y-%m-%d %H:%M:%S')}"
        return digest, header + '\n\n' + payload

    def appendSnapshotHistory(self, snapshot_text: str, timestamp: float = 0.0) -> bool:
        owner = getattr(self, 'owner', None)
        if owner is None or not hasattr(owner, 'canWritePath'):
            return False
        target = Path(getattr(owner, 'snapshotPath', str(SNAPSHOT_LOG_PATH)))
        if not owner.canWritePath(target):
            return False
        digest, entry = self.formatSnapshotHistoryEntry(snapshot_text, timestamp=timestamp)
        existing = EMPTY_STRING
        try:
            if target.exists():
                existing = File.readText(target, encoding='utf-8', errors='replace')
        except Exception as error:
            captureException(None, source='start.py', context='except@7438')
            print(f"[WARN:swallowed-exception] start.py:6166 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            existing = EMPTY_STRING
        if re.search(rf'(?m)^{re.escape(digest)}\s', existing):
            return False
        separator = '\n' * 6
        combined = entry + (separator + existing.lstrip('\n') if existing.strip() else separator)
        target.parent.mkdir(parents=True, exist_ok=True)
        File.writeText(target, combined, encoding='utf-8', errors='replace')
        return True

    def writeHeartbeatRowToDb(self, source: str = 'start.py', event_kind: str = 'heartbeat', reason: str = EMPTY_STRING, caller: str = EMPTY_STRING, phase: str = EMPTY_STRING, pid: int = 0, stack_trace: str = EMPTY_STRING, var_dump: str = EMPTY_STRING, process_snapshot: str = EMPTY_STRING, timestamp: float = 0.0, exec_code: str = EMPTY_STRING, exec_is_file: int = 0, cron_code: str = EMPTY_STRING, cron_is_file: int = 0, cron_interval_seconds: float = 0.0):
        return self.writeHeartbeatRow(source=source, event_kind=event_kind, reason=reason, caller=caller, phase=phase, pid=pid, stack_trace=stack_trace, var_dump=var_dump, process_snapshot=process_snapshot, timestamp=timestamp, exec_code=exec_code, exec_is_file=exec_is_file, cron_code=cron_code, cron_is_file=cron_is_file, cron_interval_seconds=cron_interval_seconds)

    def saveSnapshotToDb(self, info: dict[str, Any] | None, snapshot_text: str, stack_text: str, var_text: str):
        return int(self.saveSnapshot(info, stack_text, var_text) or 0)

    def readLatestSnapshotFromDb(self) -> str:
        return self.readLatestSnapshot()

    def inferDebuggerCodePayload(self, raw_text: str = EMPTY_STRING) -> tuple[str, int]:
        text = str(raw_text or EMPTY_STRING).strip()
        if not text:
            return EMPTY_STRING, 0
        if text.startswith('>>'):
            return text[2:].strip(), 1
        if text.startswith('>'):
            return text[1:].strip(), 1
        return text, 0

    def queueDebuggerExecRequest(self, raw_text: str = EMPTY_STRING) -> int:
        payload, is_file = self.inferDebuggerCodePayload(raw_text)
        if not payload:
            return 0
        return int(self.writeHeartbeatRow(
            source='start.py',
            event_kind='debugger-exec-request',
            reason='DEBUGGER-EXEC',
            caller='PromptDebugger',
            phase='DEBUGGER',
            pid=int(getattr(self.owner, 'childPid', 0) or 0),
            timestamp=time.time(),
            exec_code=payload,
            exec_is_file=is_file,
        ) or 0)

    def queueDebuggerCronRequest(self, interval_seconds: float, raw_text: str = EMPTY_STRING) -> int:
        payload, is_file = self.inferDebuggerCodePayload(raw_text)
        if not payload:
            return 0
        return int(self.writeHeartbeatRow(
            source='start.py',
            event_kind='debugger-cron-request',
            reason='DEBUGGER-CRON',
            caller='PromptDebugger',
            phase='DEBUGGER',
            pid=int(getattr(self.owner, 'childPid', 0) or 0),
            timestamp=time.time(),
            cron_code=payload,
            cron_is_file=is_file,
            cron_interval_seconds=float(interval_seconds or 0.0),
        ) or 0)

    def readDebuggerResultRow(self, reason: str = EMPTY_STRING, after_id: int = 0, timeout: float = 3.0, poll_interval: float = 0.10):
        target_reason = str(reason or EMPTY_STRING).strip()
        deadline = time.time() + max(0.10, float(timeout or 0.0))
        def reader(session):
            query = session.query(DebuggerHeartbeatRecord).filter(DebuggerHeartbeatRecord.source.in_(childDebuggerSourceNames()))
            if target_reason:
                query = query.filter(DebuggerHeartbeatRecord.reason == target_reason)
            try:
                after_value = int(after_id or 0)
            except Exception as error:
                captureException(None, source='start.py', context='except@7510')
                print(f"[WARN:swallowed-exception] start.py:6237 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                after_value = 0
            if after_value > 0:
                query = query.filter(DebuggerHeartbeatRecord.id > after_value)
            owner = getattr(self, 'owner', None)
            try:
                child_pid = int(getattr(owner, 'childPid', 0) or 0)
            except Exception as error:
                captureException(None, source='start.py', context='except@7518')
                print(f"[WARN:swallowed-exception] start.py:6244 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                child_pid = 0
            if child_pid > 0:
                query = query.filter(DebuggerHeartbeatRecord.pid == child_pid)
            return query.order_by(DebuggerHeartbeatRecord.id.desc()).first()
        last_row = None
        while time.time() < deadline:
            last_row = self.run('readDebuggerResultRow', reader, commit=False)
            if last_row is not None:
                return last_row
            time.sleep(max(0.02, float(poll_interval or 0.10)))
        return last_row

    def formatDebuggerResultRow(self, row) -> str:
        if row is None:
            return '[PromptDebugger] No debugger result row found.'
        parts = ['=' * 62, '  Child Debugger Result', '=' * 62]
        for key, label in [('created', 'Created'), ('eventKind', 'Event'), ('reason', 'Reason'), ('caller', 'Caller'), ('phase', 'Phase'), ('pid', 'PID')]:
            value = getattr(row, key, EMPTY_STRING)
            parts.append(f'  {label}: {value}')
        stack_text = str(getattr(row, 'stackTrace', EMPTY_STRING) or EMPTY_STRING)
        var_text = str(getattr(row, 'varDump', EMPTY_STRING) or EMPTY_STRING)
        proc_text = str(getattr(row, 'processSnapshot', EMPTY_STRING) or EMPTY_STRING)
        if var_text:
            parts.extend([EMPTY_STRING, '[Output]', var_text])
        if proc_text:
            parts.extend([EMPTY_STRING, '[Metadata]', proc_text])
        if stack_text:
            parts.extend([EMPTY_STRING, '[Traceback]', stack_text])
        parts.append('=' * 62)
        return '\n'.join(parts)

    def emit(self, text: str, path: str | None = None):
        if not DebugLog.lineLooksVisible(text):
            return
        try:
            self.clearStatusLine()
        except Exception as error:
            captureException(None, source='start.py', context='except@7554')
            print(f"[WARN:swallowed-exception] start.py:6279 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        payload_lines = DebugLog.iterVisibleLines(text)
        try:
            for line in payload_lines:
                sys.stderr.write(line + '\n')
            sys.stderr.flush()
        except Exception as error:
            captureException(None, source='start.py', context='except@7560')
            print(f"[WARN:swallowed-exception] start.py:6284 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        if path and self.canWritePath(path):
            self.write(path, '\n'.join(payload_lines))
    def startKernel(self):
        if not self.enabled:
            return
        try:
            self.installParentStackTracing()
        except Exception as error:
            captureException(None, source='start.py', context='except@7570')
            print(f"[WARN:swallowed-exception] start.py:6293 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            self.emit(f'[PromptDebugger] Parent stack tracing disabled: {error}', self.exceptionPath)
        try:
            if getattr(self, 'shellRpcServer', None) is None:
                self.shellRpcServer = ShellRpcServer(self)
                self.emit(f'[PromptDebugger] Shell RPC  127.0.0.1:{self.shellRpcServer.port}', self.logPath)
        except Exception as error:
            captureException(None, source='start.py', context='except@7577')
            print(f"[WARN:swallowed-exception] start.py:6299 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            self.emit(f'[PromptDebugger] Shell RPC disabled: {error}', self.exceptionPath)
            self.shellRpcServer = None
        with self.lock:
            if self.relayThread is None:
                self.startRelayServer()
            if self.watchdogThread is None:
                self.watchdogThread = START_EXECUTION_LIFECYCLE.startThread('TrioProcessWatchdog', self.threadMain(self.poll, 'TrioProcessWatchdog'), daemon=False)
                self.emit(f"[PromptDebugger] Process watchdog started  PID={os.getpid()}  freeze_threshold={self.freezeThresholdSeconds}s  poll_interval={self.deathPollInterval}s", self.logPath)
            if self.consoleThread is None:
                self.consoleThread = START_EXECUTION_LIFECYCLE.startThread('TrioCrashConsole', self.threadMain(self.consoleLoop, 'TrioCrashConsole'), daemon=False)
            if self.keyThread is None:
                self.keyStop.clear()
                self.keyThread = START_EXECUTION_LIFECYCLE.startThread('TrioKeyReader', self.threadMain(self.keyReaderLoop, 'TrioKeyReader'), daemon=True)
            if self.statusThread is None and not self.traceVerbose:
                self.statusStop.clear()
                self.statusThread = START_EXECUTION_LIFECYCLE.startThread('TrioStatusLoop', self.threadMain(self.statusLoop, 'TrioStatusLoop'), daemon=True)
            if self.serverThread is None:
                self.startSocketRepl()
    def threadMain(self, target, name: str):
        def runner():
            try:
                target()
            except Exception as error:
                captureException(None, source='start.py', context='except@7601')
                self.emit(f"[PromptDebugger:{name}] {type(error).__name__}: {error}\n" + EMPTY_STRING.join(traceback.format_exception(type(error), error, error.__traceback__)), self.exceptionPath)
        return runner
    def keyReaderLoop(self):
        try:
            if os.name == 'nt':
                self.keyReaderWindows()
            else:
                self.keyReaderPosix()
        except Exception as error:
            captureException(None, source='start.py', context='except@7610')
            print(f"[WARN:swallowed-exception] start.py:6331 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            self.emit(f'[PromptDebugger] Key reader failed: {error}', self.exceptionPath)

    def keyReaderWindows(self):
        try:
            import msvcrt
        except ImportError:
            captureException(None, source='start.py', context='except@7617')
            return
        while not self.keyStop.is_set() and not self.consoleStop.is_set():
            try:
                if not msvcrt.kbhit():
                    time.sleep(0.05)
                    continue
                ch = msvcrt.getch()
                if ch in (bytes([0]), bytes([0xE0])):
                    try:
                        msvcrt.getch()
                    except Exception as error:
                        captureException(None, source='start.py', context='except@7628')
                        print(f"[WARN:swallowed-exception] start.py:6348 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                        pass
                    continue
                try:
                    key = ch.decode('utf-8', 'replace').lower()
                except Exception as error:
                    captureException(None, source='start.py', context='except@7634')
                    print(f"[WARN:swallowed-exception] start.py:6353 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    continue
                if key == 'd' and not self.inCrashConsole and time.time() >= float(getattr(self, 'manualDebugCooldownUntil', 0.0) or 0.0):
                    try:
                        while msvcrt.kbhit():
                            buffered = msvcrt.getch()
                            try:
                                bufferedKey = buffered.decode('utf-8', 'replace').lower()
                            except Exception as error:
                                captureException(None, source='start.py', context='except@7643')
                                print(f"[WARN:swallowed-exception] start.py:6361 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                                bufferedKey = EMPTY_STRING
                            if bufferedKey != 'd':
                                break
                    except Exception as error:
                        captureException(None, source='start.py', context='except@7648')
                        print(f"[WARN:swallowed-exception] start.py:6365 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                        pass
                    self.openManualDebug()
            except Exception as error:
                captureException(None, source='start.py', context='except@7652')
                print(f"[WARN:swallowed-exception] start.py:6368 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                time.sleep(0.1)

    def keyReaderPosix(self):
        import select
        while not self.keyStop.is_set() and not self.consoleStop.is_set():
            try:
                if not sys.stdin.isatty():
                    time.sleep(0.2)
                    continue
                ready, _, _ = select.select([sys.stdin], [], [], 0.05)
                if ready:
                    ch = sys.stdin.read(1)
                    if ch and ch.lower() == 'd' and not self.inCrashConsole and time.time() >= float(getattr(self, 'manualDebugCooldownUntil', 0.0) or 0.0):
                        self.openManualDebug()
            except Exception as error:
                captureException(None, source='start.py', context='except@7668')
                print(f"[WARN:swallowed-exception] start.py:6383 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                time.sleep(0.1)

    def openManualDebug(self):
        now = time.time()
        cooldown = max(
            float(getattr(self, 'manualDebugCooldownUntil', 0.0) or 0.0),
            float(getattr(self, 'manualDebugCooldownUntil', 0.0) or 0.0),
        )
        if self.inCrashConsole or now < cooldown:
            return
        target = now + 1.5
        self.manualDebugCooldownUntil = target
        self.manualDebugCooldownUntil = target
        self.statusOverrideReason = 'DEBUG'
        self.emit('[PromptDebugger] Manual debug requested (D key)', self.logPath)
        self.openCrashConsole({
            'type_name': 'ManualDebug',
            'message': 'Manually entered debug console (D key)',
            'thread': 'TrioKeyReader',
            'timestamp': time.time(),
        }, block=False)
    def startSocketRepl(self):
        debugger = self
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                debugger.emit(f"[PromptDebugger] REPL client {self.client_address}", debugger.logPath)
                self.wfile.write(b'PromptDebugger REPL\n')
                self.wfile.flush()
                while True:  # noqa: badcode reviewed detector-style finding
                    try:
                        raw = self.rfile.readline()
                    except (ConnectionResetError, OSError):
                        captureException(None, source='start.py', context='except@7701')
                        break
                    if not raw:
                        break
                    cmd = raw.decode('utf-8', 'replace').strip()
                    if not cmd:
                        continue
                    output = debugger.dispatchConsoleCommand(cmd, interactive=False)
                    self.wfile.write((output + '\n').encode('utf-8', 'replace'))
                    self.wfile.flush()
        class Server(socketserver.TCPServer):
            allow_reuse_address = True
        try:
            self.server = Server(('127.0.0.1', 5050), Handler)
            self.serverThread = START_EXECUTION_LIFECYCLE.startThread('TrioSocketREPL', self.threadMain(lambda: cast(Any, self.server).serve_forever(poll_interval=0.5), 'TrioSocketREPL'), daemon=True)
            self.emit('[PromptDebugger] Socket REPL  127.0.0.1:5050  (nc localhost 5050)', self.logPath)
        except Exception as error:
            captureException(None, source='start.py', context='except@7717')
            print(f"[WARN:swallowed-exception] start.py:6431 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            self.emit(f"[PromptDebugger] Socket REPL disabled: {error}", self.exceptionPath)
    def startRelayServer(self):
        debugger = self
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                while True:  # noqa: badcode reviewed detector-style finding
                    try:
                        raw = self.rfile.readline()
                    except (ConnectionResetError, OSError):
                        captureException(None, source='start.py', context='except@7727')
                        break
                    if not raw:
                        break
                    try:
                        payload = json.loads(raw.decode('utf-8', 'replace'))
                    except Exception as error:
                        captureException(None, source='start.py', context='except@7733')
                        print(f"[WARN:swallowed-exception] start.py:6446 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                        debugger.emit(f'[PromptDebugger] Relay decode failed: {error}', debugger.exceptionPath)
                        continue
                    debugger.handleRelayMessage(payload)
        class Server(socketserver.TCPServer):
            allow_reuse_address = True
        self.relayServer = Server(('127.0.0.1', 0), Handler)
        self.relayPort = int(self.relayServer.server_address[1])
        self.relayThread = START_EXECUTION_LIFECYCLE.startThread('TrioRelayServer', self.threadMain(lambda: self.relayServer.serve_forever(poll_interval=0.5), 'TrioRelayServer'), daemon=True)
        self.emit(f'[PromptDebugger] Relay server  127.0.0.1:{self.relayPort}', self.logPath)
    def handleRelayMessage(self, payload: dict[str, Any]):
        try:
            if self.consoleStop.is_set():
                return
            if str(payload.get('token') or EMPTY_STRING) != self.relayToken:
                return
            kind = str(payload.get('kind') or EMPTY_STRING).strip().lower()
            data = payload.get('payload') or {}
            if kind == 'attach':
                self.childPid = int(data.get('pid') or 0)
                try:
                    self.childControlPort = int(data.get('control_port') or 0)
                except Exception as error:
                    captureException(None, source='start.py', context='except@7756')
                    print(f"[WARN:swallowed-exception] start.py:6468 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    self.childControlPort = 0
                surfaces = data.get('surfaces')
                if surfaces:
                    try:
                        self.setChildSurfaces(surfaces, source='relay-attach')
                    except Exception as error:
                        captureException(None, source='start.py', context='except@7763')
                        print(f"[WARN:swallowed-exception] start.py:6474 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                        pass
                suffix = f" control_port={self.childControlPort}" if self.childControlPort else EMPTY_STRING
                self.emit(f"[TRACE:debugger-attach] attached child_pid={self.childPid}{suffix}", self.logPath)
                if self.childControlPort and not surfaces:
                    try:
                        self.queryChildSurfaces(timeout=3.0, retries=2)
                    except Exception as error:
                        captureException(None, source='start.py', context='except@7771')
                        print(f"[WARN:swallowed-exception] start.py:6481 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                        pass
                return
            if kind == 'heartbeat':
                reason = str(data.get('reason') or 'HEARTBEAT')
                caller = str(data.get('caller') or EMPTY_STRING)
                timestamp = float(data.get('timestamp') or time.time())
                self.touchHeartbeat(reason, caller=caller, timestamp=timestamp)
                self.writeHeartbeatRowToDb(
                    source=childDebuggerTargetLabel(),
                    event_kind='heartbeat',
                    reason=reason,
                    caller=caller,
                    phase=str(self.lastProcessReason or EMPTY_STRING),
                    pid=int(data.get('pid') or self.childPid or 0),
                    timestamp=timestamp,
                )
                return
            if kind == 'process':
                reason = str(data.get('reason') or 'PROCESS')
                timestamp = float(data.get('timestamp') or time.time())
                self.touchProcessLoop(reason)
                self.writeHeartbeatRowToDb(
                    source=childDebuggerTargetLabel(),
                    event_kind='process',
                    reason=reason,
                    caller=EMPTY_STRING,
                    phase=reason,
                    pid=int(data.get('pid') or self.childPid or 0),
                    timestamp=timestamp,
                )
                return
            if kind == 'warn':
                text = str(data.get('text') or EMPTY_STRING).rstrip()
                if text:
                    self.emit(text, self.logPath)
                return
            if kind in {'die', 'fault', 'exception'}:
                text = str(data.get('text') or EMPTY_STRING).rstrip()
                if text:
                    self.emit(text, self.exceptionPath)
                if self.childSurfaceSupported('vardump', default=True):
                    try:
                        self.requestChildSnapshot(command='vardump', timeout=0.75, reason=kind)
                    except Exception as error:
                        captureException(None, source='start.py', context='except@7816')
                        print(f"[WARN:swallowed-exception] start.py:6525 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                        pass
                self.openCrashConsole({
                    'type_name': str(data.get('type_name') or 'RemoteFault'),
                    'message': str(data.get('message') or text or 'Remote fault'),
                    'traceback_text': str(data.get('traceback_text') or EMPTY_STRING),
                    'thread': str(data.get('thread') or 'child'),
                    'timestamp': float(data.get('timestamp') or time.time()),
                }, block=False)
                return
            if kind == 'freeze':
                text = str(data.get('text') or EMPTY_STRING).rstrip()
                if text:
                    self.emit(text, self.exceptionPath)
                if self.childSurfaceSupported('vardump', default=True):
                    try:
                        self.requestChildSnapshot(command='freeze_dump', timeout=0.75, reason='relay-freeze')
                    except Exception as error:
                        captureException(None, source='start.py', context='except@7834')
                        print(f"[WARN:swallowed-exception] start.py:6542 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                        pass
                self.openCrashConsole({
                    'type_name': 'ChildFreeze',
                    'message': str(data.get('message') or 'Child UI freeze'),
                    'traceback_text': str(data.get('traceback_text') or EMPTY_STRING),
                    'thread': str(data.get('thread') or 'child'),
                    'timestamp': float(data.get('timestamp') or time.time()),
                }, block=False)
                return
        except Exception as error:
            captureException(None, source='start.py', context='except@7845')
            print(f"[WARN:swallowed-exception] start.py:6552 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            self.emit(f'[PromptDebugger] Relay handle failed: {error}', self.exceptionPath)
    def touchProcessLoop(self, reason: str = 'PROCESS'):
        now = time.time()
        with self.lock:
            self.lastProcessLoop = now
            self.lastProcessReason = str(reason or 'PROCESS')
        self.writeHeartbeatRowToDb(
            source='start.py',
            event_kind='process-observed',
            reason=str(reason or 'PROCESS'),
            caller='PromptDebugger',
            phase=str(reason or 'PROCESS'),
            pid=int(self.childPid or 0),
            timestamp=now,
        )
    def touchHeartbeat(self, reason: str = 'HEARTBEAT', caller: str = EMPTY_STRING, timestamp: float = 0.0):
        now = float(timestamp or time.time())
        with self.lock:
            self.lastHeartbeat = now
            self.lastHeartbeatReason = str(reason or 'HEARTBEAT')
            self.lastHeartbeatCaller = str(caller or EMPTY_STRING)
            self.heartbeatEverFired = True
        self.writeHeartbeatRowToDb(
            source='start.py',
            event_kind='heartbeat-observed',
            reason=str(reason or 'HEARTBEAT'),
            caller=str(caller or EMPTY_STRING),
            phase=str(self.lastProcessReason or EMPTY_STRING),
            pid=int(self.childPid or 0),
            timestamp=now,
        )
        normalizedReason = str(reason or 'HEARTBEAT').strip().upper()
        if bool(getattr(self, 'offscreenEnabled', False)) and getattr(self, 'captureEngine', None) or getattr(self, 'offscreenSession', None) is not None:
            if normalizedReason == 'SHOW':
                self.scheduleOffscreenCapture('show', delaySeconds=0.40, allowRepeat=False, stamped=False)
            elif normalizedReason == 'HEALTHY':
                self.scheduleOffscreenCapture('healthy', delaySeconds=0.20, allowRepeat=False, stamped=False)
            elif normalizedReason == 'INIT':
                self.scheduleOffscreenCapture('init', delaySeconds=0.60, allowRepeat=False, stamped=False)
    def recordInit(self, obj):
        self.touchHeartbeat(f'INIT:{type(obj).__name__}')
        return obj
    def onCallbackEnter(self, *_args, **_kwargs):
        return None
    def onCallbackExit(self, *_args, **_kwargs):
        return None
    def onWarning(self, *_args, **_kwargs):
        return EMPTY_STRING
    def onFault(self, *_args, **_kwargs):
        return EMPTY_STRING
    def hasFlag(self, name):
        return False
    def flatlined(self) -> str:
        """[FLATLINED TODAY @ 12:07:13.451234 after RESIZE in TrioDesktop::HelpCenter]"""
        import datetime as datetimeModule
        ts     = float(self.lastHeartbeat or time.time())
        dt     = datetimeModule.datetime.fromtimestamp(ts)
        tod    = dt.strftime('%H:%M:%S') + f'.{dt.microsecond:06d}'
        reason = str(self.lastHeartbeatReason or 'TICK')
        caller = str(getattr(self, 'lastHeartbeatCaller', EMPTY_STRING) or EMPTY_STRING)
        caller_str = f' in TrioDesktop::{caller}' if caller else EMPTY_STRING
        return f'[FLATLINED TODAY @ {tod} after {reason}{caller_str}]'

    def crashSignature(self, info: dict[str, Any] | None = None) -> str:
        payload = dict(info or {})
        type_name = str(payload.get('type_name') or EMPTY_STRING)
        message = str(payload.get('message') or EMPTY_STRING)
        if type_name.lower() == 'freeze':
            message = re.sub(r'for\s+\d+(?:\.\d+)?s', 'for <elapsed>s', message)
            message = re.sub(r'last heartbeat=[^,)]+', f"last heartbeat={self.lastHeartbeatReason or 'HEARTBEAT'}", message)
            message = re.sub(r'last process=[^,)]+', f"last process={self.lastProcessReason or 'PROCESS'}", message)
        return '|'.join([type_name, message, str(payload.get('thread') or EMPTY_STRING), str(self.childPid or 0)])
    def formatError(self, context: str, error=None) -> str:
        label = str(context or 'runtime')
        if isinstance(error, BaseException):
            text = EMPTY_STRING.join(traceback.format_exception(type(error), error, error.__traceback__))
        elif error:
            text = str(error)
        else:
            text = 'Unknown error'
        return f"[PromptDebugger:{label}] {text}".rstrip()
    def warn(self, context: str, error=None):
        text = self.formatError(context, error)
        self.emit(text, self.logPath)
        return text
    def die(self, context: str, error=None):
        text = self.formatError(context, error)
        self.emit(text, self.exceptionPath)
        return text
    def renderParentFrames(self, include_locals: bool = False) -> str:
        frames = dict(sys._current_frames())
        threads = {thread.ident: thread for thread in threading.enumerate()}
        main_ident = getattr(threading.main_thread(), 'ident', None)
        lines = [f"[PromptDebugger] Parent stack dump  PID={os.getpid()}  threads={len(frames)}", EMPTY_STRING]
        for tid, frame in frames.items():
            thread = threads.get(tid)
            name = getattr(thread, 'name', f'Thread-{tid}')
            marker = ' [MAIN]' if tid == main_ident else EMPTY_STRING
            daemon = ' [daemon]' if bool(getattr(thread, 'daemon', False)) else EMPTY_STRING
            lines.append(f"  ─── Thread: {name}  ident={tid}{marker}{daemon} ───")
            try:
                stack = traceback.extract_stack(frame)
            except Exception as error:
                captureException(None, source='start.py', context='except@7949')
                print(f"[WARN:swallowed-exception] start.py:6655 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                lines.append(f"    <extract_stack failed: {type(error).__name__}: {error}>")
                lines.append(EMPTY_STRING)
                continue
            for item in stack:
                lines.append(f'    File "{item.filename}", line {item.lineno}, in {item.name}')
                source = str(item.line or EMPTY_STRING).strip()
                if source:
                    lines.append(f'      {source}')
            if include_locals:
                try:
                    for key, value in list(getattr(frame, 'f_locals', {}).items())[:40]:
                        lines.append(f'      {key} = {safeRepr(value)}')
                except Exception as error:
                    captureException(None, source='start.py', context='except@7963')
                    print(f"[WARN:swallowed-exception] start.py:6668 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    pass
            lines.append(EMPTY_STRING)
        return '\n'.join(lines).rstrip() + '\n'
    def renderParentLocals(self) -> str:
        try:
            lines = [
                EMPTY_STRING,
                self.psColor('=' * 78, PS_BLUE, bold=True),
                self.psColor('Parent Variable Dump', PS_BLUE, bold=True),
                self.psColor('=' * 78, PS_BLUE, bold=True),
                self.formatScalarField('Child PID', getattr(self, 'childPid', 0), PS_WHITE),
                self.formatScalarField('Child Exit Code', getattr(self, 'childExitCode', None), PS_WHITE),
                self.formatScalarField('Last Heartbeat', f"{self.lastHeartbeatReason} @ {self.lastHeartbeat:.3f}", PS_WHITE),
                self.formatScalarField('Last Process Loop', f"{self.lastProcessReason} @ {self.lastProcessLoop:.3f}", PS_WHITE),
                EMPTY_STRING,
            ]
            lines.extend(self.renderThreadFramesSection(self.collectParentThreadFrameVariables()))
            lines.append(self.psColor('=' * 78, PS_BLUE, bold=True))
            return '\n'.join(str(line) for line in lines) + '\n'
        except Exception as error:
            captureException(None, source='start.py', context='except@7984')
            print(f"[WARN:swallowed-exception] start.py:6688 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return f'<variableDump failed: {type(error).__name__}: {error}>\n'
    def capturePreConsoleSnapshot(self, info: dict[str, Any] | None = None):
        header = ['=' * 62, '  PRE-CONSOLE SNAPSHOT', '=' * 62]
        if info:
            header.append(f"  {self.flatlined()}")
            header.append(f"  Exception: {info.get('type_name', 'Unknown')}: {info.get('message', EMPTY_STRING)}")
        header.append(f"  Child PID: {self.childPid or 0}")
        header.append(f"  Last heartbeat: {self.lastHeartbeatReason} @ {self.lastHeartbeat:.6f}"
                       + (f" caller={self.lastHeartbeatCaller}" if getattr(self, 'lastHeartbeatCaller', EMPTY_STRING) else EMPTY_STRING))
        header.append(f"  Last process loop: {self.lastProcessReason} @ {self.lastProcessLoop:.3f}")
        header.append(EMPTY_STRING)
        traceback_text = str((info or {}).get('traceback_text') or EMPTY_STRING).strip()
        self.preConsoleStackText = '\n'.join(header) + self.renderParentFrames()
        if traceback_text:
            self.preConsoleStackText += '\nOriginal traceback:\n' + traceback_text + '\n'
        self.preConsoleVarText = self.renderParentLocals()
        snapshot_text = self.preConsoleStackText + '\n' + self.preConsoleVarText
        snapshot_timestamp = float((info or {}).get('timestamp') or time.time())
        self.write(self.logPath, self.preConsoleStackText)
        saved_row_id = int(self.saveSnapshotToDb(info, snapshot_text, self.preConsoleStackText, self.preConsoleVarText) or 0)
        if saved_row_id <= 0:
            self.appendSnapshotHistory(snapshot_text, timestamp=snapshot_timestamp)
    def dumpStacks(self, include_locals: bool = False):
        if self.preConsoleStackText:
            return self.preConsoleStackText
        return self.renderParentFrames(include_locals=include_locals)
    def variableDump(self):
        if self.preConsoleVarText:
            return self.preConsoleVarText
        return self.renderParentLocals()
    def heartbeatClock(self) -> str:
        try:
            return time.strftime('%H:%M:%S', time.localtime(float(self.lastHeartbeat or time.time())))
        except Exception as error:
            captureException(None, source='start.py', context='except@8019')
            print(f"[WARN:swallowed-exception] start.py:6722 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return time.strftime('%H:%M:%S')

    def clearStatusLine(self):
        try:
            if self.inCrashConsole:
                return
        except Exception as error:
            captureException(None, source='start.py', context='except@8027')
            print(f"[WARN:swallowed-exception] start.py:6729 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        try:
            previous = str(getattr(self, 'lastStatusLine', EMPTY_STRING) or EMPTY_STRING)
            if not previous:
                return
            sys.stderr.write('\r' + (' ' * len(previous)) + '\r')
            sys.stderr.flush()
            self.lastStatusLine = EMPTY_STRING
        except Exception as error:
            captureException(None, source='start.py', context='except@8037')
            print(f"[WARN:swallowed-exception] start.py:6739 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass

    def clearConsoleScreen(self):
        try:
            sys.stdout.write('\033[2J\033[H')
            sys.stdout.flush()
            return
        except Exception:
            captureException(None, source='start.py', context='except@8046')
            print('\n' * 80, end=EMPTY_STRING, flush=True)

    def statusSpinnerLine(self) -> str:
        try:
            frame = self.spinnerFrames[self.spinnerIndex % len(self.spinnerFrames)]
        except Exception as error:
            captureException(None, source='start.py', context='except@8052')
            print(f"[WARN:swallowed-exception] start.py:6753 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            frame = '▁▂▃▆█▆▃▂▁'
        self.spinnerIndex += 1
        heartbeat = self.heartbeatClock()
        override = str(getattr(self, 'statusOverrideReason', EMPTY_STRING) or EMPTY_STRING).strip().upper()
        reason = override or str(self.lastHeartbeatReason or 'INIT').strip().upper()
        md5 = str(getattr(self, 'childMd5Short', EMPTY_STRING) or EMPTY_STRING).strip()
        md5_part = f' md5:{md5}' if md5 else EMPTY_STRING
        return f"{PS_BOLD}{PS_GREEN}FLATLINE Debugger v{DEBUGGER_VERSION}{PS_RESET} {PS_GREEN}{frame}{PS_RESET} heartbeat:{heartbeat} {reason}{md5_part}"
    def statusLoop(self):
        while not self.statusStop.wait(0.10):
            try:
                if self.consoleStop.is_set() or self.inCrashConsole:
                    self.clearStatusLine()
                    continue
                proc = self.child
                if proc is None:
                    self.clearStatusLine()
                    continue
                if proc.poll() is not None:
                    self.clearStatusLine()
                    continue
                line = self.statusSpinnerLine()
                previous = str(getattr(self, 'lastStatusLine', EMPTY_STRING) or EMPTY_STRING)
                pad = max(0, len(previous) - len(line))
                # Use stderr so the spinner is visible in subprocess-relay mode
                # (parent stdout is piped to the child and never reaches the terminal)
                sys.stderr.write('\r' + line + (' ' * pad))
                sys.stderr.flush()
                self.lastStatusLine = line
            except Exception as error:
                captureException(None, source='start.py', context='except@8083')
                print(f"[WARN:swallowed-exception] start.py:6783 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass

    def formatCommandLine(self, args: list[str] | tuple[str, ...] | None = None) -> str:
        parts = [str(part or EMPTY_STRING) for part in list(args or [])]
        if not parts:
            return EMPTY_STRING
        try:
            return subprocess.list2cmdline(parts)
        except Exception as error:
            captureException(None, source='start.py', context='except@8093')
            print(f"[WARN:swallowed-exception] start.py:6792 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return ' '.join(parts)

    def socketReplTargetText(self) -> str:
        try:
            server = getattr(self, 'server', None)
            if server is None:
                return 'OFF'
            host, port = server.server_address[:2]
            return f'{host}:{int(port or 0)}'
        except Exception as error:
            captureException(None, source='start.py', context='except@8104')
            print(f"[WARN:swallowed-exception] start.py:6802 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return 'OFF'

    def shellRpcTargetText(self) -> str:
        try:
            rpc = getattr(self, 'shellRpcServer', None)
            port = int(getattr(rpc, 'port', 0) or 0)
            if port <= 0:
                return 'OFF'
            return f'127.0.0.1:{port}'
        except Exception as error:
            captureException(None, source='start.py', context='except@8115')
            print(f"[WARN:swallowed-exception] start.py:6812 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return 'OFF'

    def childOutputTargetTexts(self) -> tuple[str, str]:
        if self.enabled:
            return ('parent relay (stdout pipe)', 'parent relay (stderr pipe)')
        return ('DEVNULL', 'DEVNULL')

    def safeShortMd5(self, path: str | Path) -> str:  # noqa: nonconform reviewed return contract
        try:
            with File.tracedOpen(Path(path), 'rb') as handle:
                return hashlib.md5(handle.read()).hexdigest()[:8]
        except Exception as error:
            captureException(None, source='start.py', context='except@8128')
            print(f"[WARN:swallowed-exception] start.py:6824 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return EMPTY_STRING

    def safeFileInfo(self, path: str | Path) -> dict[str, Any]:
        target = Path(path)
        info = {
            'path': str(target),
            'mtime_epoch': 0.0,
            'mtime_text': EMPTY_STRING,
            'line_count': 0,
            'non_blank_line_count': 0,
            'size_bytes': 0,
        }
        try:
            stat = target.stat()
            info['mtime_epoch'] = float(getattr(stat, 'st_mtime', 0.0) or 0.0)
            info['size_bytes'] = int(getattr(stat, 'st_size', 0) or 0)
            if info['mtime_epoch'] > 0:
                info['mtime_text'] = datetime.datetime.fromtimestamp(info['mtime_epoch']).isoformat(sep=' ', timespec='microseconds')
        except Exception as error:
            captureException(None, source='start.py', context='except@8148')
            print(f"[WARN:swallowed-exception] start.py:6843 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        try:
            source_text = File.readText(target, encoding='utf-8', errors='replace')
            lines = source_text.splitlines()
            info['line_count'] = len(lines)
            info['non_blank_line_count'] = sum(1 for line in lines if str(line).strip())
        except Exception as error:
            captureException(None, source='start.py', context='except@8156')
            print(f"[WARN:swallowed-exception] start.py:6850 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        return info

    def formatTimestampForStatus(self, stamp: float) -> str:
        try:
            value = float(stamp or 0.0)
        except Exception as error:
            captureException(None, source='start.py', context='except@8164')
            print(f"[WARN:swallowed-exception] start.py:6857 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            value = 0.0
        if value <= 0:
            return '0.000000'
        try:
            text = datetime.datetime.fromtimestamp(value).isoformat(sep=' ', timespec='microseconds')
        except Exception as error:
            captureException(None, source='start.py', context='except@8171')
            print(f"[WARN:swallowed-exception] start.py:6863 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            text = 'unavailable'
        return f'{text} ({value:.6f})'

    def collectRuntimeDbPayload(self, payload: dict | None) -> dict[str, str]:
        payload = dict(payload or {})
        settingsPayload = dict(payload.get('settings', {}) or {})
        nested = dict(payload.get('db', {}) or {})
        settingsDb = dict(settingsPayload.get('db', {}) or {}) if isinstance(settingsPayload.get('db', {}), dict) else {}
        dbPayload: dict[str, str] = {}
        for source_dict in (settingsDb, nested):
            for key, value in dict(source_dict or {}).items():
                if value not in (None, EMPTY_STRING):
                    dbPayload[str(key)] = str(value)
        compatibility_values = {
            'host': str(payload.get('db_host', payload.get('dbHost', settingsPayload.get('db_host', settingsPayload.get('dbHost', dbPayload.get('host', EMPTY_STRING))))) or EMPTY_STRING).strip(),
            'port': str(payload.get('db_port', payload.get('dbPort', settingsPayload.get('db_port', settingsPayload.get('dbPort', dbPayload.get('port', EMPTY_STRING))))) or EMPTY_STRING).strip(),
            'user': str(payload.get('db_user', payload.get('dbUser', settingsPayload.get('db_user', settingsPayload.get('dbUser', dbPayload.get('user', EMPTY_STRING))))) or EMPTY_STRING).strip(),
            'password': str(payload.get('db_password', payload.get('dbPass', settingsPayload.get('db_password', settingsPayload.get('dbPass', dbPayload.get('password', EMPTY_STRING))))) or EMPTY_STRING),
            'database': str(payload.get('db_database', payload.get('db_name', payload.get('dbName', settingsPayload.get('db_database', settingsPayload.get('db_name', settingsPayload.get('dbName', dbPayload.get('database', EMPTY_STRING))))))) or EMPTY_STRING).strip(),
        }
        for key, value in compatibility_values.items():
            if value not in (None, EMPTY_STRING):
                dbPayload[key] = value
        return dbPayload

    def resolveMariaDbStatusTarget(self) -> tuple[str, str, str, str, str]:
        host = str(os.environ.get('TRIO_DB_HOST', EMPTY_STRING) or EMPTY_STRING).strip()
        port = str(os.environ.get('TRIO_DB_PORT', EMPTY_STRING) or EMPTY_STRING).strip()
        user = str(os.environ.get('TRIO_DB_USER', EMPTY_STRING) or EMPTY_STRING).strip()
        database = str(os.environ.get('TRIO_DB_NAME', EMPTY_STRING) or EMPTY_STRING).strip()
        source = 'env'
        if any((host, port, user, database)):
            return host, port, user, database, source
        try:
            if not RUNTIME_SETTINGS_PATH.exists():
                return EMPTY_STRING, EMPTY_STRING, EMPTY_STRING, EMPTY_STRING, 'missing runtime settings'
            payload = json.loads(File.readText(RUNTIME_SETTINGS_PATH, encoding='utf-8', errors='replace') or '{}')
            dbPayload = collectRuntimeDbPayload(payload)
            return (
                str(dbPayload.get('host', EMPTY_STRING) or EMPTY_STRING).strip(),
                str(dbPayload.get('port', EMPTY_STRING) or EMPTY_STRING).strip(),
                str(dbPayload.get('user', EMPTY_STRING) or EMPTY_STRING).strip(),
                str(dbPayload.get('database', EMPTY_STRING) or EMPTY_STRING).strip(),
                'runtime settings',
            )
        except Exception as error:
            captureException(None, source='start.py', context='except@8218')
            print(f"[WARN:swallowed-exception] start.py:6909 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return EMPTY_STRING, EMPTY_STRING, EMPTY_STRING, EMPTY_STRING, 'unavailable'

    def statusText(self) -> str:
        python_path = str(getattr(self, 'childPythonPath', EMPTY_STRING) or sys.executable or EMPTY_STRING)
        launch_args = [*childPythonCommandPrefix(python_path), str(GTP_PATH), *list(self.childArgs or [])]
        command_line = self.formatCommandLine(launch_args)
        stdout_target, stderr_target = self.childOutputTargetTexts()
        backend = str(os.environ.get('TRIO_DB_BACKEND', 'sqlite') or 'sqlite').strip().lower()
        now_epoch = time.time()
        runtime_microseconds = max(0, int(round((now_epoch - float(getattr(self, 'debuggerStartTime', now_epoch) or now_epoch)) * 1000000.0)))
        target_info = self.safeFileInfo(GTP_PATH)
        debugger_md5 = self.safeShortMd5(__file__)
        target_md5 = str(getattr(self, 'childMd5Short', EMPTY_STRING) or EMPTY_STRING).strip() or self.safeShortMd5(GTP_PATH)
        status_lines = [
            EMPTY_STRING, '=' * 62, '  PromptDebugger Status', '=' * 62,
            f'  Debugger version: {self.debuggerVersion}',
            f'  System time: {self.formatTimestampForStatus(now_epoch)}',
            f'  Debugger started: {self.formatTimestampForStatus(getattr(self, "debuggerStartTime", now_epoch))}',
            f'  Runtime: {runtime_microseconds} microseconds ({runtime_microseconds / 1000000.0:.6f} seconds)',
            f'  PID: {os.getpid()}',
            f'  Child PID: {self.childPid or 0}',
            f'  Child alive: {"YES" if getattr(getattr(self, "child", None), "poll", lambda: None)() is None and getattr(self, "child", None) is not None else "NO"}',
            f'  Child exit code: {self.childExitCode}',
            f'  Child control port: {self.childControlPort or 0}',
            f'  Launch mode: {"debug / parent relay" if self.enabled else "production / detached child"}',
            f'  Offscreen: {"ON" if bool(getattr(self, "offscreenEnabled", False)) else "OFF"}',
            f'  Offscreen display: {str(getattr(getattr(self, "offscreenSession", None), "displayName", EMPTY_STRING) or "(none)")}',
            f'  Python: {python_path}',
            f'  Running: {GTP_PATH}',
            f'  Args: {" ".join(str(arg) for arg in list(self.childArgs or [])) or "(none)"}',
            f'  Command line: {command_line}',
            f'  Debugger md5: {debugger_md5 or "(unavailable)"}',
            f'  Target md5: {target_md5 or "(unavailable)"}',
            f'  Child filemtime: {str(target_info.get("mtime_text", EMPTY_STRING) or "unavailable")} ({float(target_info.get("mtime_epoch", 0.0) or 0.0):.6f})',
            f'  Child lines: {int(target_info.get("line_count", 0) or 0)}',
            f'  Child non-blank lines: {int(target_info.get("non_blank_line_count", 0) or 0)}',
            f'  Last heartbeat: {self.lastHeartbeatReason} @ {self.formatTimestampForStatus(self.lastHeartbeat)}',
            f'  Last process loop: {self.lastProcessReason} @ {self.formatTimestampForStatus(self.lastProcessLoop)}',
            f'  Child surfaces: {", ".join(sorted(getattr(self, "childSurfaces", set()) or set())) if bool(getattr(self, "childSurfacesKnown", False)) else "(probing)"}',
            f'  Missing surfaces: {", ".join(self.childSurfaceMissingList()) or "(none)"}',
            f'  Debug log: {self.logPath}',
            f'  Snapshots: {self.snapshotPath}',
            f'  Exceptions: {self.exceptionPath}',
            f'  Child stdout: {stdout_target}',
            f'  Child stderr: {stderr_target}',
            f'  Runtime settings: {RUNTIME_SETTINGS_PATH}',
            f'  DB backend: {backend}',
        ]
        if backend == 'sqlite':
            sqlite_path = str(os.environ.get('TRIO_SQLITE_PATH', EMPTY_STRING) or self.resolveSqlitePath() or EMPTY_STRING).strip()
            status_lines.append(f'  DB target: {sqlite_path or "(unresolved)"}')
        else:
            host, port, user, database, source = self.resolveMariaDbStatusTarget()
            target = host or 'localhost'
            if port:
                target += f':{port}'
            if database:
                target += f'/{database}'
            if user:
                target += f' user={user}'
            status_lines.append(f'  DB target: {target or "(unresolved)"}')
            status_lines.append(f'  DB source: {source}')
        status_lines += [
            f'  Socket REPL: {self.socketReplTargetText()}',
            f'  Shell RPC: {self.shellRpcTargetText()}',
            f'  Relay port: {self.relayPort}',
            f'  Connection monitor: {"ON" if self.connectionMonitorEnabled else "OFF"}',
            f'  Proxy bind: {getattr(getattr(self, "trafficProxyServer", None), "endpointUrl", lambda: "(offline)")() if getattr(self, "trafficProxyServer", None) is not None else "(offline)"}',
            f'  Proxy mode: {str(getattr(getattr(self, "trafficProxyServer", None), "mode", EMPTY_STRING) or "(none)")}',
            '=' * 62,
        ]
        return '\n'.join(status_lines) + '\n'
    def printLogs(self) -> str:
        """Dump all three log files to the console in one shot."""
        log_paths = [
            ('Debug Log',     self.logPath),
            ('Snapshots',     self.snapshotPath),
            ('Exceptions Log', self.exceptionPath),
        ]
        out = ['\n' + '=' * 62, '  -- Log Dump --', '=' * 62]
        for label, path in log_paths:
            out.append(f'\n  [{label}]  {path}')
            out.append('  ' + '-' * 58)
            try:
                text = File.readText(Path(path), encoding='utf-8', errors='replace')
                if text.strip():
                    for line in text.splitlines():
                        out.append('  ' + line)
                else:
                    out.append('  (empty)')
            except FileNotFoundError:
                captureException(None, source='start.py', context='except@8310')
                out.append('  (file not found)')
            except Exception as e:
                captureException(None, source='start.py', context='except@8312')
                print(f"[WARN:swallowed-exception] start.py:7003 {type(e).__name__}: {e}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                out.append(f'  (read error: {e})')
        out.append('=' * 62)
        return '\n'.join(out)

    def formatExceptionRowText(self, row: dict | None) -> str:
        payload = dict(row or {})
        handled_text = 'HANDLED' if int(payload.get('handled', 0) or 0) else 'UNHANDLED'
        lines = [
            self.psColor('=' * 78, PS_RED if handled_text == 'UNHANDLED' else PS_YELLOW, bold=True),
            self.psColor('Exception Row from Exceptions Table', PS_RED if handled_text == 'UNHANDLED' else PS_YELLOW, bold=True),
            self.psColor('=' * 78, PS_RED if handled_text == 'UNHANDLED' else PS_YELLOW, bold=True),
            self.psColor('created', PS_YELLOW, bold=True) + self.psColor(' = ', PS_DIM) + self.psColor(str(payload.get('created', EMPTY_STRING) or EMPTY_STRING), PS_WHITE),
            self.psColor('context', PS_YELLOW, bold=True) + self.psColor(' = ', PS_DIM) + self.psColor(str(payload.get('context', EMPTY_STRING) or EMPTY_STRING), PS_WHITE),
            self.psColor('type', PS_YELLOW, bold=True) + self.psColor(' = ', PS_DIM) + self.psColor(str(payload.get('type_name', EMPTY_STRING) or EMPTY_STRING), PS_WHITE),
            self.psColor('handled', PS_YELLOW, bold=True) + self.psColor(' = ', PS_DIM) + self.psColor(handled_text, PS_WHITE),
            self.psColor('thread', PS_YELLOW, bold=True) + self.psColor(' = ', PS_DIM) + self.psColor(str(payload.get('thread', EMPTY_STRING) or EMPTY_STRING), PS_WHITE),
            self.psColor('pid', PS_YELLOW, bold=True) + self.psColor(' = ', PS_DIM) + self.psColor(str(payload.get('pid', 0) or 0), PS_WHITE),
            self.psColor('source', PS_YELLOW, bold=True) + self.psColor(' = ', PS_DIM) + self.psColor(str(payload.get('source', EMPTY_STRING) or EMPTY_STRING), PS_WHITE),
        ]
        message_text = str(payload.get('message', EMPTY_STRING) or EMPTY_STRING).strip()
        if message_text:
            lines.append(EMPTY_STRING)
            lines.append(self.psColor('Message', PS_CYAN, bold=True))
            lines.extend(self.psColor(line, PS_WHITE) for line in message_text.splitlines())
        traceback_text = str(payload.get('traceback_text', EMPTY_STRING) or EMPTY_STRING).strip()
        if traceback_text:
            lines.append(EMPTY_STRING)
            lines.append(self.psColor('Traceback', PS_CYAN, bold=True))
            lines.extend(self.psColor(line, PS_WHITE) for line in traceback_text.splitlines())
        source_context = str(payload.get('source_context', EMPTY_STRING) or EMPTY_STRING).strip()
        if source_context:
            lines.append(EMPTY_STRING)
            lines.append(self.psColor('Source Context', PS_CYAN, bold=True))
            lines.extend(self.psColor(line, PS_DIM if not line.strip() else PS_WHITE) for line in source_context.splitlines())
        lines.append(self.psColor('=' * 78, PS_RED if handled_text == 'UNHANDLED' else PS_YELLOW, bold=True))
        return '\n'.join(lines)

    def inspectExceptionRows(self, limit: int = 20) -> str:
        rows = list(cast(Any, getattr(self, 'db', None)).readRecentExceptionRows(limit=limit, include_processed=True) or []) if getattr(self, 'db', None) is not None else []
        if not rows:
            return '[PromptDebugger] No exception rows found.'
        unprocessed_ids = [int(row.get('id', 0) or 0) for row in rows if int(row.get('processed', 0) or 0) == 0 and int(row.get('id', 0) or 0) > 0]
        if unprocessed_ids:
            try:
                self.db.markExceptionProcessed(unprocessed_ids)
            except Exception as error:
                captureException(None, source='start.py', context='except@8359')
                print(f"[WARN:swallowed-exception] start.py:7049 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
        rendered = []
        for row in rows:
            rendered.append(self.formatExceptionRowText(row))
        return '\n\n'.join(rendered)

    def renderCrashContext(self, context_lines: int = 10) -> list[str]:
        """
        Parse the pre-console stack text for the deepest gtp.py / start.py frame,
        read the source file, and return a snippet centred on the crash line like:

          In TrioDesktop::__init__()
          Line 801:   something()
          Line 802:   print(8)
          Line 803: ***CRASHED*** while(x < 0): x--
          Line 804:   return
          Line 805:   def y():
        """
        source_text = str(getattr(self, 'preConsoleStackText', EMPTY_STRING) or EMPTY_STRING)
        traceback_text = str((self.crashInfo or {}).get('traceback_text', EMPTY_STRING) or EMPTY_STRING)
        combined = (traceback_text + '\n' + source_text).strip()
        if not combined:
            return []

        # Pull every  File "...", line N, in func  entry — deepest relevant frame wins
        pattern = re.compile(r'File "([^"]+)", line (\d+), in (\S+)')
        best_file = EMPTY_STRING
        best_line = 0
        best_func = EMPTY_STRING
        for match in pattern.finditer(combined):
            fname, lineno, func = match.group(1), int(match.group(2)), match.group(3)
            # Prefer gtp.py frames over library frames
            is_app = any(k in fname for k in ('gtp.py', 'start.py', 'triodesktop'))
            if is_app or (not best_file):
                best_file, best_line, best_func = fname, lineno, func

        if not best_file or best_line <= 0:
            return []

        try:
            with File.tracedOpen(best_file, 'r', encoding='utf-8', errors='replace') as fh:
                all_lines = fh.readlines()
        except Exception as error:
            captureException(None, source='start.py', context='except@8403')
            print(f"[WARN:swallowed-exception] start.py:7092 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return []

        total = len(all_lines)
        start = max(0, best_line - context_lines - 1)
        end   = min(total, best_line + context_lines)
        snippet = []
        short_file = Path(best_file).name
        snippet.append(f'  In {short_file}::{best_func}()')
        for idx in range(start, end):
            lineno = idx + 1
            src = all_lines[idx].rstrip()
            if lineno == best_line:
                snippet.append(f'  Line {lineno}: ***CRASHED*** {src.strip()}')
            else:
                snippet.append(f'  Line {lineno}:   {src.strip()}')
        return snippet

    def ansiDim(self, text: str) -> str:
        value = str(text or EMPTY_STRING)
        return f'\x1b[90m{value}\x1b[0m'

    def ansiGreen(self, text: str) -> str:
        value = str(text or EMPTY_STRING)
        return f'\x1b[32m{value}\x1b[0m'

    def ansiRed(self, text: str) -> str:
        value = str(text or EMPTY_STRING)
        return f'\x1b[31m{value}\x1b[0m'

    def greyBoxLine(self, text: str, width: int = 78) -> str:
        value = str(text or EMPTY_STRING)
        plain = re.sub(r'\x1b\[[0-9;]*m', EMPTY_STRING, value)
        pad = max(0, width - len(plain))
        return f"\x1b[90m║\x1b[0m {value}{' ' * pad} \x1b[90m║\x1b[0m"

    def debuggerTitleLines(self) -> list[str]:
        heart = self.psColor('▁▂▃▆█▆▄▃▂▁', PS_GREEN, bold=True)
        return [
            f"\x1b[90m╔{'═' * 80}╗\x1b[0m",
            self.greyBoxLine(self.psColor(f'FLATLINE Debugger v{DEBUGGER_VERSION}', PS_WHITE, bold=True)),
            self.greyBoxLine(f'{heart}  {self.flatlined()}'),
            f"\x1b[90m╠{'═' * 80}╣\x1b[0m",
        ]

    def colorizeMenuHotkey(self, text: str, color: str = 'green') -> str:
        value = str(text or EMPTY_STRING)
        start = value.find('(')
        if start < 0:
            return value
        end = value.find(')', start + 1)
        if end != start + 2:
            return value
        key = value[start + 1:end]
        painter = self.ansiGreen if str(color or 'green').strip().lower() == 'green' else self.ansiRed
        return value[:start] + painter(key) + value[end + 1:]

    def childSurfaceSupported(self, surface: str, default: bool = True) -> bool:
        name = str(surface or EMPTY_STRING).strip().lower()
        if not name:
            return bool(default)
        if not bool(getattr(self, 'childSurfacesKnown', False)):
            return bool(default)
        current = {str(item or EMPTY_STRING).strip().lower() for item in set(getattr(self, 'childSurfaces', set()) or set())}
        return name in current

    def childSurfaceMissingList(self) -> list[str]:
        if not bool(getattr(self, 'childSurfacesKnown', False)):
            return []
        current = {str(item or EMPTY_STRING).strip().lower() for item in set(getattr(self, 'childSurfaces', set()) or set())}
        return [surface for surface in KNOWN_CHILD_DEBUGGER_SURFACES if str(surface or EMPTY_STRING).strip().lower() not in current]

    def setChildSurfaces(self, surfaces, source: str = 'child') -> set[str]:
        rows = []
        if isinstance(surfaces, str):
            iterable = [token for token in str(surfaces or EMPTY_STRING).replace(',', ' ').split() if str(token or EMPTY_STRING).strip()]
        else:
            iterable = list(surfaces or [])
        for raw in iterable:
            token = str(raw or EMPTY_STRING).strip().lower()
            if token:
                rows.append(token)
        self.childSurfaces = set(rows)
        self.childSurfacesKnown = bool(rows)
        missing = self.childSurfaceMissingList()
        if missing and not bool(getattr(self, 'childSurfaceWarned', False)):
            self.childSurfaceWarned = True
            if childDebuggerTargetLabel().lower() == 'gtp.py':
                self.emit(f"[WARNING:debugger-surfaces] {childDebuggerTargetLabel()} missing debugger surfaces: {', '.join(missing)}", self.exceptionPath)
        return set(self.childSurfaces)

    def queryChildSurfaces(self, timeout: float = 2.0, retries: int = 2) -> set[str]:
        child_python = str(getattr(self, 'childPythonPath', EMPTY_STRING) or resolvePreferredChildPython(guiRequired=True) or sys.executable or EMPTY_STRING).strip()
        args = [*childPythonCommandPrefix(child_python), str(GTP_PATH), '--debugger-query-surfaces']
        for _ in range(max(1, int(retries or 1))):
            try:
                completed = startLifecycleRunCommand(
                    args,
                    cwd=str(BASE_DIR),
                    capture_output=True,
                    text=True,
                    timeout=max(0.50, float(timeout or 0.0)),
                    encoding='utf-8',
                    errors='replace',
                )
                output = ((completed.stdout or EMPTY_STRING) + '\n' + (completed.stderr or EMPTY_STRING)).strip()
                surfaces = [token.strip().lower() for token in output.replace(',', ' ').split() if token.strip()]
                filtered = [token for token in surfaces if token in KNOWN_CHILD_DEBUGGER_SURFACES]
                if filtered:
                    return self.setChildSurfaces(filtered, source='debugger-query-surfaces-cli')
            except Exception as error:
                captureException(None, source='start.py', context='except@8514')
                print(f"[WARN:swallowed-exception] start.py:7202 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                try:
                    self.emit(f'[WARNING:debugger-surfaces] CLI surface probe failed: {type(error).__name__}: {error}', self.exceptionPath)
                except Exception as error:
                    captureException(None, source='start.py', context='except@8518')
                    print(f"[WARN:swallowed-exception] start.py:7205 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    pass
            time.sleep(0.10)
        return set(getattr(self, 'childSurfaces', set()) or set())

    def menuSurfaceLine(self, text: str, surface: str = EMPTY_STRING) -> str:
        label = self.colorizeMenuHotkey(text, 'green')
        surface_name = str(surface or EMPTY_STRING).strip().lower()
        if not surface_name or self.childSurfaceSupported(surface_name, default=True):
            return label
        return self.ansiDim(str(text or EMPTY_STRING) + '  [unavailable]')

    def childSurfaceUnavailableText(self, surface: str, label: str) -> str:
        return f'[PromptDebugger] {str(label or surface or "feature")} unavailable — {childDebuggerTargetLabel()} does not implement surface: {str(surface or EMPTY_STRING)}'

    def exitMenuLine(self) -> str:
        return self.colorizeMenuHotkey('  E(x)it Child Gracefully', 'red')

    def debuggerMenuLines(self) -> list[str]:
        return [
            self.colorizeMenuHotkey('  (L)ines of Crash', 'green'),
            self.colorizeMenuHotkey('  (S)tatus', 'green'),
            self.colorizeMenuHotkey('  (D)ump Logs', 'green'),
            self.colorizeMenuHotkey('  (I)nspect Exceptions', 'green'),
            self.menuSurfaceLine('  (V)ariables', 'vardump'),
            self.menuSurfaceLine('  (M)onitor Connections', 'accepts-proxy'),
            self.menuSurfaceLine('  (E)xecute Code', 'debugger-exec-command'),
            self.menuSurfaceLine('  (C)reate Cron', 'debugger-cron-command'),
            self.colorizeMenuHotkey('  (G)enerate AST Tree', 'green'),
            self.colorizeMenuHotkey('  (J) Git Tools', 'green'),
            self.colorizeMenuHotkey('  (K)ill Child', 'green'),
            self.colorizeMenuHotkey('  (R)estart Child', 'green'),
            self.colorizeMenuHotkey('  (B)lank Screen', 'green'),
            self.colorizeMenuHotkey('  (H)elp', 'green'),
            EMPTY_STRING,
            self.exitMenuLine(),
            self.colorizeMenuHotkey('  (Q)uit', 'green'),
        ]

    def helpScreenLines(self) -> list[str]:
        rows = [
            self.greyBoxLine(self.psColor('Debugger Whitepaper', PS_WHITE, bold=True)),
            self.greyBoxLine('L  Lines of Crash     Read-only Prism / WebEngine source viewer with print, save, and PDF.'),
            self.greyBoxLine('S  Status             Parent / child status, hashes, file targets, relay, proxy, and heartbeat.'),
            self.greyBoxLine('D  Dump Logs          Print launcher and child logs.'),
            self.greyBoxLine('I  Inspect Exceptions Read recent exception rows from the exceptions table and mark new ones processed.'),
            self.greyBoxLine('V  Variables          Parent var dump + fresh child var dump request.'),
            self.greyBoxLine('M  Monitor Connections Tail traffic rows written by the FlatLine monitor daemon.'),
            self.greyBoxLine('E  Execute Code       Editable WebEngine / Prism editor with Submit. Inline Python or >file.py.'),
            self.greyBoxLine('C  Create Cron        Editable WebEngine / Prism editor with Submit and interval seconds.'),
            self.greyBoxLine('G  Generate AST Tree  Two-tab AST viewer: Qt tree + Prism source tab.'),
            self.greyBoxLine('J  Git Tools          Open Git / GitHub helper commands.'),
            self.greyBoxLine('   gs                 status'),
            self.greyBoxLine('   ga                 add'),
            self.greyBoxLine('   gc                 commit'),
            self.greyBoxLine('   gb                 branch'),
            self.greyBoxLine('   gd / bd            diff'),
            self.greyBoxLine('   gp / gpush         push'),
            self.greyBoxLine('   gu / gpull         pull'),
            self.greyBoxLine('   gr                 remote'),
            self.greyBoxLine('   gl                 log'),
            self.greyBoxLine('   gf                 fetch'),
            self.greyBoxLine('   gi                 gitignore'),
            self.greyBoxLine('   gm / man / help    Git tools help'),
            self.greyBoxLine('   gh ...             GitHub CLI passthrough'),
            self.greyBoxLine('   auth               GitHub auth modal (username + password/token)'),
            self.greyBoxLine('K  Kill Child         Force kill the child process.'),
            self.greyBoxLine('R  Restart Child      Relaunch prompt_app.py after it stops.'),
            self.greyBoxLine('B  Blank Screen       Clear the console and redraw the debugger.'),
            self.greyBoxLine('X  Exit Child         Ask prompt_app.py to shut down gracefully.'),
            self.greyBoxLine('Q  Quit               Leave the debugger.'),
            f"[90m╠{'═' * 80}╣[0m",
            self.greyBoxLine('DB debugger transport uses the heartbeat table. Execute clears one-shot code first, cron persists.'),
            self.greyBoxLine('Traffic monitor mode reads the traffic table and shows destination, caller, headers, status, and previews.'),
            self.greyBoxLine('Offscreen modes are owned by start.py. It can launch Xvfb / Xdummy / Xpra and script clicks, moves, waits, and keys.'),
            self.greyBoxLine('Deploy monitor mode watches for zip drops, extracts them, flattens wrapper folders, and relaunches the best candidate.'),
            self.greyBoxLine('CLI info aliases: /help -help --help /? usage -usage --usage -u man ver about'),
            f"[90m╚{'═' * 80}╝[0m",
        ]
        return [EMPTY_STRING, *self.debuggerTitleLines(), *rows]

    def printBanner(self):
        self.clearStatusLine()
        info = self.crashInfo or {}
        crash_ctx = self.renderCrashContext()
        rows = [
            self.greyBoxLine(f"Exception: {info.get('type_name', 'Unknown')}: {info.get('message', EMPTY_STRING)}"),
            self.greyBoxLine(f'PID: {os.getpid()}'),
            self.greyBoxLine(f'Child PID: {self.childPid or 0}'),
            self.greyBoxLine(f'Child control port: {self.childControlPort or 0}'),
            self.greyBoxLine(f'Last Process: {time.strftime("%Y-%m-%d %H:%M:%S")}'),
        ]
        if crash_ctx:
            rows.append(f"[90m╠{'═' * 80}╣[0m")
            rows.append(self.greyBoxLine(self.psColor('Crash Location', PS_WHITE, bold=True)))
            for row in crash_ctx:
                rows.append(self.greyBoxLine(row))
        rows.append(f"[90m╠{'═' * 80}╣[0m")
        rows.append(self.greyBoxLine(self.psColor('Debugger Menu', PS_WHITE, bold=True)))
        for row in self.debuggerMenuLines():
            rows.append(self.greyBoxLine(row)) if row else rows.append(self.greyBoxLine(EMPTY_STRING))
        rows.append(f"[90m╠{'═' * 80}╣[0m")
        rows.append(self.greyBoxLine(f'Debug Log: {self.logPath}'))
        rows.append(self.greyBoxLine(f'Snapshots: {self.snapshotPath}'))
        rows.append(self.greyBoxLine(f'Exceptions: {self.exceptionPath}'))
        rows.append(f"[90m╚{'═' * 80}╝[0m")
        self.emit('\n'.join([EMPTY_STRING, *self.debuggerTitleLines(), *rows]), self.exceptionPath)

    def sendChildControlRequest(self, command: str, extra: dict | None = None, timeout: float = 2.0) -> dict:
        proc = getattr(self, 'child', None)
        if proc is None:
            return {'ok': False, 'error': 'no child process'}
        try:
            if proc.poll() is not None:
                self.markChildExited(proc)
                return {'ok': False, 'error': 'child process already exited'}
        except Exception as error:
            captureException(None, source='start.py', context='except@8635')
            print(f"[WARN:swallowed-exception] start.py:7321 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        port = int(getattr(self, 'childControlPort', 0) or 0)
        if port <= 0:
            return {'ok': False, 'error': 'child control port unavailable'}
        payload = {'token': self.relayToken, 'command': str(command or EMPTY_STRING).strip(), 'timestamp': time.time()}
        if isinstance(extra, dict):
            try:
                payload.update(extra)
            except Exception as error:
                captureException(None, source='start.py', context='except@8645')
                print(f"[WARN:swallowed-exception] start.py:7330 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
        data = (json.dumps(payload, ensure_ascii=False) + '\n').encode('utf-8', 'replace')
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=max(0.25, float(timeout or 0.0))) as sock:
                sock.settimeout(max(0.25, float(timeout or 0.0)))
                sock.sendall(data)
                raw = sock.recv(1024 * 1024)
            if not raw:
                return {'ok': True}
            try:
                return json.loads(raw.decode('utf-8', 'replace').strip() or '{}')
            except Exception as error:
                captureException(None, source='start.py', context='except@8658')
                print(f"[WARN:swallowed-exception] start.py:7342 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                return {'ok': True, 'text': raw.decode('utf-8', 'replace')}
        except Exception as outer_error:
            captureException(None, source='start.py', context='except@8661')
            print(f"[WARN:swallowed-exception] start.py:7344 {type(outer_error).__name__}: {outer_error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            try:
                if proc.poll() is not None:
                    self.markChildExited(proc)
                    return {'ok': False, 'error': 'child process already exited'}
            except Exception as poll_error:
                captureException(None, source='start.py', context='except@8667')
                print(f"[WARN:swallowed-exception] start.py:7349 {type(poll_error).__name__}: {poll_error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
            return {'ok': False, 'error': str(outer_error or 'control request failed')}

    def sendChildCommand(self, command: str) -> bool:
        reply = self.sendChildControlRequest(command, timeout=0.75)
        return bool(reply.get('ok'))

    def requestChildSnapshot(self, command: str = 'snapshot', timeout: float = 1.0, reason: str = EMPTY_STRING) -> bool:
        proc = self.child
        if proc is None:
            return False
        try:
            if proc.poll() is not None:
                self.markChildExited(proc)
                return False
        except Exception as error:
            captureException(None, source='start.py', context='except@8684')
            print(f"[WARN:swallowed-exception] start.py:7365 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        before_id = self.latestSnapshotRowId()
        if not self.sendChildCommand(command):
            return False
        try:
            self.waitForSnapshotRowAfter(before_id, timeout=max(0.05, float(timeout or 0.0)))
        except Exception as error:
            captureException(None, source='start.py', context='except@8692')
            print(f"[WARN:swallowed-exception] start.py:7372 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        if reason:
            try:
                self.emit(f'[TRACE:child-snapshot-request] command={command} reason={reason}', self.logPath)
            except Exception as error:
                captureException(None, source='start.py', context='except@8698')
                print(f"[WARN:swallowed-exception] start.py:7377 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
        return True

    def waitForChildExit(self, timeout: float = 3.0) -> bool:
        proc = self.child
        if proc is None:
            return True
        deadline = time.time() + max(0.1, float(timeout or 0.0))
        while time.time() < deadline:
            if proc.poll() is not None:
                self.markChildExited(proc)
                return True
            time.sleep(0.1)
        if proc.poll() is not None:
            self.markChildExited(proc)
            return True
        return False

    def markChildExited(self, proc = None):
        target = proc if proc is not None else self.child
        if target is None:
            self.child = None
            self.childPid = 0
            self.childControlPort = 0
            return
        try:
            code = target.poll()
        except Exception as error:
            captureException(None, source='start.py', context='except@8727')
            print(f"[WARN:swallowed-exception] start.py:7405 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            code = getattr(target, 'returncode', None)
        if code is not None:
            try:
                self.childExitCode = int(code)
            except Exception as error:
                captureException(None, source='start.py', context='except@8733')
                print(f"[WARN:swallowed-exception] start.py:7410 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                self.childExitCode = 0
            try:
                row_id = int(getattr(self, 'childProcessRowId', 0) or 0)
                if row_id > 0:
                    status_text = 'complete' if int(self.childExitCode or 0) == 0 else 'errored'
                    self.db.updateProcessRecord(row_id, status=status_text, ended_at=time.time(), exit_code=int(self.childExitCode or 0), error_message=(EMPTY_STRING if status_text == 'complete' else f'child_exit_code={self.childExitCode}'), processed=0)
            except Exception as process_error:
                captureException(None, source='start.py', context='except@8741')
                print(f"[WARN:swallowed-exception] start.py:7417 {type(process_error).__name__}: {process_error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                self.emit(f'[WARNING:process-db] child exit update failed: {type(process_error).__name__}: {process_error}', self.exceptionPath)
        if target is self.child:
            self.child = None
            self.childPid = 0
            self.childControlPort = 0
        self.closeChildStreams()
    def terminateChild(self, force: bool = False, tree: bool = True) -> str:
        proc = self.child
        if proc is None or proc.poll() is not None:
            return '[PromptDebugger] No child process running.'
        try:
            if os.name == 'nt':
                if force:
                    args = ['taskkill', '/F', '/PID', str(proc.pid)]
                    label = 'KILL'
                    if tree:
                        args.insert(2, '/T')
                        label = 'KILL TREE'
                    completed = startLifecycleRunCommand(args, capture_output=True, text=True, timeout=12)
                    output = ((completed.stdout or EMPTY_STRING) + (completed.stderr or EMPTY_STRING)).strip()
                    if completed.returncode == 0:
                        return '[PromptDebugger] ' + label
                    return '[PromptDebugger] ' + label + '\n' + (output or '[no output]')
                if tree:
                    completed = startLifecycleRunCommand(['taskkill', '/PID', str(proc.pid)], capture_output=True, text=True, timeout=12)
                    output = ((completed.stdout or EMPTY_STRING) + (completed.stderr or EMPTY_STRING)).strip()
                    if completed.returncode == 0:
                        return '[PromptDebugger] SIGTERM'
                    return '[PromptDebugger] SIGTERM\n' + (output or '[no output]')
                try:
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                    return '[PromptDebugger] CLOSE'
                except Exception as error:
                    captureException(None, source='start.py', context='except@8775')
                    print(f"[WARN:swallowed-exception] start.py:7450 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    proc.terminate()
                    return '[PromptDebugger] CLOSE'
            if force:
                if tree:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    return '[PromptDebugger] KILL TREE'
                proc.kill()
                return '[PromptDebugger] KILL'
            if tree:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    return '[PromptDebugger] SIGTERM'
                except Exception as error:
                    captureException(None, source='start.py', context='except@8789')
                    print(f"[WARN:swallowed-exception] start.py:7463 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    proc.terminate()
                    return '[PromptDebugger] SIGTERM'
            proc.terminate()
            return '[PromptDebugger] CLOSE'
        except Exception as error:
            captureException(None, source='start.py', context='except@8795')
            print(f"[WARN:swallowed-exception] start.py:7468 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return f'[PromptDebugger] Child terminate failed: {error}'

    def closeCrashConsoleIfIdle(self, reason: str = EMPTY_STRING) -> str:
        proc = self.child
        if proc is not None:
            try:
                if proc.poll() is None:
                    return EMPTY_STRING
                self.markChildExited(proc)
            except Exception as error:
                captureException(None, source='start.py', context='except@8806')
                print(f"[WARN:swallowed-exception] start.py:7478 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                self.markChildExited(proc)
        self.inCrashConsole = False
        self.consoleDone.set()
        self.consoleStop.set()
        self.consoleWake.set()
        suffix = f' ({reason})' if reason else EMPTY_STRING
        return f'[PromptDebugger] Crash console closed; debugger shutting down{suffix}'
    def requestClose(self):
        proc = self.child
        if proc is None or proc.poll() is not None:
            return self.closeCrashConsoleIfIdle('no child process running')
        if self.sendChildCommand('close'):
            def finishClose():
                if self.waitForChildExit(3.0):
                    self.closeCrashConsoleIfIdle('child exited after close')
                    return '[PromptDebugger] CLOSE complete'
                return '[PromptDebugger] CLOSE requested (child still shutting down)'
            self.runActionAsync('CloseChild', finishClose)
            return '[PromptDebugger] CLOSE requested'
        return '[PromptDebugger] CLOSE command unavailable'
    def requestSigterm(self):
        proc = self.child
        if proc is None or proc.poll() is not None:
            return self.closeCrashConsoleIfIdle('no child process running')
        def doSigterm():
            if os.name == 'nt':
                completed = startLifecycleRunCommand(['taskkill', '/PID', str(proc.pid)], capture_output=True, text=True, timeout=12)
                output = ((completed.stdout or EMPTY_STRING) + (completed.stderr or EMPTY_STRING)).strip()
                if completed.returncode == 0:
                    self.waitForChildExit(1.5)
                    self.closeCrashConsoleIfIdle('child exited after sigterm')
                    return '[PromptDebugger] SIGTERM complete'
                return '[PromptDebugger] SIGTERM\n' + (output or '[no output]')
            proc.terminate()
            self.waitForChildExit(1.5)
            self.closeCrashConsoleIfIdle('child exited after sigterm')
            return '[PromptDebugger] SIGTERM complete'
        self.runActionAsync('SigtermChild', doSigterm)
        return '[PromptDebugger] SIGTERM requested'
    def requestKill(self):
        proc = self.child
        if proc is None or proc.poll() is not None:
            return self.closeCrashConsoleIfIdle('no child process running')
        def doKill():
            result = self.terminateChild(force=True, tree=False)
            self.waitForChildExit(1.5)
            self.closeCrashConsoleIfIdle('child exited after kill')
            return result
        self.runActionAsync('KillChild', doKill)
        return '[PromptDebugger] KILL requested'

    def locateCrashSource(self) -> tuple[str, int, str]:
        combined = str((self.crashInfo or {}).get('traceback_text', EMPTY_STRING) or EMPTY_STRING)  # noqa: badcode reviewed detector-style finding
        combined += '\n' + str(getattr(self, 'preConsoleStackText', EMPTY_STRING) or EMPTY_STRING)
        best_file = EMPTY_STRING
        best_line = 0
        best_func = EMPTY_STRING
        for match in re.finditer(r'File "([^"]+)", line (\d+), in (\S+)', combined):
            file_name, line_text, func_name = match.group(1), match.group(2), match.group(3)
            try:
                line_number = int(line_text)
            except Exception as error:
                captureException(None, source='start.py', context='except@8869')
                print(f"[WARN:swallowed-exception] start.py:7540 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                line_number = 0
            is_app = any(token in str(file_name or EMPTY_STRING) for token in ('gtp.py', 'start.py', 'triodesktop'))
            if is_app or not best_file:
                best_file, best_line, best_func = str(file_name or EMPTY_STRING), int(line_number or 0), str(func_name or EMPTY_STRING)
        if not best_file:
            best_file = str(GTP_PATH)
            best_line = 1
            best_func = 'module'
        return best_file, int(best_line or 1), best_func

    def buildDebuggerSourceSnippet(self, file_name: str, line_number: int, context_lines: int = 14) -> str:
        try:
            source_lines = File.readText(Path(str(file_name or EMPTY_STRING)), encoding='utf-8', errors='replace').splitlines()
        except Exception as error:
            captureException(None, source='start.py', context='except@8884')
            print(f"[WARN:swallowed-exception] start.py:7554 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return f'# unable to read source: {type(error).__name__}: {error}\n'
        line_number = max(1, int(line_number or 1))
        start = max(1, line_number - max(1, int(context_lines or 0)))
        end = min(len(source_lines), line_number + max(1, int(context_lines or 0)))
        rows = []
        for current in range(start, end + 1):
            marker = '>>>' if current == line_number else '   '
            rows.append(f'{marker} {current:5d}  {source_lines[current - 1]}')
        return '\n'.join(rows) + '\n'

    def buildAstTreeText(self, file_name: str, focus_line: int = 0) -> str:
        target = Path(str(file_name or EMPTY_STRING))
        try:
            source_text = File.readText(target, encoding='utf-8', errors='replace')
        except Exception as error:
            captureException(None, source='start.py', context='except@8900')
            print(f"[WARN:swallowed-exception] start.py:7569 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return f'# unable to read AST source: {type(error).__name__}: {error}\n'
        try:
            tree = ast.parse(source_text, filename=str(target))
        except Exception as error:
            captureException(None, source='start.py', context='except@8905')
            print(f"[WARN:swallowed-exception] start.py:7573 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return f'# AST parse failed: {type(error).__name__}: {error}\n'
        focus_line = int(focus_line or 0)
        rows = [f'Module {target.name}', f'Path: {target}', EMPTY_STRING]
        def format_node(node):
            start = int(getattr(node, 'lineno', 0) or 0)
            end = int(getattr(node, 'end_lineno', start) or start)
            marker = '>> ' if focus_line and start <= focus_line <= end else '   '
            if isinstance(node, ast.ClassDef):
                return f'{marker}Class {node.name}  lines {start}-{end}'
            if isinstance(node, ast.FunctionDef):
                return f'{marker}Function {node.name}  lines {start}-{end}'
            if isinstance(node, ast.AsyncFunctionDef):
                return f'{marker}AsyncFunction {node.name}  lines {start}-{end}'
            if isinstance(node, ast.If):
                return f'{marker}If  lines {start}-{end}'
            return None
        stack: list[tuple[object, int]] = [(tree, 0)]
        while stack:
            node, indent = stack.pop()
            label = format_node(node)
            next_indent = indent
            if label is not None:
                rows.append('  ' * indent + label)
                next_indent = indent + 1
            children = [child for child in ast.iter_child_nodes(node) if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.If))]
            for child in reversed(children):
                stack.append((child, next_indent))
        rows.append(EMPTY_STRING)
        rows.append('# Full AST dump')
        rows.append(ast.dump(tree, indent=2, include_attributes=True))
        rows.append(EMPTY_STRING)
        return '\n'.join(rows)

    def openDebuggerCodeDialog(self, title: str, source_text: str, language_hint: str = EMPTY_STRING, read_only: bool = True, submit_label: str = 'Submit', show_submit: bool = False, interval_seconds: float | None = None, show_interval: bool = False):
        app = ensureDebuggerQtApplication()
        if app is None:
            return None
        dialog = DebuggerCodeDialog(
            debugger=self,
            title=title,
            sourceText=str(source_text or EMPTY_STRING),
            languageHint=str(language_hint or EMPTY_STRING),
            readOnly=bool(read_only),
            submitLabel=str(submit_label or 'Submit'),
            showSubmit=bool(show_submit),
            intervalSeconds=interval_seconds,
            showInterval=bool(show_interval),
        )
        result = Lifecycle.runQtBlockingCall(dialog, phase_name='start.dialog')
        if not bool(show_submit):
            return {'accepted': bool(result == QDialog.DialogCode.Accepted), 'text': dialog.currentSourceText(), 'interval_seconds': dialog.intervalValue()}
        if result != QDialog.DialogCode.Accepted:
            return None
        return {'accepted': True, 'text': dialog.currentSourceText(), 'interval_seconds': dialog.intervalValue()}

    def openCrashContextViewer(self) -> str:
        file_name, line_number, function_name = self.locateCrashSource()
        title = f'Lines of Crash — {Path(str(file_name)).name}:{int(line_number or 0)} in {function_name}'
        payload = self.buildDebuggerSourceSnippet(file_name, line_number, context_lines=18)
        dialog_result = self.openDebuggerCodeDialog(title, payload, language_hint=str(Path(str(file_name)).suffix or 'py').lstrip('.'), read_only=True)
        if dialog_result is None:
            snippet = self.renderCrashContext(10)
            if not snippet:
                return '[PromptDebugger] No crash source context available.'
            return '\n'.join([EMPTY_STRING, '  -- Crash Location --', *snippet, EMPTY_STRING])
        return EMPTY_STRING

    def openAstTreeViewer(self) -> str:
        file_name, line_number, _ = self.locateCrashSource()
        try:
            source_text = File.readText(Path(str(file_name or EMPTY_STRING)), encoding='utf-8', errors='replace')
        except Exception as error:
            captureException(None, source='start.py', context='except@8977')
            print(f"[WARN:swallowed-exception] start.py:7644 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return f'# unable to read AST source: {type(error).__name__}: {error}\n'
        payload = self.buildAstTreeText(file_name, focus_line=line_number)
        app = ensureDebuggerQtApplication()
        if app is None:
            return payload
        dialog = DebuggerAstDialog(self, str(file_name or EMPTY_STRING), source_text, focusLine=line_number, astText=payload)
        Lifecycle.runQtBlockingCall(dialog, phase_name='start.dialog')
        return EMPTY_STRING

    def promptForDebuggerCode(self, label: str = 'Execute Code') -> str:  # noqa: nonconform reviewed return contract
        while True:  # noqa: badcode reviewed detector-style finding
            try:
                raw = input(f'[PromptDebugger:{str(label or "code").lower().replace(" ", "-")}] > ').strip()
            except EOFError:
                captureException(None, source='start.py', context='except@8992')
                raw = EMPTY_STRING
            except KeyboardInterrupt:
                captureException(None, source='start.py', context='except@8994')
                raw = EMPTY_STRING
            if not raw:
                return EMPTY_STRING
            low = raw.lower()
            if low in {'q', 'quit', 'back', 'menu'}:
                return EMPTY_STRING
            return raw

    def queueDebuggerExecAndWait(self, raw_text: str) -> str:
        if not self.childSurfaceSupported('debugger-exec-command', default=True):
            return self.childSurfaceUnavailableText('debugger-exec-command', 'Execute Code')
        before_id = int(self.latestSnapshotRowId() or 0)
        row_id = int(self.queueDebuggerExecRequest(raw_text) or 0)
        if row_id <= 0:
            return '[PromptDebugger] Failed to queue execute-code request.'
        reason = f'DEBUGGER-EXEC:{row_id}'
        row = self.readDebuggerResultRow(reason=reason, after_id=before_id, timeout=10.0, poll_interval=0.10)
        if row is None:
            return f'[PromptDebugger] Execute-code queued in heartbeat row {row_id}; no child result yet.'
        return self.formatDebuggerResultRow(row)

    def queueDebuggerCronInteractive(self) -> str:
        if not self.childSurfaceSupported('debugger-cron-command', default=True):
            return self.childSurfaceUnavailableText('debugger-cron-command', 'Create Cron')
        dialog_result = self.openDebuggerCodeDialog('Create Cron', EMPTY_STRING, language_hint='py', read_only=False, submit_label='Submit', show_submit=True, interval_seconds=1.0, show_interval=True)
        if dialog_result is None:
            if ensureDebuggerQtApplication() is None:
                try:
                    seconds_raw = input('[PromptDebugger:create-cron seconds] > ').strip()
                except EOFError:
                    captureException(None, source='start.py', context='except@9024')
                    seconds_raw = EMPTY_STRING
                except KeyboardInterrupt:
                    captureException(None, source='start.py', context='except@9026')
                    seconds_raw = EMPTY_STRING
                if not seconds_raw or seconds_raw.lower() in {'q', 'quit', 'back', 'menu'}:
                    return '[PromptDebugger] Create Cron cancelled.'
                try:
                    interval_seconds = max(0.05, float(seconds_raw))
                except Exception as error:
                    captureException(None, source='start.py', context='except@9032')
                    print(f"[WARN:swallowed-exception] start.py:7698 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    return f'[PromptDebugger] Invalid cron interval: {seconds_raw}'
                raw_text = self.promptForDebuggerCode('Create Cron')
                if not raw_text:
                    return '[PromptDebugger] Create Cron cancelled.'
                row_id = int(self.queueDebuggerCronRequest(interval_seconds, raw_text) or 0)
                if row_id <= 0:
                    return '[PromptDebugger] Failed to queue cron request.'
                return f'[PromptDebugger] Cron queued in heartbeat row {row_id}  interval={interval_seconds:.2f}s'
            return '[PromptDebugger] Create Cron cancelled.'
        interval_seconds = max(0.05, float(dialog_result.get('interval_seconds') or 1.0))
        raw_text = str(dialog_result.get('text', EMPTY_STRING) or EMPTY_STRING).strip()
        if not raw_text:
            return '[PromptDebugger] Create Cron cancelled.'
        row_id = int(self.queueDebuggerCronRequest(interval_seconds, raw_text) or 0)
        if row_id <= 0:
            return '[PromptDebugger] Failed to queue cron request.'
        return f'[PromptDebugger] Cron queued in heartbeat row {row_id}  interval={interval_seconds:.2f}s'

    def executeCodeInteractive(self) -> str:
        dialog_result = self.openDebuggerCodeDialog('Execute Code', EMPTY_STRING, language_hint='py', read_only=False, submit_label='Submit', show_submit=True)
        if dialog_result is None:
            if ensureDebuggerQtApplication() is None:
                raw_text = self.promptForDebuggerCode('Execute Code')
                if not raw_text:
                    return '[PromptDebugger] Execute Code cancelled.'
                return self.queueDebuggerExecAndWait(raw_text)
            return '[PromptDebugger] Execute Code cancelled.'
        raw_text = str(dialog_result.get('text', EMPTY_STRING) or EMPTY_STRING).strip()
        if not raw_text:
            return '[PromptDebugger] Execute Code cancelled.'
        return self.queueDebuggerExecAndWait(raw_text)

    def trafficMonitorColor(self, code: str, text: str) -> str:
        return f'[{code}m{text}[0m'

    def trafficMonitorStatusText(self, status: str, error_text: str = EMPTY_STRING) -> str:
        status_text = str(status or EMPTY_STRING).strip() or 'unknown'
        lowered = status_text.lower()
        if error_text:
            return self.trafficMonitorColor('1;91', f'ERROR {status_text}')
        if lowered in {'queued', 'pending'}:
            return self.trafficMonitorColor('1;93', status_text.upper())
        if lowered in {'ok', '200', '201', '202', '204'} or lowered.isdigit() and 200 <= int(lowered) < 400:
            return self.trafficMonitorColor('1;92', f'SENT {status_text}')
        return self.trafficMonitorColor('1;93', status_text.upper())

    def trafficMonitorPreview(self, text: str, limit: int = 80) -> str:
        value = str(text or EMPTY_STRING).replace('\r', ' ').replace('\n', ' ').strip()
        value = re.sub(r'\s+', ' ', value)
        if len(value) > int(limit or 80):
            return value[:max(0, int(limit or 80) - 3)] + '...'
        return value


    def formatTrafficMonitorRow(self, row: dict[str, Any] | None) -> str:
        payload = dict(row or {})
        rid = int(payload.get('id', 0) or 0)
        created = str(payload.get('created', EMPTY_STRING) or EMPTY_STRING)
        status = str(payload.get('status', EMPTY_STRING) or EMPTY_STRING)
        destination = str(payload.get('destination', EMPTY_STRING) or EMPTY_STRING)
        length = payload.get('length', None)
        roundtrip = float(payload.get('roundtrip_microtime', 0.0) or 0.0)
        header_text = str(payload.get('headers', EMPTY_STRING) or EMPTY_STRING)
        error_text = str(payload.get('error', EMPTY_STRING) or EMPTY_STRING)
        caller_text = str(payload.get('caller', EMPTY_STRING) or EMPTY_STRING)
        response_preview = self.trafficMonitorPreview(str(payload.get('response_preview', EMPTY_STRING) or EMPTY_STRING), 80)
        status_rendered = self.trafficMonitorStatusText(status, error_text)
        def cyan(value):
            return self.trafficMonitorColor('1;96', value)

        def dim(value):
            return self.trafficMonitorColor('2', value)
        lines = [
            self.trafficMonitorColor('1;95', f'[Traffic {rid}]') + ' ' + dim(created),
            f'  {cyan("dest")}: {destination}',
            f'  {cyan("caller")}: {caller_text or "(unknown)"}',
            f'  {cyan("headers")}: {header_text or "{}"}',
            f'  {cyan("length")}: {length if length is not None else 0}',
            f'  {cyan("status")}: {status_rendered}',
            f'  {cyan("roundtrip")}: {roundtrip:.0f}µs',
            f'  {cyan("response")}: {response_preview or "(empty)"}',
        ]
        if error_text:
            lines.append(f'  {self.trafficMonitorColor("1;91", "error")}: {error_text}')
        return '\n'.join(lines)

    def connectionMonitorLoop(self):
        while not self.connectionMonitorStop.wait(1.0):
            try:
                rows = list(self.db.readUnprocessedTrafficRows(limit=200) or [])
                if not rows:
                    continue
                printed_ids = []
                for row in rows:
                    printed_ids.append(int(row.get('id', 0) or 0))
                    block = self.formatTrafficMonitorRow(row)
                    self.emit(block, self.logPath)
                    try:
                        print(block, flush=True)
                    except Exception as error:
                        captureException(None, source='start.py', context='except@9133')
                        print(f"[WARN:swallowed-exception] start.py:7795 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                        pass
                self.db.markTrafficProcessed(printed_ids)
            except Exception as error:
                captureException(None, source='start.py', context='except@9137')
                print(f"[WARN:swallowed-exception] start.py:7798 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                self.emit(f'[PromptDebugger] Connection monitor error: {error}', self.exceptionPath)

    def toggleConnectionMonitor(self) -> str:
        if not self.childSurfaceSupported('accepts-proxy', default=False):
            return '[PromptDebugger] Connection monitor unavailable: child does not expose accepts-proxy.'
        if self.connectionMonitorEnabled:
            self.connectionMonitorEnabled = False
            self.connectionMonitorStop.set()
            return '[PromptDebugger] Connection monitor OFF'
        self.connectionMonitorStop = threading.Event()
        self.connectionMonitorEnabled = True
        self.connectionMonitorThread = START_EXECUTION_LIFECYCLE.startThread('TrioConnectionMonitor', self.threadMain(self.connectionMonitorLoop, 'TrioConnectionMonitor'), daemon=True)
        return '[PromptDebugger] Connection monitor ON (printing unprocessed traffic rows)'
    def normalizeConsoleCommand(self, cmd: str) -> str:
        token = str(cmd or EMPTY_STRING)
        try:
            token = token.replace('\x00', EMPTY_STRING).replace('\r', EMPTY_STRING).replace('\n', EMPTY_STRING)
        except Exception as error:
            captureException(None, source='start.py', context='except@9156')
            print(f"[WARN:swallowed-exception] start.py:7816 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        token = token.strip().strip("'\"`")
        token = EMPTY_STRING.join(ch for ch in token if ch.isprintable())
        return token.strip()

    def commandAliases(self) -> dict[str, set[str]]:
        return DEBUGGER_COMMAND_ALIASES

    def commandMatches(self, token: str, group: str) -> bool:
        aliases = self.commandAliases().get(str(group or EMPTY_STRING), set())
        return str(token or EMPTY_STRING).strip().lower() in aliases
    def dispatchConsoleCommand(self, cmd: str, interactive: bool = True) -> str:
        token = self.normalizeConsoleCommand(cmd)
        lowered = str(token or EMPTY_STRING).strip().lower()
        primary = str(lowered.split()[0] if lowered else EMPTY_STRING)
        git_tokens = {
            'git', '.git', 'j', 'gittools', 'gm', 'ga', 'gc', 'gb', 'gd', 'bd', 'gp', 'gpush', 'gu', 'gpull',
            'gr', 'gl', 'gf', 'gi', 'gco', 'gsw', 'add', 'commit', 'branch', 'diff', 'push', 'pull', 'remote',
            'log', 'fetch', 'checkout', 'switch', 'auth', 'gh', 'gitignore', 'init'
        }
        if lowered.startswith('git ') or lowered.startswith('.git ') or lowered.startswith('gh ') or primary in git_tokens:
            return self.handleGitConsoleCommand(cmd, interactive=True)
        return self.executeConsoleLeafAction(cmd, token)

    def gitRepoRoot(self) -> Path:
        return Path(BASE_DIR)

    def openGitAuthDialog(self) -> dict[str, Any]:
        if not ensureDebuggerQtImports():
            return {'accepted': False, 'error': debuggerQtUnavailableText()}
        app = ensureDebuggerQtApplication()
        if app is None:
            return {'accepted': False, 'error': debuggerQtUnavailableText()}
        dialog = GitAuthDialog(debugger=self, parent=None, repoRoot=self.gitRepoRoot())
        result = Lifecycle.runQtBlockingCall(dialog, phase_name='start.dialog')
        payload = dialog.payload()
        payload['accepted'] = bool(result == QDialog.DialogCode.Accepted)
        return payload

    def executeGitAuthInteractive(self) -> str:
        dialog_result = self.openGitAuthDialog()
        if not bool(dialog_result.get('accepted')):
            return '[TrioGit] Auth cancelled.'
        return applyGitAuthSettings(dialog_result, cwd=self.gitRepoRoot(), interactive=True)

    def openGitignoreEditor(self) -> str:
        repo_root = self.gitRepoRoot()
        path = repo_root / '.gitignore'
        existing = EMPTY_STRING
        try:
            if path.exists():
                existing = File.readText(path, encoding='utf-8', errors='replace')
        except Exception as error:
            captureException(None, source='start.py', context='except@9210')
            print(f"[WARN:swallowed-exception] start.py:7869 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return f'[TrioGit] Could not read {path}: {error}'
        if not ensureDebuggerQtImports():
            return f'[TrioGit] .gitignore path: {path}'
        result = self.openDebuggerCodeDialog('Edit .gitignore', existing, language_hint='gitignore', read_only=False, submit_label='Save', show_submit=True)
        if not cast(Any, result).get('accepted'):
            return '[TrioGit] .gitignore edit cancelled.'
        try:
            File.writeText(path, str(cast(Any, result).get('text', EMPTY_STRING) or EMPTY_STRING), encoding='utf-8')
            return f'[TrioGit] Saved {path}'
        except Exception as error:
            captureException(None, source='start.py', context='except@9221')
            print(f"[WARN:swallowed-exception] start.py:7879 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return f'[TrioGit] Could not save {path}: {error}'


    def handleGitConsoleCommand(self, raw_cmd: str, interactive: bool = True) -> str:
        text = str(raw_cmd or EMPTY_STRING).strip()
        lowered = text.lower()
        if lowered.startswith('.git'):
            payload = text[4:].strip()
        elif lowered.startswith('git '):
            payload = text[3:].strip()
        elif lowered.startswith('gh '):
            payload = 'gh ' + text[3:].strip()
        elif lowered in {'git', '.git', 'j', 'gittools'}:
            payload = EMPTY_STRING
        elif lowered in {'gm', 'man', 'help'}:
            payload = 'help'
        elif lowered == 'gh':
            payload = 'gh'
        else:
            payload = text
        args = shlex.split(payload) if payload else []
        primary = str(args[0]).strip().lower() if args else EMPTY_STRING
        if primary in {'gitignore', 'gi'} and interactive:
            return self.openGitignoreEditor()
        if primary in {'auth', 'gauth', 'githubauth'} and interactive:
            return self.executeGitAuthInteractive()
        return runGitCommand(args, cwd=self.gitRepoRoot(), interactive=interactive)


    def executeConsoleLeafAction(self, cmd: str, token: str) -> str:
        low = str(token or EMPTY_STRING).lower()
        if self.commandMatches(low, 'quit'):
            try:
                self.dismissedSignature = self.crashSignature(self.crashInfo)
                self.consoleSnoozeUntil = time.time() + max(self.freezeThresholdSeconds * 4.0, 12.0)
                self.inCrashConsole = False
                self.consoleDone.set()
                self.consoleStop.set()
                self.consoleWake.set()
                self.statusOverrideReason = EMPTY_STRING
                self.manualDebugCooldownUntil = time.time() + 6.0
            except Exception as error:
                captureException(None, source='start.py', context='except@9264')
                print(f"[WARN:swallowed-exception] start.py:7921 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
            print('[PromptDebugger] Crash console closed — exiting.', flush=True)
            clearPycacheDirectories(BASE_DIR, reason='crash-console-quit')
            try:
                os._exit(0)  # lifecycle-ok: user explicitly closed the interactive crash console.
            except Exception:
                captureException(None, source='start.py', context='except@9271')
                try:
                    sys.exit(0)  # lifecycle-ok: fallback only if hard exit is unavailable.
                except SystemExit:
                    captureException(None, source='start.py', context='except@9274')
                    raise
        if self.commandMatches(low, 'vars'):
            if not self.childSurfaceSupported('vardump', default=True):
                return self.childSurfaceUnavailableText('vardump', 'Variables')
            try:
                self.requestChildSnapshot(command='vardump', timeout=1.10, reason='vars-command')
            except Exception as error:
                captureException(None, source='start.py', context='except@9281')
                print(f"[WARN:swallowed-exception] start.py:7937 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
            try:
                rendered = self.renderChildVarDumpFromHeartbeat()
                if 'No child var dump found' not in rendered:
                    return rendered
            except Exception as error:
                captureException(None, source='start.py', context='except@9288')
                print(f"[WARN:swallowed-exception] start.py:7943 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                self.emit(f'[PromptDebugger] Child var dump render failed: {error}', self.exceptionPath)
            snapshot_text = self.readLatestSnapshotFromDb()
            if 'No child snapshot' not in snapshot_text and 'unavailable' not in snapshot_text and 'failed' not in snapshot_text:
                return snapshot_text
            return self.variableDump()
        if self.commandMatches(low, 'crash_context'):
            return self.openCrashContextViewer()
        if self.commandMatches(low, 'close'):
            return self.requestClose()
        if self.commandMatches(low, 'kill'):
            return self.requestKill()
        if self.commandMatches(low, 'clear_screen'):
            self.clearConsoleScreen()
            self.printBanner()
            return EMPTY_STRING
        if self.commandMatches(low, 'connection_monitor'):
            return self.toggleConnectionMonitor()
        if self.commandMatches(low, 'logs'):
            logs = self.printLogs()
            print(logs, flush=True)
            self.write(self.logPath, logs + ('\n' if not str(logs).endswith('\n') else EMPTY_STRING))
            return EMPTY_STRING
        if self.commandMatches(low, 'exceptions'):
            return self.inspectExceptionRows()
        if self.commandMatches(low, 'status'):
            return self.statusText()
        if self.commandMatches(low, 'help'):
            print('\n'.join(self.helpScreenLines()), flush=True)
            return EMPTY_STRING
        if self.commandMatches(low, 'restart'):
            proc = self.child
            if proc is not None and proc.poll() is None:
                return '[PromptDebugger] Child is still running — kill it first (press 5)'
            self.dismissedSignature = EMPTY_STRING
            self.lastOpenedSignature = EMPTY_STRING
            self.inCrashConsole = False
            self.heartbeatEverFired = False
            self.childExitCode = None
            self.childProcessRowId = 0
            self.consoleDone.set()
            try:
                self.launchChild(self.childArgs)
                watchdog = getattr(self, 'watchdogThread', None)
                if watchdog is None or not watchdog.is_alive():
                    self.consoleStop.clear()
                    self.watchdogThread = START_EXECUTION_LIFECYCLE.startThread('TrioProcessWatchdog', self.threadMain(self.poll, 'TrioProcessWatchdog'), daemon=False)
                return f'[PromptDebugger] Child relaunched  pid={self.childPid}'
            except Exception as error:
                captureException(None, source='start.py', context='except@9337')
                print(f"[WARN:swallowed-exception] start.py:7991 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                return f'[PromptDebugger] Relaunch failed: {error}'
        if self.commandMatches(low, 'exec_code'):
            return self.executeCodeInteractive()
        if self.commandMatches(low, 'create_cron'):
            return self.queueDebuggerCronInteractive()
        if self.commandMatches(low, 'ast_tree'):
            return self.openAstTreeViewer()
        if self.commandMatches(low, 'git_tools'):
            return self.handleGitConsoleCommand(cmd, interactive=True)
        return f'[PromptDebugger] Unknown command: {cmd}'
    def consoleLoop(self):
        while not self.consoleStop.is_set():
            self.consoleWake.wait()  # block-ok debugger-console-event-wait
            self.consoleWake.clear()
            if self.consoleStop.is_set():
                break
            self.printBanner()
            while self.inCrashConsole and not self.consoleStop.is_set():
                try:
                    self.lastStatusLine = EMPTY_STRING
                    raw = input('[PromptDebugger] > ')
                except EOFError:
                    captureException(None, source='start.py', context='except@9360')
                    raw = 'q'
                except KeyboardInterrupt:
                    captureException(None, source='start.py', context='except@9362')
                    raw = 'q'
                if raw.strip() in ('\x03',):
                    raw = 'q'
                cmd = self.normalizeConsoleCommand(raw)
                if not cmd:
                    continue
                output = self.dispatchConsoleCommand(cmd, interactive=True)
                if output:
                    print(output, flush=True)
                    self.write(self.logPath, output + ('\n' if not str(output).endswith('\n') else EMPTY_STRING))
    def startSnapshotCapture(self, payload: dict | None):
        info = dict(payload or {})
        existing = getattr(self, 'snapshotThread', None)
        try:
            if existing is not None and existing.is_alive():
                return existing
        except Exception as error:
            captureException(None, source='start.py', context='except@9379')
            print(f"[WARN:swallowed-exception] start.py:8032 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        def runner():
            if self.childSurfaceSupported('vardump', default=True):
                try:
                    self.requestChildSnapshot(command='vardump', timeout=0.90, reason=str(info.get('type_name', 'pre-console')))
                except Exception as error:
                    captureException(None, source='start.py', context='except@9386')
                    print(f"[WARN:swallowed-exception] start.py:8038 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    pass
            try:
                self.capturePreConsoleSnapshot(info)
            except Exception as error:
                captureException(None, source='start.py', context='except@9391')
                print(f"[WARN:swallowed-exception] start.py:8042 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                self.emit(f'[PromptDebugger] Snapshot capture failed: {error}', self.exceptionPath)
        thread = START_EXECUTION_LIFECYCLE.startThread('TrioSnapshotCapture', self.threadMain(runner, 'TrioSnapshotCapture'), daemon=True)
        self.snapshotThread = thread
        try:
            pass
        except Exception as error:
            captureException(None, source='start.py', context='except@9398')
            print(f"[WARN:swallowed-exception] start.py:8048 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            self.emit(f'[PromptDebugger] Snapshot thread start failed: {error}', self.exceptionPath)
        return thread
    def runActionAsync(self, label: str, target):
        def runner():
            try:
                result = target()
                if result:
                    self.emit(str(result), self.exceptionPath)
            except Exception as error:
                captureException(None, source='start.py', context='except@9408')
                print(f"[WARN:swallowed-exception] start.py:8057 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                self.emit(f'[PromptDebugger] {label} failed: {error}', self.exceptionPath)
        thread = START_EXECUTION_LIFECYCLE.startThread(f'TrioAction:{label}', self.threadMain(runner, f'TrioAction:{label}'), daemon=True)
        self.actionThread = thread

    def openCrashConsole(self, info: dict | None, block: bool = False):
        payload = dict(info or {})
        try:
            if self.inCrashConsole and str((self.crashInfo or {}).get('type_name', EMPTY_STRING) or EMPTY_STRING) == str(payload.get('type_name', EMPTY_STRING) or EMPTY_STRING):
                self.crashInfo = payload
                return
        except Exception as error:
            captureException(None, source='start.py', context='except@9420')
            print(f"[WARN:swallowed-exception] start.py:8068 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        signature = self.crashSignature(payload)
        with self.lock:
            now = time.time()
            if signature and self.inCrashConsole and signature == self.lastOpenedSignature:
                self.crashInfo = payload
                return
            if signature and signature == self.dismissedSignature and now < float(self.consoleSnoozeUntil or 0.0):
                self.crashInfo = payload
                return
            self.crashInfo = payload
            self.lastOpenedSignature = signature
            if bool(getattr(self, 'offscreenEnabled', False)) and getattr(self, 'captureEngine', None) or getattr(self, 'offscreenSession', None) is not None:
                self.scheduleOffscreenCapture('crash', delaySeconds=0.0, allowRepeat=True, stamped=True)
            self.startSnapshotCapture(self.crashInfo)
            self.consoleDone.clear()
            self.inCrashConsole = True
            self.consoleWake.set()
        if block and str(payload.get('type_name', EMPTY_STRING) or EMPTY_STRING) != 'Freeze':
            self.consoleDone.wait()  # block-ok debugger-console-event-wait
    def formatFaultRowText(self, row: dict | None) -> str:
        payload = dict(row or {})
        lines = [
            self.psColor('=' * 78, PS_RED, bold=True),
            self.psColor('Child Fault from Faults Table', PS_RED, bold=True),
            self.psColor('=' * 78, PS_RED, bold=True),
            self.psColor('reason', PS_YELLOW, bold=True) + self.psColor(' = ', PS_DIM) + self.psColor(str(payload.get('reason', EMPTY_STRING) or EMPTY_STRING), PS_WHITE),
            self.psColor('caller', PS_YELLOW, bold=True) + self.psColor(' = ', PS_DIM) + self.psColor(str(payload.get('caller', EMPTY_STRING) or EMPTY_STRING), PS_WHITE),
            self.psColor('source', PS_YELLOW, bold=True) + self.psColor(' = ', PS_DIM) + self.psColor(str(payload.get('source', EMPTY_STRING) or EMPTY_STRING), PS_WHITE),
            self.psColor('thread', PS_YELLOW, bold=True) + self.psColor(' = ', PS_DIM) + self.psColor(str(payload.get('thread', EMPTY_STRING) or EMPTY_STRING), PS_WHITE),
            self.psColor('pid', PS_YELLOW, bold=True) + self.psColor(' = ', PS_DIM) + self.psColor(str(payload.get('pid', 0) or 0), PS_WHITE),
            self.psColor('created', PS_YELLOW, bold=True) + self.psColor(' = ', PS_DIM) + self.psColor(str(payload.get('created', EMPTY_STRING) or EMPTY_STRING), PS_WHITE),
        ]
        stack_text = str(payload.get('stack_trace', EMPTY_STRING) or EMPTY_STRING).strip()
        if stack_text:
            lines.append(EMPTY_STRING)
            lines.append(self.psColor('Stack / Fault Data', PS_CYAN, bold=True))
            lines.extend(self.psColor(line, PS_WHITE) for line in stack_text.splitlines())
        var_text = str(payload.get('var_dump', EMPTY_STRING) or EMPTY_STRING).strip()
        if var_text:
            lines.append(EMPTY_STRING)
            lines.append(self.psColor('Variable Dump', PS_CYAN, bold=True))
            lines.extend(self.psColor(line, PS_DIM if not line.strip() else PS_WHITE) for line in var_text.splitlines())
        process_text = str(payload.get('process_snapshot', EMPTY_STRING) or EMPTY_STRING).strip()
        if process_text:
            lines.append(EMPTY_STRING)
            lines.append(self.psColor('Process Snapshot', PS_CYAN, bold=True))
            lines.extend(self.psColor(line, PS_DIM if not line.strip() else PS_WHITE) for line in process_text.splitlines())
        lines.append(self.psColor('=' * 78, PS_RED, bold=True))
        return '\n'.join(lines)

    def processHeartbeatRows(self):
        baseline = int((getattr(self, 'dbPollBaseline', {}) or {}).get('heartbeat', 0) or 0)
        rows = list(cast(Any, getattr(self, 'db', None)).readUnprocessedHeartbeatRows(limit=100, min_id=baseline) or []) if getattr(self, 'db', None) is not None else []
        if not rows:
            return False
        self.advanceDbPollBaseline('heartbeat', rows)
        self.advanceDbPollBaseline('exceptions', rows)
        self.advanceDbPollBaseline('faults', rows)
        ids = [int(row.get('id', 0) or 0) for row in rows if int(row.get('id', 0) or 0) > 0]
        for row in rows:
            kind = str(row.get('event_kind', EMPTY_STRING) or EMPTY_STRING).strip().lower()
            reason = str(row.get('reason', EMPTY_STRING) or EMPTY_STRING)
            caller = str(row.get('caller', EMPTY_STRING) or EMPTY_STRING)
            timestamp = float(row.get('heartbeat_microtime', 0.0) or time.time())
            if kind == 'heartbeat':
                self.touchHeartbeat(reason or 'HEARTBEAT', caller=caller, timestamp=timestamp)
            elif kind == 'process':
                with self.lock:
                    self.lastProcessLoop = timestamp
                    self.lastProcessReason = str(reason or 'PROCESS')
        if ids:
            try:
                self.db.markHeartbeatProcessed(ids)
            except Exception as error:
                captureException(None, source='start.py', context='except@9496')
                print(f"[WARN:swallowed-exception] start.py:8143 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
        return True

    def processUnhandledExceptionRows(self):
        baseline = int((getattr(self, 'dbPollBaseline', {}) or {}).get('exceptions', 0) or 0)
        rows = list(cast(Any, getattr(self, 'db', None)).readUnprocessedUnhandledExceptionRows(limit=10, min_id=baseline) or []) if getattr(self, 'db', None) is not None else []
        if not rows:
            return False
        ids = [int(row.get('id', 0) or 0) for row in rows if int(row.get('id', 0) or 0) > 0]
        if ids:
            try:
                self.db.markExceptionProcessed(ids)
            except Exception as error:
                captureException(None, source='start.py', context='except@9510')
                print(f"[WARN:swallowed-exception] start.py:8156 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
        for row in rows:
            pretty = self.formatExceptionRowText(row)
            self.emit(pretty, self.exceptionPath)
            self.openCrashConsole({
                'type_name': str(row.get('type_name', 'UnhandledException') or 'UnhandledException'),
                'message': str(row.get('message', 'Unhandled exception') or 'Unhandled exception'),
                'thread': str(row.get('thread', threading.current_thread().name) or threading.current_thread().name),
                'timestamp': time.time(),
                'traceback_text': pretty,
            }, block=False)
        return True

    def processHeartbeatFaultRows(self):
        baseline = int((getattr(self, 'dbPollBaseline', {}) or {}).get('faults', 0) or 0)
        rows = list(cast(Any, getattr(self, 'db', None)).readUnprocessedFaultRows(limit=10, min_id=baseline) or []) if getattr(self, 'db', None) is not None else []
        if not rows:
            return False
        ids = [int(row.get('id', 0) or 0) for row in rows if int(row.get('id', 0) or 0) > 0]
        if ids:
            try:
                self.db.markFaultProcessed(ids)
            except Exception as error:
                captureException(None, source='start.py', context='except@9534')
                print(f"[WARN:swallowed-exception] start.py:8179 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
        for row in rows:
            pretty = self.formatFaultRowText(row)
            self.emit(pretty, self.exceptionPath)
            self.openCrashConsole({
                'type_name': 'ChildFault',
                'message': str(row.get('reason', 'Child fault') or 'Child fault'),
                'thread': str(row.get('thread', threading.current_thread().name) or threading.current_thread().name),
                'timestamp': time.time(),
                'traceback_text': pretty,
            }, block=False)
        return True

    def poll(self):
        min_threshold = max(float(getattr(self, 'freezeThresholdSeconds', 8.0) or 8.0), 20.0)
        while not self.consoleStop.wait(self.deathPollInterval):
            try:
                self.processHeartbeatRows()
            except Exception as error:
                captureException(None, source='start.py', context='except@9554')
                print(f"[WARN:swallowed-exception] start.py:8198 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
            try:
                self.processUnhandledExceptionRows()
            except Exception as error:
                captureException(None, source='start.py', context='except@9559')
                print(f"[WARN:swallowed-exception] start.py:8202 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
            try:
                self.processHeartbeatFaultRows()
            except Exception as error:
                captureException(None, source='start.py', context='except@9564')
                print(f"[WARN:swallowed-exception] start.py:8206 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
            proc = self.child
            if proc is not None:
                try:
                    code = proc.poll()
                except Exception as error:
                    captureException(None, source='start.py', context='except@9571')
                    print(f"[WARN:swallowed-exception] start.py:8212 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    code = None
                if code is not None and self.childExitCode is None:
                    self.markChildExited(proc)
                    if int(self.childExitCode or 0) != 0:
                        self.openCrashConsole({
                            'type_name': 'ChildExit',
                            'message': f'{GTP_PATH.name} exited with code {self.childExitCode}',
                            'thread': threading.current_thread().name,
                            'timestamp': time.time(),
                            'traceback_text': self.readFile(self.exceptionPath),
                        }, block=True)
                    continue
            if self.childExitCode is not None:
                if self.inCrashConsole:
                    self.consoleDone.wait()  # block-ok debugger-console-event-wait
                self.statusStop.set()
                self.consoleStop.set()
                return
            if not self.childSurfaceSupported('heartbeat', default=True) or not self.childSurfaceSupported('poll', default=True):
                continue
            if not self.heartbeatEverFired:
                continue
            now = time.time()
            process_age = now - float(getattr(self, 'lastProcessLoop', 0.0) or 0.0)
            heartbeat_age = now - float(getattr(self, 'lastHeartbeat', 0.0) or 0.0)
            stale = now - max(self.lastHeartbeat, self.lastProcessLoop)
            if process_age < min_threshold or heartbeat_age < min_threshold:
                continue
            if float(getattr(self, 'lastProcessLoop', 0.0) or 0.0) and (now - float(getattr(self, 'lastProcessLoop', 0.0) or 0.0)) < 15.0:
                continue
            if float(getattr(self, 'lastHeartbeat', 0.0) or 0.0) and (now - float(getattr(self, 'lastHeartbeat', 0.0) or 0.0)) < 15.0:
                continue
            if stale >= min_threshold and (now - self.lastFreezeDump) >= min_threshold:
                if self.inCrashConsole:
                    continue
                self.lastFreezeDump = now
                try:
                    self.requestChildSnapshot(command='freeze_dump', timeout=0.90, reason='watchdog-freeze')
                except Exception as error:
                    captureException(None, source='start.py', context='except@9611')
                    print(f"[WARN:swallowed-exception] start.py:8251 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    pass
                self.openCrashConsole({
                    'type_name': 'Freeze',
                    'message': self.flatlined() + f'  stalled={stale:.1f}s',
                    'thread': threading.current_thread().name,
                    'timestamp': time.time(),
                }, block=False)
    def closeChildStreams(self):
        for attr in ('_childStdoutHandle', '_childStderrHandle'):
            handle = getattr(self, attr, None)
            setattr(self, attr, None)
            if handle is None:
                continue
            try:
                handle.flush()
            except Exception as error:
                captureException(None, source='start.py', context='except@9628')
                print(f"[WARN:swallowed-exception] start.py:8267 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
            try:
                handle.close()
            except Exception as error:
                captureException(None, source='start.py', context='except@9633')
                print(f"[WARN:swallowed-exception] start.py:8271 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass

    def startChildPipePump(self, pipe, label: str, path: str):
        def runner():
            blank_suppressed = 0
            try:
                self.emit(f'[PromptDebugger] child {label} pump started; blank/control-only lines are filtered', self.logPath)
                while True:  # noqa: badcode reviewed detector-style finding
                    chunk = pipe.readline()
                    if not chunk:
                        break
                    visible_lines = DebugLog.iterVisibleLines(chunk)
                    if not visible_lines:
                        blank_suppressed += max(1, str(chunk).count('\n'))
                        continue
                    for text in visible_lines:
                        visible_text = DebugLog.visibleText(text)
                        lowered = visible_text.lower()
                        if not lowered:
                            blank_suppressed += 1
                            continue
                        if label == 'stderr':
                            if 'ffmpeg log:' in lowered:
                                continue
                            if lowered.startswith('qt.multimedia.ffmpeg:'):
                                continue
                            if lowered.startswith('using qt multimedia with ffmpeg version'):
                                continue
                        try:
                            self.touchProcessLoop(f'PIPE:{str(label or "child").upper()}')
                        except Exception as error:
                            captureException(None, source='start.py', context='except@9657')
                            print(f"[WARN:swallowed-exception] start.py:8294 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                            pass
                        verbose_line = self.tryFormatTraceVerboseLine(visible_text)
                        if verbose_line is not None:
                            self.emit(verbose_line, path)
                            continue
                        prefixed = f'[child:{label}] {visible_text}'
                        self.emit(prefixed, path)
                if blank_suppressed:
                    self.emit(f'[PromptDebugger] child {label} pump suppressed {blank_suppressed} blank/control-only line(s)', self.logPath)
                self.emit(f'[PromptDebugger] child {label} pump ended', self.logPath)
            except Exception as error:
                captureException(None, source='start.py', context='except@9666')
                print(f"[WARN:swallowed-exception] start.py:8302 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                self.emit(f'[PromptDebugger] {label} pump failed: {error}', self.exceptionPath)
        thread = START_EXECUTION_LIFECYCLE.startThread(f'TrioChild{label.title()}Pump', self.threadMain(runner, f'TrioChild{label.title()}Pump'), daemon=True)
        return thread

    def shutdownKernel(self):
        try:
            rpc = getattr(self, 'shellRpcServer', None)
            if rpc is not None:
                rpc.close()
                self.shellRpcServer = None
        except Exception as error:
            captureException(None, source='start.py', context='except@9678')
            print(f"[WARN:swallowed-exception] start.py:8313 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        self.statusStop.set()
        self.clearStatusLine()
        self.consoleStop.set()
        self.consoleWake.set()
        self.keyStop.set()
        self.connectionMonitorStop.set()
        for server_attr in ('server', 'relayServer'):
            server = getattr(self, server_attr, None)
            if server is None:
                continue
            try:
                server.shutdown()
            except Exception as error:
                captureException(None, source='start.py', context='except@9693')
                print(f"[WARN:swallowed-exception] start.py:8327 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
            try:
                server.server_close()
            except Exception as error:
                captureException(None, source='start.py', context='except@9698')
                print(f"[WARN:swallowed-exception] start.py:8331 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
            setattr(self, server_attr, None)
        self.closeChildStreams()
        current = threading.current_thread()
        for attr in ('serverThread', 'relayThread', 'watchdogThread', 'consoleThread', 'keyThread', 'connectionMonitorThread', 'statusThread', 'childStdoutThread', 'childStderrThread'):
            thread = getattr(self, attr, None)
            if thread is None or thread is current:
                continue
            try:
                if thread.is_alive():
                    thread.join(timeout=0.75)
            except Exception as error:
                captureException(None, source='start.py', context='except@9711')
                print(f"[WARN:swallowed-exception] start.py:8343 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
        self.stopOffscreenSession()
    def startTrafficProxyServer(self):
        if not bool(getattr(self, 'enabled', False)):
            return None
        existing = getattr(self, 'trafficProxyServer', None)
        if existing is not None and getattr(existing, 'httpd', None) is not None:
            return existing
        bind_value = str(os.environ.get('TRIO_PROXY_BIND', '127.0.0.1:6666') or '127.0.0.1:6666').strip()
        host = '127.0.0.1'
        port = 6666
        if ':' in bind_value:
            host, port_text = bind_value.rsplit(':', 1)
            host = str(host or '127.0.0.1').strip() or '127.0.0.1'
            try:
                port = int(port_text or 6666)
            except Exception as error:
                captureException(None, source='start.py', context='except@9729')
                print(f"[WARN:swallowed-exception] start.py:8360 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                port = 6666
        server = TrioTrafficProxyServer(self, host=host, port=port)
        try:
            server.start()
            self.trafficProxyServer = server
            self.emit(f'[PromptDebugger] Traffic proxy bound to {server.endpointUrl()}', self.logPath)
            try:
                with urllib.request.urlopen(server.endpointUrl(), timeout=1.0) as response:
                    if int(getattr(response, 'status', 200) or 200) == 200:
                        self.emit(f'[PromptDebugger] Traffic proxy probe ok  {server.endpointUrl()}', self.logPath)
            except Exception as probe_error:
                captureException(None, source='start.py', context='except@9741')
                print(f"[WARN:swallowed-exception] start.py:8371 {type(probe_error).__name__}: {probe_error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                self.emit(f'[WARNING:proxy] probe failed for {server.endpointUrl()}: {type(probe_error).__name__}: {probe_error}', self.exceptionPath)
            return server
        except Exception as error:
            captureException(None, source='start.py', context='except@9745')
            print(f"[WARN:swallowed-exception] start.py:8374 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            self.emit(f'[WARNING:proxy] bind failed for http://{host}:{port}: {type(error).__name__}: {error}', self.exceptionPath)
            return None

    def stopTrafficProxyServer(self):
        server = getattr(self, 'trafficProxyServer', None)
        if server is None:
            return False
        try:
            server.stop()
            return True
        finally:
            self.trafficProxyServer = None

    def launchChild(self, argv: list[str]) -> Any:
        if str(os.environ.get('TRIO_DB_BACKEND', 'sqlite') or 'sqlite').strip().lower() == 'mariadb':
            seedRuntimeDbEnvFromFile()
        if getattr(self, 'shellRpcServer', None) is None:
            try:
                self.shellRpcServer = ShellRpcServer(self)
                self.emit(f'[PromptDebugger] Shell RPC  127.0.0.1:{self.shellRpcServer.port}', self.logPath)
            except Exception as error:
                captureException(None, source='start.py', context='except@9767')
                print(f"[WARN:swallowed-exception] start.py:8395 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                self.emit(f'[PromptDebugger] Shell RPC disabled: {error}', self.exceptionPath)
                self.shellRpcServer = None
        env = os.environ.copy()
        env['TRIO_BASE_DIR'] = str(BASE_DIR)
        env['TRIO_DEBUGGER_ENABLED'] = '1' if self.enabled else '0'
        env['TRIO_DEBUGGER_HOST'] = '127.0.0.1'
        env['TRIO_DEBUGGER_PORT'] = str(self.relayPort)
        env['TRIO_DEBUGGER_TOKEN'] = self.relayToken
        rpc = getattr(self, 'shellRpcServer', None)
        if rpc is not None and getattr(rpc, 'port', 0):
            env['TRIO_SHELL_HOST'] = '127.0.0.1'
            env['TRIO_SHELL_PORT'] = str(rpc.port)
            env['TRIO_SHELL_TOKEN'] = str(self.relayToken or EMPTY_STRING)
        env.setdefault('PYTHONUNBUFFERED', '1')
        prepareTraceEnvironment(argv, env, debugWritesAllowed=self.debugArtifactWritesAllowed())
        offscreenSession = getattr(self, 'captureEngine', None) or getattr(self, 'offscreenSession', None)
        launchArgv = list(argv or [])
        if offscreenSession is not None:
            env = offscreenSession.buildEnvironment(env)
            env.setdefault('TRIO_ALLOW_MANAGED_DISPLAY_WEBENGINE', '0')
            env.setdefault('QTWEBENGINE_DISABLE_SANDBOX', '1')
            env.setdefault('QT_USE_NATIVE_WINDOWS', '1')
            chromium_flags = str(env.get('QTWEBENGINE_CHROMIUM_FLAGS', EMPTY_STRING) or EMPTY_STRING).strip()
            for flag in ('--no-sandbox', '--disable-gpu', '--disable-gpu-compositing'):
                if flag not in chromium_flags:
                    chromium_flags = (chromium_flags + ' ' + flag).strip()
            if chromium_flags:
                env['QTWEBENGINE_CHROMIUM_FLAGS'] = chromium_flags.strip()
            existingGeometry = False
            for index, token in enumerate(launchArgv):
                text = str(token or EMPTY_STRING).strip()
                if text == '-qwindowgeometry' or text.startswith('-qwindowgeometry'):
                    existingGeometry = True
                    break
            if not existingGeometry:
                launchArgv.extend(['-qwindowgeometry', offscreenSession.windowGeometryArgument()])
        childPython = str(getattr(self, 'childPythonPath', EMPTY_STRING) or resolvePreferredChildPython(guiRequired=not cliHasAnyFlag(launchArgv, HEADLESS_FLAGS))).strip() or str(sys.executable or EMPTY_STRING)
        self.childPythonPath = childPython
        env['TRIO_CHILD_PYTHON'] = childPython
        try:
            self.emit(f'[PromptDebugger] Prompt child surface probe skipped patch={PROMPT_RELEASE_PATCH_LEVEL} reason=avoid-startup-timeout', self.logPath)
        except Exception as error:
            captureException(None, source='start.py', context='except@9810')
            print(f"[WARN:swallowed-exception] start.py:8437 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        if self.enabled:
            try:
                proxy_server = self.startTrafficProxyServer()
                if proxy_server is not None and not any(str(token or EMPTY_STRING).strip().lower() in {'--proxy', '-proxy', '/proxy'} for token in launchArgv):
                    launchArgv.extend(['--proxy', proxy_server.endpoint()])
            except Exception as error:
                captureException(None, source='start.py', context='except@9817')
                print(f"[WARN:swallowed-exception] start.py:8444 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                self.emit(f'[PromptDebugger] Traffic proxy disabled: {type(error).__name__}: {error}', self.exceptionPath)
        args = [*childPythonCommandPrefix(childPython), str(GTP_PATH), *launchArgv]
        self.childArgs = list(launchArgv)
        self.closeChildStreams()
        relayChildOutput = True  # Prompt V138: relay child output so startup/push/GUI traces are visible
        if relayChildOutput:
            popen_kwargs = {
                'cwd': str(BASE_DIR),
                'env': env,
                'stdin': subprocess.DEVNULL,
                'stdout': subprocess.PIPE,
                'stderr': subprocess.PIPE,
                'text': True,
                'encoding': 'utf-8',
                'errors': 'replace',
                'bufsize': 1,
            }
            if self.enabled:
                mode_label = 'debug (output→parent relay)'
            else:
                mode_label = 'production (trace output→parent relay)'
        else:
            self.childStdoutHandle = None
            self.childStderrHandle = None
            popen_kwargs = {
                'cwd': str(BASE_DIR),
                'env': env,
                'stdin': subprocess.DEVNULL,
                'stdout': subprocess.DEVNULL,
                'stderr': subprocess.DEVNULL,
            }
            if os.name == 'nt':
                flags = 0
                for name in ('DETACHED_PROCESS', 'CREATE_NEW_PROCESS_GROUP', 'CREATE_DEFAULT_ERROR_MODE', 'CREATE_BREAKAWAY_FROM_JOB'):
                    flags |= int(getattr(subprocess, name, 0) or 0)
                if flags:
                    popen_kwargs['creationflags'] = flags
            else:
                popen_kwargs['start_new_session'] = True
            mode_label = 'production (output→discarded)'
        launch_command_text = subprocess.list2cmdline([str(part) for part in args])
        self.emit(f'[PromptDebugger] LAUNCH CHILD BEGIN cwd={BASE_DIR} python={childPython} mode={mode_label}', self.logPath)
        self.emit(f'[PromptDebugger] LAUNCH CHILD COMMAND {launch_command_text}', self.logPath)
        try:
            proc = START_EXECUTION_LIFECYCLE.startProcess('PromptChildProcess', args, **popen_kwargs)
        except Exception:
            captureException(None, source='start.py', context='except@9861')
            self.emit('[PromptDebugger] LAUNCH CHILD FAILED before process handle was created', self.exceptionPath)
            self.closeChildStreams()
            raise
        self.consoleStop.clear()
        self.childProcessRowId = int(self.db.insertProcessRecord(
            source='start.py',
            phase_key='parent-launch-child',
            phase_name='Parent Launch Child',
            process_name='PromptChildProcess',
            kind='process',
            pid=int(getattr(proc, 'pid', 0) or 0),
            status='running',
            started_at=time.time(),
            ttl_seconds=float(os.environ.get('PROMPT_CHILD_TTL_SECONDS', '0') or 0),
            command=launch_command_text,
            metadata=json.dumps({'argv': launchArgv, 'mode': mode_label}, ensure_ascii=False, default=str),
            processed=0,
        ) or 0)
        self.child = proc
        self.childPid = int(proc.pid)
        self.childControlPort = 0
        self.childSurfaces = set()
        self.childSurfacesKnown = False
        self.childSurfaceWarned = False
        self.childExitCode = None
        if relayChildOutput:
            self.childStdoutHandle = proc.stdout
            self.childStderrHandle = proc.stderr
            if proc.stdout is not None:
                self.childStdoutThread = self.startChildPipePump(proc.stdout, 'stdout', self.logPath)
            if proc.stderr is not None:
                self.childStderrThread = self.startChildPipePump(proc.stderr, 'stderr', self.exceptionPath)
        self.emit(f'[PromptDebugger] Child launched  pid={self.childPid}  file={GTP_PATH.name}  mode={mode_label}  display={str(getattr(getattr(self, "offscreenSession", None), "displayName", EMPTY_STRING) or "native")}', self.logPath)
        return proc
    def checkErroredProcessRows(self) -> bool:
        try:
            expired_rows = self.db.expireOverdueProcessRows()
            for expired in list(expired_rows or []):
                pid = int(expired.get('pid') or 0)
                if pid > 0 and pid != os.getpid():
                    try:
                        if os.name == 'nt':
                            startLifecycleRunCommand(['taskkill', '/F', '/PID', str(pid)], capture_output=True, text=True, timeout=8, check=False)
                        else:
                            os.kill(pid, signal.SIGTERM)
                    except Exception as error:
                        captureException(None, source='start.py', context='except@9906')
                        print(f"[WARN:swallowed-exception] start.py:8532 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                        pass
        except Exception as expire_error:
            captureException(None, source='start.py', context='except@9909')
            print(f"[WARN:swallowed-exception] start.py:8534 {type(expire_error).__name__}: {expire_error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            self.emit(f'[WARNING:process-db] ttl supervision failed: {type(expire_error).__name__}: {expire_error}', self.exceptionPath)
        try:
            rows = list(self.db.readErroredProcessRows(limit=20) or [])
        except Exception as read_error:
            captureException(None, source='start.py', context='except@9914')
            print(f"[WARN:swallowed-exception] start.py:8538 {type(read_error).__name__}: {read_error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            self.emit(f'[WARNING:process-db] read errored processes failed: {type(read_error).__name__}: {read_error}', self.exceptionPath)
            return False
        if not rows:
            return False
        first = rows[0]
        process_text = json.dumps(rows, ensure_ascii=False, indent=2, default=str)
        self.emit('[PromptDebugger] Errored process row detected; opening debug screen.\n' + process_text, self.exceptionPath)
        if not self.inCrashConsole:
            self.openCrashConsole({
                'type_name': 'ErroredProcessRecord',
                'message': str(first.get('error_message') or first.get('fault_reason') or f"process {first.get('id')} errored"),
                'thread': threading.current_thread().name,
                'timestamp': time.time(),
                'traceback_text': str(first.get('traceback_text') or EMPTY_STRING),
                'process_snapshot': process_text,
            }, block=False)
        try:
            self.db.markProcessRowsProcessed([row.get('id') for row in rows])
        except Exception as error:
            captureException(None, source='start.py', context='except@9934')
            print(f"[WARN:swallowed-exception] start.py:8557 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        return True

    def waitForChild(self, pollInterval: float = 0.25) -> int:  # noqa: nonconform reviewed return contract
        proc = self.child
        if proc is None:
            return int(self.childExitCode or 1)
        try:
            while True:  # noqa: badcode reviewed detector-style finding
                code = proc.poll()
                if code is not None:
                    self.markChildExited(proc)
                    should_open = int(self.childExitCode or 0) != 0 and not self.inCrashConsole
                    if should_open and time.time() >= float(getattr(self, 'consoleSnoozeUntil', 0.0) or 0.0):
                        self.openCrashConsole({
                            'type_name': 'ChildExit',
                            'message': f'{GTP_PATH.name} exited with code {self.childExitCode}',
                            'thread': threading.current_thread().name,
                            'timestamp': time.time(),
                            'traceback_text': self.readFile(self.exceptionPath),
                        }, block=True)
                    return int(self.childExitCode or 0)
                self.checkErroredProcessRows()
                if float(getattr(self, 'consoleSnoozeUntil', 0.0) or 0.0) > time.time() and not self.inCrashConsole:
                    time.sleep(max(0.05, float(pollInterval or 0.25)))
                    continue
                time.sleep(max(0.05, float(pollInterval or 0.25)))
        finally:
            self.closeChildStreams()
PS_RESET = '\x1b[0m'
PS_BOLD = '\x1b[1m'
PS_ITALIC = '\x1b[3m'
PS_DIM = '\x1b[90m'
PS_RED = '\x1b[91m'
PS_GREEN = '\x1b[92m'
PS_YELLOW = '\x1b[93m'
PS_BLUE = '\x1b[94m'
PS_MAGENTA = '\x1b[95m'
PS_CYAN = '\x1b[96m'
PS_WHITE = '\x1b[97m'


class FileAstScopeIndex(ast.NodeVisitor):
    def __init__(self, file_path: str):
        self.filePath = str(file_path or EMPTY_STRING)  # noqa: nonconform
        self.moduleGlobals: dict[str, dict[str, Any]] = {}
        self.classDefinitions: dict[str, dict[str, Any]] = {}
        self.functionDefinitions: list[dict[str, Any]] = []
        self.localsByScope: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        self.attrsByClass: dict[str, dict[str, dict[str, Any]]] = {}
        self.scopeStack: list[tuple[str, str]] = []

    def currentClassName(self) -> str:
        for kind, name in reversed(self.scopeStack):
            if kind == 'class':
                return str(name or EMPTY_STRING)
        return EMPTY_STRING

    def currentFunctionName(self) -> str:
        for kind, name in reversed(self.scopeStack):
            if kind == 'function':
                return str(name or EMPTY_STRING)
        return EMPTY_STRING

    def qualifiedFunctionName(self, name: str) -> str:
        parts = [entry_name for entry_kind, entry_name in self.scopeStack if entry_kind in {'class', 'function'}]
        parts.append(str(name or EMPTY_STRING))
        return '.'.join(part for part in parts if part)

    def recordGlobal(self, name: str, lineno: int, kind: str = 'assignment'):
        key = str(name or EMPTY_STRING)
        if not key or key in self.moduleGlobals:
            return
        self.moduleGlobals[key] = {
            'name': key,
            'kind': str(kind or 'assignment'),
            'line': int(lineno or 0),
            'class_name': EMPTY_STRING,
            'function_name': EMPTY_STRING,
            'file': self.filePath,
        }

    def recordLocal(self, name: str, lineno: int, kind: str = 'assignment'):
        key = str(name or EMPTY_STRING)
        function_name = self.currentFunctionName()
        if not key or not function_name:
            return
        class_name = self.currentClassName()
        scope_key = (str(class_name or EMPTY_STRING), str(function_name or EMPTY_STRING))
        bucket = self.localsByScope.setdefault(scope_key, {})
        if key in bucket:
            return
        bucket[key] = {
            'name': key,
            'kind': str(kind or 'assignment'),
            'line': int(lineno or 0),
            'class_name': str(class_name or EMPTY_STRING),
            'function_name': str(function_name or EMPTY_STRING),
            'file': self.filePath,
        }

    def recordAttribute(self, name: str, lineno: int, method_name: str, kind: str = 'attribute'):
        class_name = self.currentClassName()
        key = str(name or EMPTY_STRING)
        if not class_name or not key:
            return
        bucket = self.attrsByClass.setdefault(str(class_name or EMPTY_STRING), {})
        if key in bucket:
            return
        bucket[key] = {
            'name': key,
            'kind': str(kind or 'attribute'),
            'line': int(lineno or 0),
            'class_name': str(class_name or EMPTY_STRING),
            'function_name': str(method_name or EMPTY_STRING),
            'file': self.filePath,
        }

    def recordTarget(self, target, lineno: int, kind: str = 'assignment'):  # norecurse: intentional finite AST assignment target walk
        if isinstance(target, ast.Name):
            if self.currentFunctionName():
                self.recordLocal(target.id, lineno, kind)
            else:
                self.recordGlobal(target.id, lineno, kind)
            return
        if isinstance(target, ast.Attribute):
            if isinstance(target.value, ast.Name) and str(target.value.id or EMPTY_STRING) == 'self':
                self.recordAttribute(str(target.attr or EMPTY_STRING), lineno, self.currentFunctionName(), kind)
            return
        if isinstance(target, (ast.Tuple, ast.List, ast.Set)):
            for elt in list(getattr(target, 'elts', []) or []):
                self.recordTarget(elt, lineno, kind)
            return
        if isinstance(target, ast.Starred):
            self.recordTarget(getattr(target, 'value', None), lineno, kind)

    def visit_ClassDef(self, node):
        class_name = str(getattr(node, 'name', EMPTY_STRING) or EMPTY_STRING)
        self.classDefinitions.setdefault(class_name, {
            'name': class_name,
            'kind': 'class',
            'line': int(getattr(node, 'lineno', 0) or 0),
            'end_line': int(getattr(node, 'end_lineno', getattr(node, 'lineno', 0)) or 0),
            'class_name': class_name,
            'function_name': EMPTY_STRING,
            'file': self.filePath,
        })
        if not self.currentFunctionName():
            self.recordGlobal(class_name, getattr(node, 'lineno', 0), 'class')
        else:
            self.recordLocal(class_name, getattr(node, 'lineno', 0), 'class')
        self.scopeStack.append(('class', class_name))
        self.generic_visit(node)
        self.scopeStack.pop()

    def visit_FunctionDef(self, node):
        self._visitFunctionLike(node, async_kind=False)

    def visit_AsyncFunctionDef(self, node):
        self._visitFunctionLike(node, async_kind=True)

    def _visitFunctionLike(self, node, async_kind: bool = False):
        function_name = str(getattr(node, 'name', EMPTY_STRING) or EMPTY_STRING)
        meta = {
            'name': function_name,
            'qualname': self.qualifiedFunctionName(function_name),
            'kind': 'async_function' if async_kind else 'function',
            'line': int(getattr(node, 'lineno', 0) or 0),
            'end_line': int(getattr(node, 'end_lineno', getattr(node, 'lineno', 0)) or 0),
            'class_name': str(self.currentClassName() or EMPTY_STRING),
            'function_name': function_name,
            'file': self.filePath,
        }
        self.functionDefinitions.append(meta)
        if not self.currentFunctionName():
            self.recordGlobal(function_name, getattr(node, 'lineno', 0), meta['kind'])
        else:
            self.recordLocal(function_name, getattr(node, 'lineno', 0), meta['kind'])
        self.scopeStack.append(('function', function_name))
        args = list(getattr(getattr(node, 'args', None), 'posonlyargs', []) or [])  # noqa: badcode reviewed detector-style finding
        args += list(getattr(getattr(node, 'args', None), 'args', []) or [])  # noqa: badcode reviewed detector-style finding
        args += list(getattr(getattr(node, 'args', None), 'kwonlyargs', []) or [])
        vararg = getattr(getattr(node, 'args', None), 'vararg', None)
        kwarg = getattr(getattr(node, 'args', None), 'kwarg', None)
        if vararg is not None:
            args.append(vararg)
        if kwarg is not None:
            args.append(kwarg)
        for arg in args:
            arg_name = str(getattr(arg, 'arg', EMPTY_STRING) or EMPTY_STRING)
            if arg_name:
                self.recordLocal(arg_name, getattr(arg, 'lineno', getattr(node, 'lineno', 0)), 'argument')
        self.generic_visit(node)
        self.scopeStack.pop()

    def visit_Assign(self, node):
        for target in list(getattr(node, 'targets', []) or []):
            self.recordTarget(target, getattr(node, 'lineno', 0), 'assignment')
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        self.recordTarget(getattr(node, 'target', None), getattr(node, 'lineno', 0), 'annotation')
        self.generic_visit(node)

    def visit_AugAssign(self, node):
        self.recordTarget(getattr(node, 'target', None), getattr(node, 'lineno', 0), 'augassign')
        self.generic_visit(node)

    def visit_For(self, node):
        self.recordTarget(getattr(node, 'target', None), getattr(node, 'lineno', 0), 'loop_target')
        self.generic_visit(node)

    def visit_AsyncFor(self, node):
        self.recordTarget(getattr(node, 'target', None), getattr(node, 'lineno', 0), 'loop_target')
        self.generic_visit(node)

    def visit_With(self, node):
        for item in list(getattr(node, 'items', []) or []):
            optional_vars = getattr(item, 'optional_vars', None)
            if optional_vars is not None:
                self.recordTarget(optional_vars, getattr(node, 'lineno', 0), 'with_target')
        self.generic_visit(node)

    def visit_AsyncWith(self, node):
        for item in list(getattr(node, 'items', []) or []):
            optional_vars = getattr(item, 'optional_vars', None)
            if optional_vars is not None:
                self.recordTarget(optional_vars, getattr(node, 'lineno', 0), 'with_target')
        self.generic_visit(node)

    def visit_ExceptHandler(self, node):
        name = getattr(node, 'name', None)
        if isinstance(name, str) and name:
            if self.currentFunctionName():
                self.recordLocal(name, getattr(node, 'lineno', 0), 'exception')
            else:
                self.recordGlobal(name, getattr(node, 'lineno', 0), 'exception')
        self.generic_visit(node)

    def visit_Import(self, node):
        for alias in list(getattr(node, 'names', []) or []):
            alias_name = str(getattr(alias, 'asname', None) or getattr(alias, 'name', EMPTY_STRING) or EMPTY_STRING)
            local_name = alias_name.split('.')[0]
            if self.currentFunctionName():
                self.recordLocal(local_name, getattr(node, 'lineno', 0), 'import')
            else:
                self.recordGlobal(local_name, getattr(node, 'lineno', 0), 'import')
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        for alias in list(getattr(node, 'names', []) or []):
            alias_name = str(getattr(alias, 'asname', None) or getattr(alias, 'name', EMPTY_STRING) or EMPTY_STRING)
            if self.currentFunctionName():
                self.recordLocal(alias_name, getattr(node, 'lineno', 0), 'import')
            else:
                self.recordGlobal(alias_name, getattr(node, 'lineno', 0), 'import')
        self.generic_visit(node)

    def findScopeForFrame(self, function_name: str, lineno: int) -> dict[str, Any] | None:
        target_name = str(function_name or EMPTY_STRING)
        target_line = int(lineno or 0)
        matches = []
        for meta in self.functionDefinitions:
            if str(meta.get('name', EMPTY_STRING)) != target_name:
                continue
            start = int(meta.get('line', 0) or 0)
            end = int(meta.get('end_line', start) or start)
            if start <= target_line <= max(start, end):
                matches.append(meta)
        if not matches:
            return None
        matches.sort(key=lambda item: (int(item.get('end_line', item.get('line', 0)) or 0) - int(item.get('line', 0) or 0), -int(item.get('line', 0) or 0)))
        return matches[0]


class TrioTrafficProxyServer:
    def __init__(self, owner, host: str = '127.0.0.1', port: int = 6666):
        self.owner = owner  # noqa: nonconform
        self.host = str(host or '127.0.0.1')  # noqa: nonconform
        self.port = int(port or 6666)
        self.httpd = None  # noqa: nonconform
        self.thread = None
        self.startedAt = 0.0
        self.messageCount = 0  # noqa: nonconform
        self.mode = 'parent'  # noqa: nonconform
        self.isElevated = bool((os.name != 'nt' and hasattr(os, 'geteuid') and os.geteuid() == 0) or (os.name == 'nt' and bool(getattr(ctypes.windll.shell32, 'IsUserAnAdmin', lambda: 0)()))) if 'ctypes' in globals() else False  # noqa: nonconform

    def endpoint(self) -> str:
        return f'{self.host}:{self.port}'

    def endpointUrl(self) -> str:
        return 'http://' + self.endpoint()

    def statusPayload(self) -> dict[str, Any]:
        owner = getattr(self, 'owner', None)
        return {
            'ok': True,
            'mode': str(getattr(self, 'mode', 'parent') or 'parent'),
            'started_at': float(getattr(self, 'startedAt', 0.0) or 0.0),
            'started_text': datetime.datetime.fromtimestamp(float(getattr(self, 'startedAt', time.time()) or time.time())).isoformat(sep=' ', timespec='seconds'),
            'messages_processed': int(getattr(self, 'messageCount', 0) or 0),
            'is_elevated': bool(getattr(self, 'isElevated', False)),
            'version': '1.0',
            'md5': safeFileMd5Hex(__file__) or EMPTY_STRING,
            'bound_url': self.endpointUrl(),
            'entry': 'start.py' + (' --is-proxy-daemon=1' if str(getattr(self, 'mode', 'parent')) == 'daemon' else EMPTY_STRING),
            'pid': os.getpid(),
            'child_pid': int(getattr(owner, 'childPid', 0) or 0) if owner is not None else 0,
        }

    def statusText(self) -> str:
        payload = self.statusPayload()
        return '\n'.join([
            f"FlatLine Traffic Monitor bound to {payload['bound_url']}",
            f"mode={payload['mode']}",
            f"entry={payload['entry']}",
            f"started={payload['started_text']}",
            f"messages_processed={payload['messages_processed']}",
            f"isElevated={payload['is_elevated']}",
            f"version={payload['version']}",
            f"md5={payload['md5']}",
        ]) + '\n'

    def start(self):
        owner = self.owner
        outer = self
        class Handler(http.server.BaseHTTPRequestHandler):
            server_version = 'TrioTrafficProxy/1.0'
            def log_message(self, format, *args):
                return
            def _send_json(self, status_code: int, payload: dict):
                raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                self.send_response(int(status_code or 200))
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _send_text(self, status_code: int, text_value: str):
                raw = str(text_value or EMPTY_STRING).encode('utf-8', 'replace')
                self.send_response(int(status_code or 200))
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path.rstrip('/') not in {'', '/status'}:
                    self._send_text(404, 'Not Found\n')
                    return
                self._send_text(200, outer.statusText())

            def do_POST(self):
                if self.path.rstrip('/') not in {'', '/send'}:
                    self._send_json(404, {'ok': False, 'error': 'unknown path'})
                    return
                try:
                    length = int(self.headers.get('Content-Length', '0') or 0)
                except Exception as error:
                    captureException(None, source='start.py', context='except@10294')
                    print(f"[WARN:swallowed-exception] start.py:8917 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    length = 0
                raw_body = self.rfile.read(max(0, length)) if length > 0 else b''
                try:
                    payload = json.loads(raw_body.decode('utf-8', 'replace') or '{}')
                except Exception as error:
                    captureException(None, source='start.py', context='except@10300')
                    print(f"[WARN:swallowed-exception] start.py:8922 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    self._send_json(400, {'ok': False, 'error': f'invalid json: {error}'})
                    return
                dest = str(payload.get('dest', EMPTY_STRING) or EMPTY_STRING).strip()
                method = str(payload.get('method', 'POST') or 'POST').upper()
                timeout = max(0.25, float(payload.get('timeout', 60.0) or 60.0))
                headers = dict(payload.get('headers', {}) or {})
                caller_text = str(payload.get('caller', EMPTY_STRING) or EMPTY_STRING).strip()
                data_text = str(payload.get('data', EMPTY_STRING) or EMPTY_STRING)
                encoding = str(payload.get('data_encoding', 'text') or 'text').strip().lower()
                if not dest:
                    self._send_json(400, {'ok': False, 'error': 'missing dest'})
                    return
                if encoding == 'base64':
                    request_data = base64.standard_b64decode(data_text) if data_text else b''
                elif method == 'GET' and not data_text:
                    request_data = None
                else:
                    request_data = data_text.encode('utf-8', 'replace')
                row_id = 0
                started = time.time()
                try:
                    row_id = int(owner.db.writeTrafficRow(
                        headers_text=json.dumps(headers, ensure_ascii=False, default=str),
                        data_text=data_text,
                        status_text='queued',
                        error_text=EMPTY_STRING,
                        length_value=len(request_data or b'') if request_data is not None else 0,
                        destination_text=dest,
                        roundtrip_microtime=0.0,
                        processed=0,
                        timestamp=started,
                        caller_text=caller_text,
                        response_preview_text=EMPTY_STRING,
                    ) or 0)
                except Exception as error:
                    captureException(None, source='start.py', context='except@10336')
                    print(f"[WARN:swallowed-exception] start.py:8957 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    row_id = 0
                try:
                    req = urllib.request.Request(dest, data=request_data, headers=headers, method=method)
                    with urllib.request.urlopen(req, timeout=timeout) as response:
                        response_bytes = response.read()
                        response_headers = dict(getattr(response, 'headers', {}) or {})
                        status_code = int(getattr(response, 'status', getattr(response, 'code', 200)) or 200)
                    roundtrip = float((time.time() - started) * 1000000.0)
                    try:
                        response_text = response_bytes.decode('utf-8')
                        response_encoding = 'text'
                        response_payload = response_text
                    except Exception as error:
                        captureException(None, source='start.py', context='except@10350')
                        print(f"[WARN:swallowed-exception] start.py:8970 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                        response_encoding = 'base64'
                        response_payload = base64.standard_b64encode(response_bytes).decode('ascii')
                    preview_text = str((response_bytes.decode('utf-8', 'replace') if response_bytes else EMPTY_STRING) or EMPTY_STRING)
                    preview_text = re.sub(r'\s+', ' ', preview_text.replace('\r', ' ').replace('\n', ' ')).strip()
                    if len(preview_text) > 80:
                        preview_text = preview_text[:77] + '...'
                    owner.db.updateTrafficRow(row_id, status_text=str(status_code), error_text=EMPTY_STRING, length_value=len(response_bytes), roundtrip_microtime=roundtrip, caller_text=caller_text, response_preview_text=preview_text)
                    outer.messageCount = int(getattr(outer, 'messageCount', 0) or 0) + 1
                    self._send_json(200, {
                        'ok': True,
                        'id': row_id,
                        'status': str(status_code),
                        'error': EMPTY_STRING,
                        'length': len(response_bytes),
                        'destination': dest,
                        'roundtrip_microtime': roundtrip,
                        'response_headers': response_headers,
                        'response_encoding': response_encoding,
                        'response_data': response_payload,
                    })
                except Exception as error:
                    captureException(None, source='start.py', context='except@10372')
                    print(f"[WARN:swallowed-exception] start.py:8991 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    roundtrip = float((time.time() - started) * 1000000.0)
                    owner.db.updateTrafficRow(row_id, status_text='error', error_text=f'{type(error).__name__}: {error}', length_value=0, roundtrip_microtime=roundtrip, caller_text=caller_text, response_preview_text=EMPTY_STRING)
                    outer.messageCount = int(getattr(outer, 'messageCount', 0) or 0) + 1
                    self._send_json(502, {
                        'ok': False,
                        'id': row_id,
                        'status': 'error',
                        'error': f'{type(error).__name__}: {error}',
                        'length': 0,
                        'destination': dest,
                        'roundtrip_microtime': roundtrip,
                        'response_headers': {},
                        'response_encoding': 'text',
                        'response_data': EMPTY_STRING,
                    })

        class Server(http.server.ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.httpd = Server((self.host, self.port), Handler)
        self.startedAt = time.time()
        self.thread = START_EXECUTION_LIFECYCLE.startThread('TrioTrafficProxy', self.httpd.serve_forever, daemon=True)
        return True

    def stop(self):
        try:
            if self.httpd is not None:
                self.httpd.shutdown()
                self.httpd.server_close()
        except Exception as error:
            captureException(None, source='start.py', context='except@10404')
            print(f"[WARN:swallowed-exception] start.py:9022 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        self.httpd = None


class PromptDebugger(DebuggerDatabase):
    def __init__(self, enabled: bool = False):
        DebuggerDatabase.__init__(self, self)
        self.enabled = bool(enabled)
        self.debuggerVersion = DEBUGGER_VERSION  # noqa: nonconform
        self.debuggerStartTime = time.time()  # noqa: nonconform
        self.traceVerbose = False  # noqa: nonconform
        self.logPath = str(DEBUG_LOG_PATH)
        self.snapshotPath = str(SNAPSHOT_LOG_PATH)  # noqa: nonconform
        self.exceptionPath = str(EXCEPTION_LOG_PATH)
        self.childStdoutPath = 'DEVNULL'  # noqa: nonconform
        self.childStderrPath = 'DEVNULL'  # noqa: nonconform
        self.tracePath = str(LOG_DIR / 'trace.log')  # noqa: nonconform
        self.traceRawPath = str(LOG_DIR / 'trace-full.log')  # noqa: nonconform
        self.traceCoveragePath = EMPTY_STRING  # noqa: nonconform
        self.parentFaultPath = str(PARENT_FAULT_LOG_PATH)  # noqa: nonconform
        self.parentFaultHandle = None  # noqa: nonconform
        self.parentStackTracingInstalled = False  # noqa: nonconform
        self.offscreenEnabled = False  # noqa: nonconform
        self.captureEngineKind = 'xvfb'
        self.offscreenScreenSpec = CaptureEngine.DEFAULT_SCREEN  # noqa: nonconform
        self.captureEngine = None  # noqa: nonconform
        self.offscreenSession = None  # noqa: nonconform
        self.offscreenCaptureFlags = set()  # noqa: nonconform
        self.offscreenCaptureThreads = {}  # noqa: nonconform
        self.offscreenBackgroundColor = None  # noqa: nonconform
        self.offscreenBackgroundAlpha = 0  # noqa: nonconform
        self.offscreenActionPlan = []  # noqa: nonconform
        self.captureEngineKind = 'xvfb'  # noqa: nonconform
        self.offscreenActionThread = None  # noqa: nonconform
        self.proxyInitialized = False  # noqa: nonconform
        self.owner = self  # noqa: nonconform
        self.connectionPath = EMPTY_STRING  # noqa: nonconform
        self.connection = None  # noqa: nonconform
        self.db = DebuggerDatabase(self)
        self.lock = threading.RLock()  # noqa: nonconform
        self.consoleStop = threading.Event()
        self.consoleWake = threading.Event()
        self.consoleDone = threading.Event()  # noqa: nonconform
        self.statusStop = threading.Event()  # noqa: nonconform
        self.keyStop = threading.Event()  # noqa: nonconform
        self.connectionMonitorStop = threading.Event()  # noqa: nonconform
        self.child = None
        self.childArgs = []  # noqa: nonconform
        self.childPid = 0  # noqa: nonconform
        self.childExitCode = None  # noqa: nonconform
        self.childProcessRowId = 0  # noqa: nonconform
        self.childControlPort = 0  # noqa: nonconform
        self.childSurfaces = set()  # noqa: nonconform
        self.childSurfacesKnown = False  # noqa: nonconform
        self.childSurfaceWarned = False  # noqa: nonconform
        self.childStdoutHandle = None  # noqa: nonconform
        self.childStderrHandle = None  # noqa: nonconform
        self.childStdoutThread = None  # noqa: nonconform
        self.childStderrThread = None  # noqa: nonconform
        self.childMd5Short = EMPTY_STRING  # noqa: nonconform
        self.shellRpcServer = None  # noqa: nonconform
        self.server = None
        self.serverThread = None  # noqa: nonconform
        self.relayServer = None  # noqa: nonconform
        self.relayThread = None  # noqa: nonconform
        self.relayPort = 0  # noqa: nonconform
        self.relayToken = hashlib.sha256(f'{os.getpid()}:{time.time()}'.encode('utf-8', 'replace')).hexdigest()  # noqa: nonconform
        self.watchdogThread = None  # noqa: nonconform
        self.consoleThread = None  # noqa: nonconform
        self.keyThread = None  # noqa: nonconform
        self.statusThread = None  # noqa: nonconform
        self.snapshotThread = None  # noqa: nonconform
        self.actionThread = None  # noqa: nonconform
        self.connectionMonitorThread = None  # noqa: nonconform
        self.connectionMonitorEnabled = False  # noqa: nonconform
        self.lastConnectionText = EMPTY_STRING  # noqa: nonconform
        self.lastConnectionDigest = EMPTY_STRING  # noqa: nonconform
        self.trafficProxyServer = None  # noqa: nonconform
        self.trafficProxyAddress = '127.0.0.1:6666'  # noqa: nonconform
        self.lastHeartbeat = 0.0  # noqa: nonconform
        self.lastHeartbeatReason = 'BOOT'  # noqa: nonconform
        self.lastHeartbeatCaller = EMPTY_STRING  # noqa: nonconform
        self.lastProcessLoop = 0.0  # noqa: nonconform
        self.lastProcessReason = 'BOOT'  # noqa: nonconform
        self.heartbeatEverFired = False  # noqa: nonconform
        self.statusOverrideReason = EMPTY_STRING  # noqa: nonconform
        self.lastStatusLine = EMPTY_STRING  # noqa: nonconform
        self.spinnerFrames = list(HEARTBEAT_FRAMES)  # noqa: nonconform
        self.spinnerIndex = 0  # noqa: nonconform
        self.inCrashConsole = False  # noqa: nonconform
        self.consoleSnoozeUntil = 0.0  # noqa: nonconform
        self.manualDebugCooldownUntil = 0.0  # noqa: nonconform
        self.crashInfo = {}  # noqa: nonconform
        self.dismissedSignature = EMPTY_STRING  # noqa: nonconform
        self.lastOpenedSignature = EMPTY_STRING  # noqa: nonconform
        self.lastFreezeDump = EMPTY_STRING  # noqa: nonconform
        self.preConsoleStackText = EMPTY_STRING  # noqa: nonconform
        self.preConsoleVarText = EMPTY_STRING  # noqa: nonconform
        self.freezeThresholdSeconds = 8.0  # noqa: nonconform
        self.deathPollInterval = 1.0  # noqa: nonconform
        self.astIndexCache = {}  # noqa: nonconform
        self.astIndexErrorCache = {}  # noqa: nonconform
        self.dbPollBaseline = {'heartbeat': 0, 'faults': 0, 'exceptions': 0}  # noqa: nonconform
        self.captureDbPollBaseline()

    def captureDbPollBaseline(self):
        try:
            baseline = dict(cast(Any, getattr(self, 'db', None)).readLatestDebuggerRowIds() or {}) if getattr(self, 'db', None) is not None else {}
        except Exception as error:
            captureException(None, source='start.py', context='except@10514')
            print(f"[WARN:swallowed-exception] start.py:9132 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            baseline = {}
        self.dbPollBaseline = {
            'heartbeat': int(baseline.get('heartbeat', 0) or 0),
            'faults': int(baseline.get('faults', 0) or 0),
            'exceptions': int(baseline.get('exceptions', 0) or 0),
        }
        return dict(self.dbPollBaseline)

    def advanceDbPollBaseline(self, key: str, rows) -> int:
        baseline = dict(getattr(self, 'dbPollBaseline', {}) or {})
        current = int(baseline.get(key, 0) or 0)
        for row in list(rows or []):
            try:
                current = max(current, int(row.get('id', 0) or 0))
            except Exception as error:
                captureException(None, source='start.py', context='except@10530')
                print(f"[WARN:swallowed-exception] start.py:9147 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
        baseline[key] = current
        self.dbPollBaseline = baseline
        return current

    def configureOffscreen(self, enabled: bool = False, captureEngineKind: str = 'xvfb', screenSpec: str = EMPTY_STRING, backgroundColor=None, backgroundAlpha: int = 0, actions=None):
        self.offscreenEnabled = bool(enabled)
        self.captureEngineKind = str(captureEngineKind or 'xvfb').strip().lower() or 'xvfb'
        self.offscreenScreenSpec = str(screenSpec or getattr(self, 'offscreenScreenSpec', CaptureEngine.DEFAULT_SCREEN) or CaptureEngine.DEFAULT_SCREEN).strip() or CaptureEngine.DEFAULT_SCREEN
        self.offscreenBackgroundColor = tuple(backgroundColor[:3]) if isinstance(backgroundColor, (tuple, list)) and len(backgroundColor) >= 3 else None
        self.offscreenBackgroundAlpha = max(0, min(255, int(backgroundAlpha or 0)))
        self.offscreenActionPlan = list(actions or [])
        return self.offscreenEnabled

    def startOffscreenSession(self):
        if not bool(getattr(self, 'offscreenEnabled', False)):
            return None
        session = getattr(self, 'captureEngine', None)
        desiredKind = str(getattr(self, 'captureEngineKind', 'xvfb') or 'xvfb').strip().lower() or 'xvfb'
        if session is None or str(getattr(session, 'ENGINE_KIND', desiredKind) or desiredKind).strip().lower() != desiredKind:
            session = createCaptureEngine(desiredKind, self, screenSpec=getattr(self, 'offscreenScreenSpec', CaptureEngine.DEFAULT_SCREEN), backgroundColor=self.offscreenBackgroundColor, backgroundAlpha=self.offscreenBackgroundAlpha)
            self.captureEngine = session
            self.offscreenSession = session
        else:
            session.screenSpec = session.normalizeScreenSpec(getattr(self, 'offscreenScreenSpec', session.screenSpec))
            session.backgroundColor = tuple(self.offscreenBackgroundColor[:3]) if isinstance(self.offscreenBackgroundColor, (tuple, list)) and len(self.offscreenBackgroundColor) >= 3 else None
            session.backgroundAlpha = max(0, min(255, int(self.offscreenBackgroundAlpha or 0)))
        session.start()
        self.captureEngine = session
        self.offscreenSession = session
        return session

    def stopOffscreenSession(self):
        session = getattr(self, 'captureEngine', None) or getattr(self, 'captureEngine', None) or getattr(self, 'offscreenSession', None)
        self.captureEngine = None
        self.offscreenSession = None
        if session is not None:
            try:
                session.stop()
            except Exception as error:
                captureException(None, source='start.py', context='except@10571')
                print(f"[WARN:swallowed-exception] start.py:9187 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                self.emit(f'[PromptDebugger] Offscreen shutdown error: {type(error).__name__}: {error}', self.exceptionPath)
        return True

    def captureOffscreenScreenshot(self, outputPath: str | Path | None = None, windowId: str = EMPTY_STRING) -> str:
        if not self.debugArtifactWritesAllowed():
            raise RuntimeError('Offscreen screenshots are disabled when --debug is not set')
        session = getattr(self, 'captureEngine', None) or getattr(self, 'offscreenSession', None)
        if session is None:
            raise RuntimeError('Offscreen session is not active')
        return str(session.captureScreenshot(outputPath=outputPath, windowId=windowId))

    def findChildOffscreenWindows(self):
        session = getattr(self, 'captureEngine', None) or getattr(self, 'offscreenSession', None)
        if session is None:
            return []
        return list(session.findChildWindows(self.childPid))

    def offscreenCapturePath(self, reason: str = 'capture', suffix: str = '.png', stamped: bool = False) -> str:
        slug = re.sub(r'[^a-z0-9]+', '_', str(reason or 'capture').strip().lower()).strip('_') or 'capture'
        if stamped:
            stamp = time.strftime('%Y%m%d_%H%M%S')
            return str(SCREENSHOTS_DIR / f'offscreen_{slug}_{stamp}{suffix}')
        return str(SCREENSHOTS_DIR / f'offscreen_{slug}{suffix}')

    def scheduleOffscreenCapture(self, reason: str = 'capture', delaySeconds: float = 0.0, allowRepeat: bool = False, stamped: bool = False):
        if not self.debugArtifactWritesAllowed():
            return None
        if not bool(getattr(self, 'offscreenEnabled', False)):
            return None
        if getattr(self, 'captureEngine', None) or getattr(self, 'offscreenSession', None) is None:
            return None
        flag = str(reason or 'capture').strip().lower() or 'capture'
        if not allowRepeat and flag in getattr(self, 'offscreenCaptureFlags', set()):
            return None
        if not allowRepeat:
            self.offscreenCaptureFlags.add(flag)
        existing = getattr(self, 'offscreenCaptureThreads', {}).get(flag)
        if existing is not None and existing.is_alive():
            return existing
        def runner():
            try:
                delayValue = float(delaySeconds or 0.0)
                if delayValue > 0.0:
                    time.sleep(delayValue)
                outputPath = self.offscreenCapturePath(flag, '.png', stamped=stamped)
                saved = self.captureOffscreenScreenshot(outputPath=outputPath)
                self.emit(f'[PromptDebugger] Offscreen capture saved  reason={flag}  file={saved}', self.logPath)
            except Exception as error:
                captureException(None, source='start.py', context='except@10620')
                print(f"[WARN:swallowed-exception] start.py:9235 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                self.emit(f'[PromptDebugger] Offscreen capture failed  reason={flag}  error={type(error).__name__}: {error}', self.exceptionPath)
            finally:
                try:
                    self.offscreenCaptureThreads.pop(flag, None)
                except Exception as error:
                    captureException(None, source='start.py', context='except@10626')
                    print(f"[WARN:swallowed-exception] start.py:9240 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    pass
        thread = START_EXECUTION_LIFECYCLE.startThread(f'OffscreenCapture:{flag}', self.threadMain(runner, f'OffscreenCapture:{flag}'), daemon=True)
        self.offscreenCaptureThreads[flag] = thread
        return thread

    def resolveOffscreenWindowId(self) -> str:
        session = getattr(self, 'captureEngine', None) or getattr(self, 'offscreenSession', None)
        if session is None:
            return EMPTY_STRING
        return str(session.bestWindowId(self.childPid) or EMPTY_STRING).strip()

    def sendOffscreenKey(self, keySpec: str, windowId: str = EMPTY_STRING) -> bool:
        session = getattr(self, 'captureEngine', None) or getattr(self, 'offscreenSession', None)
        targetId = str(windowId or self.resolveOffscreenWindowId() or EMPTY_STRING).strip()
        if session is None or not targetId:
            return False
        session.focusWindow(targetId)
        return bool(session.sendKey(targetId, keySpec))

    def sendOffscreenText(self, textValue: str, windowId: str = EMPTY_STRING) -> bool:
        session = getattr(self, 'captureEngine', None) or getattr(self, 'offscreenSession', None)
        targetId = str(windowId or self.resolveOffscreenWindowId() or EMPTY_STRING).strip()
        if session is None or not targetId:
            return False
        session.focusWindow(targetId)
        return bool(session.typeText(targetId, textValue))

    def sendOffscreenMove(self, x: int, y: int, windowId: str = EMPTY_STRING) -> bool:
        session = getattr(self, 'captureEngine', None) or getattr(self, 'offscreenSession', None)
        targetId = str(windowId or self.resolveOffscreenWindowId() or EMPTY_STRING).strip()
        if session is None or not targetId:
            return False
        session.focusWindow(targetId)
        return bool(session.move(targetId, x, y))

    def sendOffscreenClick(self, x: int, y: int, button: int = 1, windowId: str = EMPTY_STRING) -> bool:
        session = getattr(self, 'captureEngine', None) or getattr(self, 'offscreenSession', None)
        targetId = str(windowId or self.resolveOffscreenWindowId() or EMPTY_STRING).strip()
        if session is None or not targetId:
            return False
        session.focusWindow(targetId)
        return bool(session.click(targetId, x, y, button=button))

    def scheduleOffscreenActions(self, actions=None, delaySeconds: float = 1.25):
        plan = list(actions if actions is not None else getattr(self, 'offscreenActionPlan', []))
        if not bool(getattr(self, 'offscreenEnabled', False)) or not plan or getattr(self, 'captureEngine', None) or getattr(self, 'offscreenSession', None) is None:
            return None
        existing = getattr(self, 'offscreenActionThread', None)
        if existing is not None and existing.is_alive():
            return existing
        def runner():
            try:
                if delaySeconds > 0:
                    time.sleep(float(delaySeconds))
                targetId = EMPTY_STRING
                deadline = time.time() + 20.0
                while time.time() < deadline and not targetId:
                    targetId = self.resolveOffscreenWindowId()
                    if targetId:
                        break
                    time.sleep(0.25)
                if not targetId:
                    raise RuntimeError('No visible offscreen child window was found for scripted actions')
                for index, action in enumerate(plan, 1):
                    kind = str(action.get('kind', EMPTY_STRING) or EMPTY_STRING).strip().lower()
                    ok = False
                    if kind == 'click':
                        ok = self.sendOffscreenClick(int(action.get('x', 0) or 0), int(action.get('y', 0) or 0), int(action.get('button', 1) or 1), windowId=targetId)
                    elif kind == 'move':
                        ok = self.sendOffscreenMove(int(action.get('x', 0) or 0), int(action.get('y', 0) or 0), windowId=targetId)
                    elif kind == 'wait':
                        ok = True
                        time.sleep(max(0.0, float(action.get('seconds', 0.0) or 0.0)))
                    elif kind == 'key':
                        ok = self.sendOffscreenKey(str(action.get('value', EMPTY_STRING) or EMPTY_STRING), windowId=targetId)
                    elif kind == 'type':
                        ok = self.sendOffscreenText(str(action.get('value', EMPTY_STRING) or EMPTY_STRING), windowId=targetId)
                    time.sleep(0.40 if kind != 'wait' else 0.10)
                    label = f'action_{index:02d}_{kind or "step"}'
                    try:
                        saved = self.captureOffscreenScreenshot(self.offscreenCapturePath(label, '.png', stamped=False), windowId=targetId)
                        self.emit(f'[PromptDebugger] Offscreen action {index}  kind={kind}  ok={ok}  file={saved}', self.logPath)
                    except Exception as captureError:
                        captureException(None, source='start.py', context='except@10710')
                        print(f"[WARN:swallowed-exception] start.py:9323 {type(captureError).__name__}: {captureError}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                        self.emit(f'[PromptDebugger] Offscreen action capture failed  kind={kind}  error={type(captureError).__name__}: {captureError}', self.exceptionPath)
            except Exception as error:
                captureException(None, source='start.py', context='except@10713')
                print(f"[WARN:swallowed-exception] start.py:9325 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                self.emit(f'[PromptDebugger] Offscreen action sequence failed: {type(error).__name__}: {error}', self.exceptionPath)
        thread = START_EXECUTION_LIFECYCLE.startThread('OffscreenActions', self.threadMain(runner, 'OffscreenActions'), daemon=True)
        self.offscreenActionThread = thread
        return thread

    def isDebugArtifactPath(self, path: str | Path | None) -> bool:
        try:
            target = Path(path or EMPTY_STRING)
        except Exception as error:
            captureException(None, source='start.py', context='except@10723')
            print(f"[WARN:swallowed-exception] start.py:9334 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return False
        name = str(target.name or EMPTY_STRING).strip().lower()
        if not name:
            return False
        if name in DEBUG_ARTIFACT_FILE_NAMES:
            return True
        return bool(target.parent == SCREENSHOTS_DIR.resolve() and name.startswith('offscreen_') and name.endswith('.png'))

    def debugArtifactWritesAllowed(self) -> bool:
        return bool(getattr(self, 'enabled', False))

    def canWritePath(self, path: str | Path | None) -> bool:
        if not path:
            return False
        if self.isDebugArtifactPath(path):
            return self.debugArtifactWritesAllowed()
        return True

    def write(self, path: str | Path, text: str):
        if not self.canWritePath(path):
            return
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = str(text or EMPTY_STRING)
        if payload and not payload.endswith('\n'):
            payload += '\n'
        with File.tracedOpen(target, 'a', encoding='utf-8', errors='replace') as handle:
            handle.write(payload)


    def readFile(self, path: str | Path, default: str = EMPTY_STRING) -> str:
        try:
            target = Path(path or EMPTY_STRING)
        except Exception as error:
            captureException(None, source='start.py', context='except@10758')
            print(f"[WARN:swallowed-exception] start.py:9368 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return str(default or EMPTY_STRING)
        try:
            if not target.exists():
                return str(default or EMPTY_STRING)
            return File.readText(target, encoding='utf-8', errors='replace')
        except Exception as error:
            captureException(None, source='start.py', context='except@10765')
            print(f"[WARN:swallowed-exception] start.py:9374 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return str(default or EMPTY_STRING)


    def psColor(self, text: str, color_code: str = PS_WHITE, bold: bool = False, italic: bool = False) -> str:
        prefix = EMPTY_STRING
        if bold:
            prefix += PS_BOLD
        if italic:
            prefix += PS_ITALIC
        prefix += str(color_code or EMPTY_STRING)
        return prefix + str(text or EMPTY_STRING) + PS_RESET

    def safeTypeName(self, value: Any) -> str:
        try:
            value_type = type(value)
            module_name = str(getattr(value_type, '__module__', EMPTY_STRING) or EMPTY_STRING)
            qual_name = str(getattr(value_type, '__qualname__', getattr(value_type, '__name__', value_type)) or value_type)
            if module_name and module_name not in {'builtins', '__builtin__'}:
                return f'{module_name}.{qual_name}'
            return str(qual_name)
        except Exception as error:
            captureException(None, source='start.py', context='except@10787')
            print(f"[WARN:swallowed-exception] start.py:9395 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return 'unknown'

    def shortTypeName(self, type_path: str) -> str:
        text = str(type_path or EMPTY_STRING).strip()
        if not text:
            return 'unknown'
        text = text.replace("'", EMPTY_STRING)
        if '.' in text:
            text = text.split('.')[-1]
        return text or 'unknown'

    def safePreview(self, value: Any, max_length: int = 320) -> str:
        try:
            text = repr(value)
        except BaseException as outer_error:
            captureException(None, source='start.py', context='except@10803')
            print(f"[WARN:swallowed-exception] start.py:9410 {type(outer_error).__name__}: {outer_error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            try:
                detail = str(outer_error)
            except Exception as detail_error:
                captureException(None, source='start.py', context='except@10807')
                print(f"[WARN:swallowed-exception] start.py:9413 {type(detail_error).__name__}: {detail_error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                try:
                    detail = repr(getattr(outer_error, 'args', ()))
                except Exception as args_error:
                    captureException(None, source='start.py', context='except@10811')
                    print(f"[WARN:swallowed-exception] start.py:9416 {type(args_error).__name__}: {args_error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    detail = 'unprintable error'
            text = f'<repr failed: {type(outer_error).__name__}: {detail}>'
        text = str(text or EMPTY_STRING).replace('\r\n', '\n').replace('\r', '\n')
        if len(text) > int(max_length or 320):
            return text[:max(0, int(max_length or 320) - 1)] + '…'
        return text

    def safeDescribeValue(self, value: Any) -> dict[str, Any]:
        type_path = self.safeTypeName(value)
        row = {
            'type': type_path,
            'type_path': type_path,
            'short_type': self.shortTypeName(type_path),
            'id': int(id(value)),
        }
        row['repr'] = self.safePreview(value)
        try:
            row['truthy'] = bool(value)
        except Exception as error:
            captureException(None, source='start.py', context='except@10831')
            print(f"[WARN:swallowed-exception] start.py:9435 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            row['truthy_error'] = f'{type(error).__name__}: {error}'
        if isinstance(value, (str, bytes, bytearray, list, tuple, dict, set, frozenset, range)):
            try:
                row['length'] = len(value)
            except Exception:
                captureException(None, source='start.py', context='except@10837')
                row['length_error'] = 'len-failed'
        try:
            if hasattr(value, '__dict__'):
                dict_value = getattr(value, '__dict__', None)
                if isinstance(dict_value, dict):
                    keys = sorted(str(key) for key in dict_value.keys())
                    row['attr_count'] = len(keys)
                    row['attr_keys'] = keys
        except Exception as error:
            captureException(None, source='start.py', context='except@10846')
            print(f"[WARN:swallowed-exception] start.py:9448 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            row['attr_error'] = f'{type(error).__name__}: {error}'
        try:
            module_name = str(getattr(type(value), '__module__', EMPTY_STRING) or EMPTY_STRING)
            if module_name:
                row['module'] = module_name
        except Exception as error:
            captureException(None, source='start.py', context='except@10853')
            print(f"[WARN:swallowed-exception] start.py:9454 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        try:
            qual_name = str(getattr(type(value), '__qualname__', getattr(type(value), '__name__', EMPTY_STRING)) or EMPTY_STRING)
            if qual_name:
                row['qualname'] = qual_name
        except Exception as error:
            captureException(None, source='start.py', context='except@10860')
            print(f"[WARN:swallowed-exception] start.py:9460 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        return row

    def describeNamespace(self, namespace: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        try:
            items = list(dict(namespace or {}).items())
        except Exception as error:
            captureException(None, source='start.py', context='except@10869')
            print(f"[WARN:swallowed-exception] start.py:9468 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            try:
                items = list(cast(Any, namespace).items())
            except Exception as error:
                captureException(None, source='start.py', context='except@10873')
                print(f"[WARN:swallowed-exception] start.py:9471 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                items = []
        for key, value in sorted(items, key=lambda item: str(item[0])):
            name = str(key)
            rows[name] = self.safeDescribeValue(value)
        return rows

    def collectParentThreadFrameVariables(self) -> list[dict[str, Any]]:
        frame_map = dict(sys._current_frames())
        thread_map = {int(getattr(thread, 'ident', 0) or 0): thread for thread in threading.enumerate() if int(getattr(thread, 'ident', 0) or 0)}
        main_ident = int(getattr(threading.main_thread(), 'ident', 0) or 0)
        rows = []
        for ident, frame in sorted(frame_map.items(), key=lambda item: (0 if int(item[0] or 0) == main_ident else 1, str(getattr(thread_map.get(int(item[0] or 0)), 'name', EMPTY_STRING) or EMPTY_STRING), int(item[0] or 0))):
            thread = thread_map.get(int(ident or 0))
            thread_row = {
                'name': str(getattr(thread, 'name', EMPTY_STRING) or f'Thread-{ident}'),
                'ident': int(ident or 0),
                'daemon': bool(getattr(thread, 'daemon', False)),
                'frame_count': 0,
                'frames': [],
            }
            depth = 0
            current = frame
            while current is not None:
                code = getattr(current, 'f_code', None)
                file_name = str(getattr(code, 'co_filename', EMPTY_STRING) or EMPTY_STRING)
                function_name = str(getattr(code, 'co_name', EMPTY_STRING) or EMPTY_STRING)
                lineno = int(getattr(current, 'f_lineno', 0) or 0)
                frame_row = {
                    'depth': depth,
                    'filename': file_name,
                    'function': function_name,
                    'lineno': lineno,
                    'locals': self.describeNamespace(getattr(current, 'f_locals', {})),
                }
                thread_row['frames'].append(frame_row)
                thread_row['frame_count'] += 1
                current = getattr(current, 'f_back', None)
                depth += 1
            rows.append(thread_row)
        return rows

    def parseJsonObject(self, text: str):
        try:
            return json.loads(str(text or EMPTY_STRING))
        except Exception as error:
            captureException(None, source='start.py', context='except@10919')
            print(f"[WARN:swallowed-exception] start.py:9516 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return None

    def normalizeAstFilePath(self, file_path: str | Path) -> str:
        try:
            return str(Path(file_path).resolve())
        except Exception as error:
            captureException(None, source='start.py', context='except@10926')
            print(f"[WARN:swallowed-exception] start.py:9522 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return str(file_path or EMPTY_STRING)

    def getAstIndex(self, file_path: str | Path):
        normalized = self.normalizeAstFilePath(file_path)
        if not normalized or not normalized.lower().endswith('.py'):
            return None
        if normalized in self.astIndexCache:
            return self.astIndexCache.get(normalized)
        if normalized in self.astIndexErrorCache:
            return None
        try:
            source = File.readText(Path(normalized), encoding='utf-8', errors='replace')
            tree = ast.parse(source, filename=normalized)
            index = FileAstScopeIndex(normalized)
            index.visit(tree)
            self.astIndexCache[normalized] = index
            return index
        except Exception as error:
            captureException(None, source='start.py', context='except@10945')
            print(f"[WARN:swallowed-exception] start.py:9540 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            self.astIndexErrorCache[normalized] = f'{type(error).__name__}: {error}'
            return None

    def baseName(self, file_path: str | Path) -> str:
        try:
            return Path(file_path).name
        except Exception as error:
            captureException(None, source='start.py', context='except@10953')
            print(f"[WARN:swallowed-exception] start.py:9547 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return str(file_path or EMPTY_STRING)

    def inferClassName(self, type_name: str) -> str:
        text = str(type_name or EMPTY_STRING).strip()
        if not text:
            return EMPTY_STRING
        text = text.replace("'", EMPTY_STRING)
        return text.split('.')[-1]

    def formatOriginText(self, meta: dict[str, Any] | None, fallback_file: str = EMPTY_STRING, fallback_method: str = EMPTY_STRING) -> str:
        if not meta:
            file_name = self.baseName(fallback_file) if fallback_file else 'unknown'
            if fallback_method:
                return f'{file_name} :: {fallback_method}()'
            return f'{file_name} :: unknown'
        file_name = self.baseName(str(meta.get('file', fallback_file) or fallback_file or 'unknown'))
        class_name = str(meta.get('class_name', EMPTY_STRING) or EMPTY_STRING)
        function_name = str(meta.get('function_name', fallback_method) or fallback_method or EMPTY_STRING)
        line_no = int(meta.get('line', 0) or 0)
        parts = [file_name, '::']
        if class_name:
            parts.append(class_name)
        if function_name:
            if class_name:
                parts[-1] = parts[-1] + '.' + function_name + '()'
            else:
                parts.append(function_name + '()')
        if not class_name and not function_name:
            parts.append('module')
        if line_no > 0:
            parts.append(f'line {line_no}')
        return ' '.join(part for part in parts if part)

    def resolveFrameVariableOrigin(self, frame_file: str, function_name: str, lineno: int, variable_name: str) -> dict[str, Any] | None:
        index = self.getAstIndex(frame_file)
        if index is None:
            return None
        scope = index.findScopeForFrame(function_name, lineno)
        if scope is None:
            return index.moduleGlobals.get(str(variable_name or EMPTY_STRING))
        scope_key = (str(scope.get('class_name', EMPTY_STRING) or EMPTY_STRING), str(scope.get('function_name', EMPTY_STRING) or EMPTY_STRING))
        local_meta = dict(index.localsByScope.get(scope_key, {}).get(str(variable_name or EMPTY_STRING), {}) or {})
        if local_meta:
            return local_meta
        return {
            'file': str(scope.get('file', frame_file) or frame_file),
            'class_name': str(scope.get('class_name', EMPTY_STRING) or EMPTY_STRING),
            'function_name': str(scope.get('function_name', function_name) or function_name),
            'line': int(scope.get('line', lineno) or lineno),
            'kind': 'scope',
        }

    def resolveGlobalVariableOrigin(self, file_path: str, variable_name: str) -> dict[str, Any] | None:
        index = self.getAstIndex(file_path)
        if index is None:
            return None
        name = str(variable_name or EMPTY_STRING)
        return dict(index.moduleGlobals.get(name, {}) or index.classDefinitions.get(name, {}) or {}) or None

    def resolveAttributeOrigin(self, file_path: str, class_name: str, attribute_name: str) -> dict[str, Any] | None:
        index = self.getAstIndex(file_path)
        if index is None:
            return None
        bucket = dict(index.attrsByClass.get(str(class_name or EMPTY_STRING), {}) or {})
        hit = dict(bucket.get(str(attribute_name or EMPTY_STRING), {}) or {})
        if hit:
            return hit
        name = str(attribute_name or EMPTY_STRING)
        candidates = []
        for candidate_class, attrs in index.attrsByClass.items():
            if name in attrs:
                meta = dict(attrs.get(name, {}) or {})
                meta.setdefault('class_name', candidate_class)
                candidates.append(meta)
        if len(candidates) == 1:
            return candidates[0]
        return None

    def formatScalarField(self, label: str, value: Any, color_code: str = PS_GREEN) -> str:
        return f"  {self.psColor(label + ':', PS_BLUE, bold=True)} {self.psColor(safeRepr(value, 240), color_code)}"

    def objectScopeName(self, origin: dict[str, Any] | None = None, fallback_file: str = EMPTY_STRING, fallback_method: str = EMPTY_STRING) -> str:
        meta = dict(origin or {})
        class_name = str(meta.get('class_name', EMPTY_STRING) or EMPTY_STRING).strip()
        if class_name:
            return class_name
        fallback_method = str(fallback_method or EMPTY_STRING).strip()
        if fallback_method and fallback_method not in {'module', '<module>'}:
            return fallback_method
        try:
            stem = Path(str(meta.get('file', fallback_file) or fallback_file or EMPTY_STRING)).stem
            if stem:
                return stem
        except Exception as error:
            captureException(None, source='start.py', context='except@11048')
            print(f"[WARN:swallowed-exception] start.py:9641 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        return 'module'

    def formatGroundedVariablePathParts(self, name: str, payload: dict[str, Any] | None, origin: dict[str, Any] | None = None, fallback_file: str = EMPTY_STRING, fallback_method: str = EMPTY_STRING) -> dict[str, str]:
        row = dict(payload or {})
        type_path = str(row.get('type_path', row.get('type', row.get('kind', 'unknown'))) or 'unknown')
        short_type = str(row.get('short_type', EMPTY_STRING) or EMPTY_STRING).strip() or self.shortTypeName(type_path)
        scope_name = self.objectScopeName(origin=origin, fallback_file=fallback_file, fallback_method=fallback_method)
        variable_name = str(name or EMPTY_STRING).strip() or 'value'
        if variable_name in {'self', 'cls'} and short_type == scope_name:
            full = f'{scope_name}::{variable_name}'
        else:
            full = f'{scope_name}::{short_type} {variable_name}' if scope_name else f'{short_type} {variable_name}'
        return {
            'scope': scope_name,
            'type_short': short_type,
            'name': variable_name,
            'full': full,
            'type_path': type_path,
        }

    def variableSortKey(self, name: str, payload: dict[str, Any] | None, origin: dict[str, Any] | None = None, fallback_file: str = EMPTY_STRING, fallback_method: str = EMPTY_STRING) -> tuple[str, str]:
        parts = self.formatGroundedVariablePathParts(name, payload, origin=origin, fallback_file=fallback_file, fallback_method=fallback_method)
        return (str(parts.get('full', EMPTY_STRING)).lower(), str(name or EMPTY_STRING).lower())

    def formatVariableValueLines(self, repr_text: str, indent: str = '  ') -> list[str]:
        text = str(repr_text or EMPTY_STRING)
        if not text:
            return []
        rendered_lines = text.splitlines() or [text]
        rows = []
        if len(rendered_lines) == 1:
            rows.append(indent + self.psColor('value', PS_GREEN, bold=True) + self.psColor(' = ', PS_BLUE, bold=True) + self.psColor(rendered_lines[0], PS_WHITE))
            return rows
        rows.append(indent + self.psColor('value', PS_GREEN, bold=True) + self.psColor(' =', PS_BLUE, bold=True))
        for line in rendered_lines:
            rows.append(indent + '  ' + self.psColor(line, PS_WHITE))
        return rows

    def formatVariableEntry(self, name: str, payload: dict[str, Any] | None, origin: dict[str, Any] | None = None, fallback_file: str = EMPTY_STRING, fallback_method: str = EMPTY_STRING, indent: str = '  ') -> list[str]:
        row = dict(payload or {})
        var_id = row.get('id', None)
        repr_text = str(row.get('repr', row.get('message', EMPTY_STRING)) or EMPTY_STRING)
        length_text = row.get('length', None)
        truthy_value = row.get('truthy', None)
        attr_count = row.get('attr_count', None)
        attr_keys = list(row.get('attr_keys', []) or [])
        parts = self.formatGroundedVariablePathParts(name, row, origin=origin, fallback_file=fallback_file, fallback_method=fallback_method)
        full_path = str(parts.get('full', name) or name)
        type_path = str(parts.get('type_path', row.get('type', 'unknown')) or row.get('type', 'unknown') or 'unknown')
        type_short = str(parts.get('type_short', self.shortTypeName(type_path)) or self.shortTypeName(type_path))
        scope_name = str(parts.get('scope', EMPTY_STRING) or EMPTY_STRING)
        variable_name = str(parts.get('name', name) or name)
        lines = []
        header = indent
        if scope_name:
            header += self.psColor(scope_name, PS_MAGENTA, bold=True)
            header += self.psColor('::', PS_DIM)
        if variable_name in {'self', 'cls'} and scope_name and type_short == scope_name:
            header += self.psColor(variable_name, PS_CYAN, bold=True)
        else:
            header += self.psColor(type_short, PS_YELLOW, bold=True, italic=True)
            header += self.psColor(' ', PS_DIM)
            header += self.psColor(variable_name, PS_CYAN, bold=True)
        if var_id is not None:
            header += '  ' + self.psColor(f'id={var_id}', PS_DIM)
        lines.append(header)
        lines.append(indent + '  ' + self.psColor('path', PS_BLUE, bold=True) + self.psColor(' = ', PS_DIM) + self.psColor(full_path, PS_WHITE))
        lines.append(indent + '  ' + self.psColor('type', PS_BLUE, bold=True) + self.psColor(' = ', PS_DIM) + self.psColor(type_path, PS_YELLOW, bold=True, italic=True))
        lines.extend(self.formatVariableValueLines(repr_text, indent=indent + '  '))
        detail_parts = []
        if length_text is not None:
            detail_parts.append(f'len={length_text}')
        if truthy_value is not None:
            detail_parts.append(f'truthy={truthy_value}')
        if attr_count is not None:
            detail_parts.append(f'attrs={attr_count}')
        if detail_parts:
            lines.append(indent + '  ' + self.psColor('meta', PS_BLUE, bold=True) + self.psColor(' = ', PS_DIM) + self.psColor(' | '.join(detail_parts), PS_DIM))
        if attr_keys:
            lines.append(indent + '  ' + self.psColor('attr keys', PS_BLUE, bold=True) + self.psColor(' = ', PS_DIM) + self.psColor(', '.join(attr_keys[:24]), PS_DIM))
        origin_text = self.formatOriginText(origin, fallback_file=fallback_file, fallback_method=fallback_method)
        lines.append(indent + '  ' + self.psColor('defined at', PS_BLUE, bold=True) + self.psColor(' = ', PS_DIM) + self.psColor(origin_text, PS_WHITE))
        return lines

    def renderNamespaceSection(self, title: str, namespace: dict[str, Any] | None, resolver, fallback_file: str = EMPTY_STRING, fallback_method: str = EMPTY_STRING) -> list[str]:
        rows = [self.psColor(title, PS_BLUE, bold=True)]
        namespace_map = dict(namespace or {})
        if not namespace_map:
            rows.append('  ' + self.psColor('(empty)', PS_DIM))
            return rows
        entries = []
        for name, raw_payload in list(namespace_map.items()):
            payload = dict(raw_payload or {})
            origin = None
            try:
                origin = resolver(str(name), payload)
            except Exception as error:
                captureException(None, source='start.py', context='except@11147')
                print(f"[WARN:swallowed-exception] start.py:9739 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                origin = None
            entries.append((self.variableSortKey(str(name), payload, origin=origin, fallback_file=fallback_file, fallback_method=fallback_method), str(name), payload, origin))
        for _, name, payload, origin in sorted(entries, key=lambda item: item[0]):
            rows.extend(self.formatVariableEntry(str(name), payload, origin=origin, fallback_file=fallback_file, fallback_method=fallback_method))
        return rows

    def renderThreadFramesSection(self, frames: list[dict[str, Any]] | None) -> list[str]:
        rows = [self.psColor('Thread Frames', PS_BLUE, bold=True)]
        frame_rows = list(frames or [])
        if not frame_rows:
            rows.append('  ' + self.psColor('(none)', PS_DIM))
            return rows
        for thread_info in frame_rows:
            thread_name = str(thread_info.get('name', EMPTY_STRING) or EMPTY_STRING)
            ident = int(thread_info.get('ident', 0) or 0)
            daemon = bool(thread_info.get('daemon', False))
            frame_count = int(thread_info.get('frame_count', 0) or 0)
            thread_header = f'{thread_name or "Thread"} ident={ident} frames={frame_count}'
            if daemon:
                thread_header += ' daemon=True'
            rows.append('  ' + self.psColor(thread_header, PS_MAGENTA, bold=True))
            for frame in list(thread_info.get('frames', []) or []):
                function_name = str(frame.get('function', EMPTY_STRING) or EMPTY_STRING)
                file_name = str(frame.get('filename', EMPTY_STRING) or EMPTY_STRING)
                lineno = int(frame.get('lineno', 0) or 0)
                depth = int(frame.get('depth', 0) or 0)
                scope_info = self.resolveFrameVariableOrigin(file_name, function_name, lineno, EMPTY_STRING)
                scope_text = self.formatOriginText(scope_info, fallback_file=file_name, fallback_method=function_name)
                frame_header = f'{scope_text} @ runtime line {lineno}'
                if depth > 0:
                    frame_header += f'  depth={depth}'
                rows.append('    ' + self.psColor(frame_header, PS_WHITE, bold=True))
                locals_map = dict(frame.get('locals', {}) or {})
                rows.append('      ' + self.psColor(f'locals={len(locals_map)}', PS_DIM))
                if not locals_map:
                    rows.append('      ' + self.psColor('(no locals)', PS_DIM))
                    rows.append(EMPTY_STRING)
                    continue
                entries = []
                for local_name, local_payload in list(locals_map.items()):
                    payload = dict(local_payload or {})
                    origin = self.resolveFrameVariableOrigin(file_name, function_name, lineno, str(local_name))
                    entries.append((self.variableSortKey(str(local_name), payload, origin=origin, fallback_file=file_name, fallback_method=function_name), str(local_name), payload, origin))
                for _, local_name, payload, origin in sorted(entries, key=lambda item: item[0]):
                    rows.extend(self.formatVariableEntry(str(local_name), payload, origin=origin, fallback_file=file_name, fallback_method=function_name, indent='      '))
                rows.append(EMPTY_STRING)
        return rows

    def renderGcInventorySection(self, gc_payload: dict[str, Any] | None) -> list[str]:
        rows = [self.psColor('GC Inventory', PS_BLUE, bold=True)]
        payload = dict(gc_payload or {})
        rows.append(self.formatScalarField('Tracked objects', payload.get('tracked_object_total', 0), PS_WHITE))
        type_counts = dict(payload.get('type_counts', {}) or {})
        if not type_counts:
            rows.append('  ' + self.psColor('(no gc inventory)', PS_DIM))
            return rows
        rows.append('  ' + self.psColor('Top tracked types', PS_MAGENTA, bold=True))
        for index, (type_name, count) in enumerate(sorted(type_counts.items(), key=lambda item: (-int(item[1]), str(item[0])))):
            if index >= 40:
                rows.append('    ' + self.psColor(f'... {len(type_counts) - 40} more types omitted', PS_DIM))
                break
            rows.append('    ' + self.psColor(str(type_name), PS_CYAN) + '  ' + self.psColor(str(count), PS_YELLOW))
        type_samples = dict(payload.get('type_samples', {}) or {})
        for type_name, samples in list(type_samples.items())[:12]:
            sample_rows = list(samples or [])
            if not sample_rows:
                continue
            rows.append('  ' + self.psColor(f'Samples for {type_name}', PS_MAGENTA, bold=True))
            for sample in sample_rows[:3]:
                rows.append('    ' + self.psColor(str(sample), PS_GREEN))
        return rows

    def renderProcessSnapshotSection(self, process_payload: dict[str, Any] | None) -> list[str]:
        rows = [self.psColor('Process Snapshot', PS_BLUE, bold=True)]
        payload = dict(process_payload or {})
        rows.append(self.formatScalarField('Reason', payload.get('reason', EMPTY_STRING), PS_WHITE))
        thread_rows = list(payload.get('threads', []) or [])
        if not thread_rows:
            rows.append('  ' + self.psColor('(no process thread snapshot)', PS_DIM))
            return rows
        for thread in thread_rows:
            title = f"{thread.get('name', 'Thread')} ident={thread.get('ident', 0)} alive={thread.get('alive', False)} daemon={thread.get('daemon', False)}"
            rows.append('  ' + self.psColor(title, PS_CYAN))
        return rows

    def readLatestChildVarDumpRow(self):
        child_pid = 0
        try:
            child_pid = int(getattr(self, 'childPid', 0) or 0)
        except Exception as error:
            captureException(None, source='start.py', context='except@11238')
            print(f"[WARN:swallowed-exception] start.py:9829 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            child_pid = 0
        def reader(_session):
            query = (
                DebuggerHeartbeatRecord
                .select()
                .where(
                    (DebuggerHeartbeatRecord.source.in_(childDebuggerSourceNames()))
                    & (DebuggerHeartbeatRecord.varDump.is_null(False))
                    & (DebuggerHeartbeatRecord.varDump != EMPTY_STRING)
                )
            )
            if child_pid > 0:
                query = query.where(DebuggerHeartbeatRecord.pid == child_pid)
            return query.order_by(DebuggerHeartbeatRecord.id.desc()).first()
        return self.runDebuggerSqlite('readLatestChildVarDumpRow', reader, commit=False)

    def renderChildVarDumpFromHeartbeat(self) -> str:
        row = self.readLatestChildVarDumpRow()
        if row is None:
            return '[PromptDebugger] No child var dump found in heartbeat table.'
        var_payload = self.parseJsonObject(getattr(row, 'varDump', EMPTY_STRING))
        process_payload = self.parseJsonObject(getattr(row, 'processSnapshot', EMPTY_STRING))
        if not isinstance(var_payload, dict):
            return self.formatSnapshotRow(row)
        gtp_file = self.normalizeAstFilePath(GTP_PATH)
        debugger_class_name = 'RemoteDebuggerProxy'
        application_payload = dict(var_payload.get('application', {}) or {}) if isinstance(var_payload.get('application', {}), dict) else {}
        application_class_name = self.inferClassName(str(application_payload.get('class', EMPTY_STRING) or EMPTY_STRING))
        lines = [
            self.psColor('=' * 78, PS_BLUE, bold=True),
            self.psColor('Child Variable Dump from Heartbeat Table', PS_BLUE, bold=True),
            self.psColor('=' * 78, PS_BLUE, bold=True),
            self.formatScalarField('Created', getattr(row, 'created', EMPTY_STRING), PS_WHITE),
            self.formatScalarField('Reason', getattr(row, 'reason', EMPTY_STRING), PS_WHITE),
            self.formatScalarField('Caller', getattr(row, 'caller', EMPTY_STRING), PS_WHITE),
            self.formatScalarField('Phase', getattr(row, 'phase', EMPTY_STRING), PS_WHITE),
            self.formatScalarField('PID', getattr(row, 'pid', 0), PS_WHITE),
            self.formatScalarField('Dump timestamp', var_payload.get('timestamp', EMPTY_STRING), PS_WHITE),
            self.formatScalarField('Dump thread', var_payload.get('thread', EMPTY_STRING), PS_WHITE),
            EMPTY_STRING,
        ]
        lines.extend(self.renderNamespaceSection(
            'Debugger Attributes',
            var_payload.get('debugger', {}),
            resolver=lambda name, payload: self.resolveAttributeOrigin(gtp_file, debugger_class_name, name),
            fallback_file=gtp_file,
            fallback_method='RemoteDebuggerProxy',
        ))
        lines.append(EMPTY_STRING)
        lines.extend(self.renderNamespaceSection(
            'Application Attributes',
            application_payload.get('attributes', {}),
            resolver=lambda name, payload: self.resolveAttributeOrigin(gtp_file, application_class_name, name),
            fallback_file=gtp_file,
            fallback_method=application_class_name or 'TrioDesktop',
        ))
        lines.append(EMPTY_STRING)
        lines.extend(self.renderNamespaceSection(
            'Lifecycle Attributes',
            application_payload.get('lifecycle_attributes', {}),
            resolver=lambda name, payload: self.resolveAttributeOrigin(gtp_file, 'ApplicationLifecycleController', name) or self.resolveAttributeOrigin(gtp_file, EMPTY_STRING, name),
            fallback_file=gtp_file,
            fallback_method='ApplicationLifecycleController',
        ))
        lines.append(EMPTY_STRING)
        lines.extend(self.renderNamespaceSection(
            'GTP Globals',
            var_payload.get('gtp_globals', {}),
            resolver=lambda name, payload: self.resolveGlobalVariableOrigin(gtp_file, name),
            fallback_file=gtp_file,
            fallback_method='module',
        ))
        lines.append(EMPTY_STRING)
        lines.extend(self.renderThreadFramesSection(var_payload.get('thread_frames', [])))
        lines.append(EMPTY_STRING)
        lines.extend(self.renderGcInventorySection(var_payload.get('gc_inventory', {})))
        lines.append(EMPTY_STRING)
        lines.extend(self.renderProcessSnapshotSection(process_payload if isinstance(process_payload, dict) else {}))
        lines.append(self.psColor('=' * 78, PS_BLUE, bold=True))
        return '\n'.join(str(line) for line in lines)

    def installParentStackTracing(self):
        if self.parentStackTracingInstalled:
            return True
        self.parentStackTracingInstalled = True
        try:
            target = Path(self.parentFaultPath)
            target.parent.mkdir(parents=True, exist_ok=True)
            self.parentFaultHandle = File.tracedOpen(target, 'a', encoding='utf-8', errors='replace')
            faulthandler.enable(self.parentFaultHandle, all_threads=True)
        except Exception as error:
            captureException(None, source='start.py', context='except@11330')
            print(f"[WARN:swallowed-exception] start.py:9920 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            try:
                faulthandler.enable(all_threads=True)
            except Exception as error:
                captureException(None, source='start.py', context='except@11334')
                print(f"[WARN:swallowed-exception] start.py:9923 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
        previous_sys_hook = getattr(sys, 'excepthook', None)
        previous_thread_hook = getattr(threading, 'excepthook', None)

        def report_parent_exception(label: str, exc_type, exc_value, exc_tb):
            try:
                traceback_text = EMPTY_STRING.join(traceback.format_exception(exc_type, exc_value, exc_tb)).rstrip()
            except Exception as error:
                captureException(None, source='start.py', context='except@11343')
                print(f"[WARN:swallowed-exception] start.py:9931 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                traceback_text = f'{getattr(exc_type, "__name__", "Exception")}: {exc_value}'
            info = {
                'type_name': getattr(exc_type, '__name__', 'Exception'),
                'message': str(exc_value or EMPTY_STRING),
                'thread': label,
                'timestamp': time.time(),
                'traceback_text': traceback_text,
                'pid': os.getpid(),
                'where': 'start.py',
            }
            try:
                self.emit(f'[PromptDebugger:parent-exception:{label}]\n{traceback_text}', self.exceptionPath)
            except Exception as error:
                captureException(None, source='start.py', context='except@11357')
                print(f"[WARN:swallowed-exception] start.py:9944 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
            try:
                self.capturePreConsoleSnapshot(info)
            except Exception as error:
                captureException(None, source='start.py', context='except@11362')
                print(f"[WARN:swallowed-exception] start.py:9948 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
            try:
                self.writeHeartbeatRowToDb(source='start.py', event_kind='parent-exception', reason=info['message'], caller=label, phase='PARENT', pid=os.getpid(), stack_trace=self.renderParentFrames(), var_dump=self.renderParentLocals(), process_snapshot=traceback_text, timestamp=time.time())
            except Exception as error:
                captureException(None, source='start.py', context='except@11367')
                print(f"[WARN:swallowed-exception] start.py:9952 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass

        def parent_sys_hook(exc_type, exc_value, exc_tb):
            report_parent_exception('main-thread', exc_type, exc_value, exc_tb)
            if callable(previous_sys_hook) and previous_sys_hook not in {parent_sys_hook, sys.__excepthook__}:
                return previous_sys_hook(exc_type, exc_value, exc_tb)
            return sys.__excepthook__(exc_type, exc_value, exc_tb)

        sys.excepthook = parent_sys_hook  # monkeypatch-ok: debugger-owned parent exception reporting hook.

        if hasattr(threading, 'excepthook'):
            def parent_thread_hook(args):
                thread_name = getattr(getattr(args, 'thread', None), 'name', 'thread')
                report_parent_exception(thread_name, args.exc_type, args.exc_value, args.exc_traceback)
                if callable(previous_thread_hook) and previous_thread_hook is not parent_thread_hook:
                    return previous_thread_hook(args)
                return None
            threading.excepthook = parent_thread_hook  # monkeypatch-ok: debugger-owned thread exception reporting hook.
        return True

    def configureTraceVerbose(self, argv=None):
        self.traceVerbose = bool(traceVerboseRequested(argv))
        return self.traceVerbose

    def tryFormatTraceVerboseLine(self, text: str) -> str:
        content = str(text or EMPTY_STRING).strip()
        if not self.traceVerbose or not content:
            return EMPTY_STRING
        return content


class ShellRpcServer:
    def __init__(self, owner):
        self.owner = owner  # noqa: nonconform
        self.token = str(getattr(owner, 'relayToken', EMPTY_STRING) or EMPTY_STRING)
        self.loop = asyncio.new_event_loop()  # noqa: nonconform
        self.loopThread = START_EXECUTION_LIFECYCLE.startThread('TrioShellRpcLoop', self.loopMain, daemon=True)  # noqa: nonconform
        self.server = None
        self.thread = None
        self.port = 0
        self.startServer()

    def loopMain(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    async def runCommandAsync(self, command: str, timeout: int, sendEvent):
        collected = []
        stderr_lines = []
        creation_kwargs = {}
        if os.name == 'nt' and not str(os.environ.get('PROMPT_ALLOW_CHILD_WINDOWS', '') or '').strip():
            try:
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= getattr(subprocess, 'STARTF_USESHOWWINDOW', 1)
                startupinfo.wShowWindow = 0
                creation_kwargs['startupinfo'] = startupinfo
                creation_kwargs['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
                DebugLog.writeLine('SPAWN:ASYNC:BEGIN callsite=start.py:ShellRpcServer.runCommandAsync command=powershell -NoProfile -NonInteractive -Command <shell-rpc>', level='PROCESS', source='start.py', stream='stdout')
            except Exception as error:
                captureException(error, source='start.py', context='shell-rpc-hidden-process-setup', handled=True)
        proc = await asyncio.create_subprocess_exec(
            'powershell', '-NoProfile', '-NonInteractive', '-Command', str(command or EMPTY_STRING),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **creation_kwargs,
        )

        async def pump(reader, kind: str, sink):
            while True:  # noqa: badcode reviewed detector-style finding
                line = await reader.readline()
                if not line:
                    break
                text = line.decode('utf-8', 'replace').rstrip('\r\n')
                if not text:
                    continue
                sink.append(text)
                sendEvent(kind, text)

        stdout_task = self.loop.create_task(pump(proc.stdout, 'stdout', collected))
        stderr_task = self.loop.create_task(pump(proc.stderr, 'stderr', stderr_lines))
        exit_code = 0
        try:
            await asyncio.wait_for(proc.wait(), timeout=max(5, int(timeout or 30)))  # block-ok async-process-wait-is-wrapped-by-wait_for
            exit_code = int(proc.returncode or 0)
        except asyncio.TimeoutError:
            captureException(None, source='start.py', context='except@11441')
            try:
                proc.kill()
            except Exception as error:
                captureException(None, source='start.py', context='except@11444')
                print(f"[WARN:swallowed-exception] start.py:10028 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
            exit_code = -9
            timeout_text = f'[killed: exceeded {max(5, int(timeout or 30))}s timeout]'
            collected.append(timeout_text)
            sendEvent('stdout', timeout_text)
        await stdout_task
        await stderr_task
        result = '\n'.join(collected).strip()
        if stderr_lines:
            result = (result + '\n[stderr]\n' if result else '[stderr]\n') + '\n'.join(stderr_lines)
        return exit_code, result or '(no output)'

    def executeSync(self, command: str, timeout: int, sendEvent):
        future = asyncio.run_coroutine_threadsafe(
            self.runCommandAsync(command, timeout, sendEvent),
            self.loop,
        )
        return future.result(timeout=max(8, int(timeout or 30)) + 5)

    def startServer(self):
        owner = self.owner
        rpc = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                lock = threading.RLock()

                def sendEvent(kind: str, data: str):
                    payload = {'event': str(kind or EMPTY_STRING), 'data': str(data or EMPTY_STRING)}
                    blob = (json.dumps(payload, ensure_ascii=False) + '\n').encode('utf-8', 'replace')
                    with lock:
                        try:
                            self.wfile.write(blob)
                            self.wfile.flush()
                        except Exception as error:
                            captureException(None, source='start.py', context='except@11480')
                            print(f"[WARN:swallowed-exception] start.py:10063 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                            pass

                try:
                    raw = self.rfile.readline()
                    if not raw:
                        return
                    payload = json.loads(raw.decode('utf-8', 'replace'))
                except Exception as error:
                    captureException(None, source='start.py', context='except@11489')
                    print(f"[WARN:swallowed-exception] start.py:10071 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    sendEvent('error', f'Invalid shell RPC payload: {error}')
                    return
                if str(payload.get('token') or EMPTY_STRING) != rpc.token:
                    sendEvent('error', 'Invalid shell RPC token.')
                    return
                command = str(payload.get('command') or EMPTY_STRING).strip()
                try:
                    timeout = int(payload.get('timeout') or 30)
                except Exception as error:
                    captureException(None, source='start.py', context='except@11499')
                    print(f"[WARN:swallowed-exception] start.py:10080 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    timeout = 30
                if not command:
                    sendEvent('error', 'No command provided.')
                    return
                try:
                    owner.touchProcessLoop('SHELL_RPC')
                except Exception as error:
                    captureException(None, source='start.py', context='except@11507')
                    print(f"[WARN:swallowed-exception] start.py:10087 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    pass
                try:
                    exit_code, result = rpc.executeSync(command, timeout, sendEvent)
                    with lock:
                        try:
                            self.wfile.write((json.dumps({'event': 'done', 'code': int(exit_code), 'result': str(result or EMPTY_STRING)}, ensure_ascii=False) + '\n').encode('utf-8', 'replace'))
                            self.wfile.flush()
                        except Exception as error:
                            captureException(None, source='start.py', context='except@11516')
                            print(f"[WARN:swallowed-exception] start.py:10095 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                            pass
                except Exception as error:
                    captureException(None, source='start.py', context='except@11519')
                    print(f"[WARN:swallowed-exception] start.py:10097 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    sendEvent('error', f'{type(error).__name__}: {error}')

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True

        self.server = Server(('127.0.0.1', 0), Handler)
        self.port = int(self.server.server_address[1] or 0)
        self.thread = START_EXECUTION_LIFECYCLE.startThread('TrioShellRpcServer', lambda: cast(Any, self.server).serve_forever(poll_interval=0.5), daemon=True)

    def close(self):
        try:
            if self.server is not None:
                self.server.shutdown()
                self.server.server_close()
        except Exception as error:
            captureException(None, source='start.py', context='except@11535')
            print(f"[WARN:swallowed-exception] start.py:10112 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        self.server = None
        try:
            if self.loop is not None:
                self.loop.call_soon_threadsafe(self.loop.stop)
        except Exception as error:
            captureException(None, source='start.py', context='except@11542')
            print(f"[WARN:swallowed-exception] start.py:10118 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        try:
            if self.loop is not None:
                if self.loop.is_running():
                    print('[PromptDebugger] Shell RPC loop close skipped because loop is still running; joining loop thread first', file=sys.stderr, flush=True)
                else:
                    self.loop.close()
        except Exception as error:
            captureException(None, source='start.py', context='except@11551')
            print(f"[WARN:swallowed-exception] start.py:10123 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        try:
            if self.thread is not None and self.thread.is_alive():
                self.thread.join(timeout=1.0)
        except Exception as error:
            captureException(None, source='start.py', context='except@11557')
            print(f"[WARN:swallowed-exception] start.py:10128 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        try:
            if self.loopThread is not None and self.loopThread.is_alive():
                self.loopThread.join(timeout=1.0)
        except Exception as error:
            captureException(None, source='start.py', context='except@11563')
            print(f"[WARN:swallowed-exception] start.py:10133 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass


RUNTIME_SETTINGS_PATH = BASE_DIR / 'trio_runtime_settings.json'



def collectRuntimeDbPayload(payload: dict | None) -> dict[str, str]:
    payload = dict(payload or {})
    settingsPayload = dict(payload.get('settings', {}) or {})
    nested = dict(payload.get('db', {}) or {})
    settingsDb = dict(settingsPayload.get('db', {}) or {}) if isinstance(settingsPayload.get('db', {}), dict) else {}
    dbPayload: dict[str, str] = {}
    for source_dict in (settingsDb, nested):
        for key, value in dict(source_dict or {}).items():
            if value not in (None, EMPTY_STRING):
                dbPayload[str(key)] = str(value)
    compatibility_values = {
        'host': str(payload.get('db_host', payload.get('dbHost', settingsPayload.get('db_host', settingsPayload.get('dbHost', dbPayload.get('host', EMPTY_STRING))))) or EMPTY_STRING).strip(),
        'port': str(payload.get('db_port', payload.get('dbPort', settingsPayload.get('db_port', settingsPayload.get('dbPort', dbPayload.get('port', EMPTY_STRING))))) or EMPTY_STRING).strip(),
        'user': str(payload.get('db_user', payload.get('dbUser', settingsPayload.get('db_user', settingsPayload.get('dbUser', dbPayload.get('user', EMPTY_STRING))))) or EMPTY_STRING).strip(),
        'password': str(payload.get('db_password', payload.get('dbPass', settingsPayload.get('db_password', settingsPayload.get('dbPass', dbPayload.get('password', EMPTY_STRING))))) or EMPTY_STRING),
        'database': str(payload.get('db_database', payload.get('db_name', payload.get('dbName', settingsPayload.get('db_database', settingsPayload.get('db_name', settingsPayload.get('dbName', dbPayload.get('database', EMPTY_STRING))))))) or EMPTY_STRING).strip(),
    }
    for key, value in compatibility_values.items():
        if value not in (None, EMPTY_STRING):
            dbPayload[key] = value
    return dbPayload


def seedRuntimeDbEnvFromFile():
    try:
        backend = str(os.environ.get('TRIO_DB_BACKEND', 'sqlite') or 'sqlite').strip().lower()
        if backend != 'mariadb':
            for key in ('TRIO_DB_HOST', 'TRIO_DB_PORT', 'TRIO_DB_USER', 'TRIO_DB_PASS', 'TRIO_DB_NAME'):
                os.environ.pop(key, None)
            return
        if not RUNTIME_SETTINGS_PATH.exists():
            return
        payload = json.loads(File.readText(RUNTIME_SETTINGS_PATH, encoding='utf-8', errors='replace') or '{}')
        dbPayload = collectRuntimeDbPayload(payload)
        envLookup = {
            'TRIO_DB_HOST': str(dbPayload.get('host', EMPTY_STRING) or EMPTY_STRING).strip(),
            'TRIO_DB_PORT': str(dbPayload.get('port', EMPTY_STRING) or EMPTY_STRING).strip(),
            'TRIO_DB_USER': str(dbPayload.get('user', EMPTY_STRING) or EMPTY_STRING).strip(),
            'TRIO_DB_PASS': str(dbPayload.get('password', EMPTY_STRING) or EMPTY_STRING),
            'TRIO_DB_NAME': str(dbPayload.get('database', EMPTY_STRING) or EMPTY_STRING).strip(),
        }
        for key, value in envLookup.items():
            if value not in (None, EMPTY_STRING):
                os.environ[key] = str(value)
        try:
            print(f"[TRACE:db-env-seed] host={envLookup.get('TRIO_DB_HOST',EMPTY_STRING)} port={envLookup.get('TRIO_DB_PORT',EMPTY_STRING)} user={envLookup.get('TRIO_DB_USER',EMPTY_STRING)} database={envLookup.get('TRIO_DB_NAME',EMPTY_STRING)} password_len={len(str(envLookup.get('TRIO_DB_PASS',EMPTY_STRING) or EMPTY_STRING))}", file=sys.stderr, flush=True)
        except Exception as error:
            captureException(None, source='start.py', context='except@11618')
            print(f"[WARN:swallowed-exception] start.py:10187 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
    except Exception as error:
        captureException(None, source='start.py', context='except@11621')
        try:
            print(f"[WARNING:db-env-seed] {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        except Exception as error:
            captureException(None, source='start.py', context='except@11624')
            print(f"[WARN:swallowed-exception] start.py:10192 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass

def debugEnabled(argv: list[str]) -> bool:
    return cliHasAnyFlag(argv, DEBUG_FLAGS)

def mariaEnabled(argv: list[str]) -> bool:
    return cliHasAnyFlag(argv, MARIA_FLAGS)

def seedDatabaseBackend(argv: list[str]) -> str:
    backend = 'mariadb' if mariaEnabled(argv) else 'sqlite'
    os.environ['TRIO_DB_BACKEND'] = backend
    if backend != 'mariadb':
        for key in ('TRIO_DB_HOST', 'TRIO_DB_PORT', 'TRIO_DB_USER', 'TRIO_DB_PASS', 'TRIO_DB_NAME'):
            os.environ.pop(key, None)
        appdata = os.environ.get('APPDATA') or str(Path.home())
        db_dir = Path(appdata) / 'TrioDesktop'
        try:
            db_dir.mkdir(parents=True, exist_ok=True)
        except Exception as error:
            captureException(None, source='start.py', context='except@11644')
            print(f"[WARN:swallowed-exception] start.py:10211 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        os.environ['TRIO_SQLITE_PATH'] = str(db_dir / 'triodesktop.db')
    return backend


def warnMissingCliValues(argv=None) -> None:
    languageState = readCliOption(argv, KNOWN_CHILD_FLAG_GROUPS.get('language', set()), takesValue=True, knownAliases=START_VALID_CLI_ALIASES)
    if bool(languageState.get('present')) and bool(languageState.get('missing_value')):
        token = str(languageState.get('token') or 'language')
        print(f'[WARNING:start.py.cli] Language arg requires a value: {token}', file=sys.stderr, flush=True)


class StartProcessRecord:
    """A start.py runtime unit owned by a lifecycle phase.

    This mirrors the Prompt app lifecycle contract: phases own processes;
    processes carry status, start time, TTL, callbacks, fault/error metadata,
    and a database row id when persistence is available.
    """

    def __init__(self, name: str, kind: str = 'phase-callback', handle=None, ttl: float = 0.0, pid: int | None = None, command: str = EMPTY_STRING, metadata=None):
        self.name = str(name or 'start-process')
        self.kind = str(kind or 'phase-callback')
        self.handle = handle
        self.ttl = float(ttl or 0.0)
        self.pid = int(pid or os.getpid())
        self.command = str(command or EMPTY_STRING)
        self.metadata = dict(metadata or {}) if isinstance(metadata, dict) else {}
        self.status = 'pending'
        self.startedAt = 0.0
        self.endedAt = 0.0
        self.exitCode = None
        self.errorType = EMPTY_STRING
        self.errorMessage = EMPTY_STRING
        self.tracebackText = EMPTY_STRING
        self.faultReason = EMPTY_STRING
        self.dbRowId = 0

    def start(self):
        self.status = 'running'
        self.startedAt = time.time()
        result = None
        if callable(self.handle):
            result = self.handle()
        elif hasattr(self.handle, 'start'):
            result = cast(Any, self.handle).start()
        result_pid = int(getattr(result, 'pid', 0) or getattr(self.handle, 'pid', 0) or 0)
        if result_pid > 0:
            self.pid = result_pid
        return result

    def complete(self, exit_code: int | None = None):
        self.status = 'complete'
        self.exitCode = exit_code
        self.endedAt = time.time()

    def error(self, message: str = EMPTY_STRING):
        self.status = 'errored'
        self.errorMessage = str(message or 'error')
        self.endedAt = time.time()

    def exception(self, exc: BaseException):
        self.status = 'errored'
        self.errorType = type(exc).__name__
        self.errorMessage = str(exc)
        self.tracebackText = traceback.format_exc()
        self.endedAt = time.time()

    def fault(self, reason: str):
        self.status = 'errored'
        self.faultReason = str(reason or 'fault')
        self.errorMessage = self.faultReason
        self.endedAt = time.time()

    def snapshot(self) -> dict[str, Any]:
        return {
            'name': self.name,
            'kind': self.kind,
            'pid': int(self.pid or 0),
            'status': self.status,
            'started_at': float(self.startedAt or 0.0),
            'ended_at': float(self.endedAt or 0.0),
            'ttl_seconds': float(self.ttl or 0.0),
            'exit_code': self.exitCode,
            'error_type': self.errorType,
            'error_message': self.errorMessage,
            'traceback_text': self.tracebackText,
            'fault_reason': self.faultReason,
            'db_row_id': int(self.dbRowId or 0),
        }


class StartPhase:
    """First-class launcher lifecycle phase with a process and callbacks."""

    def __init__(self, key: str, callback, label: str = EMPTY_STRING, group: str = 'startup', ttl: float = 0.0, onComplete=None, onError=None, onException=None, onFault=None):
        self.key = str(key or EMPTY_STRING).strip()
        self.label = str(label or self.key or 'Start Phase')
        self.group = str(group or 'startup').strip() or 'startup'
        self.status = 'pending'
        self.process = StartProcessRecord(self.label, kind='phase-callback', handle=callback, ttl=ttl, metadata={'phase_key': self.key, 'group': self.group})
        self.onComplete = onComplete  # noqa: nonconform
        self.onError = onError  # noqa: nonconform
        self.onException = onException  # noqa: nonconform
        self.onFault = onFault  # noqa: nonconform

    def get(self, key: str, default=None):
        if key == 'key':
            return self.key
        if key == 'callback':
            return self.process.handle
        if key == 'label':
            return self.label
        if key == 'group':
            return self.group
        return default

    def start(self, controller):
        self.status = 'running'
        controller.activePhases[self.key] = self
        controller.persistPhaseProcessStart(self)
        started = time.monotonic()
        controller.emit(f'[STARTUP:PHASE:BEGIN] key={self.key} label={self.label} group={self.group}')
        try:
            result = self.process.start()
            if self.process.status == 'running':
                self.process.complete()
            self.status = self.process.status
            controller.persistPhaseProcessStatus(self)
            controller.phaseResults[self.key] = self.status
            controller.activePhases.pop(self.key, None)
            elapsed = time.monotonic() - started
            controller.emit(f'[STARTUP:PHASE:END] key={self.key} status={self.status} elapsed={elapsed:.3f}s')
            if callable(self.onComplete):
                self.onComplete(controller, self, result)
            return result
        except Exception as exc:
            captureException(None, source='start.py', context='except@11778')
            self.process.exception(exc)
            self.status = 'errored'
            controller.persistPhaseProcessStatus(self)
            controller.phaseResults[self.key] = self.status
            controller.activePhases.pop(self.key, None)
            elapsed = time.monotonic() - started
            controller.emit(f'[STARTUP:PHASE:FAILED] key={self.key} status=errored elapsed={elapsed:.3f}s type={type(exc).__name__} message={exc}', getattr(controller, 'exceptionPath', None))
            if callable(self.onException):
                self.onException(controller, self, exc)
            raise


class StartLifecycleController:
    PHASE_MANIFEST = (
        ('parent-bootstrap-runtime-paths', 'bootstrapRuntimePathsPhase', EMPTY_STRING, 'startup'),
        ('parent-entry-git-cli', 'gitCliPhase', EMPTY_STRING, 'startup'),
        ('parent-bootstrap-early-cli', 'earlyCliPhase', EMPTY_STRING, 'startup'),
        ('parent-entry-vulture-cli', 'vultureCliPhase', EMPTY_STRING, 'startup'),
        ('parent-entry-push-cli', 'pushCliPhase', EMPTY_STRING, 'startup'),
        ('parent-entry-packaging-cli', 'packagingCliPhase', EMPTY_STRING, 'startup'),
        ('parent-entry-deploy-monitor', 'deployMonitorPhase', EMPTY_STRING, 'startup'),
        ('parent-entry-deploy-once', 'deployOncePhase', EMPTY_STRING, 'startup'),
        ('parent-entry-auto-zip-deploy-monitor', 'autoZipDeployMonitorPhase', EMPTY_STRING, 'startup'),
        ('parent-entry-proxy-daemon', 'proxyDaemonPhase', EMPTY_STRING, 'startup'),
        ('parent-entry-proxy-bind', 'proxyBindPhase', EMPTY_STRING, 'startup'),
        ('parent-entry-weekly-update-check', 'weeklyUpdateCheckPhase', EMPTY_STRING, 'startup'),
        ('parent-entry-clear-pycache', 'clearPycacheEntryPhase', EMPTY_STRING, 'startup'),
        ('parent-entry-print-identity', 'printIdentityPhase', EMPTY_STRING, 'startup'),
        ('parent-entry-register-atexit', 'registerAtexitPhase', EMPTY_STRING, 'startup'),
        ('parent-entry-expand-zips', 'expandZipsPhase', EMPTY_STRING, 'startup'),
        ('parent-bootstrap-parse-cli', 'parseCliPhase', EMPTY_STRING, 'startup'),
        ('parent-bootstrap-warn-missing-values', 'warnMissingCliValuesPhase', EMPTY_STRING, 'startup'),
        ('parent-bootstrap-install-missing-dependencies', 'installMissingDependenciesPhase', EMPTY_STRING, 'startup'),
        ('parent-bootstrap-verify-required-modules', 'verifyRequiredModulesPhase', EMPTY_STRING, 'startup'),
        ('parent-bootstrap-verify-bundle-artifacts', 'verifyBundleArtifactsPhase', EMPTY_STRING, 'startup'),
        ('parent-bootstrap-preflight-compile', 'preflightCompilePhase', EMPTY_STRING, 'startup'),
        ('parent-bootstrap-verify-gtp-exists', 'verifyGtpExistsPhase', EMPTY_STRING, 'startup'),
        ('parent-bootstrap-emit-unknown-cli-warnings', 'emitUnknownCliWarningsPhase', EMPTY_STRING, 'startup'),
        ('parent-bootstrap-seed-db-backend', 'seedDatabaseBackendPhase', EMPTY_STRING, 'startup'),
        ('parent-bootstrap-fast-headless-exec', 'fastHeadlessExecPhase', EMPTY_STRING, 'startup'),
        ('parent-bootstrap-create-debugger', 'createDebuggerPhase', EMPTY_STRING, 'startup'),
        ('parent-bootstrap-start-offscreen-display', 'startOffscreenDisplayPhase', EMPTY_STRING, 'startup'),
        ('parent-bootstrap-configure-debugger-trace', 'configureDebuggerTracePhase', EMPTY_STRING, 'startup'),
        ('parent-bootstrap-capture-hashes', 'captureHashesPhase', EMPTY_STRING, 'startup'),
        ('parent-bootstrap-start-kernel', 'startKernelPhase', EMPTY_STRING, 'startup'),
        ('parent-launch-child', 'launchChildPhase', EMPTY_STRING, 'startup'),
        ('parent-offscreen-scripted-actions', 'runOffscreenActionPhase', EMPTY_STRING, 'startup'),
        ('parent-arm-sigint', 'armSigintPhase', EMPTY_STRING, 'startup'),
        ('parent-await-child', 'awaitChildPhase', EMPTY_STRING, 'startup'),
    )

    def __init__(self, argv=None):
        self.argv = list(argv or [])
        self.childArgv = []  # noqa: nonconform
        self.unknownArgv = []  # noqa: nonconform
        self.offscreenBackgroundColor = None  # noqa: nonconform
        self.offscreenBackgroundAlpha = 0  # noqa: nonconform
        self.offscreenActionPlan = []  # noqa: nonconform
        self.captureEngineKind = 'auto'  # noqa: nonconform
        self.childPythonPath = str(sys.executable or EMPTY_STRING)  # noqa: nonconform
        self.debugger = None  # noqa: nonconform
        self.exitCode = 1
        self.stopRequested = False  # noqa: nonconform
        self.skipFinalPycacheCleanup = False  # noqa: nonconform
        self.originalSigintHandler = None  # noqa: nonconform
        self.phaseDefinitions = []  # noqa: nonconform
        self.phaseMapByKey: dict[str, StartPhase] = {}
        self.activePhases: dict[str, StartPhase] = {}
        self.phaseResults: dict[str, str] = {}
        self.processPersistenceReady = False  # noqa: nonconform
        self.exceptionPath = str(EXCEPTION_LOG_PATH)
        self.childPid = 0  # noqa: nonconform
        self.registerDefaultPhases()

    def emit(self, text: str, path: str | None = None):
        if not DebugLog.lineLooksVisible(text):
            return
        message = '\n'.join(DebugLog.iterVisibleLines(text))
        print(message, flush=True)
        if path:
            try:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                with File.tracedOpen(path, 'a', encoding='utf-8', errors='replace') as handle:
                    handle.write(message + ('\n' if not message.endswith('\n') else EMPTY_STRING))
            except Exception as error:
                captureException(None, source='start.py', context='except@11859')
                print(f"[WARN:swallowed-exception] start.py:10425 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass

    def _phaseDatabase(self):
        debugger = getattr(self, 'debugger', None)
        db = getattr(debugger, 'db', None) if debugger is not None else None
        return db

    def persistPhaseProcessStart(self, phase: StartPhase) -> None:
        if not self.processPersistenceReady:
            return
        db = self._phaseDatabase()
        if db is None:
            return
        try:
            process = phase.process
            process.dbRowId = int(db.insertProcessRecord(
                source='start.py',
                phase_key=str(phase.key),
                phase_name=str(phase.label),
                process_name=str(process.name),
                kind=str(process.kind),
                pid=int(process.pid or os.getpid()),
                status=str(process.status or 'running'),
                started_at=float(process.startedAt or time.time()),
                ttl_seconds=float(process.ttl or 0.0),
                command=str(process.command or EMPTY_STRING),
                metadata=json.dumps(process.snapshot(), ensure_ascii=False, default=str),
                processed=0,
            ) or 0)
        except Exception as error:
            captureException(None, source='start.py', context='except@11890')
            print(f"[WARN:swallowed-exception] start.py:10455 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            self.emit(f'[WARNING:start-lifecycle-db] process start persist failed: {type(error).__name__}: {error}', self.exceptionPath)

    def persistPhaseProcessStatus(self, phase: StartPhase) -> None:
        if not self.processPersistenceReady:
            return
        db = self._phaseDatabase()
        if db is None:
            return
        process = phase.process
        if int(process.dbRowId or 0) <= 0:
            return
        try:
            db.updateProcessRecord(
                int(process.dbRowId),
                status=str(process.status or phase.status),
                ended_at=float(process.endedAt or 0.0),
                exit_code=process.exitCode,
                error_type=str(process.errorType or EMPTY_STRING),
                error_message=str(process.errorMessage or EMPTY_STRING),
                traceback_text=str(process.tracebackText or EMPTY_STRING),
                fault_reason=str(process.faultReason or EMPTY_STRING),
                metadata=json.dumps(process.snapshot(), ensure_ascii=False, default=str),
            )
        except Exception as error:
            captureException(None, source='start.py', context='except@11915')
            print(f"[WARN:swallowed-exception] start.py:10479 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            self.emit(f'[WARNING:start-lifecycle-db] process status persist failed: {type(error).__name__}: {error}', self.exceptionPath)

    def registerPhase(self, key: str, callback, label: str = EMPTY_STRING, group: str = 'startup', ttl: float = 0.0, **callbacks):
        phaseKey = str(key or EMPTY_STRING).strip()
        if not phaseKey or not callable(callback):
            return None
        phase = StartPhase(phaseKey, callback, label=label or phaseKey, group=group, ttl=ttl, **callbacks)
        self.phaseDefinitions.append(phase)
        self.phaseMapByKey[phaseKey] = phase
        return phase

    def phaseEntries(self, group: str = EMPTY_STRING):
        phaseGroup = str(group or EMPTY_STRING).strip()
        entries = list(self.phaseDefinitions or [])
        if not phaseGroup:
            return entries
        return [entry for entry in entries if str(entry.get('group', EMPTY_STRING) or EMPTY_STRING).strip() == phaseGroup]

    def registerDefaultPhases(self):
        for key, callbackName, label, group in self.PHASE_MANIFEST:
            callback = getattr(self, str(callbackName), None)
            if not callable(callback):
                raise AttributeError(f'StartLifecycleController missing phase callback: {callbackName}')
            self.registerPhase(key, callback, label=label, group=group)

    def runPhaseGroup(self, group: str = 'startup'):
        phases = self.phaseEntries(group)
        self.emit(f'[STARTUP:GROUP:BEGIN] group={group} phases={len(phases)} argv={self.argv}')
        for phase in phases:
            if self.stopRequested:
                self.emit(f'[STARTUP:GROUP:STOP] group={group} before={phase.key} exit_code={self.exitCode}')
                break
            phase.start(self)
        self.emit(f'[STARTUP:GROUP:END] group={group} exit_code={self.exitCode} stop={self.stopRequested}')
        return int(self.exitCode)

    def bootstrapRuntimePathsPhase(self):
        DebugLog.trace('STARTUP PHASE: bootstrap runtime paths begin base=' + str(BASE_DIR), level='TRACE', source='start.py')
        os.chdir(str(BASE_DIR))
        if str(BASE_DIR) not in sys.path:
            sys.path.insert(0, str(BASE_DIR))
        os.environ.setdefault('TRIO_BASE_DIR', str(BASE_DIR))
        ensurePromptMetadataConfig()
        DebugLog.trace('STARTUP PHASE: bootstrap runtime paths complete debug_log=' + str(DEBUG_LOG_PATH), level='TRACE', source='start.py')
        return True

    def earlyCliPhase(self):
        result = runStartCliInfo(self.argv)
        if result is not None:
            self.exitCode = int(result)
            self.stopRequested = True
            self.skipFinalPycacheCleanup = True
        return True

    def vultureCliPhase(self):
        result = runVultureIfRequested(self.argv)
        if result is not None:
            self.exitCode = int(result)
            self.stopRequested = True
            self.skipFinalPycacheCleanup = True
        return True

    def gitCliPhase(self):
        if gitRequested(self.argv):
            self.exitCode = int(runGitCli(self.argv))
            self.stopRequested = True
        return True

    def pushCliPhase(self):
        result = runPushIfRequested(self.argv)
        if result is not None:
            self.exitCode = int(result)
            self.stopRequested = True
        return True

    def packagingCliPhase(self):
        result = runPackagingIfRequested(self.argv)
        if result is not None:
            self.exitCode = int(result)
            self.stopRequested = True
        return True

    def deployMonitorPhase(self):
        if deployMonitorRequested(self.argv):
            self.exitCode = int(runDeployMonitor(self.argv))
            self.stopRequested = True
        return True

    def deployOncePhase(self):
        if deployOnceRequested(self.argv):
            self.exitCode = int(runDeployOnce(self.argv))
            self.stopRequested = True
        return True

    def autoZipDeployMonitorPhase(self):
        if any(isDeployZipCandidate(path) for path in BASE_DIR.glob('*.zip')):
            self.exitCode = int(runDeployMonitor(self.argv))
            self.stopRequested = True
        return True

    def proxyDaemonPhase(self):
        if proxyDaemonRequested(self.argv):
            self.exitCode = int(runProxyDaemon(self.argv))
            self.stopRequested = True
        return True

    def proxyBindPhase(self):
        bind_override = proxyBindValue(self.argv)
        if bind_override:
            os.environ['TRIO_PROXY_BIND'] = bind_override
        return True

    def weeklyUpdateCheckPhase(self):
        runWeeklyUpdateCheckIfDue(self.argv)
        return True

    def clearPycacheEntryPhase(self):
        clearPycacheDirectories(BASE_DIR, reason='startup-entry')
        return True

    def printIdentityPhase(self):
        printLauncherIdentity(GTP_PATH, context='startup-entry')
        return True

    def registerAtexitPhase(self):
        atexit.register(clearPycacheDirectories, BASE_DIR, reason='process-exit')  # lifecycle-ok: registered inside StartLifecycleController phase.
        return True

    def expandZipsPhase(self):
        expandZipsInDirectory(BASE_DIR)
        return True

    def parseCliPhase(self):
        _, self.childArgv, self.unknownArgv = parseStartCli(self.argv)
        languageCode = startCliLanguageCode(self.argv)
        if languageCode:
            os.environ['PROMPT_LANGUAGE'] = languageCode
        self.offscreenBackgroundColor, self.offscreenBackgroundAlpha = parseBackgroundColorAndAlpha(self.argv)
        self.offscreenActionPlan = parseOffscreenActionPlan(self.argv)
        self.captureEngineKind = requestedCaptureEngineKind(self.argv)
        self.childPythonPath = resolvePreferredChildPython(guiRequired=not cliHasAnyFlag(self.childArgv, HEADLESS_FLAGS))
        return True

    def warnMissingCliValuesPhase(self):
        warnMissingCliValues(self.argv)
        return True

    def installMissingDependenciesPhase(self):
        self.emit('[STARTUP:DEPS:INSTALL:BEGIN] checking/installing missing launcher Python dependencies')
        installMissingDependencies(self.argv)
        self.emit('[STARTUP:DEPS:INSTALL:END] dependency installer phase completed')
        return True

    def verifyRequiredModulesPhase(self):
        self.emit('[STARTUP:DEPS:VERIFY:BEGIN] checking Linux/system requirements and required Python modules before GUI launch')
        verifyLinuxSystemRequirementsOrDie(self.argv)
        missing_modules = missingRequiredModules(self.argv)
        if missing_modules:
            missing_names = ', '.join(module_name for module_name, _ in missing_modules)
            package_names = ', '.join(uniquePackagesForMissingModules(missing_modules))
            self.emit(f'[STARTUP:DEPS:VERIFY:FAILED] missing_modules={missing_names} expected_packages={package_names}', self.exceptionPath)
        verifyRequiredModulesOrDie(self.argv)
        self.emit('[STARTUP:DEPS:VERIFY:END] all required modules are importable; safe to launch child GUI')
        return True

    def verifyBundleArtifactsPhase(self):
        verifyBundleArtifactsOrDie(BASE_DIR)
        return True

    def preflightCompilePhase(self):
        preflightCompileOrDie(GTP_PATH)
        return True

    def verifyGtpExistsPhase(self):
        if not GTP_PATH.exists():
            raise RuntimeError(f'Missing prompt_app.py next to start.py: {GTP_PATH}')
        return True

    def emitUnknownCliWarningsPhase(self):
        for token in list(self.unknownArgv or []):
            print(f'[WARNING:start.py.cli] Unknown CLI arg ignored by start.py: {token}', file=sys.stderr, flush=True)
        return True

    def seedDatabaseBackendPhase(self):
        seedDatabaseBackend(self.childArgv)
        self.processPersistenceReady = True
        return True

    def fastHeadlessExecPhase(self):
        headlessRequested = cliHasAnyFlag(self.childArgv, HEADLESS_FLAGS)
        verboseTraceRequested = traceVerboseRequested(self.childArgv)
        if headlessRequested and not verboseTraceRequested and not debugEnabled(self.argv) and not offscreenRequested(self.argv):
            env = os.environ.copy()
            prepareTraceEnvironment(self.childArgv, env, debugWritesAllowed=debugEnabled(self.argv))
            childPython = str(getattr(self, 'childPythonPath', EMPTY_STRING) or resolvePreferredChildPython(guiRequired=False)).strip() or str(sys.executable or EMPTY_STRING)
            self.childPythonPath = childPython
            cmd = [*childPythonCommandPrefix(childPython), str(GTP_PATH), *self.childArgv]
            os.execvpe(childPython, cmd, env)  # lifecycle-bypass-ok phase-ownership-ok child-reexec-handled-by-parent-phase
        return True

    def createDebuggerPhase(self):
        self.debugger = PromptDebugger(debugEnabled(self.argv))
        self.debugger.childPythonPath = str(getattr(self, 'childPythonPath', EMPTY_STRING) or resolvePreferredChildPython(guiRequired=not cliHasAnyFlag(self.childArgv, HEADLESS_FLAGS))).strip()
        return True

    def startOffscreenDisplayPhase(self):
        if self.debugger is None:
            return True
        useOffscreen = offscreenRequested(self.argv)
        self.debugger.configureOffscreen(useOffscreen, captureEngineKind=self.captureEngineKind, screenSpec=CaptureEngine.DEFAULT_SCREEN, backgroundColor=self.offscreenBackgroundColor, backgroundAlpha=self.offscreenBackgroundAlpha, actions=self.offscreenActionPlan)
        if useOffscreen:
            self.debugger.startOffscreenSession()
        return True

    def configureDebuggerTracePhase(self):
        if self.debugger is not None:
            self.debugger.configureTraceVerbose(self.childArgv)
        return True

    def captureHashesPhase(self):
        if self.debugger is None:
            return True
        fullStartMd5 = safeFileMd5Hex(__file__) or '????????????????????????????????'
        fullTargetMd5 = safeFileMd5Hex(str(GTP_PATH)) or '????????????????????????????????'
        self.debugger.childMd5Short = fullTargetMd5[:8]
        printLauncherIdentity(GTP_PATH, preferredChildPython=str(getattr(self, 'childPythonPath', EMPTY_STRING) or EMPTY_STRING).strip(), context='startup-phases')
        print(f'[PromptDebugger] start.py short-md5={fullStartMd5[:8]}', flush=True)
        print(f'[PromptDebugger] {GTP_PATH.name} short-md5={fullTargetMd5[:8]}', flush=True)
        return True

    def startKernelPhase(self):
        if self.debugger is None:
            return True
        self.debugger.startKernel()
        if self.debugger.enabled:
            self.debugger.emit('[PromptDebugger] Kernel launch mode: subprocess relay', self.debugger.logPath)
        return True

    def launchChildPhase(self):
        if self.debugger is None:
            return True
        self.emit(f'[STARTUP:CHILD:LAUNCH:BEGIN] target={GTP_PATH} argv={self.childArgv}')
        try:
            self.debugger.launchChild(self.childArgv)
            self.emit(f'[STARTUP:CHILD:LAUNCH:END] child_pid={getattr(self.debugger, "childPid", 0)}')
        except BaseException as error:
            captureException(None, source='start.py', context='except@12144')
            print(f"[WARN:swallowed-exception] start.py:10705 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            self.debugger.die('start.launch', error)
            self.exitCode = 1
            self.stopRequested = True
        return True

    def runOffscreenActionPhase(self):
        if self.debugger is None:
            return True
        if bool(getattr(self.debugger, 'offscreenEnabled', False)) and list(getattr(self.debugger, 'offscreenActionPlan', []) or []):
            self.debugger.scheduleOffscreenActions()
        return True

    def armSigintPhase(self):
        if self.debugger is None:
            return True
        try:
            import signal as signalModule
            self.originalSigintHandler = signalModule.getsignal(signalModule.SIGINT)
            def onCtrlC(_sig, _frame):
                cast(Any, self.debugger).requestSigterm()
                cast(Any, self.debugger).closeChildStreams()
                cast(Any, self.debugger).consoleStop.set()
                cast(Any, self.debugger).consoleWake.set()
                if callable(self.originalSigintHandler):
                    signalModule.signal(signalModule.SIGINT, self.originalSigintHandler)
            signalModule.signal(signalModule.SIGINT, onCtrlC)
        except Exception as error:
            captureException(None, source='start.py', context='except@12172')
            print(f"[WARN:swallowed-exception] start.py:10732 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        return True

    def awaitChildPhase(self):
        if self.debugger is None or self.stopRequested:
            return True
        self.exitCode = int(self.debugger.waitForChild(pollInterval=0.10 if self.debugger.enabled else 0.25))
        self.stopRequested = True
        return True

    def shutdown(self):
        if self.debugger is not None:
            self.debugger.shutdownKernel()
        return True

    def run(self) -> int:
        try:
            self.runPhaseGroup('startup')
            return int(self.exitCode)
        finally:
            self.shutdown()



HEARTBEAT_FRAMES = ['▁▁▂▃▄▆█▆▄▃▂▁▁', '▁▂▃▄▆█▆▄▃▂▁▁▁', '▂▃▄▆█▆▄▃▂▁▁▁▁', '▃▄▆█▆▄▃▂▁▁▁▁▁', '▄▆█▆▄▃▂▁▁▁▁▁▁', '▆█▆▄▃▂▁▁▁▁▁▁▁', '█▆▄▃▂▁▁▁▁▁▁▁▁', '▆▄▃▂▁▁▁▁▁▁▁▁▂']
DEPLOY_TOUCH_EXTENSIONS = {'.py', '.md', '.html', '.js', '.css', '.json', '.txt', '.bat', '.php', '.xml', '.yml', '.yaml'}
DEPLOY_IGNORED_ZIP_NAMES = {'assets.zip'}  # assets/assets.zip is bundled data, not an autoload deployment zip.
_LAST_CONSOLE_FRAME_LINES = 0



def consoleSupportsAnsi() -> bool:
    try:
        if not getattr(sys.stdout, 'isatty', lambda: False)():
            return False
    except Exception as error:
        captureException(None, source='start.py', context='except@12209')
        print(f"[WARN:swallowed-exception] start.py:10769 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        return False
    if os.name != 'nt':
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        if handle in (0, -1):
            return False
        mode = ctypes.c_uint()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)) == 0:
            return False
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        return True
    except Exception as error:
        captureException(None, source='start.py', context='except@12225')
        print(f"[WARN:swallowed-exception] start.py:10784 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        return bool(os.environ.get('WT_SESSION') or os.environ.get('TERM_PROGRAM') or os.environ.get('ConEmuANSI') == 'ON')


def ansi(code: str) -> str:
    return f'\x1b[{code}m' if consoleSupportsAnsi() else EMPTY_STRING


def stripAnsi(text_value: str) -> str:
    return re.sub(r'\x1b\[[0-9;]*m', EMPTY_STRING, str(text_value or EMPTY_STRING))


def writeConsoleBorderLine(width: int, top: bool = True, color_code: str = '90') -> str:
    if consoleSupportsAnsi():
        left = '╔' if top else '╚'
        fill = '═' * max(0, int(width or 0))
        right = '╗' if top else '╝'
        return f"{ansi(color_code)}{left}{fill}{right}{ansi('0')}"
    return '+' + ('-' * max(0, int(width or 0))) + '+'


def writeConsoleBodyLine(content: str, width: int, color_code: str = '90') -> str:
    inner = str(content or EMPTY_STRING)
    plain = stripAnsi(inner)
    padded = inner + (' ' * max(0, int(width or 0) - len(plain)))
    if consoleSupportsAnsi():
        return f"{ansi(color_code)}║{ansi('0')} {padded} {ansi(color_code)}║{ansi('0')}"
    return f"| {stripAnsi(padded)} |"


def writeConsoleFrame(title: str, rows: list[str], color_code: str = '90') -> str:
    values = [str(title or EMPTY_STRING)] + [str(row or EMPTY_STRING) for row in list(rows or [])]
    width = max(len(stripAnsi(value)) for value in values) + 2
    frame = [
        writeConsoleBorderLine(width + 2, top=True, color_code=color_code),
        writeConsoleBodyLine(str(title or EMPTY_STRING), width, color_code=color_code),
    ]
    for row in list(rows or []):
        frame.append(writeConsoleBodyLine(str(row or EMPTY_STRING), width, color_code=color_code))
    frame.append(writeConsoleBorderLine(width + 2, top=False, color_code=color_code))
    return '\n'.join(frame)


def clearConsoleFrame(lines_count: int) -> None:
    if lines_count <= 0:
        return
    if consoleSupportsAnsi():
        for index in range(int(lines_count)):
            if index > 0:
                sys.stdout.write('\x1b[1A')
            sys.stdout.write('\r\x1b[2K')
        sys.stdout.flush()
    else:
        sys.stdout.write('\r')
        sys.stdout.flush()


def repaintConsoleFrame(frame_text: str) -> None:
    global _LAST_CONSOLE_FRAME_LINES
    lines = str(frame_text or EMPTY_STRING).splitlines() or [EMPTY_STRING]
    clearConsoleFrame(_LAST_CONSOLE_FRAME_LINES)
    sys.stdout.write('\n'.join(lines))
    sys.stdout.write('\n')
    sys.stdout.flush()
    _LAST_CONSOLE_FRAME_LINES = len(lines)


def buildDeployProgressTitleLine(label: str, width: int) -> str:
    return str(label or 'Deploying').ljust(width)[:width]


def buildDeployProgressValueLine(percent: float, width: int = 42) -> str:
    pct = max(0.0, min(100.0, float(percent or 0.0)))
    filled = int(round((pct / 100.0) * width))
    if consoleSupportsAnsi():
        parts = []
        for index in range(width):
            if index < filled:
                code = '32' if index < max(1, width // 3) else ('92' if index < max(2, (2 * width) // 3) else '1;92')
                parts.append(f"{ansi(code)}█{ansi('0')}")
            else:
                parts.append(' ')
        bar = ''.join(parts)
    else:
        bar = ('#' * filled) + (' ' * max(0, width - filled))
    return f'{bar} {pct:6.2f}%'


def buildDeployProgressShadowLine(width: int) -> str:
    if consoleSupportsAnsi():
        return f"{ansi('90')}  {'░' * max(0, width)}{ansi('0')}"
    return EMPTY_STRING


def renderConsoleBox(title: str, rows: list[str], color_code: str = '\x1b[90m') -> str:
    match = re.search(r'\x1b\[([0-9;]+)m', str(color_code or EMPTY_STRING))
    resolved = str(match.group(1) or '90') if match else '90'
    return writeConsoleFrame(title, rows, color_code=resolved)


def renderDeployProgressFrame(label: str, percent: float, width: int = 42) -> str:
    rows = [
        buildDeployProgressTitleLine(label, width + 8),
        buildDeployProgressValueLine(percent, width),
    ]
    frame = writeConsoleFrame(str(label or 'Deploying'), rows, color_code='90')
    shadow = buildDeployProgressShadowLine(width + 12)
    if shadow:
        frame += '\n' + shadow
    return frame


def renderDeployProgressBar(label: str, percent: float, width: int = 42) -> str:
    return renderDeployProgressFrame(label, percent, width=width)


def heartbeatMonitorFrames() -> list[str]:
    return [
        '__/^^\\____/^^\\__',
        '_/^^\\____/^^\\___',
        '/^^\\____/^^\\____',
        '^^\\____/^^\\_____',
        '^\\____/^^\\______',
        '\\____/^^\\_______',
        '____/^^\\_________',
        '___/^^\\____/^^\\_',
    ]


def buildHeartbeatMonitorText(frame_index: int, candidate_script: str = EMPTY_STRING) -> str:
    frames = heartbeatMonitorFrames()
    frame = frames[int(frame_index or 0) % len(frames)]
    candidate_name = Path(candidate_script).name if str(candidate_script or EMPTY_STRING).strip() else 'none'
    if consoleSupportsAnsi():
        pulse = f"{ansi('1;92')}{frame}{ansi('0')}"
        title = f"{ansi('1;92')}FLATLINE Debugger v{DEBUGGER_VERSION}{ansi('0')}"
    else:
        pulse = frame
        title = f'FLATLINE Debugger v{DEBUGGER_VERSION}'
    return f'{title} {pulse} candidate={candidate_name} [R relaunch | Q quit]'


def deployMonitorHeartbeatLine(frame_index: int, candidate_script: str = EMPTY_STRING) -> str:
    return buildHeartbeatMonitorText(frame_index, candidate_script)



def deployMonitorRequested(argv=None) -> bool:
    return bool(readCliOption(argv, DEPLOY_MONITOR_FLAGS, takesValue=False, knownAliases=START_VALID_CLI_ALIASES).get('present'))

def deployOnceRequested(argv=None) -> bool:
    return bool(readCliOption(argv, DEPLOY_ONCE_FLAGS, takesValue=False, knownAliases=START_VALID_CLI_ALIASES).get('present'))

def mergeDirectoryContents(source_path: Path, destination_path: Path) -> None:
    source = Path(source_path)
    destination = Path(destination_path)
    destination.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        return
    for item in sorted(source.rglob('*'), key=lambda path: len(path.relative_to(source).parts)):
        if item.is_dir():
            (destination / item.relative_to(source)).mkdir(parents=True, exist_ok=True)
            continue
        dest = destination / item.relative_to(source)
        dest.parent.mkdir(parents=True, exist_ok=True)
        File.copy2(str(item), str(dest))
        try:
            item.unlink()
        except Exception as error:
            captureException(error, source='start.py', context='deploy-merge-unlink', handled=True)
            print(f"[WARN:deploy] could not delete merged source {item}: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
    for directory in sorted([path for path in source.rglob('*') if path.is_dir()], key=lambda path: len(path.relative_to(source).parts), reverse=True):
        with contextlib.suppress(Exception):
            directory.rmdir()


def promoteSingleChildFolder(base_path: Path) -> bool:
    base = Path(base_path)
    if (base / 'start.py').exists():
        return False
    candidate_directories = [item for item in base.iterdir() if item.is_dir() and item.name not in {'.git', '.venv', 'venv', '__pycache__', 'debs', 'linux_runtime'}]
    if len(candidate_directories) != 1:
        return False
    directory = candidate_directories[0]
    print(f'[INFO] Promoting files from {directory}', flush=True)
    mergeDirectoryContents(directory, base)
    shutil.rmtree(directory, ignore_errors=True)
    return True


def flattenProjectWrappers(base_path: Path) -> bool:
    promoted = False
    if promoteSingleChildFolder(base_path):
        promoted = True
    if promoteSingleChildFolder(base_path):
        promoted = True
    return promoted


def touchExtractedProjectFiles(base_path: Path, touch_time: datetime.datetime) -> None:
    target_timestamp = touch_time.astimezone(datetime.timezone.utc).timestamp()
    for path in Path(base_path).rglob('*'):
        if path.is_file() and path.suffix.lower() in DEPLOY_TOUCH_EXTENSIONS:
            try:
                os.utime(path, (target_timestamp, target_timestamp))
            except Exception as error:
                captureException(error, source='start.py', context='deploy-touch-extracted-file', handled=True)
                print(f"[WARN:deploy] could not touch {path}: {type(error).__name__}: {error}", file=sys.stderr, flush=True)


_FAILED_DEPLOY_ZIP_KEYS: set[str] = set()


def deployWarn(message: str) -> None:  # noqa: N802
    text = str(message or EMPTY_STRING)
    print(f'[WARN:deploy] {text}', file=sys.stderr, flush=True)
    try:
        appendRunLog(f'[WARN:deploy] {text}')
    except Exception:
        pass


def deployZipKey(zip_path: Path) -> str:  # noqa: N802
    path = Path(zip_path)
    try:
        stat = path.stat()
        return f'{path.resolve()}|{int(stat.st_size)}|{int(stat.st_mtime)}'
    except Exception:
        return str(path.resolve())


def deployZipFailedBefore(zip_path: Path) -> bool:  # noqa: N802
    return deployZipKey(Path(zip_path)) in _FAILED_DEPLOY_ZIP_KEYS


def markDeployZipFailed(zip_path: Path, reason: str) -> None:  # noqa: N802
    key = deployZipKey(Path(zip_path))
    _FAILED_DEPLOY_ZIP_KEYS.add(key)
    deployWarn(f'zip marked failed for this launcher session; will not retry until file changes zip={Path(zip_path).name} reason={reason}')


def _normalizeZipMemberName(member_name: str) -> str:
    return str(member_name or EMPTY_STRING).replace('\\', '/').lstrip('/')


def _zipMemberBasename(member_name: str) -> str:
    return Path(_normalizeZipMemberName(member_name)).name.lower()


def safeZipMtimeUtc(zip_path: Path) -> datetime.datetime:  # noqa: N802
    try:
        return datetime.datetime.fromtimestamp(Path(zip_path).stat().st_mtime, tz=datetime.timezone.utc)
    except FileNotFoundError as error:
        captureException(error, source='start.py', context='deploy-zip-mtime-disappeared', handled=True)
        return datetime.datetime.fromtimestamp(0, tz=datetime.timezone.utc)
    except Exception as error:
        captureException(error, source='start.py', context='deploy-zip-mtime', handled=True)
        return datetime.datetime.now(datetime.timezone.utc)


def zipLooksLikeDeployment(zip_path: Path) -> bool:  # noqa: N802
    """Return True only for Prompt deployment bundles.

    This is the useful part of the uploaded launcher branch, adapted for
    Prompt.  The deploy monitor must not unpack unrelated tool/vendor archives
    just because a .zip lands in the project root.
    """
    target = Path(zip_path)
    if not target.exists():
        deployWarn(f'Zip disappeared before inspection; skipping {target.name}')
        return False
    if not isDeployZipCandidate(target):
        return False
    try:
        with zipfile.ZipFile(target, 'r') as archive:
            names = [_normalizeZipMemberName(info.filename) for info in archive.infolist() if not info.is_dir()]
    except FileNotFoundError as error:
        captureException(error, source='start.py', context='deploy-inspect-zip-disappeared', handled=True)
        deployWarn(f'Zip disappeared before inspection; skipping {target.name}')
        return False
    except zipfile.BadZipFile as error:
        captureException(error, source='start.py', context='deploy-bad-zip-inspect', handled=True)
        deployWarn(f'Bad zip skipped {target.name}: {error}')
        return False
    except Exception as error:
        captureException(error, source='start.py', context='deploy-inspect-zip', handled=True)
        deployWarn(f'Could not inspect zip {target.name}: {type(error).__name__}: {error}')
        return False
    basenames = {_zipMemberBasename(name) for name in names}
    if 'start.py' not in basenames:
        print(f'[INFO] Skipping non-Prompt zip {target.name}: no start.py deployment marker.', flush=True)
        return False
    prompt_markers = {'prompt_app.py', 'frozen_prompt_entry.py', 'auto.ps1', 'auto_build_exes.ps1'}
    if not (prompt_markers & basenames):
        print(f'[INFO] Skipping non-Prompt zip {target.name}: missing Prompt app/deployer marker.', flush=True)
        return False
    return True


def _deploySafeDestinationForMember(destination: Path, member_name: str) -> Path:
    normalized = _normalizeZipMemberName(member_name)
    if not normalized or normalized.endswith('/'):
        return Path(destination)
    root = Path(destination).resolve()
    target = (root / normalized).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise RuntimeError(f'unsafe zip member path outside deployment root: {member_name!r}')
    return target


def _deployMemberIdentical(archive: zipfile.ZipFile, info: zipfile.ZipInfo, target: Path) -> bool:
    try:
        if not target.exists() or not target.is_file() or int(target.stat().st_size) != int(info.file_size):
            return False
        digest_existing = hashlib.md5(target.read_bytes()).hexdigest()
        with archive.open(info, 'r') as handle:
            digest_new = hashlib.md5(handle.read()).hexdigest()
        return digest_existing == digest_new
    except Exception:
        return False


def _extractZipMemberAtomic(archive: zipfile.ZipFile, info: zipfile.ZipInfo, destination: Path) -> Path:
    target = _deploySafeDestinationForMember(destination, info.filename)
    if info.is_dir() or str(info.filename or EMPTY_STRING).endswith('/'):
        target.mkdir(parents=True, exist_ok=True)
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    if _deployMemberIdentical(archive, info, target):
        return target
    temp_path = target.with_name(target.name + f'.deploytmp-{os.getpid()}')
    try:
        with archive.open(info, 'r') as src, File(temp_path).open('wb') as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        os.replace(temp_path, target)
        return target
    except Exception:
        with contextlib.suppress(Exception):
            temp_path.unlink(missing_ok=True)
        raise


def _repairDeployExtractionFailure(target: Path, error: BaseException) -> bool:
    """Best-effort recovery for locked/read-only deploy files."""
    try:
        failed_target = Path(target)
        if failed_target.exists():
            try:
                File(failed_target).setPermissions('666' if failed_target.is_file() else '777')
            except Exception:
                pass
            if os.name == 'nt':
                try:
                    File(failed_target).killLockingProcesses(only_known_children=True)
                except Exception as lock_error:
                    captureException(lock_error, source='start.py', context='deploy-kill-locking-processes', handled=True)
        return True
    except Exception as repair_error:
        captureException(repair_error, source='start.py', context='deploy-repair-extraction-failure', handled=True)
        deployWarn(f'extraction repair failed target={target}: {type(repair_error).__name__}: {repair_error}; original={type(error).__name__}: {error}')
        return False


def extractZipIntoDirectory(zip_path: Path, destination_path: Path, *, attempt: int = 1) -> bool:
    zip_target = Path(zip_path)
    destination = Path(destination_path)
    if not zip_target.exists():
        deployWarn(f'Zip disappeared before extraction; skipping {zip_target.name}')
        return False
    if deployZipFailedBefore(zip_target):
        deployWarn(f'previous deployment failure remembered; skipping unchanged zip={zip_target.name}')
        return False
    failed_info: zipfile.ZipInfo | None = None
    failed_target: Path | None = None
    try:
        with zipfile.ZipFile(zip_target, 'r') as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            total = max(1, len(infos))
            for index, info in enumerate(infos, start=1):
                failed_info = info
                failed_target = _deploySafeDestinationForMember(destination, info.filename)
                _extractZipMemberAtomic(archive, info, destination)
                percent = (index / total) * 100.0
                repaintConsoleFrame(renderDeployProgressBar(f'Extracting {zip_target.name}', percent))
            clearConsoleFrame(_LAST_CONSOLE_FRAME_LINES)
            sys.stdout.write('\r')
            sys.stdout.flush()
            return True
    except FileNotFoundError as error:
        captureException(error, source='start.py', context='deploy-extract-zip-disappeared', handled=True)
        deployWarn(f'Zip disappeared during extraction; skipping {zip_target.name}')
        return False
    except zipfile.BadZipFile as error:
        captureException(error, source='start.py', context='deploy-bad-zip', handled=True)
        deployWarn(f'Bad zip skipped {zip_target.name}: {error}')
        markDeployZipFailed(zip_target, f'bad zip: {error}')
        return False
    except Exception as error:
        captureException(error, source='start.py', context='deploy-extraction-failure', handled=True)
        clearConsoleFrame(_LAST_CONSOLE_FRAME_LINES)
        member = str(getattr(failed_info, 'filename', EMPTY_STRING) or '<unknown>')
        deployWarn(f'extraction failed attempt={attempt} zip={zip_target.name} member={member} target={failed_target or destination} error={type(error).__name__}: {error}')
        if attempt < 2 and _repairDeployExtractionFailure(failed_target or destination, error):
            deployWarn(f'retrying extraction once after repair zip={zip_target.name}')
            return extractZipIntoDirectory(zip_target, destination, attempt=attempt + 1)
        markDeployZipFailed(zip_target, f'{type(error).__name__}: {error}')
        return False


def isDeployZipCandidate(path: Path) -> bool:
    try:
        candidate = Path(path)
        return candidate.suffix.lower() == '.zip' and candidate.name.lower() not in DEPLOY_IGNORED_ZIP_NAMES
    except Exception as error:
        captureException(error, source='start.py', context='deploy-zip-candidate', handled=True)
        return False


def expandZipsInDirectory(root_path: Path) -> datetime.datetime | None:
    root = Path(root_path)
    try:
        raw_zip_files = [path for path in root.glob('*.zip') if path.exists()]
    except Exception as error:
        captureException(error, source='start.py', context='deploy-list-zips', handled=True)
        deployWarn(f'Could not list zip files in {root}: {type(error).__name__}: {error}')
        return None
    zip_files = sorted([path for path in raw_zip_files if zipLooksLikeDeployment(path)], key=lambda path: safeZipMtimeUtc(path).timestamp())
    if not zip_files:
        return None
    newest_deployment = None
    extracted_any = False
    for zip_path in zip_files:
        if deployZipFailedBefore(zip_path):
            deployWarn(f'skipping failed unchanged zip={zip_path.name}')
            continue
        stamp = safeZipMtimeUtc(zip_path)
        if newest_deployment is None or stamp > newest_deployment:
            newest_deployment = stamp
        print(f'[INFO] Extracting {zip_path.name}', flush=True)
        extracted = extractZipIntoDirectory(zip_path, root)
        if not extracted:
            continue
        extracted_any = True
        touchExtractedProjectFiles(root, stamp)
        try:
            zip_path.unlink()
            print(f'[INFO] Deleted {zip_path.name}', flush=True)
        except FileNotFoundError as error:
            captureException(error, source='start.py', context='deploy-delete-zip-disappeared', handled=True)
            deployWarn(f'Zip was already deleted after extraction: {zip_path.name}')
        except Exception as error:
            captureException(error, source='start.py', context='deploy-delete-zip', handled=True)
            deployWarn(f'Could not delete zip {zip_path.name}: {type(error).__name__}: {error}')
    if extracted_any:
        flattenProjectWrappers(root)
        return newest_deployment
    return None

def resolveLaunchScript(root_path: Path, deployment_floor_utc: datetime.datetime | None = None) -> str:
    root = Path(root_path)
    floor_ts = None
    if deployment_floor_utc is not None:
        try:
            floor_ts = deployment_floor_utc.astimezone(datetime.timezone.utc).timestamp() - 1.0
        except Exception as error:
            captureException(None, source='start.py', context='except@12482')
            print(f"[WARN:swallowed-exception] start.py:11034 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            floor_ts = None

    def is_eligible(path: Path) -> bool:
        try:
            if floor_ts is None:
                return True
            return path.stat().st_mtime >= floor_ts
        except Exception as error:
            captureException(None, source='start.py', context='except@12491')
            print(f"[WARN:swallowed-exception] start.py:11042 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return False

    preferred_names = ('start.py', 'gtp.py', 'main.py')
    for name in preferred_names:
        matches = [candidate for candidate in sorted(root.rglob(name), key=lambda candidate: str(candidate)) if is_eligible(candidate)]
        if matches:
            return str(matches[0].resolve())
    matches = [candidate for candidate in sorted(root.rglob('*.py'), key=lambda candidate: str(candidate)) if is_eligible(candidate)]
    return str(matches[0].resolve()) if matches else EMPTY_STRING


def getDefaultDeployScriptArgs(argv=None) -> list[str]:
    filtered = []
    for token in list(argv or []):
        low = str(token or EMPTY_STRING).strip().lower()
        if low in DEPLOY_MONITOR_FLAGS or low in DEPLOY_ONCE_FLAGS:
            continue
        filtered.append(str(token or EMPTY_STRING))
    return filtered or ['--debug', '--trace-verbose']

def launchTargetScript(script_path: str, argv=None, manual: bool = False) -> bool:
    _ = manual
    target = str(script_path or EMPTY_STRING).strip()
    if not target or not Path(target).exists():
        print('[WARN] No Python script found to run.', flush=True)
        return False
    args = getDefaultDeployScriptArgs(argv)
    python_executable = str(resolvePreferredChildPython(guiRequired=('--headless' not in args and '/headless' not in args)) or sys.executable or EMPTY_STRING).strip()
    if not python_executable:
        return False
    cmd = [python_executable, target, *args]
    display_name = Path(target).name
    print(f'[INFO] Launched {display_name}; output is piped to {promptRunLogPath()}', flush=True)
    try:
        env = dict(os.environ)
        env['PROMPT_RUN_LOG_ROOT'] = str(Path(target).resolve().parent)
        env['PROMPT_RUN_LOG'] = str(promptRunLogPath())
        run_log_handle = openRunLogStream()
        proc = START_EXECUTION_LIFECYCLE.startProcess(f'Launch:{display_name}', cmd, cwd=str(Path(target).resolve().parent), stdout=run_log_handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env)
        setattr(proc, '_prompt_run_log_handle', run_log_handle)
        return True
    except Exception as error:
        captureException(None, source='start.py', context='except@12537')
        print(f'[ERROR] Failed launching {display_name}: {error}', flush=True)
        return False

def readSingleConsoleKey(timeout_seconds: float = 1.0) -> str:
    deadline = time.time() + max(0.05, float(timeout_seconds or 0.0))
    if os.name == 'nt':
        try:
            import msvcrt
            while time.time() < deadline:
                if msvcrt.kbhit():
                    raw = msvcrt.getch()
                    if raw in (b'\x00', b'\xe0'):
                        try:
                            msvcrt.getch()
                        except Exception as error:
                            captureException(None, source='start.py', context='except@12552')
                            print(f"[WARN:swallowed-exception] start.py:11102 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                            pass
                        continue
                    try:
                        return raw.decode('utf-8', 'replace')
                    except Exception as error:
                        captureException(None, source='start.py', context='except@12558')
                        print(f"[WARN:swallowed-exception] start.py:11107 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                        return EMPTY_STRING
                time.sleep(0.05)
        except Exception as error:
            captureException(None, source='start.py', context='except@12562')
            print(f"[WARN:swallowed-exception] start.py:11110 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return EMPTY_STRING
        return EMPTY_STRING
    try:
        import select
        import termios
        import tty
        if not sys.stdin.isatty():
            time.sleep(timeout_seconds)
            return EMPTY_STRING
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            ready, _, _ = select.select([sys.stdin], [], [], max(0.05, float(timeout_seconds or 0.0)))
            if ready:
                return sys.stdin.read(1)
            return EMPTY_STRING
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception as error:
        captureException(None, source='start.py', context='except@12583')
        print(f"[WARN:swallowed-exception] start.py:11128 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        time.sleep(timeout_seconds)
        return EMPTY_STRING

def runDeployMonitor(argv=None) -> int:
    root = Path.cwd()
    print(renderConsoleBox(startVersionText(), [f'Root: {root}', 'Mode: zip deploy monitor', 'Press R to rerun, Q/Esc/any other key to quit.'], color_code='\x1b[90m'), flush=True)
    frame_index = 0
    deployment_floor = None
    candidate = resolveLaunchScript(root)
    while True:  # noqa: badcode reviewed detector-style finding
        deployed = expandZipsInDirectory(root)
        if deployed is not None:
            deployment_floor = deployed
            candidate = resolveLaunchScript(root, deployment_floor)
            if candidate:
                launchTargetScript(candidate, argv=argv, manual=False)
        else:
            candidate = resolveLaunchScript(root, deployment_floor) if deployment_floor is not None else resolveLaunchScript(root)
        sys.stdout.write('\r' + deployMonitorHeartbeatLine(frame_index, candidate))
        sys.stdout.flush()
        key = readSingleConsoleKey(1.0)
        frame_index += 1
        if not key:
            continue
        low = key.lower()
        sys.stdout.write('\n')
        sys.stdout.flush()
        if low == 'r':
            if candidate:
                launchTargetScript(candidate, argv=argv, manual=True)
            else:
                print('[WARN] No Python entrypoint found to run.', flush=True)
            continue
        break
    print('\nMonitor stopped. Press any key to exit.', flush=True)
    if os.name == 'nt':
        readSingleConsoleKey(30.0)
    return 0

def runDeployOnce(argv=None) -> int:
    root = Path.cwd()
    deployed = expandZipsInDirectory(root)
    candidate = resolveLaunchScript(root, deployed) if deployed is not None else resolveLaunchScript(root)
    if deployed is not None and candidate:
        launchTargetScript(candidate, argv=argv, manual=False)
    print(renderConsoleBox('Deploy Once', [f'Root: {root}', f'Expanded zips: {bool(deployed)}', f'Candidate: {Path(candidate).name if candidate else "none"}'], color_code='\x1b[90m'), flush=True)
    return 0


def proxyDaemonRequested(argv=None) -> bool:
    return bool(readCliOption(argv, PROXY_DAEMON_FLAGS, takesValue=False, knownAliases=START_VALID_CLI_ALIASES).get('present'))


def proxyBindValue(argv=None) -> str:
    state = readCliOption(argv, PROXY_BIND_FLAGS, takesValue=True, knownAliases=START_VALID_CLI_ALIASES)
    return str(state.get('value') or EMPTY_STRING).strip()


def gitRequested(argv=None) -> bool:
    return bool(readCliOption(argv, GIT_FLAGS, takesValue=False, knownAliases=START_VALID_CLI_ALIASES).get('present'))


def extractGitCliArgs(argv=None) -> list[str]:
    tokens = [str(token or EMPTY_STRING).strip() for token in list(argv or []) if str(token or EMPTY_STRING).strip()]
    for index, token in enumerate(tokens):
        lowered = token.lower()
        if lowered in GIT_FLAGS:
            return tokens[index + 1:]
        for alias in GIT_FLAGS:
            prefix = alias + '='
            if lowered.startswith(prefix):
                payload = token[len(alias) + 1:].strip()
                return shlex.split(payload) if payload else []
    return []



def appendToolDirectoryToPath(executable_path: str | Path, env: dict | None = None) -> str:
    target_env = env if isinstance(env, dict) else os.environ
    path_text = str(target_env.get('PATH', EMPTY_STRING) or EMPTY_STRING)
    exe_path = Path(executable_path or EMPTY_STRING)
    parent = exe_path.parent if exe_path.name else exe_path
    parent_text = str(parent or EMPTY_STRING).strip()
    if not parent_text:
        return path_text
    current_parts = [part for part in path_text.split(os.pathsep) if str(part or EMPTY_STRING).strip()]
    normalized_existing = {str(Path(part)).lower() for part in current_parts if str(part or EMPTY_STRING).strip()}
    if str(Path(parent_text)).lower() in normalized_existing:
        return path_text
    new_parts = [parent_text, *current_parts]
    new_value = os.pathsep.join(new_parts)
    target_env['PATH'] = new_value
    return new_value


def toolExecutableCandidates(tool_name: str) -> list[Path]:
    name = str(tool_name or EMPTY_STRING).strip().lower()
    candidates: list[Path] = []
    if name == 'git':
        env_candidates = [
            os.environ.get('TRIO_GIT_EXE'),
            os.environ.get('GIT_EXE'),
            os.environ.get('GIT_PYTHON_GIT_EXECUTABLE'),
        ]
    elif name == 'gh':
        env_candidates = [
            os.environ.get('TRIO_GH_EXE'),
            os.environ.get('GH_EXE'),
        ]
    else:
        env_candidates = []
    for raw in env_candidates:
        text = str(raw or EMPTY_STRING).strip()
        if text:
            candidates.append(Path(text))
    if platform.system().lower().startswith('win'):
        roots = [
            Path(os.environ.get('ProgramFiles', 'C:/Program Files')),
            Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')),
            Path.home() / 'AppData' / 'Local' / 'Programs',
            Path.home() / 'AppData' / 'Local' / 'GitHubCLI',
        ]
        names = {
            'git': (
                ('Git', 'cmd', 'git.exe'),
                ('Git', 'bin', 'git.exe'),
                ('GitHub Desktop', 'app-*', 'resources', 'app', 'git', 'cmd', 'git.exe'),
            ),
            'gh': (
                ('GitHub CLI', 'gh.exe'),
                ('GitHubCLI', 'gh.exe'),
                ('GitHub CLI', 'bin', 'gh.exe'),
            ),
        }.get(name, ())
        for root in roots:
            for rel in names:
                if '*' in ''.join(rel):
                    try:
                        pattern = str(root.joinpath(*rel))
                        for found in map(Path, glob.glob(pattern)):
                            candidates.append(found)
                    except Exception as error:
                        captureException(None, source='start.py', context='except@12726')
                        print(f"[WARN:swallowed-exception] start.py:11270 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                        pass
                else:
                    candidates.append(root.joinpath(*rel))
    else:
        unix_names = {
            'git': ('/usr/bin/git', '/usr/local/bin/git', '/opt/homebrew/bin/git'),
            'gh': ('/usr/bin/gh', '/usr/local/bin/gh', '/opt/homebrew/bin/gh'),
        }.get(name, ())
        candidates.extend(Path(item) for item in unix_names)
    unique: list[Path] = []
    seen = set()
    for candidate in candidates:
        text = str(candidate or EMPTY_STRING).strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            unique.append(Path(text))
    return unique


def locateToolExecutable(tool_name: str, install_if_missing: bool = False) -> tuple[str, str]:
    name = str(tool_name or EMPTY_STRING).strip().lower()
    if not name:
        return EMPTY_STRING, '[TrioGit] Missing tool name.'
    found = shutil.which(name)
    if found:
        appendToolDirectoryToPath(found)
        os.environ[f'TRIO_{name.upper()}_EXE'] = str(found)
        if name == 'git':
            os.environ['GIT_PYTHON_GIT_EXECUTABLE'] = str(found)
        return str(found), f'[TrioGit] {name} found on PATH: {found}'
    for candidate in toolExecutableCandidates(name):
        try:
            if candidate.exists():
                appendToolDirectoryToPath(candidate)
                os.environ[f'TRIO_{name.upper()}_EXE'] = str(candidate)
                if name == 'git':
                    os.environ['GIT_PYTHON_GIT_EXECUTABLE'] = str(candidate)
                return str(candidate), f'[TrioGit] {name} found at {candidate}'
        except Exception as error:
            captureException(None, source='start.py', context='except@12766')
            print(f"[WARN:swallowed-exception] start.py:11309 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
    if not install_if_missing:
        return EMPTY_STRING, f'[TrioGit] {name} executable not found.'
    install_output = attemptToolInstall(name)
    found = shutil.which(name)
    if found:
        appendToolDirectoryToPath(found)
        os.environ[f'TRIO_{name.upper()}_EXE'] = str(found)
        if name == 'git':
            os.environ['GIT_PYTHON_GIT_EXECUTABLE'] = str(found)
        return str(found), install_output + f'\n[TrioGit] {name} installed: {found}'
    for candidate in toolExecutableCandidates(name):
        try:
            if candidate.exists():
                appendToolDirectoryToPath(candidate)
                os.environ[f'TRIO_{name.upper()}_EXE'] = str(candidate)
                if name == 'git':
                    os.environ['GIT_PYTHON_GIT_EXECUTABLE'] = str(candidate)
                return str(candidate), install_output + f'\n[TrioGit] {name} installed: {candidate}'
        except Exception as error:
            captureException(None, source='start.py', context='except@12787')
            print(f"[WARN:swallowed-exception] start.py:11329 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
    return EMPTY_STRING, install_output + f'\n[TrioGit] {name} executable not found after install attempt.'


def attemptToolInstall(tool_name: str) -> str:
    name = str(tool_name or EMPTY_STRING).strip().lower()
    if not name:
        return '[TrioGit] Missing tool name for install.'
    commands: list[list[str]] = []
    notes: list[str] = []
    python_exe = str(sys.executable or EMPTY_STRING).strip()
    if platform.system().lower().startswith('win'):
        winget = shutil.which('winget')
        choco = shutil.which('choco')
        if winget:
            if name == 'git':
                commands.append([winget, 'install', '--id', 'Git.Git', '-e', '--accept-package-agreements', '--accept-source-agreements'])
            elif name == 'gh':
                commands.append([winget, 'install', '--id', 'GitHub.cli', '-e', '--accept-package-agreements', '--accept-source-agreements'])
        if choco:
            if name == 'git':
                commands.append([choco, 'install', 'git', '-y'])
            elif name == 'gh':
                commands.append([choco, 'install', 'gh', '-y'])
    else:
        if python_exe:
            commands.append([python_exe, '-m', 'pip', 'install', '--upgrade', 'pip'])
            if name == 'git':
                commands.append([python_exe, '-m', 'pip', 'install', '--upgrade', 'GitPython', 'dulwich'])
                notes.append('[TrioGit] Unix auto-install now uses pip helpers instead of apt/dnf/yum/brew.')
                notes.append('[TrioGit] pip installs Python-side Git support, but it does not always provide the native git executable.')
                notes.append('[TrioGit] After the pip step, PATH/common locations are probed again for a real git binary.')
            elif name == 'gh':
                commands.append([python_exe, '-m', 'pip', 'install', '--upgrade', 'ghapi'])
                notes.append('[TrioGit] Unix auto-install now uses pip helpers instead of apt/dnf/yum/brew.')
                notes.append('[TrioGit] pip installs ghapi for GitHub API access, but it does not always provide the native gh executable.')
                notes.append('[TrioGit] After the pip step, PATH/common locations are probed again for a real gh binary.')
    commands = [cmd for cmd in commands if cmd]
    if not commands:
        return f'[TrioGit] No automatic installer path is configured for {name} on this platform.'
    lines = [f'[TrioGit] Attempting to install {name}.']
    lines.extend(note for note in notes if str(note or EMPTY_STRING).strip())
    for command in commands:
        ok, stdout_text, stderr_text, exit_code = StartDependencyRegistry.runCommandDetailed(command, label=f'{name} install', timeout=1800)
        rendered = ' '.join(shlex.quote(str(part or EMPTY_STRING)) for part in command)
        lines.append(f'[TrioGit] install command={rendered}')
        lines.append(f'[TrioGit] install exit={exit_code}')
        if stdout_text:
            lines.extend(['[stdout]', str(stdout_text).strip()])
        if stderr_text:
            lines.extend(['[stderr]', str(stderr_text).strip()])
        if ok and name != 'git':
            break
    return '\n'.join(part for part in lines if str(part or EMPTY_STRING).strip())


def debuggerGitConfigValue(key: str, repo_root: str | Path = BASE_DIR) -> str:
    config_key = str(key or EMPTY_STRING).strip()
    if not config_key:
        return EMPTY_STRING
    git_exe, _ = locateToolExecutable('git', install_if_missing=False)
    if not git_exe:
        return EMPTY_STRING
    try:
        completed = startLifecycleRunCommand([git_exe, 'config', '--get', config_key], cwd=str(Path(repo_root or BASE_DIR)), capture_output=True, text=True)
        if int(getattr(completed, 'returncode', 1)) != 0:
            return EMPTY_STRING
        return str(completed.stdout or EMPTY_STRING).strip()
    except Exception as error:
        captureException(None, source='start.py', context='except@12857')
        print(f"[WARN:swallowed-exception] start.py:11398 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        return EMPTY_STRING


def gitHelpText(repo_root: str | Path = BASE_DIR) -> str:
    root = Path(repo_root or BASE_DIR)
    lines = [
        '[TrioGit] Git tools for the current TrioDesktop branch.',
        f'Repo root: {root}',
        '',
        'Primary commands:',
        '  gs / status                git status --short --branch',
        '  ga / add [paths...]        git add . (default) or the provided paths',
        '  gc / commit -m "msg"       git commit with a message',
        '  gb / branch                git branch --all',
        '  gd / bd / diff             git diff --stat and git diff',
        '  gp / gpush / push          git push',
        '  gu / gpull / pull          git pull',
        '  gr / remote                git remote -v',
        '  gl / log                   git log --oneline --decorate -n 20',
        '  gf / fetch                 git fetch --all --prune',
        '  gsw / switch <name>        git switch <name>',
        '  gco / checkout <name>      git checkout <name>',
        '  init                       git init',
        '  gi / gitignore             print the .gitignore path; debugger mode opens an editor',
        '',
        'GitHub / auth:',
        '  auth                       open the Qt auth modal in debugger mode',
        '  auth status                gh auth status',
        '  auth logout                gh auth logout',
        '  auth setup-git             gh auth setup-git',
        '  gh <args...>               pass through directly to GitHub CLI',
        '',
        'Help:',
        '  gm / man / help            show this help',
        '  version / ver / v          git --version',
        '',
        'Anything else is passed through to git or gh directly.',
    ]
    return '\n'.join(lines)


def runGitProcess(args: list[str], cwd: str | Path = BASE_DIR) -> str:
    repo_root = Path(cwd or BASE_DIR)
    git_exe, detect_text = locateToolExecutable('git', install_if_missing=True)
    if not git_exe:
        return detect_text
    command = [git_exe, *list(args or [])]
    completed = startLifecycleRunCommand(command, cwd=str(repo_root), capture_output=True, text=True)
    stdout = str(completed.stdout or EMPTY_STRING).strip()
    stderr = str(completed.stderr or EMPTY_STRING).strip()
    lines = [detect_text, f"[TrioGit] cwd={repo_root}", f"[TrioGit] command={' '.join(shlex.quote(part) for part in command)}", f"[TrioGit] exit={completed.returncode}"]
    if stdout:
        lines.extend(['', '[stdout]', stdout])
    if stderr:
        lines.extend(['', '[stderr]', stderr])
    return '\n'.join(part for part in lines if str(part or EMPTY_STRING).strip()).strip()


def runGhProcess(args: list[str], cwd: str | Path = BASE_DIR, stdin_text: str = EMPTY_STRING) -> str:
    repo_root = Path(cwd or BASE_DIR)
    gh_exe, detect_text = locateToolExecutable('gh', install_if_missing=True)
    if not gh_exe:
        return detect_text
    command = [gh_exe, *list(args or [])]
    completed = startLifecycleRunCommand(command, cwd=str(repo_root), input=str(stdin_text or EMPTY_STRING), capture_output=True, text=True)
    stdout = str(completed.stdout or EMPTY_STRING).strip()
    stderr = str(completed.stderr or EMPTY_STRING).strip()
    lines = [detect_text, f"[TrioGit] cwd={repo_root}", f"[TrioGit] command={' '.join(shlex.quote(part) for part in command)}", f"[TrioGit] exit={completed.returncode}"]
    if stdout:
        lines.extend(['', '[stdout]', stdout])
    if stderr:
        lines.extend(['', '[stderr]', stderr])
    return '\n'.join(part for part in lines if str(part or EMPTY_STRING).strip()).strip()


def chooseGitCredentialHelper() -> str:
    for helper in ('manager-core', 'manager', 'store'):
        if helper == 'store':
            return helper
        test = shutil.which(f'git-credential-{helper}')
        if test:
            return helper
    return 'store'


def applyGitAuthSettings(payload: dict[str, Any], cwd: str | Path = BASE_DIR, interactive: bool = False) -> str:
    repo_root = Path(cwd or BASE_DIR)
    values = dict(payload or {})
    author_name = str(values.get('author_name', EMPTY_STRING) or EMPTY_STRING).strip()
    author_email = str(values.get('author_email', EMPTY_STRING) or EMPTY_STRING).strip()
    username = str(values.get('username', EMPTY_STRING) or EMPTY_STRING).strip()
    password = str(values.get('password', EMPTY_STRING) or EMPTY_STRING)
    host = str(values.get('host', 'github.com') or 'github.com').strip() or 'github.com'
    setup_git = bool(values.get('setup_git'))
    lines: list[str] = []
    if author_name:
        lines.append(runGitProcess(['config', 'user.name', author_name], cwd=repo_root))
    if author_email:
        lines.append(runGitProcess(['config', 'user.email', author_email], cwd=repo_root))
    if not username and not password:
        if lines:
            return '\n\n'.join(lines)
        return '[TrioGit] Auth needs at least a username and password/token.'
    helper = chooseGitCredentialHelper()
    lines.append(runGitProcess(['config', 'credential.helper', helper], cwd=repo_root))
    if username and password:
        approve_payload = f'protocol=https\nhost={host}\nusername={username}\npassword={password}\n\n'
        git_exe, detect_text = locateToolExecutable('git', install_if_missing=True)
        if git_exe:
            completed = startLifecycleRunCommand([git_exe, 'credential', 'approve'], cwd=str(repo_root), input=approve_payload, capture_output=True, text=True)
            block = [detect_text, f"[TrioGit] credential approve host={host}", f"[TrioGit] exit={completed.returncode}"]
            if str(completed.stdout or EMPTY_STRING).strip():
                block.extend(['[stdout]', str(completed.stdout).strip()])
            if str(completed.stderr or EMPTY_STRING).strip():
                block.extend(['[stderr]', str(completed.stderr).strip()])
            lines.append('\n'.join(block))
    if password:
        gh_path, _ = locateToolExecutable('gh', install_if_missing=True)
        if gh_path:
            login_output = runGhProcess(['auth', 'login', '--hostname', host, '--with-token'], cwd=repo_root, stdin_text=password + '\n')
            lines.append(login_output)
            if setup_git:
                lines.append(runGhProcess(['auth', 'setup-git'], cwd=repo_root))
        else:
            lines.append('[TrioGit] gh CLI not found; git credentials were stored through git credential approve instead.')
    return '\n\n'.join(part for part in lines if str(part or EMPTY_STRING).strip()).strip()


def normalizeGitAlias(primary: str) -> str:
    token = str(primary or EMPTY_STRING).strip().lower()
    mapping = {
        'gs': 'status',
        'ga': 'add',
        'gc': 'commit',
        'gb': 'branch',
        'gd': 'diff',
        'bd': 'diff',
        'gp': 'push',
        'gpush': 'push',
        'gu': 'pull',
        'gpull': 'pull',
        'gr': 'remote',
        'gl': 'log',
        'gf': 'fetch',
        'gco': 'checkout',
        'gsw': 'switch',
        'gm': 'help',
        'man': 'help',
        'gi': 'gitignore',
        'gauth': 'auth',
    }
    return mapping.get(token, token)


def runGitCommand(args: list[str], cwd: str | Path = BASE_DIR, interactive: bool = False) -> str:
    repo_root = Path(cwd or BASE_DIR)
    normalized = [str(part or EMPTY_STRING).strip() for part in list(args or []) if str(part or EMPTY_STRING).strip()]
    if not normalized:
        return gitHelpText(repo_root)
    primary = normalizeGitAlias(normalized[0])
    tail = normalized[1:]
    if primary in {'help', 'h', '?'}:
        return gitHelpText(repo_root)
    if primary in {'version', 'ver', 'v'}:
        return runGitProcess(['--version'], cwd=repo_root)
    if primary == 'gitignore':
        return f"[TrioGit] .gitignore path: {repo_root / '.gitignore'}"
    if primary == 'status':
        return runGitProcess(['status', '--short', '--branch'], cwd=repo_root)
    if primary == 'branch':
        return runGitProcess(['branch', '--all'], cwd=repo_root)
    if primary == 'log':
        return runGitProcess(['log', '--oneline', '--decorate', '-n', '20'], cwd=repo_root)
    if primary == 'diff':
        stat_text = runGitProcess(['diff', '--stat'], cwd=repo_root)
        full_text = runGitProcess(['diff'], cwd=repo_root)
        return stat_text + '\n\n' + full_text
    if primary == 'add':
        return runGitProcess(['add', *(tail or ['.'])], cwd=repo_root)
    if primary == 'commit':
        commit_args = ['commit', *tail]
        if '-m' not in commit_args and '--message' not in commit_args:
            if not interactive:
                return '[TrioGit] commit needs a message. Use: --git commit -m "message"'
            try:
                message = input('[TrioGit:commit message] > ').strip()
            except Exception as error:
                captureException(None, source='start.py', context='except@13045')
                print(f"[WARN:swallowed-exception] start.py:11585 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                message = EMPTY_STRING
            if not message:
                return '[TrioGit] Commit cancelled.'
            commit_args = ['commit', '-m', message]
        return runGitProcess(commit_args, cwd=repo_root)
    if primary == 'fetch':
        return runGitProcess(['fetch', *(tail or ['--all', '--prune'])], cwd=repo_root)
    if primary == 'remote':
        return runGitProcess(['remote', *(tail or ['-v'])], cwd=repo_root)
    if primary == 'gh':
        return runGhProcess(tail or ['auth', 'status'], cwd=repo_root)
    if primary == 'auth':
        if not tail:
            return '[TrioGit] auth opens the Qt modal in debugger mode. CLI usage: --git auth login --username NAME --password TOKEN [--host github.com] [--author-name NAME] [--author-email EMAIL]'
        sub = normalizeGitAlias(tail[0])
        tail_rest = tail[1:]
        if sub == 'status':
            return runGhProcess(['auth', 'status'], cwd=repo_root)
        if sub == 'logout':
            host = 'github.com'
            if '--host' in tail_rest:
                idx = tail_rest.index('--host')
                if idx + 1 < len(tail_rest):
                    host = str(tail_rest[idx + 1] or host).strip() or host
            return runGhProcess(['auth', 'logout', '--hostname', host], cwd=repo_root)
        if sub in {'setup-git', 'setupgit'}:
            return runGhProcess(['auth', 'setup-git'], cwd=repo_root)
        if sub in {'login', 'save', 'store'}:
            parsed = {'host': 'github.com', 'setup_git': True}
            index = 0
            while index < len(tail_rest):
                key = str(tail_rest[index] or EMPTY_STRING).strip().lower()
                value = str(tail_rest[index + 1] or EMPTY_STRING).strip() if index + 1 < len(tail_rest) else EMPTY_STRING
                if key in {'--username', '-u'}:
                    parsed['username'] = value
                    index += 2
                    continue
                if key in {'--password', '--token', '-p'}:
                    parsed['password'] = value
                    index += 2
                    continue
                if key == '--host':
                    parsed['host'] = value or 'github.com'
                    index += 2
                    continue
                if key == '--author-name':
                    parsed['author_name'] = value
                    index += 2
                    continue
                if key == '--author-email':
                    parsed['author_email'] = value
                    index += 2
                    continue
                if key == '--no-setup-git':
                    parsed['setup_git'] = False
                    index += 1
                    continue
                index += 1
            return applyGitAuthSettings(parsed, cwd=repo_root, interactive=interactive)
        return runGhProcess(['auth', sub, *tail_rest], cwd=repo_root)
    if primary in {'init', 'push', 'pull', 'checkout', 'switch'}:
        return runGitProcess([primary, *tail], cwd=repo_root)
    return runGitProcess([primary, *tail], cwd=repo_root)

def runGitCli(argv=None) -> int:
    args = extractGitCliArgs(argv)
    print(runGitCommand(args, cwd=BASE_DIR, interactive=False), flush=True)
    return 0


def runProxyDaemon(argv=None) -> int:
    bind_value = proxyBindValue(argv) or str(os.environ.get('TRIO_PROXY_BIND', '127.0.0.1:6666') or '127.0.0.1:6666').strip()
    os.environ['TRIO_PROXY_BIND'] = bind_value
    host = '127.0.0.1'
    port = 6666
    if ':' in bind_value:
        host, port_text = bind_value.rsplit(':', 1)
        host = str(host or '127.0.0.1').strip() or '127.0.0.1'
        try:
            port = int(port_text or 6666)
        except Exception as error:
            captureException(None, source='start.py', context='except@13127')
            print(f"[WARN:swallowed-exception] start.py:11666 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            port = 6666
    class _ProxyOwner:
        def __init__(self):
            self.db = DebuggerDatabase(self)
            self.logPath = str(DEBUG_LOG_PATH)
            self.exceptionPath = str(EXCEPTION_LOG_PATH)
            self.childPid = 0  # noqa: nonconform
        def emit(self, text: str, path: str | None = None):
            print(str(text or EMPTY_STRING), flush=True)
            if path:
                try:
                    with File.tracedOpen(path, 'a', encoding='utf-8', errors='replace') as handle:
                        handle.write(str(text or EMPTY_STRING) + ('\n' if not str(text or EMPTY_STRING).endswith('\n') else EMPTY_STRING))
                except Exception as error:
                    captureException(None, source='start.py', context='except@13142')
                    print(f"[WARN:swallowed-exception] start.py:11680 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    pass
    owner = _ProxyOwner()
    server = TrioTrafficProxyServer(owner, host=host, port=port)
    server.mode = 'daemon'
    server.start()
    print(server.statusText(), flush=True)
    try:
        cast(Any, server.thread).join()  # block-ok proxy-daemon-foreground-join
    except KeyboardInterrupt:
        captureException(None, source='start.py', context='except@13152')
        server.stop()
    return 0

def main() -> int:  # phase-hooks-ok main delegates to startup phase group
    controller = StartLifecycleController(sys.argv[1:])
    try:
        return int(controller.run())
    finally:
        if not bool(getattr(controller, 'skipFinalPycacheCleanup', False)):
            clearPycacheDirectories(BASE_DIR, reason='shutdown')

if __name__ == '__main__':
    _prompt_exit_code = int(main() or 0)
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception as error:
        captureException(None, source='start.py', context='except@13169')
        print(f"[WARN:swallowed-exception] start.py:11706 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        pass
    os._exit(_prompt_exit_code)  # lifecycle-ok: final launcher hard exit after main lifecycle cleanup and stream flush.
