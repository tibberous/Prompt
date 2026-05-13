from __future__ import annotations

import builtins
import datetime
import os
import re
import sqlite3
import sys
import threading  # thread-ok
import traceback
from pathlib import Path
from typing import Any


class DebugLog:
    """Single Prompt debug transport.

    Every important startup/build/fault message should be visible on the screen
    and persisted in root-level debug.log. This class deliberately owns the
    print hook, unhandled exception hook, and dependency-free SQLite exception
    fallback so missing SQLAlchemy/PySide6 does not make diagnostics disappear.
    """

    _installed = False
    _excepthook_installed = False
    _reentrant = False
    _original_print = builtins.print
    _original_excepthook = getattr(sys, 'excepthook', None)
    _debug_log_prepared = False
    _debug_log_write_count = 0


    @staticmethod
    def _emergencyExceptionDbPath() -> Path:
        raw = str(os.environ.get('TRIO_SQLITE_PATH', '') or os.environ.get('PROMPT_DEBUG_DB', '') or '').strip()
        if raw:
            return Path(raw).expanduser()
        root_raw = str(os.environ.get('PROMPT_DEBUG_ROOT', '') or os.environ.get('PROMPT_RUN_LOG_ROOT', '') or '').strip()
        if root_raw:
            return Path(root_raw).expanduser() / 'workspaces' / 'prompt_debugger.sqlite3'
        try:
            return Path(__file__).resolve().parent / 'workspaces' / 'prompt_debugger.sqlite3'
        except BaseException:  # swallow-ok: no stable DB path exists while calculating emergency DB path
            return Path.cwd() / 'workspaces' / 'prompt_debugger.sqlite3'

    @staticmethod
    def saveExceptionFallback(exc: BaseException | None = None, *, source: str = 'Prompt', context: str = '', handled: bool = True, extra: str = '') -> int:  # noqa: nonconform reviewed return contract
        """Best-effort DB persistence for swallowed/emergency handlers.

        This deliberately avoids DebugLog.writeLine(), DebugLog.exception(), and
        runtimeRoot() so a failing logger can still record the original fault in
        the same exceptions table start.py reads.
        """
        try:
            error = exc if exc is not None else sys.exc_info()[1]
            type_name = type(error).__name__ if error is not None else 'UnknownException'
            message = str(error or '')
            tb = getattr(error, '__traceback__', None) if error is not None else sys.exc_info()[2]
            traceback_text = ''.join(traceback.format_exception(type(error), error, tb)) if error is not None else ''.join(traceback.format_stack())
            db_path = DebugLog._emergencyExceptionDbPath()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(db_path)) as conn:  # raw-sql-ok emergency-sqlite-exception-persistence
                conn.execute(  # raw-sql-ok emergency-sqlite-exception-persistence
                    'CREATE TABLE IF NOT EXISTS exceptions ('
                    'id INTEGER PRIMARY KEY AUTOINCREMENT, '
                    'created TEXT, source TEXT, context TEXT, type_name TEXT, message TEXT, '
                    'traceback_text TEXT, source_context TEXT, thread TEXT, pid INTEGER, '
                    'handled INTEGER DEFAULT 1, processed INTEGER DEFAULT 0)'
                )
                cur = conn.execute(  # raw-sql-ok emergency-sqlite-exception-persistence
                    'INSERT INTO exceptions (created, source, context, type_name, message, traceback_text, source_context, thread, pid, handled, processed) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)',
                    (
                        datetime.datetime.now().isoformat(sep=' ', timespec='microseconds'),
                        str(source or 'Prompt'),
                        str(context or ''),
                        type_name,
                        message,
                        traceback_text,
                        str(extra or ''),
                        threading.current_thread().name,
                        int(os.getpid()),
                        1 if handled else 0,
                    ),
                )
                conn.commit()
                return int(cur.lastrowid or 0)
        except BaseException:  # swallow-ok: final DB fallback failed; no further DB surface is possible
            return 0

    @staticmethod
    def runtimeRoot() -> Path:
        raw = str(os.environ.get('PROMPT_DEBUG_ROOT', '') or os.environ.get('PROMPT_RUN_LOG_ROOT', '') or '').strip()
        if raw:
            return Path(raw).expanduser().resolve()
        try:
            if bool(getattr(sys, 'frozen', False)):
                exe_dir = Path(sys.executable).resolve().parent
                if exe_dir.name.lower() == 'dist':
                    return exe_dir.parent
                return exe_dir
            return Path(__file__).resolve().parent
        except Exception as error:
            DebugLog.saveExceptionFallback(error, source='DebugLog.py', context='runtimeRoot', handled=True)
            return Path.cwd().resolve()

    @staticmethod
    def debugLogPath() -> Path:
        raw = str(os.environ.get('PROMPT_DEBUG_LOG', '') or '').strip()
        if raw:
            return Path(raw).expanduser().resolve()
        return DebugLog.runtimeRoot() / 'debug.log'

    @staticmethod
    def defaultDatabasePath() -> Path:
        raw = str(os.environ.get('TRIO_SQLITE_PATH', '') or os.environ.get('PROMPT_DEBUG_DB', '') or '').strip()
        if raw:
            return Path(raw).expanduser().resolve()
        return DebugLog.runtimeRoot() / 'workspaces' / 'prompt_debugger.sqlite3'

    @staticmethod
    def _stamp() -> str:
        return datetime.datetime.now().isoformat(sep=' ', timespec='microseconds')

    @staticmethod
    def _debugLogLimitBytes() -> int:
        raw = str(os.environ.get('PROMPT_DEBUG_LOG_MAX_MB', '') or '').strip()
        try:
            mb = int(raw) if raw else 25
        except ValueError:
            mb = 25
        return max(1, mb) * 1024 * 1024

    @staticmethod
    def _debugLogKeepBytes() -> int:
        raw = str(os.environ.get('PROMPT_DEBUG_LOG_KEEP_MB', '') or '').strip()
        try:
            mb = int(raw) if raw else 5
        except ValueError:
            mb = 5
        return max(1, mb) * 1024 * 1024

    @staticmethod
    def trimDebugLogIfNeeded(*, force: bool = False) -> None:
        """Keep debug.log useful without letting release builds create 80 MB+ logs.

        The handbook rule is not "delete the evidence"; it is "preserve the
        evidence in a form the developer can actually read."  This keeps the
        newest diagnostic tail and writes a marker explaining exactly what was
        trimmed. Set PROMPT_DEBUG_LOG_MAX_MB=0 only by also setting
        PROMPT_APPEND_DEBUG_LOG=1 when intentionally preserving a giant log.
        """
        try:
            if str(os.environ.get('PROMPT_DISABLE_DEBUG_LOG_TRIM', '') or '').strip().lower() in {'1', 'true', 'yes', 'on'}:
                return
            path = DebugLog.debugLogPath()
            if not path.exists() or not path.is_file():
                return
            limit = DebugLog._debugLogLimitBytes()
            if limit <= 0:
                return
            size = path.stat().st_size
            if size <= limit and not force:
                return
            keep = min(DebugLog._debugLogKeepBytes(), max(1024, limit))
            with builtins.open(path, 'rb') as handle:  # file-io-ok: DebugLog owns log compaction.
                if size > keep:
                    handle.seek(-keep, os.SEEK_END)
                data = handle.read()
            marker = (
                f'{DebugLog._stamp()} [LOG-TRIM] pid={os.getpid()} source=DebugLog.py '
                f'debug.log exceeded {limit} bytes; kept newest {len(data)} of {size} bytes. '
                'Set PROMPT_DISABLE_DEBUG_LOG_TRIM=1 to disable.\n'
            ).encode('utf-8', errors='replace')
            with builtins.open(path, 'wb') as handle:  # file-io-ok: DebugLog owns log compaction.
                handle.write(marker)
                handle.write(data)
        except Exception as error:
            DebugLog.saveExceptionFallback(error, source='DebugLog.py', context='trimDebugLogIfNeeded', handled=True)
            return

    @staticmethod
    def prepareDebugLog() -> None:
        """Truncate debug.log once at the start of a launcher tree.

        The first process that installs DebugLog zeros debug.log before the
        first write, then marks the environment so child builder processes
        append to the same fresh file instead of wiping the parent context. Set
        PROMPT_APPEND_DEBUG_LOG=1 only when intentionally preserving old logs.
        """
        if DebugLog._debug_log_prepared:
            return
        DebugLog._debug_log_prepared = True
        if str(os.environ.get('PROMPT_APPEND_DEBUG_LOG', '') or '').strip().lower() in {'1', 'true', 'yes', 'on'}:
            return
        if str(os.environ.get('PROMPT_DEBUG_LOG_TRUNCATED', '') or '').strip():
            return
        try:
            path = DebugLog.debugLogPath()
            path.parent.mkdir(parents=True, exist_ok=True)
            with builtins.open(path, 'w', encoding='utf-8', errors='replace') as handle:  # file-io-ok: DebugLog owns debug.log lifecycle.
                handle.write('')
            os.environ['PROMPT_DEBUG_LOG_TRUNCATED'] = '1'
        except Exception as error:
            # Diagnostics must never crash before diagnostics exist.
            DebugLog.saveExceptionFallback(error, source='DebugLog.py', context='prepareDebugLog', handled=True)
            return

    @staticmethod
    def _writeRaw(line: str) -> None:
        DebugLog.prepareDebugLog()
        path = DebugLog.debugLogPath()
        path.parent.mkdir(parents=True, exist_ok=True)
        with builtins.open(path, 'a', encoding='utf-8', errors='replace') as handle:  # file-io-ok: DebugLog is the central low-level transport.
            handle.write(str(line).rstrip('\n') + '\n')
        DebugLog._debug_log_write_count += 1
        if DebugLog._debug_log_write_count % 200 == 0:
            DebugLog.trimDebugLogIfNeeded()

    @staticmethod
    def visibleText(value: object) -> str:
        """Return printable content after removing ANSI/control-only noise."""
        try:
            text = str(value if value is not None else '')
            text = re.sub(r'\x1b\][^\x07]*(?:\x07|\x1b\\)', '', text)
            text = re.sub(r'\x1b\[[0-9;?]*[ -/]*[@-~]', '', text)
            text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
            return text.strip()
        except Exception as error:
            DebugLog.saveExceptionFallback(error, source='DebugLog.py', context='visibleText', handled=True)
            return str(value or '').strip()

    @staticmethod
    def lineLooksVisible(value: object) -> bool:
        return bool(DebugLog.visibleText(value))

    @staticmethod
    def iterVisibleLines(value: object) -> list[str]:
        try:
            payload = str(value if value is not None else '')
        except Exception as error:
            DebugLog.saveExceptionFallback(error, source='DebugLog.py', context='iterVisibleLines', handled=True)
            payload = str(value or '')
        lines: list[str] = []
        for raw in payload.splitlines():
            if DebugLog.lineLooksVisible(raw):
                lines.append(raw.rstrip('\r\n'))
        if not lines and DebugLog.lineLooksVisible(payload):
            lines.append(payload.rstrip('\r\n'))
        return lines

    @staticmethod
    def writeLine(message: object, *, level: str = 'TRACE', source: str = '', stream: str = '') -> None:
        try:
            for raw in DebugLog.iterVisibleLines(message):
                prefix = f'{DebugLog._stamp()} [{str(level or "TRACE").upper()}] pid={os.getpid()} thread={threading.current_thread().name}'
                if source:
                    prefix += f' source={source}'
                if stream:
                    prefix += f' stream={stream}'
                DebugLog._writeRaw(prefix + ' ' + raw.rstrip())
        except Exception as error:
            # Emergency logger must never crash the app.
            DebugLog.saveExceptionFallback(error, source='DebugLog.py', context='writeLine', handled=True)
            return

    @staticmethod
    def trace(message: object, *, level: str = 'TRACE', source: str = '', stream: str = 'stdout', to_screen: bool = True) -> None:
        if not DebugLog.lineLooksVisible(message):
            return
        DebugLog.writeLine(message, level=level, source=source, stream=stream)
        if to_screen:
            try:
                output = sys.stderr if str(stream or '').lower() == 'stderr' or str(level or '').upper() in {'WARN', 'ERROR', 'FATAL'} else sys.stdout
                DebugLog._original_print(f'[{str(level or "TRACE").upper()}:{source or "Prompt"}] {message}', file=output, flush=True)
            except Exception as error:
                DebugLog.saveExceptionFallback(error, source='DebugLog.py', context='trace-screen', handled=True)
                return

    @staticmethod
    def stage(name: str, detail: object = '', *, source: str = 'Prompt', level: str = 'STAGE', stream: str = 'stdout') -> None:
        label = str(name or '').strip() or 'stage'
        suffix = str(detail or '').strip()
        DebugLog.trace(f'{label}' + (f' {suffix}' if suffix else ''), level=level, source=source, stream=stream)

    @staticmethod
    def _streamLabel(file_obj: Any) -> str:
        try:
            if file_obj is sys.stderr:
                return 'stderr'
            if file_obj is sys.stdout or file_obj is None:
                return 'stdout'
            name = str(getattr(file_obj, 'name', '') or '').strip()
            return name or type(file_obj).__name__
        except Exception as error:
            DebugLog.saveExceptionFallback(error, source='DebugLog.py', context='_streamLabel', handled=True)
            return 'stream'

    @staticmethod
    def _classifyPrintLevel(text: object, stream: str) -> str:
        """Classify mirrored print() output without turning every stderr trace into ERROR.

        Qt/PySide and the child relay sometimes write normal diagnostic trace lines
        to stderr. Before this classifier, those lines were duplicated as
        [ERROR] in debug.log, which made routine startup look broken. Real
        exceptions/faults still stay ERROR/FATAL.
        """
        visible = DebugLog.visibleText(text)
        upper = visible.upper()
        if any(marker in upper for marker in ('[FATAL', ' FATAL ', 'FATAL:', '[CRITICAL', 'CRITICAL:')):
            return 'FATAL'
        if any(marker in upper for marker in ('[CAPTURED-EXCEPTION', 'TRACEBACK', '[ERROR', ' ERROR ', 'ERROR:', ':ERROR]', '[FAILED', ' FAILED ', 'FAILED:', '[FAULT', ' FAULT ', 'FAULT:')):
            return 'ERROR'
        if any(marker in upper for marker in ('[WARN', '[WARNING', ' WARNING ', 'WARN:', ':WARN]')):
            return 'WARN'
        if upper.startswith('[TRACE') or '[TRACE:' in upper or upper.startswith('[PROMPT:') or upper.startswith('[STARTUP:') or upper.startswith('[TRIODEBUGGER]'):
            return 'TRACE'
        if upper.startswith('[BUILD') or upper.startswith('[PACKAGER') or upper.startswith('[INSTALLER') or upper.startswith('[STAGE'):
            return 'TRACE'
        return 'STDERR' if stream == 'stderr' else 'PRINT'

    @staticmethod
    def _mirroredPrint(*args: Any, **kwargs: Any) -> None:
        if DebugLog._reentrant:
            DebugLog._original_print(*args, **kwargs)
            return
        DebugLog._reentrant = True
        try:
            sep = str(kwargs.get('sep', ' '))
            end = str(kwargs.get('end', '\n'))
            file_obj = kwargs.get('file', None)
            stream = DebugLog._streamLabel(file_obj)
            text = sep.join(str(arg) for arg in args) + end
            if not DebugLog.lineLooksVisible(text):
                return
            level = DebugLog._classifyPrintLevel(text, stream)
            for raw in DebugLog.iterVisibleLines(text):
                DebugLog.writeLine(raw, level=level, source='print', stream=stream)
            DebugLog._original_print(*args, **kwargs)
        finally:
            DebugLog._reentrant = False

    @staticmethod
    def install(*, source: str = 'Prompt', announce: bool = True) -> None:
        if not DebugLog._installed:
            DebugLog._installed = True
            builtins.print = DebugLog._mirroredPrint  # type: ignore[assignment]
            if announce:
                DebugLog.trace(f'print mirror installed; debug_log={DebugLog.debugLogPath()}', level='DEBUGLOG', source=source)
        if not DebugLog._excepthook_installed:
            DebugLog._excepthook_installed = True
            DebugLog._original_excepthook = getattr(sys, 'excepthook', None)
            def _hook(exc_type: type[BaseException], exc_value: BaseException, exc_tb: Any) -> None:
                DebugLog.exception(exc_value, source=source, context='sys.excepthook', handled=False, tb=exc_tb, save_db=True)
                original = DebugLog._original_excepthook
                if callable(original) and original is not _hook:
                    try:
                        original(exc_type, exc_value, exc_tb)
                    except Exception as error:
                        DebugLog.saveExceptionFallback(error, source=source, context='sys.excepthook:original', handled=True)
            sys.excepthook = _hook

    @staticmethod
    def ensureExceptionTable(db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(db_path)) as conn:  # raw-sql-ok emergency-sqlite-exception-persistence
            conn.execute(  # raw-sql-ok emergency-sqlite-exception-persistence
                'CREATE TABLE IF NOT EXISTS exceptions ('
                'id INTEGER PRIMARY KEY AUTOINCREMENT, '
                'created TEXT, source TEXT, context TEXT, type_name TEXT, message TEXT, '
                'traceback_text TEXT, source_context TEXT, thread TEXT, pid INTEGER, '
                'handled INTEGER DEFAULT 1, processed INTEGER DEFAULT 0)'
            )
            conn.commit()

    @staticmethod
    def saveExceptionRow(*, db_path: Path | None, created: str, source: str, context: str, type_name: str, message: str, traceback_text: str, source_context: str, handled: bool) -> int:  # noqa: nonconform reviewed return contract
        target = Path(db_path) if db_path is not None else DebugLog.defaultDatabasePath()
        DebugLog.ensureExceptionTable(target)
        with sqlite3.connect(str(target)) as conn:  # raw-sql-ok emergency-sqlite-exception-persistence
            cur = conn.execute(  # raw-sql-ok emergency-sqlite-exception-persistence
                'INSERT INTO exceptions (created, source, context, type_name, message, traceback_text, source_context, thread, pid, handled, processed) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)',
                (created, source, context, type_name, message, traceback_text, source_context, threading.current_thread().name, int(os.getpid()), 1 if handled else 0),
            )
            conn.commit()
            return int(cur.lastrowid or 0)

    @staticmethod
    def exception(exc: BaseException | None = None, *, source: str = 'Prompt', context: str = '', handled: bool = True, extra: str = '', tb: Any = None, db_path: Path | None = None, save_db: bool = True) -> int:
        if exc is None:
            exc = sys.exc_info()[1]
        type_name = type(exc).__name__ if exc is not None else 'UnknownException'
        message = str(exc or '')
        trace_tb = tb if tb is not None else (getattr(exc, '__traceback__', None) if exc is not None else sys.exc_info()[2])
        traceback_text = ''.join(traceback.format_exception(type(exc), exc, trace_tb)) if exc is not None else ''.join(traceback.format_stack())
        created = DebugLog._stamp()
        header = f'[CAPTURED-EXCEPTION] source={source} context={context} handled={1 if handled else 0} type={type_name} message={message}'
        DebugLog.writeLine(header, level='ERROR', source=source, stream='stderr')
        if extra:
            DebugLog.writeLine(f'[CAPTURED-EXCEPTION:EXTRA] {extra}', level='ERROR', source=source, stream='stderr')
        for line in traceback_text.rstrip().splitlines():
            DebugLog.writeLine(f'[CAPTURED-EXCEPTION:TRACEBACK] {line}', level='ERROR', source=source, stream='stderr')
        try:
            DebugLog._original_print(header, file=sys.stderr, flush=True)
        except Exception as error:
            DebugLog.saveExceptionFallback(error, source=source, context='DebugLog.exception:screen-print', handled=True, extra=traceback_text)
        row_id = 0
        if save_db:
            try:
                row_id = DebugLog.saveExceptionRow(
                    db_path=db_path,
                    created=created,
                    source=str(source or ''),
                    context=str(context or ''),
                    type_name=type_name,
                    message=message,
                    traceback_text=traceback_text,
                    source_context=str(extra or ''),
                    handled=bool(handled),
                )
                DebugLog.writeLine(f'[CAPTURED-EXCEPTION:DB] row_id={row_id} path={db_path or DebugLog.defaultDatabasePath()}', level='ERROR', source=source, stream='stderr')
            except Exception as db_error:
                DebugLog.writeLine(f'[CAPTURED-EXCEPTION-DB-FAILED] {type(db_error).__name__}: {db_error}', level='ERROR', source=source, stream='stderr')
                DebugLog.saveExceptionFallback(db_error, source=source, context='DebugLog.exception:db-save-failed', handled=True, extra=traceback_text)
        return row_id
