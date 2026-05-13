#!/usr/bin/env python3
"""Frozen Prompt EXE bootstrap.

PyInstaller/Nuitka windowed builds can die before the Qt UI paints and before
stdout/stderr are visible.  This tiny entrypoint runs before prompt_app imports
Qt, points logs back to the project root, and writes fatal startup failures to
root debug.log/run.log/errors.txt.
"""
from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path
import runpy
import sys
import traceback


def _bundle_root() -> Path:
    try:
        if bool(getattr(sys, 'frozen', False)):
            exe_dir = Path(sys.executable).resolve().parent
            if exe_dir.name.lower() == 'dist':
                return exe_dir.parent
            return exe_dir
        return Path(__file__).resolve().parent
    except BaseException:  # swallow-ok: earliest frozen bootstrap cannot fault before logging exists.
        return Path.cwd().resolve()


def _append(path: Path, line: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a', encoding='utf-8', errors='replace') as handle:  # file-io-ok: earliest frozen bootstrap logging before File wrapper import.
            handle.write(str(line).rstrip('\n') + '\n')
    except BaseException:  # swallow-ok: best-effort root file logging before app imports.
        pass


def _install_root_log_env(root: Path) -> None:
    os.environ.setdefault('PROMPT_BUNDLE_ROOT', str(root))
    os.environ.setdefault('PROMPT_RUNTIME_ROOT', str(root))
    os.environ.setdefault('PROMPT_RUN_LOG_ROOT', str(root))
    os.environ.setdefault('PROMPT_DEBUG_ROOT', str(root))
    os.environ.setdefault('PROMPT_DEBUG_LOG', str(root / 'debug.log'))
    os.environ.setdefault('PROMPT_RUN_LOG', str(root / 'run.log'))
    os.environ.setdefault('PROMPT_ERRORS_LOG', str(root / 'errors.log'))
    os.environ.setdefault('PYTHONDONTWRITEBYTECODE', '1')


def _log(root: Path, message: str) -> None:
    stamp = _dt.datetime.now().isoformat(timespec='seconds')
    line = f'{stamp} [FROZEN-ENTRY] {message}'
    _append(root / 'debug.log', line)
    _append(root / 'run.log', line)


def main() -> int:
    root = _bundle_root()
    _install_root_log_env(root)
    _log(root, f'BEGIN root={root} executable={sys.executable} argv={sys.argv!r}')
    try:
        try:
            from DebugLog import DebugLog
            DebugLog.install(source='frozen_prompt_entry.py')
            DebugLog.writeLine(f'Frozen entry started root={root} executable={sys.executable}', source='frozen_prompt_entry.py')
        except BaseException as log_error:  # swallow-ok: bootstrap continues with root file logging fallback.
            _log(root, f'DebugLog install failed but startup will continue: {type(log_error).__name__}: {log_error}')
        smoke_requested = any(str(arg).lower() in {'--frozen-import-smoke', '--import-smoke', '/frozen-import-smoke'} for arg in sys.argv[1:])
        target = root / 'prompt_app.py'
        if not target.exists():
            # In true onefile mode the source file may be unpacked elsewhere.
            target = Path(__file__).resolve().parent / 'prompt_app.py'
        filtered_args = [arg for arg in sys.argv[1:] if str(arg).lower() not in {'--frozen-import-smoke', '--import-smoke', '/frozen-import-smoke'}]
        if smoke_requested or bool(getattr(sys, 'frozen', False)):
            # Static import on purpose: PyInstaller/Nuitka must see prompt_app as
            # a real module dependency.  The previous dynamic import path built
            # Prompt-PyInstaller.exe but the smoke test failed with
            # ModuleNotFoundError: No module named 'prompt_app'.
            import prompt_app as _prompt_app  # noqa: PLC0415
            if smoke_requested:
                _log(root, 'FROZEN-IMPORT-SMOKE OK prompt_app import completed')
                return 0
            sys.argv = ['prompt_app.py', *filtered_args]
            rc = int(_prompt_app.main() or 0)
            _log(root, f'EXIT rc={rc}')
            return rc
        if not target.exists():
            raise FileNotFoundError(f'prompt_app.py not found beside root={root} or entry={Path(__file__).resolve().parent}')
        sys.argv = [str(target), *filtered_args]
        runpy.run_path(str(target), run_name='__main__')
        _log(root, 'EXIT rc=0')
        return 0
    except SystemExit as exc:
        code = int(exc.code or 0) if isinstance(exc.code, int) else 1
        _log(root, f'SystemExit code={code}')
        return code
    except BaseException as exc:
        tb = traceback.format_exc()
        _log(root, f'FATAL {type(exc).__name__}: {exc}')
        _append(root / 'debug.log', tb)
        _append(root / 'run.log', tb)
        _append(root / 'errors.log', f'{_dt.datetime.now().isoformat(timespec="seconds")} [FROZEN-ENTRY:FATAL] {type(exc).__name__}: {exc}\n{tb}')
        try:
            from DebugLog import DebugLog
            DebugLog.exception(exc, source='frozen_prompt_entry.py', context='main', handled=False, extra=f'root={root}', save_db=True)
        except BaseException:  # swallow-ok: fatal exception is already written to root debug/run/errors logs.
            pass
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
