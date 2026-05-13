"""
Prompt product definition:
- Prompt is intended to be a desktop "programming language" / DSL workbench for LLMs, not just a plain prompt editor.
- A Doctype is a rule / response-preference layer. Example directives can include WARN, ASK, INFER, etc.
- Capped directive words are user-definable and can expand into other actions. Example: STOP might mean
  QUIT WORKING, UPDATE SPRINT, and GIVE ZIP. Likewise, ZIP can mean any archive format such as zip,
  tar.gz, RAR, or a general archive handoff depending on the current doctype / workflow definition.
- The app is conceptually split into three columns / surfaces: Generate Prompt, Doctypes, and Workflows
  (the prompt-generator editor).
- The current authoring pipeline is: Markdown -> HTML -> LLM prompts.

In other words, Prompt is meant to help define reusable instruction vocabularies, doctypes, workflows,
and generated prompts so prompt-building can be structured more like writing in a language than typing
one giant ad-hoc paragraph every time.
"""

from __future__ import annotations

# BUILD-MODE-GUI-GUARD v178: build/packaging mode must never construct the Qt app.
# Packagers analyze this file, but accidentally executing it during --build should
# fail loudly in the logs instead of opening a dead blank WebEngine window.
import os as _prompt_build_guard_os
import sys as _prompt_build_guard_sys
if __name__ == '__main__' and str(_prompt_build_guard_os.environ.get('PROMPT_BUILD_MODE', '') or '').strip().lower() in {'1', 'true', 'yes', 'on'}:
    print('[PROMPT_APP:BUILD-GUARD] prompt_app.py was invoked during build mode; refusing to start GUI.', flush=True)
    raise SystemExit(91)


import base64
import configparser
import copy
import datetime
import html as html_lib
import hashlib
import json
import os
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import threading  # thread-ok
import traceback
import time

from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING, cast

from File import File
from DebugLog import DebugLog
from Lifecycle import Lifecycle
DebugLog.install(source='prompt_app.py')
# Audited: 04/29/26 Runtime exception capture surface v163
_PROMPT_EXCEPTION_CAPTURE_PATCH = 'V171_RUN_LOG_PIPELINE_CAPTURE'
_CAPTURE_EXCEPTION_REENTRANT = False

def _capture_exception_log_path() -> Path:
    try:
        helper = globals().get('_prompt_errors_log_path')
        if helper is not None:
            return Path(helper())
    except Exception:  # swallow-ok: emergency exception recorder must never recurse
        pass
    try:
        base = Path(__file__).resolve().parent
    except Exception:  # swallow-ok: emergency exception recorder must never recurse
        base = Path.cwd()
    return base / 'errors.txt'


def _capture_exception_db_path() -> Path:
    raw = str(os.environ.get('TRIO_SQLITE_PATH', '') or '').strip()
    if raw:
        return Path(raw).expanduser()
    try:
        root = Path(__file__).resolve().parent
        return root / 'workspaces' / 'prompt_debugger.sqlite3'
    except Exception:  # swallow-ok: emergency exception recorder must never recurse
        return Path.cwd() / 'prompt_exceptions.sqlite3'


def captureException(exc: BaseException | None = None, *, source: str = 'prompt_app.py', context: str = '', handled: bool = True, extra: str = '') -> int:
    """Trace and persist an exception so the launcher debugger can read it from the exceptions table."""
    global _CAPTURE_EXCEPTION_REENTRANT
    if _CAPTURE_EXCEPTION_REENTRANT:
        return 0
    _CAPTURE_EXCEPTION_REENTRANT = True
    row_id = 0
    try:
        if exc is None:
            exc = sys.exc_info()[1]
        if exc is not None and not isinstance(exc, BaseException):
            exc = RuntimeError(str(exc))
        type_name = type(exc).__name__ if exc is not None else 'UnknownException'
        message = str(exc or '')
        tb = getattr(exc, '__traceback__', None) if exc is not None else sys.exc_info()[2]
        traceback_text = ''.join(traceback.format_exception(type(exc), exc, tb)) if exc is not None else ''.join(traceback.format_stack())
        created = datetime.datetime.now().isoformat(sep=' ', timespec='microseconds')
        source_text = str(source or Path(__file__).name)
        context_text = str(context or '')
        try:
            fallback_row = DebugLog.exception(exc, source=source_text, context=context_text, handled=handled, extra=extra, db_path=_capture_exception_db_path(), save_db=not bool(globals().get('HAS_SQLALCHEMY', False)))
            if fallback_row and not row_id:
                row_id = int(fallback_row)
        except Exception as error:
            DebugLog.saveExceptionFallback(error, source='prompt_app.py', context=f'{context_text}:debuglog-fallback', handled=True, extra=traceback_text)
        try:
            helper = globals().get('_append_prompt_error_log')
            if helper is not None:
                helper(f'{created} [CAPTURED-EXCEPTION] source={source_text} context={context_text} handled={int(bool(handled))} type={type_name} message={message}')
                if extra:
                    helper(f'{created} [CAPTURED-EXCEPTION:EXTRA] {extra}')
                for line in traceback_text.rstrip().splitlines():
                    helper(f'{created} [CAPTURED-EXCEPTION:TRACEBACK] {line}')
            else:
                target = _capture_exception_log_path()
                target.parent.mkdir(parents=True, exist_ok=True)
                with File.tracedOpen(target, 'a', encoding='utf-8', errors='replace') as handle:
                    handle.write(f'{created} [CAPTURED-EXCEPTION] source={source_text} context={context_text} handled={int(bool(handled))} type={type_name} message={message}\n')
                    handle.write(traceback_text.rstrip() + '\n')
        except Exception as error:  # emergency recorder must never recurse; persist fallback row
            DebugLog.saveExceptionFallback(error, source='prompt_app.py', context=f'{context_text}:debuglog-fallback', handled=True, extra=traceback_text)
        try:
            print(f'[CAPTURED-EXCEPTION] source={source_text} context={context_text} type={type_name}: {message}', file=sys.stderr, flush=True)
        except Exception as error:  # emergency recorder must never recurse; persist fallback row
            DebugLog.saveExceptionFallback(error, source='prompt_app.py', context=f'{context_text}:debuglog-fallback', handled=True, extra=traceback_text)
        try:
            if bool(globals().get('HAS_SQLALCHEMY', False)):
                engine_factory = globals().get('create_engine')
                session_factory = globals().get('sessionmaker')
                base_cls = globals().get('PromptOrmBase')
                record_cls = globals().get('DebuggerExceptionOrm')
                if engine_factory is not None and session_factory is not None and base_cls is not None and record_cls is not None:
                    db_path = _capture_exception_db_path()
                    db_path.parent.mkdir(parents=True, exist_ok=True)
                    engine = engine_factory(f'sqlite:///{db_path}', future=True)
                    try:
                        base_cls.metadata.create_all(engine)
                        SessionFactory = session_factory(bind=engine, future=True)
                        with SessionFactory() as session:
                            row = record_cls(created=created, source=source_text, context=context_text, type_name=type_name, message=message, traceback_text=traceback_text, source_context=extra, thread=str(getattr(threading.current_thread(), 'name', '') or ''), pid=int(os.getpid()), handled=1 if handled else 0, processed=0)
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
                DebugLog.saveExceptionFallback(error, source='prompt_app.py', context=f'{context_text}:sqlalchemy-db-fallback-debuglog', handled=True, extra=traceback_text)
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
# Runtime: 0.000s ExceptionCaptureSurface Outcome (defines errors.txt+DB exception capture for debugger) \\greenSUCCESS!

try:
    import data as prompt_embedded_data
    PROMPT_EMBEDDED_DATA_IMPORT_ERROR: BaseException | None = None
except Exception as _prompt_embedded_data_import_error:
    captureException(None, source='prompt_app.py', context='except@40')
    prompt_embedded_data = None
    PROMPT_EMBEDDED_DATA_IMPORT_ERROR = _prompt_embedded_data_import_error



class _MissingSqlAlchemySymbol:
    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f'SQLAlchemy is required for Prompt database access: {SQLALCHEMY_IMPORT_ERROR!r}')

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
String: Any
Text: Any
create_engine: Any
select: Any
sessionmaker: Any
DeclarativeBase: Any
SQLALCHEMY_IMPORT_ERROR: BaseException | None = None

try:
    from sqlalchemy import Column as _SqlColumn, Float as _SqlFloat, Integer as _SqlInteger, String as _SqlString, Text as _SqlText, create_engine as _SqlCreateEngine, select as _SqlSelect
    from sqlalchemy.orm import DeclarativeBase as _SqlDeclarativeBase, sessionmaker as _SqlSessionmaker
    # Nuitka can omit SQLAlchemy's strategy registration modules when they are
    # only imported indirectly.  Import them explicitly before mapper classes
    # are declared so ColumnProperty's default loader strategy is registered.
    import sqlalchemy.orm.strategies as _SqlOrmStrategies  # noqa: F401
    import sqlalchemy.orm.strategy_options as _SqlOrmStrategyOptions  # noqa: F401
    import sqlalchemy.orm.loading as _SqlOrmLoading  # noqa: F401
    import sqlalchemy.orm.context as _SqlOrmContext  # noqa: F401
    Column = cast(Any, _SqlColumn)
    Float = cast(Any, _SqlFloat)
    Integer = cast(Any, _SqlInteger)
    String = cast(Any, _SqlString)
    Text = cast(Any, _SqlText)
    create_engine = cast(Any, _SqlCreateEngine)
    select = cast(Any, _SqlSelect)
    sessionmaker = cast(Any, _SqlSessionmaker)
    DeclarativeBase = cast(Any, _SqlDeclarativeBase)
    HAS_SQLALCHEMY = True
except Exception as error:
    captureException(None, source='prompt_app.py', context='except@90', handled=False, extra='critical dependency import failed: sqlalchemy')
    DebugLog.trace(f'CRITICAL DEPENDENCY MISSING: SQLAlchemy is required before the GUI can open. error={type(error).__name__}: {error}', level='FATAL', source='prompt_app.py', stream='stderr')
    print(f"[WARN:swallowed-exception] prompt_app.py:40 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
    HAS_SQLALCHEMY = False
    SQLALCHEMY_IMPORT_ERROR = error
    _missingSqlAlchemy = _MissingSqlAlchemySymbol()
    Column = Float = Integer = String = Text = create_engine = select = sessionmaker = cast(Any, _missingSqlAlchemy)
    class _FallbackDeclarativeBase:
        metadata: Any = _missingSqlAlchemy
        __table__: Any = _missingSqlAlchemy
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(f'SQLAlchemy is required for Prompt database access: {SQLALCHEMY_IMPORT_ERROR!r}')
    DeclarativeBase = cast(Any, _FallbackDeclarativeBase)


def _prompt_critical_dependency_exit_if_needed() -> None:
    if bool(globals().get('HAS_SQLALCHEMY', False)):
        return
    if __name__ != '__main__':
        return
    message = f'Prompt cannot open because SQLAlchemy is missing. Launch through start.py or install SQLAlchemy. error={SQLALCHEMY_IMPORT_ERROR!r}'
    DebugLog.trace(message, level='FATAL', source='prompt_app.py', stream='stderr')
    captureException(SQLALCHEMY_IMPORT_ERROR, source='prompt_app.py', context='module:missing-sqlalchemy-before-qt-imports', handled=False, extra=message)
    raise SystemExit(2)


_prompt_critical_dependency_exit_if_needed()


EMPTY_STRING = ''





def promptLifecycleRunCommand(args, **kwargs):
    run_ctor = getattr(subprocess, 'run')
    return run_ctor(args, **kwargs)


if HAS_SQLALCHEMY or TYPE_CHECKING:
    class PromptOrmBase(DeclarativeBase):
        pass

    class PromptSettingOrm(PromptOrmBase):
        __tablename__ = 'settings'
        key = Column(String, primary_key=True)
        value = Column(Text, nullable=False)

    class DebuggerHeartbeatOrm(PromptOrmBase):
        __tablename__ = 'heartbeat'
        id = Column(Integer, primary_key=True, autoincrement=True)
        created = Column(Text)
        heartbeat_microtime = Column(Float)
        source = Column(Text)
        event_kind = Column(Text)
        reason = Column(Text)
        caller = Column(Text)
        phase = Column(Text)
        pid = Column(Integer)
        stack_trace = Column(Text)
        var_dump = Column(Text)
        process_snapshot = Column(Text)
        exec = Column(Text)
        exec_is_file = Column(Integer, default=0)
        cron = Column(Text)
        cron_is_file = Column(Integer, default=0)
        cron_interval_seconds = Column(Float, default=0.0)
        processed = Column(Integer, default=0)

    class DebuggerExceptionOrm(PromptOrmBase):
        __tablename__ = 'exceptions'
        id = Column(Integer, primary_key=True, autoincrement=True)
        created = Column(Text)
        source = Column(Text)
        context = Column(Text)
        type_name = Column("type_name", Text)
        message = Column(Text)
        traceback_text = Column("traceback_text", Text)
        source_context = Column("source_context", Text)
        thread = Column(Text)
        pid = Column(Integer)
        handled = Column(Integer, default=1)
        processed = Column(Integer, default=0)

    class PromptProcessRecordOrm(PromptOrmBase):
        __tablename__ = 'processes'
        id = Column(Integer, primary_key=True, autoincrement=True)
        created = Column(Text)
        updated = Column(Text)
        source = Column(Text)
        phase_key = Column(Text)
        phase_name = Column(Text)
        process_name = Column(Text)
        kind = Column(Text)
        pid = Column(Integer)
        status = Column(Text)
        started_at = Column(Float)
        ended_at = Column(Float)
        ttl_seconds = Column(Float)
        exit_code = Column(Integer)
        error_type = Column(Text)
        error_message = Column(Text)
        traceback_text = Column(Text)
        fault_reason = Column(Text)
        command = Column(Text)
        metadataText = Column("metadata", Text)
        processed = Column(Integer, default=0)
else:
    PromptOrmBase = cast(Any, None)
    PromptSettingOrm = cast(Any, None)
    DebuggerHeartbeatOrm = cast(Any, None)
    DebuggerExceptionOrm = cast(Any, None)
    PromptProcessRecordOrm = cast(Any, None)


class PromptSqlAlchemyStore:
    def __init__(self, path: Path):
        if not HAS_SQLALCHEMY:
            raise RuntimeError(f'SQLAlchemy is required for Prompt database access: {SQLALCHEMY_IMPORT_ERROR!r}')
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f'sqlite:///{self.path}', future=True)  # noqa: nonconform
        self.SessionFactory = sessionmaker(bind=self.engine, future=True)  # noqa: nonconform
        PromptOrmBase.metadata.create_all(self.engine)

    def session(self):
        return self.SessionFactory()

    def upsert_setting(self, key: str, value: str) -> None:
        with self.session() as session:
            row = session.get(PromptSettingOrm, str(key))
            if row is None:
                row = PromptSettingOrm(key=str(key), value=str(value))
                session.add(row)
            else:
                row.value = str(value)
            session.commit()

    def get_setting(self, key: str, default: str = EMPTY_STRING) -> str:  # noqa: nonconform reviewed return contract
        with self.session() as session:
            row = session.get(PromptSettingOrm, str(key))
            return str(default if row is None else row.value)

    def settings_rows(self, keys: list[str] | None = None) -> list[tuple[str, str]]:  # noqa: nonconform reviewed return contract
        with self.session() as session:
            stmt = select(PromptSettingOrm)
            if keys:
                stmt = stmt.where(PromptSettingOrm.key.in_([str(key) for key in keys]))
            stmt = stmt.order_by(PromptSettingOrm.key)
            return [(str(row.key), str(row.value)) for row in session.scalars(stmt).all()]

    def insert_heartbeat(self, **values) -> None:
        allowed = {
            'created', 'heartbeat_microtime', 'source', 'event_kind', 'reason', 'caller', 'phase', 'pid',
            'stack_trace', 'var_dump', 'process_snapshot', 'exec', 'exec_is_file', 'cron', 'cron_is_file',
            'cron_interval_seconds', 'processed'
        }
        payload = {key: value for key, value in dict(values).items() if key in allowed}
        with self.session() as session:
            session.add(DebuggerHeartbeatOrm(**payload))
            session.commit()

    def clear_heartbeat_exec(self, row_id: int) -> bool:  # noqa: nonconform reviewed return contract
        with self.session() as session:
            row = session.get(DebuggerHeartbeatOrm, int(row_id))
            if row is None:
                return False
            row.exec = EMPTY_STRING
            row.exec_is_file = 0
            session.commit()
            return True

    def pending_debugger_commands(self, source: str, pid: int) -> list[dict]:  # noqa: nonconform reviewed return contract
        with self.session() as session:
            stmt = (
                select(DebuggerHeartbeatOrm)
                .where(DebuggerHeartbeatOrm.source == str(source))
                .where(DebuggerHeartbeatOrm.pid == int(pid))
                .where((DebuggerHeartbeatOrm.exec.isnot(None)) | (DebuggerHeartbeatOrm.cron.isnot(None)))
                .order_by(DebuggerHeartbeatOrm.id.asc())
            )
            rows = []
            for row in session.scalars(stmt).all():
                exec_value = str(row.exec or EMPTY_STRING)
                cron_value = str(row.cron or EMPTY_STRING)
                if not exec_value and not cron_value:
                    continue
                rows.append({column.name: getattr(row, column.name) for column in DebuggerHeartbeatOrm.__table__.columns})
            return rows

    def _now_iso(self, stamp: float | None = None) -> str:
        value = float(stamp or time.time())
        return datetime.datetime.fromtimestamp(value).isoformat(sep=' ', timespec='microseconds')

    def _process_record_payload(self, values: dict) -> dict:
        mapped = dict(values or {})
        if 'metadata' in mapped and 'metadataText' not in mapped:
            mapped['metadataText'] = mapped.pop('metadata')
        allowed = {'created', 'updated', 'source', 'phase_key', 'phase_name', 'process_name', 'kind', 'pid', 'status', 'started_at', 'ended_at', 'ttl_seconds', 'exit_code', 'error_type', 'error_message', 'traceback_text', 'fault_reason', 'command', 'metadataText', 'processed'}
        return {key: value for key, value in mapped.items() if key in allowed}

    def _process_record_dict(self, row) -> dict:
        return {
            'id': int(row.id or 0),
            'created': str(row.created or EMPTY_STRING),
            'updated': str(row.updated or EMPTY_STRING),
            'source': str(row.source or EMPTY_STRING),
            'phase_key': str(row.phase_key or EMPTY_STRING),
            'phase_name': str(row.phase_name or EMPTY_STRING),
            'process_name': str(row.process_name or EMPTY_STRING),
            'kind': str(row.kind or EMPTY_STRING),
            'pid': int(row.pid or 0),
            'status': str(row.status or EMPTY_STRING),
            'started_at': float(row.started_at or 0.0),
            'ended_at': float(row.ended_at or 0.0),
            'ttl_seconds': float(row.ttl_seconds or 0.0),
            'exit_code': row.exit_code,
            'error_type': str(row.error_type or EMPTY_STRING),
            'error_message': str(row.error_message or EMPTY_STRING),
            'traceback_text': str(row.traceback_text or EMPTY_STRING),
            'fault_reason': str(row.fault_reason or EMPTY_STRING),
            'command': str(row.command or EMPTY_STRING),
            'metadata': str(row.metadataText or EMPTY_STRING),
            'processed': int(row.processed or 0),
        }

    def insert_process_record(self, **values) -> int:  # noqa: nonconform reviewed return contract
        stamp = float(values.get('started_at') or time.time())
        payload = self._process_record_payload(dict(values))
        payload.setdefault('created', self._now_iso(stamp))
        payload.setdefault('updated', self._now_iso())
        payload.setdefault('source', Path(__file__).name)
        payload.setdefault('pid', int(os.getpid()))
        payload.setdefault('status', 'registered')
        payload.setdefault('started_at', stamp)
        payload.setdefault('ttl_seconds', 0.0)
        payload.setdefault('processed', 0)
        with self.session() as session:
            row = PromptProcessRecordOrm(**payload)
            session.add(row)
            session.flush()
            row_id = int(row.id or 0)
            session.commit()
            return row_id

    def update_process_record(self, row_id: int, **values) -> bool:  # noqa: nonconform reviewed return contract
        row_id = int(row_id or 0)
        if row_id <= 0:
            return False
        payload = self._process_record_payload(dict(values))
        payload['updated'] = self._now_iso()
        with self.session() as session:
            row = session.get(PromptProcessRecordOrm, row_id)
            if row is None:
                return False
            for key, value in payload.items():
                setattr(row, key, value)
            session.commit()
            return True

    def process_rows_by_status(self, statuses: list[str], limit: int = 250) -> list[dict]:  # noqa: nonconform reviewed return contract
        wanted = [str(value or EMPTY_STRING).strip() for value in list(statuses or []) if str(value or EMPTY_STRING).strip()]
        if not wanted:
            return []
        with self.session() as session:
            stmt = (
                select(PromptProcessRecordOrm)
                .where(PromptProcessRecordOrm.status.in_(wanted))
                .order_by(PromptProcessRecordOrm.id.asc())
                .limit(max(1, int(limit or 250)))
            )
            return [self._process_record_dict(row) for row in session.scalars(stmt).all()]

    def active_process_rows(self, limit: int = 250) -> list[dict]:
        return self.process_rows_by_status(['registered', 'pending', 'running'], limit=limit)



_PROMPT_ORM_STORES: dict[str, PromptSqlAlchemyStore] = {}


def prompt_orm_store(path: Path) -> PromptSqlAlchemyStore:
    resolved = str(Path(path).resolve())
    store = _PROMPT_ORM_STORES.get(resolved)
    if store is None:
        store = PromptSqlAlchemyStore(Path(resolved))
        _PROMPT_ORM_STORES[resolved] = store
    return store


def build_cli_alias_set(*names: str, include_question: bool = False) -> set[str]:
    aliases: set[str] = set()
    for raw_name in list(names or []):
        name = str(raw_name or EMPTY_STRING).strip().lower()
        if not name:
            continue
        aliases.add(name)
        if name == '?':
            aliases.add('/?')
            continue
        aliases.add('/' + name)
        aliases.add('-' + name)
        aliases.add('--' + name)
    if include_question:
        aliases.update({'?', '/?'})
    return aliases


def normalized_cli_tokens(argv=None) -> list[str]:
    try:
        tokens = [str(token or EMPTY_STRING) for token in list(argv or sys.argv or [])]
    except Exception as error:
        captureException(None, source='prompt_app.py', context='except@368')
        print(f"[WARN:swallowed-exception] prompt_app.py:314 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        tokens = []
    if tokens and str(Path(tokens[0]).name).lower().endswith('.py'):
        tokens = tokens[1:]
    return [str(token or EMPTY_STRING).strip() for token in tokens if str(token or EMPTY_STRING).strip()]


def strip_cli_value_quotes(value: str) -> str:
    value_text = str(value or EMPTY_STRING).strip()
    if len(value_text) >= 2 and value_text[0] == value_text[-1] and value_text[0] in {'"', "'"}:
        return value_text[1:-1].strip()
    return value_text


def cli_token_looks_like_option(token: str, known_aliases: set[str] | None = None) -> bool:
    raw_text = str(token or EMPTY_STRING).strip()
    lowered = raw_text.lower()
    if not lowered:
        return False
    aliases = set(known_aliases or set())
    if lowered in aliases:
        return True
    for alias in list(aliases):
        for separator in ('=', ':'):
            if lowered.startswith(alias + separator):
                return True
    if lowered == '?':
        return True
    if raw_text.startswith('/'):
        try:
            if Path(raw_text).exists():
                return False
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@401')
            print(f"[WARN:swallowed-exception] prompt_app.py:346 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        return True
    return lowered.startswith('-')


def read_cli_option(argv=None, aliases=None, takes_value: bool = False, known_aliases: set[str] | None = None) -> dict[str, Any]:
    tokens = list(normalized_cli_tokens(argv))
    alias_set = {str(alias or EMPTY_STRING).strip().lower() for alias in list(aliases or []) if str(alias or EMPTY_STRING).strip()}
    state = {'present': False, 'value': EMPTY_STRING, 'missing_value': False, 'token': EMPTY_STRING, 'index': -1}
    for index, token in enumerate(tokens):
        lowered = str(token or EMPTY_STRING).strip().lower()
        for alias in list(alias_set):
            if lowered == alias:
                state.update({'present': True, 'token': token, 'index': index})
                if takes_value:
                    if index + 1 < len(tokens) and not cli_token_looks_like_option(tokens[index + 1], known_aliases or alias_set):
                        state['value'] = strip_cli_value_quotes(tokens[index + 1])
                    else:
                        state['missing_value'] = True
                return state
            for separator in ('=', ':'):
                prefix = alias + separator
                if lowered.startswith(prefix):
                    value_text = strip_cli_value_quotes(token[len(alias) + 1:])
                    state.update({'present': True, 'token': token, 'index': index, 'value': value_text})
                    if takes_value and not value_text:
                        state['missing_value'] = True
                    return state
    return state


def read_cli_arguments(argv=None, aliases=None, argument_count: int = 0, known_aliases: set[str] | None = None) -> dict[str, Any]:
    tokens = list(normalized_cli_tokens(argv))
    alias_set = {str(alias or EMPTY_STRING).strip().lower() for alias in list(aliases or []) if str(alias or EMPTY_STRING).strip()}
    state: dict[str, Any] = {'present': False, 'values': [], 'missing_count': 0, 'token': EMPTY_STRING, 'index': -1}
    wanted_count = max(0, int(argument_count or 0))
    for index, token in enumerate(tokens):
        lowered = str(token or EMPTY_STRING).strip().lower()
        for alias in list(alias_set):
            if lowered == alias:
                values: list[str] = []
                cursor = index + 1
                while cursor < len(tokens) and len(values) < wanted_count and not cli_token_looks_like_option(tokens[cursor], known_aliases or alias_set):
                    values.append(strip_cli_value_quotes(tokens[cursor]))
                    cursor += 1
                state.update({'present': True, 'values': values, 'missing_count': max(0, wanted_count - len(values)), 'token': token, 'index': index})
                return state
            for separator in ('=', ':'):
                prefix = alias + separator
                if lowered.startswith(prefix):
                    first_value = strip_cli_value_quotes(token[len(alias) + 1:])
                    values = [first_value] if first_value else []
                    cursor = index + 1
                    while cursor < len(tokens) and len(values) < wanted_count and not cli_token_looks_like_option(tokens[cursor], known_aliases or alias_set):
                        values.append(strip_cli_value_quotes(tokens[cursor]))
                        cursor += 1
                    state.update({'present': True, 'values': values, 'missing_count': max(0, wanted_count - len(values)), 'token': token, 'index': index})
                    return state
    return state


CLI_DEBUGGER_QUERY_SURFACES_FLAGS = build_cli_alias_set('debugger-query-surfaces')
CLI_DEBUGGER_EXEC_COMMAND_FLAGS = build_cli_alias_set('debugger-exec-command')
CLI_DEBUGGER_CRON_COMMAND_FLAGS = build_cli_alias_set('debugger-cron-command')
CLI_DEBUGGER_INTERVAL_FLAGS = build_cli_alias_set('debugger-interval', 'debugger-command-interval')
CLI_DEBUGGER_COUNT_FLAGS = build_cli_alias_set('debugger-count', 'debugger-command-count')
CLI_PROXY_FLAGS = build_cli_alias_set('proxy')
CLI_EXPORT_GLYPH_FLAGS = build_cli_alias_set('export-glyph')
CLI_EXPORT_FONT_INFO_FLAGS = build_cli_alias_set('export-font-info')
CLI_VALID_ALIASES = set().union(
    CLI_DEBUGGER_QUERY_SURFACES_FLAGS,
    CLI_DEBUGGER_EXEC_COMMAND_FLAGS,
    CLI_DEBUGGER_CRON_COMMAND_FLAGS,
    CLI_DEBUGGER_INTERVAL_FLAGS,
    CLI_DEBUGGER_COUNT_FLAGS,
    CLI_PROXY_FLAGS,
    CLI_EXPORT_GLYPH_FLAGS,
    CLI_EXPORT_FONT_INFO_FLAGS,
)

DEBUGGER_QUERY_SURFACES = ('heartbeat', 'vardump', 'poll', 'debugger-exec-command', 'debugger-cron-command', 'accepts-proxy')


def _resolve_debugger_code_payload(text: str = EMPTY_STRING, file_path: str = EMPTY_STRING) -> tuple[str, str, str]:
    payload_text = str(text or EMPTY_STRING).strip()
    payload_file = str(file_path or EMPTY_STRING).strip()
    if payload_file:
        candidate = Path(payload_file).expanduser()
        if not candidate.is_absolute():
            candidate = (Path(__file__).resolve().parent / candidate).resolve()
        return File.readText(candidate, encoding='utf-8', errors='replace'), str(candidate), str(candidate)
    if payload_text:
        candidate = Path(payload_text).expanduser()
        if candidate.exists() and candidate.is_file():
            resolved = candidate.resolve()
            return File.readText(resolved, encoding='utf-8', errors='replace'), str(resolved), str(resolved)
        return payload_text, '<debugger-exec-command>', EMPTY_STRING
    return EMPTY_STRING, EMPTY_STRING, EMPTY_STRING


def _run_debugger_code_payload(text: str = EMPTY_STRING, file_path: str = EMPTY_STRING, namespace: dict | None = None) -> dict:
    import contextlib
    import io
    try:
        source_text, label, resolved_path = _resolve_debugger_code_payload(text=text, file_path=file_path)
    except Exception as error:
        captureException(None, source='prompt_app.py', context='except@508')
        print(f"[WARN:swallowed-exception] prompt_app.py:452 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        return {'ok': False, 'error': f'{type(error).__name__}: {error}', 'text': EMPTY_STRING, 'traceback_text': EMPTY_STRING, 'label': EMPTY_STRING, 'source_path': EMPTY_STRING}
    if not str(source_text or EMPTY_STRING).strip():
        return {'ok': False, 'error': 'empty debugger command', 'text': EMPTY_STRING, 'traceback_text': EMPTY_STRING, 'label': str(label or EMPTY_STRING), 'source_path': str(resolved_path or EMPTY_STRING)}
    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    exec_globals = dict(namespace or {})
    exec_globals.setdefault('__name__', '__prompt_debugger__')
    exec_globals.setdefault('__file__', str(Path(__file__).resolve()))
    exec_globals.setdefault('APP_ROOT', str(Path(__file__).resolve().parent))
    exec_globals.setdefault('Path', Path)
    try:
        compiled = compile(  # monkeypatch-ok: debugger execute-command intentionally compiles operator-provided code.
            str(source_text or EMPTY_STRING), str(label or '<debugger-exec-command>'), 'exec')
        with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
            exec(compiled, exec_globals, exec_globals)  # monkeypatch-ok: debugger execute-command intentionally runs operator-provided code
        output_text = (stdout_buffer.getvalue() or EMPTY_STRING) + (stderr_buffer.getvalue() or EMPTY_STRING)
        return {'ok': True, 'text': str(output_text or EMPTY_STRING), 'traceback_text': EMPTY_STRING, 'label': str(label or EMPTY_STRING), 'source_path': str(resolved_path or EMPTY_STRING), 'type': 'debugger-exec-command'}
    except Exception as error:
        captureException(None, source='prompt_app.py', context='except@527')
        trace_text = ''.join(traceback.format_exception(type(error), error, error.__traceback__))
        output_text = (stdout_buffer.getvalue() or EMPTY_STRING) + (stderr_buffer.getvalue() or EMPTY_STRING)
        return {'ok': False, 'error': f'{type(error).__name__}: {error}', 'text': str(output_text or EMPTY_STRING), 'traceback_text': str(trace_text or EMPTY_STRING), 'label': str(label or EMPTY_STRING), 'source_path': str(resolved_path or EMPTY_STRING), 'type': 'debugger-exec-command'}


def _import_cli_fonttools_ttfont():
    try:
        from fontTools.ttLib import TTFont as CliTTFont
        return CliTTFont, EMPTY_STRING
    except Exception as error:
        captureException(None, source='prompt_app.py', context='except@537')
        print(f"[WARN:swallowed-exception] prompt_app.py:479 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        return None, f'fontTools is required: {type(error).__name__}: {error}'


def _normalize_cli_codepoint(value: str) -> int:
    text_value = str(value or EMPTY_STRING).strip()
    if not text_value:
        raise ValueError('Missing Unicode codepoint value.')
    if len(text_value) == 1:
        return ord(text_value)
    normalized = text_value.upper().replace('_', EMPTY_STRING).replace('-', EMPTY_STRING).strip()
    if normalized.startswith('U+'):
        normalized = normalized[2:]
    elif normalized.startswith('0X'):
        normalized = normalized[2:]
    base = 16 if any(ch in 'ABCDEF' for ch in normalized) or text_value.strip().upper().startswith(('U+', '0X')) else 10
    return int(normalized, base)


def _font_table_name_strings(font) -> dict[int, list[str]]:
    table = font['name'] if 'name' in font else None
    results: dict[int, list[str]] = {}
    if table is None:
        return results
    for record in list(getattr(table, 'names', []) or []):
        try:
            value = str(record.toUnicode() or EMPTY_STRING).strip()
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@565')
            print(f"[WARN:swallowed-exception] prompt_app.py:506 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            continue
        if not value:
            continue
        values = results.setdefault(int(record.nameID), [])
        if value not in values:
            values.append(value)
    return results


def _font_primary_name(name_map: dict[int, list[str]], name_id: int, fallback: str = EMPTY_STRING) -> str:
    values = list(name_map.get(int(name_id), []))
    return values[0] if values else str(fallback or EMPTY_STRING)


def _font_info_output_path(font_path: Path) -> Path:
    return font_path.with_name(font_path.stem + '_font_info.html')


def _font_glyph_output_path(font_path: Path, codepoint: int) -> Path:
    return font_path.with_name(font_path.stem + f'_glyph_U+{codepoint:04X}.html')


def _font_cli_html_page(title: str, body_html: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_lib.escape(str(title or 'Prompt Font Export'))}</title>
<style>
:root {{
  color-scheme: dark;
}}
html, body {{
  margin: 0;
  padding: 0;
  background: #111318;
  color: #f2f4f8;
  font-family: Arial, Helvetica, sans-serif;
}}
body {{
  padding: 24px;
}}
h1, h2 {{
  margin: 0 0 16px 0;
}}
p, li {{
  line-height: 1.45;
}}
.grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
  margin: 16px 0 22px;
}}
.card {{
  background: #181c23;
  border: 1px solid #323a49;
  border-radius: 12px;
  padding: 14px;
}}
.muted {{
  color: #a8b2c4;
}}
.preview {{
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 180px;
  border-radius: 14px;
  border: 1px solid #3e4a61;
  background: #0e1117;
  margin: 16px 0 22px;
}}
.glyph-preview {{
  font-size: 96px;
  line-height: 1;
}}
table {{
  width: 100%;
  border-collapse: collapse;
  margin-top: 14px;
}}
th, td {{
  border: 1px solid #364053;
  padding: 8px 10px;
  text-align: left;
  vertical-align: top;
}}
th {{
  background: #1d2330;
}}
tbody tr:nth-child(even) {{
  background: #151a22;
}}
code {{
  font-family: Consolas, Menlo, monospace;
  color: #d6e6ff;
}}
.small {{
  font-size: 12px;
}}
</style>
</head>
<body>
{body_html}
</body>
</html>
"""


def _build_font_info_html(font_path: Path) -> str:
    CliTTFont, error_text = _import_cli_fonttools_ttfont()
    if CliTTFont is None:
        raise RuntimeError(error_text)
    font = CliTTFont(str(font_path))
    try:
        name_map = _font_table_name_strings(font)
        family_name = _font_primary_name(name_map, 16, _font_primary_name(name_map, 1, font_path.stem))
        subfamily_name = _font_primary_name(name_map, 17, _font_primary_name(name_map, 2, 'Regular'))
        full_name = _font_primary_name(name_map, 4, f'{family_name} {subfamily_name}'.strip())
        postscript_name = _font_primary_name(name_map, 6, re.sub(r'[^A-Za-z0-9-]+', EMPTY_STRING, full_name.replace(' ', '-')) or font_path.stem)
        version_name = _font_primary_name(name_map, 5, EMPTY_STRING)
        manufacturer = _font_primary_name(name_map, 8, EMPTY_STRING)
        designer = _font_primary_name(name_map, 9, EMPTY_STRING)
        description = _font_primary_name(name_map, 10, EMPTY_STRING)
        designer_url = _font_primary_name(name_map, 12, EMPTY_STRING)
        license_text = _font_primary_name(name_map, 13, EMPTY_STRING)
        license_url = _font_primary_name(name_map, 14, EMPTY_STRING)
        os2_table = font['OS/2'] if 'OS/2' in font else None
        head_table = font['head'] if 'head' in font else None
        post_table = font['post'] if 'post' in font else None
        cmap_entries: list[tuple[int, str]] = []
        cmap_table = font.getBestCmap() or {}
        for codepoint, glyph_name in sorted(cmap_table.items(), key=lambda item: int(item[0])):
            cmap_entries.append((int(codepoint), str(glyph_name or EMPTY_STRING)))
        summary_cards = [
            ('Family', family_name),
            ('Subfamily', subfamily_name),
            ('Full Name', full_name),
            ('PostScript', postscript_name),
            ('Version', version_name),
            ('File', str(font_path)),
            ('Glyph Order Count', str(len(list(font.getGlyphOrder() or [])))),
            ('Mapped Unicode Glyphs', str(len(cmap_entries))),
        ]
        if os2_table is not None:
            summary_cards.extend([
                ('Weight Class', str(getattr(os2_table, 'usWeightClass', EMPTY_STRING) or EMPTY_STRING)),
                ('Width Class', str(getattr(os2_table, 'usWidthClass', EMPTY_STRING) or EMPTY_STRING)),
                ('fsSelection', str(getattr(os2_table, 'fsSelection', EMPTY_STRING) or EMPTY_STRING)),
            ])
        if head_table is not None:
            summary_cards.append(('Units Per Em', str(getattr(head_table, 'unitsPerEm', EMPTY_STRING) or EMPTY_STRING)))
        if post_table is not None:
            summary_cards.append(('Italic Angle', str(getattr(post_table, 'italicAngle', EMPTY_STRING) or EMPTY_STRING)))
        cards_html = ''.join(
            f'<div class="card"><div class="small muted">{html_lib.escape(label)}</div><div>{html_lib.escape(value or "—")}</div></div>'
            for label, value in summary_cards
        )
        extra_sections: list[str] = []
        for label, value in (
            ('Manufacturer', manufacturer),
            ('Designer', designer),
            ('Description', description),
            ('Designer URL', designer_url),
            ('License', license_text),
            ('License URL', license_url),
        ):
            if str(value or EMPTY_STRING).strip():
                extra_sections.append(f'<div class="card"><div class="small muted">{html_lib.escape(label)}</div><div>{html_lib.escape(value)}</div></div>')
        rows_html = []
        for codepoint, glyph_name in cmap_entries:
            char_text = chr(codepoint)
            safe_char = html_lib.escape(char_text)
            rows_html.append(
                '<tr>'
                f'<td class="glyph-preview" style="font-size:32px;">{safe_char}</td>'
                f'<td><code>{codepoint}</code></td>'
                f'<td><code>U+{codepoint:04X}</code></td>'
                f'<td><code>{html_lib.escape(glyph_name)}</code></td>'
                '</tr>'
            )
        body_html = (
            f'<h1>{html_lib.escape(full_name or font_path.stem)}</h1>'
            '<p class="muted">Generated by Prompt --export-font-info.</p>'
            f'<div class="grid">{cards_html}</div>'
            + (f'<div class="grid">{"".join(extra_sections)}</div>' if extra_sections else EMPTY_STRING)
            + '<h2>Supported Glyphs</h2>'
            + '<table><thead><tr><th>Glyph</th><th>Int Value</th><th>U+ Code</th><th>Glyph Name</th></tr></thead><tbody>'
            + (''.join(rows_html) if rows_html else '<tr><td colspan="4">No Unicode-mapped glyphs found.</td></tr>')
            + '</tbody></table>'
        )
        return _font_cli_html_page(f'{full_name or font_path.stem} font info', body_html)
    finally:
        try:
            font.close()
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@764')
            print(f"[WARN:swallowed-exception] prompt_app.py:704 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass


def _build_font_glyph_html(font_path: Path, codepoint: int) -> str:
    CliTTFont, error_text = _import_cli_fonttools_ttfont()
    if CliTTFont is None:
        raise RuntimeError(error_text)
    font = CliTTFont(str(font_path))
    try:
        name_map = _font_table_name_strings(font)
        family_name = _font_primary_name(name_map, 16, _font_primary_name(name_map, 1, font_path.stem))
        subfamily_name = _font_primary_name(name_map, 17, _font_primary_name(name_map, 2, 'Regular'))
        full_name = _font_primary_name(name_map, 4, f'{family_name} {subfamily_name}'.strip())
        cmap_table = font.getBestCmap() or {}
        glyph_name = str(cmap_table.get(int(codepoint), EMPTY_STRING) or EMPTY_STRING)
        supported = bool(glyph_name)
        char_text = chr(int(codepoint))
        support_text = 'supported' if supported else 'not mapped in this font'
        cards_html = ''.join(
            f'<div class="card"><div class="small muted">{html_lib.escape(label)}</div><div>{html_lib.escape(value)}</div></div>'
            for label, value in (
                ('Family', family_name or font_path.stem),
                ('Full Name', full_name or font_path.stem),
                ('Int Value', str(int(codepoint))),
                ('U+ Code', f'U+{int(codepoint):04X}'),
                ('Glyph Name', glyph_name or '—'),
                ('Status', support_text),
                ('File', str(font_path)),
            )
        )
        body_html = (
            f'<h1>{html_lib.escape(full_name or font_path.stem)} glyph export</h1>'
            '<p class="muted">Generated by Prompt --export-glyph.</p>'
            f'<div class="preview"><div class="glyph-preview">{html_lib.escape(char_text)}</div></div>'
            f'<div class="grid">{cards_html}</div>'
        )
        return _font_cli_html_page(f'{full_name or font_path.stem} U+{int(codepoint):04X}', body_html)
    finally:
        try:
            font.close()
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@806')
            print(f"[WARN:swallowed-exception] prompt_app.py:745 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass


def _run_font_cli_exports() -> None:
    export_glyph_state = read_cli_arguments(sys.argv, CLI_EXPORT_GLYPH_FLAGS, argument_count=2, known_aliases=CLI_VALID_ALIASES)
    if export_glyph_state.get('present'):
        values = list(export_glyph_state.get('values') or [])
        if int(export_glyph_state.get('missing_count') or 0) > 0:
            print('Usage: prompt_app.py --export-glyph "file.ttf" U+0000', file=sys.stderr)
            raise SystemExit(1)  # lifecycle-ok: intentional early Prompt CLI exit before GUI lifecycle starts
        font_path = Path(str(values[0] or EMPTY_STRING)).expanduser()
        if not font_path.is_absolute():
            font_path = font_path.resolve()
        if not font_path.exists():
            print(f'Font file not found: {font_path}', file=sys.stderr)
            raise SystemExit(1)  # lifecycle-ok: intentional early Prompt CLI exit before GUI lifecycle starts
        try:
            codepoint = _normalize_cli_codepoint(str(values[1] or EMPTY_STRING))
            output_path = _font_glyph_output_path(font_path, codepoint)
            File.writeText(output_path, _build_font_glyph_html(font_path, codepoint), encoding='utf-8')
            print(str(output_path))
            raise SystemExit(0)  # lifecycle-ok: intentional early Prompt CLI exit before GUI lifecycle starts
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@830')
            print(f'Export glyph failed: {type(error).__name__}: {error}', file=sys.stderr)
            raise SystemExit(1)  # lifecycle-ok: intentional early Prompt CLI exit before GUI lifecycle starts
    export_info_state = read_cli_arguments(sys.argv, CLI_EXPORT_FONT_INFO_FLAGS, argument_count=1, known_aliases=CLI_VALID_ALIASES)
    if export_info_state.get('present'):
        values = list(export_info_state.get('values') or [])
        if int(export_info_state.get('missing_count') or 0) > 0:
            print('Usage: prompt_app.py --export-font-info "file.ttf"', file=sys.stderr)
            raise SystemExit(1)  # lifecycle-ok: intentional early Prompt CLI exit before GUI lifecycle starts
        font_path = Path(str(values[0] or EMPTY_STRING)).expanduser()
        if not font_path.is_absolute():
            font_path = font_path.resolve()
        if not font_path.exists():
            print(f'Font file not found: {font_path}', file=sys.stderr)
            raise SystemExit(1)  # lifecycle-ok: intentional early Prompt CLI exit before GUI lifecycle starts
        try:
            output_path = _font_info_output_path(font_path)
            File.writeText(output_path, _build_font_info_html(font_path), encoding='utf-8')
            print(str(output_path))
            raise SystemExit(0)  # lifecycle-ok: intentional early Prompt CLI exit before GUI lifecycle starts
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@850')
            print(f'Export font info failed: {type(error).__name__}: {error}', file=sys.stderr)
            raise SystemExit(1)  # lifecycle-ok: intentional early Prompt CLI exit before GUI lifecycle starts


def _run_early_debugger_cli() -> None:
    if read_cli_option(sys.argv, CLI_DEBUGGER_QUERY_SURFACES_FLAGS, takes_value=False, known_aliases=CLI_VALID_ALIASES).get('present'):
        print(' '.join(DEBUGGER_QUERY_SURFACES))
        raise SystemExit(0)  # lifecycle-ok: intentional early Prompt CLI exit before GUI lifecycle starts
    exec_state = read_cli_option(sys.argv, CLI_DEBUGGER_EXEC_COMMAND_FLAGS, takes_value=True, known_aliases=CLI_VALID_ALIASES)
    if exec_state.get('present'):
        result = _run_debugger_code_payload(text=str(exec_state.get('value') or EMPTY_STRING))
        payload = ((result.get('text') or EMPTY_STRING) + ((result.get('traceback_text') or EMPTY_STRING) if not result.get('ok') else EMPTY_STRING)).strip()
        if payload:
            print(payload)
        raise SystemExit(0 if result.get('ok') else 1)  # lifecycle-ok: intentional early Prompt CLI exit before GUI lifecycle starts
    cron_state = read_cli_option(sys.argv, CLI_DEBUGGER_CRON_COMMAND_FLAGS, takes_value=True, known_aliases=CLI_VALID_ALIASES)
    if cron_state.get('present'):
        interval_state = read_cli_option(sys.argv, CLI_DEBUGGER_INTERVAL_FLAGS, takes_value=True, known_aliases=CLI_VALID_ALIASES)
        count_state = read_cli_option(sys.argv, CLI_DEBUGGER_COUNT_FLAGS, takes_value=True, known_aliases=CLI_VALID_ALIASES)
        try:
            interval_value = max(0.05, float(interval_state.get('value') or 1.0))
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@872')
            print(f"[WARN:swallowed-exception] prompt_app.py:810 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            interval_value = 1.0
        try:
            count_value = max(1, int(count_state.get('value') or 3))
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@877')
            print(f"[WARN:swallowed-exception] prompt_app.py:814 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            count_value = 3
        exit_code = 0
        for index in range(count_value):
            result = _run_debugger_code_payload(text=str(cron_state.get('value') or EMPTY_STRING))
            payload = ((result.get('text') or EMPTY_STRING) + ((result.get('traceback_text') or EMPTY_STRING) if not result.get('ok') else EMPTY_STRING)).strip()
            if payload:
                if index > 0:
                    print()
                print(payload)
            if not result.get('ok'):
                exit_code = 1
                break
            if index + 1 < count_value:
                time.sleep(interval_value)
        raise SystemExit(exit_code)  # lifecycle-ok: intentional early Prompt CLI exit before GUI lifecycle starts


_run_early_debugger_cli()
_run_font_cli_exports()

TRIO_OFFSCREEN_ACTIVE = str(os.environ.get('TRIO_OFFSCREEN', EMPTY_STRING) or EMPTY_STRING).strip().lower() in {'1', 'true', 'yes', 'on'}
TRIO_MANAGED_DISPLAY_ACTIVE = str(os.environ.get('TRIO_MANAGED_DISPLAY', EMPTY_STRING) or EMPTY_STRING).strip().lower() in {'1', 'true', 'yes', 'on'}
TRIO_ALLOW_MANAGED_DISPLAY_WEBENGINE = str(os.environ.get('TRIO_ALLOW_MANAGED_DISPLAY_WEBENGINE', EMPTY_STRING) or EMPTY_STRING).strip().lower() in {'1', 'true', 'yes', 'on'}
PROMPT_WEBENGINE_FALLBACK_ACTIVE = bool((TRIO_OFFSCREEN_ACTIVE or TRIO_MANAGED_DISPLAY_ACTIVE) and not TRIO_ALLOW_MANAGED_DISPLAY_WEBENGINE)
PROMPT_WEBENGINE_LOAD_MODE = str(os.environ.get('PROMPT_WEBENGINE_LOAD_MODE', 'sethtml') or 'sethtml').strip().lower()
if PROMPT_WEBENGINE_LOAD_MODE not in {'sethtml', 'file'}:
    PROMPT_WEBENGINE_LOAD_MODE = 'sethtml'
PROMPT_WEBENGINE_SELFTEST_FLAGS = {'--webengine-selftest', '--webengine-smoke', '--prompt-selftest', '--load-selftest'}
PROMPT_WEBENGINE_SELFTEST_ACTIVE = any(str(arg or EMPTY_STRING).strip().lower() in PROMPT_WEBENGINE_SELFTEST_FLAGS for arg in list(sys.argv or []))

# v159: packaged EXEs can show blank QWebEngine views when Chromium chooses a
# broken GPU path. Set conservative defaults before importing QtWebEngine.
os.environ.setdefault('QT_OPENGL', 'software')
_existing_chromium_flags = str(os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', '') or '').strip()
_required_chromium_flags = ['--disable-gpu', '--disable-features=CalculateNativeWinOcclusion']
for _flag in _required_chromium_flags:
    if _flag not in _existing_chromium_flags:
        _existing_chromium_flags = (_existing_chromium_flags + ' ' + _flag).strip()
os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = _existing_chromium_flags

from PySide6.QtCore import QByteArray, QEvent, QMetaObject, QMimeData, QObject, QPoint, QSize, Qt, QTimer, QUrl, Slot  # noqa: E402
from PySide6.QtGui import QAction, QActionGroup, QContextMenuEvent, QFont, QFontDatabase, QIcon, QKeySequence, QPainter, QPixmap, QShortcut, QTextCursor, QTextDocument  # noqa: E402
from PySide6.QtWebChannel import QWebChannel  # noqa: E402
from PySide6.QtWebEngineCore import (  # noqa: E402
    QWebEnginePage,
    QWebEngineSettings,
)
from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMenuBar,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QSizePolicy,
    QStatusBar,
    QTabWidget,
    QTextBrowser,
    QToolBar,
    QVBoxLayout,
    QWidget,
)
try:  # noqa: E402
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QSoundEffect
    PROMPT_QT_SOUND_AVAILABLE = True
except Exception as _prompt_sound_import_error:  # noqa: E402
    captureException(_prompt_sound_import_error, source='prompt_app.py', context='qt-sound-import', handled=True)
    QAudioOutput = None
    QMediaPlayer = None
    QSoundEffect = None
    PROMPT_QT_SOUND_AVAILABLE = False

DEBUG_CLI_FLAGS = {'--debug', '--trace', '--verbose-trace', '--headless'}

try:
    from fontTools.ttLib import TTFont
    from fontTools.subset import Options as FontSubsetOptions
    from fontTools.subset import Subsetter as FontSubsetter
    from fontTools.subset import load_font as load_subset_font
    from fontTools.subset import save_font as save_subset_font
except Exception as error:
    captureException(None, source='prompt_app.py', context='except@966')
    print(f"[WARN:swallowed-exception] prompt_app.py:886 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
    TTFont = None
    FontSubsetOptions = None
    FontSubsetter = None
    load_subset_font = None
    save_subset_font = None

try:
    import brotli as _brotli_module
except Exception as error:
    captureException(None, source='prompt_app.py', context='except@976')
    print(f"[WARN:swallowed-exception] prompt_app.py:895 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
    try:
        import brotlicffi as _brotli_module
    except Exception as error:
        captureException(None, source='prompt_app.py', context='except@980')
        print(f"[WARN:swallowed-exception] prompt_app.py:898 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        _brotli_module = None


_MODELLESS_SOURCE_DIALOGS: list[QDialog] = []

# Prompt runtime constants kept near the Qt/bootstrap constants so Pyright and runtime imports
# see a single source of truth instead of relying on generated/injected globals.
PHASE_BOOTSTRAP = 10
PHASE_LOAD_DATA = 20
PHASE_RUNTIME_THREADS = 1000
PHASE_RUNTIME_TIMERS = 2000
PHASE_MAIN_BOOT = 3000
PROMPT_EMPTY_SENTINEL = '__prompt_empty__'
PROMPT_NEW_SENTINEL = '__prompt_new__'
PROMPT_FILENAME_FILTER = 'Prompt Markdown (*.prompt.md *.md);;Markdown (*.md);;HTML (*.html);;Text (*.txt);;All Files (*)'
FONT_FILENAME_FILTER = 'Font Files (*.ttf *.otf *.ttc *.woff *.woff2);;All Files (*)'
PROMPT_SORT_FALLBACK = 999999
PROMPT_TITLE_ACRONYMS = {
    'AI', 'API', 'AST', 'CLI', 'CMS', 'CPU', 'CSV', 'CSS', 'DB', 'DNS', 'DOM', 'DSL',
    'EXE', 'FAQ', 'GUI', 'HTML', 'HTTP', 'HTTPS', 'ID', 'IDE', 'JS', 'JSON', 'LLM',
    'MD', 'ORM', 'PDF', 'PHP', 'PWA', 'QA', 'QT', 'ROI', 'RPC', 'SDK', 'SEO',
    'SQL', 'SQLITE', 'SSL', 'SVG', 'TCR', 'TTL', 'UI', 'URL', 'UX', 'XML', 'ZIP',
}
WEBENGINE_DIAGNOSTIC_SCRIPT = '''
(function(){
    try {
        window.__promptWebEngineDiagnosticsInstalled = true;
        window.addEventListener('error', function(event) {
            console.error('[prompt-webengine][error] ' + (event && event.message ? event.message : 'unknown'));
        });
        window.addEventListener('unhandledrejection', function(event) {
            console.error('[prompt-webengine][promise] ' + (event && event.reason ? event.reason : 'unknown'));
        });
        console.log('[prompt-webengine] diagnostics installed');
    } catch (error) {
        console.error('[prompt-webengine][install-failed] ' + String(error && error.stack ? error.stack : error));
    }
})();
'''

CSS_NAMED_COLORS = {
  "aliceblue": "#f0f8ff",
  "antiquewhite": "#faebd7",
  "aqua": "#00ffff",
  "aquamarine": "#7fffd4",
  "azure": "#f0ffff",
  "beige": "#f5f5dc",
  "bisque": "#ffe4c4",
  "black": "#000000",
  "blanchedalmond": "#ffebcd",
  "blue": "#0000ff",
  "blueviolet": "#8a2be2",
  "brown": "#a52a2a",
  "burlywood": "#deb887",
  "cadetblue": "#5f9ea0",
  "chartreuse": "#7fff00",
  "chocolate": "#d2691e",
  "coral": "#ff7f50",
  "cornflowerblue": "#6495ed",
  "cornsilk": "#fff8dc",
  "crimson": "#dc143c",
  "cyan": "#00ffff",
  "darkblue": "#00008b",
  "darkcyan": "#008b8b",
  "darkgoldenrod": "#b8860b",
  "darkgray": "#a9a9a9",
  "darkgreen": "#006400",
  "darkgrey": "#a9a9a9",
  "darkkhaki": "#bdb76b",
  "darkmagenta": "#8b008b",
  "darkolivegreen": "#556b2f",
  "darkorange": "#ff8c00",
  "darkorchid": "#9932cc",
  "darkred": "#8b0000",
  "darksalmon": "#e9967a",
  "darkseagreen": "#8fbc8f",
  "darkslateblue": "#483d8b",
  "darkslategray": "#2f4f4f",
  "darkslategrey": "#2f4f4f",
  "darkturquoise": "#00ced1",
  "darkviolet": "#9400d3",
  "deeppink": "#ff1493",
  "deepskyblue": "#00bfff",
  "dimgray": "#696969",
  "dimgrey": "#696969",
  "dodgerblue": "#1e90ff",
  "firebrick": "#b22222",
  "floralwhite": "#fffaf0",
  "forestgreen": "#228b22",
  "fuchsia": "#ff00ff",
  "gainsboro": "#dcdcdc",
  "ghostwhite": "#f8f8ff",
  "gold": "#ffd700",
  "goldenrod": "#daa520",
  "gray": "#808080",
  "green": "#008000",
  "greenyellow": "#adff2f",
  "grey": "#808080",
  "honeydew": "#f0fff0",
  "hotpink": "#ff69b4",
  "indianred": "#cd5c5c",
  "indigo": "#4b0082",
  "ivory": "#fffff0",
  "khaki": "#f0e68c",
  "lavender": "#e6e6fa",
  "lavenderblush": "#fff0f5",
  "lawngreen": "#7cfc00",
  "lemonchiffon": "#fffacd",
  "lightblue": "#add8e6",
  "lightcoral": "#f08080",
  "lightcyan": "#e0ffff",
  "lightgoldenrodyellow": "#fafad2",
  "lightgray": "#d3d3d3",
  "lightgreen": "#90ee90",
  "lightgrey": "#d3d3d3",
  "lightpink": "#ffb6c1",
  "lightsalmon": "#ffa07a",
  "lightseagreen": "#20b2aa",
  "lightskyblue": "#87cefa",
  "lightslategray": "#778899",
  "lightslategrey": "#778899",
  "lightsteelblue": "#b0c4de",
  "lightyellow": "#ffffe0",
  "lime": "#00ff00",
  "limegreen": "#32cd32",
  "linen": "#faf0e6",
  "magenta": "#ff00ff",
  "maroon": "#800000",
  "mediumaquamarine": "#66cdaa",
  "mediumblue": "#0000cd",
  "mediumorchid": "#ba55d3",
  "mediumpurple": "#9370db",
  "mediumseagreen": "#3cb371",
  "mediumslateblue": "#7b68ee",
  "mediumspringgreen": "#00fa9a",
  "mediumturquoise": "#48d1cc",
  "mediumvioletred": "#c71585",
  "midnightblue": "#191970",
  "mintcream": "#f5fffa",
  "mistyrose": "#ffe4e1",
  "moccasin": "#ffe4b5",
  "navajowhite": "#ffdead",
  "navy": "#000080",
  "oldlace": "#fdf5e6",
  "olive": "#808000",
  "olivedrab": "#6b8e23",
  "orange": "#ffa500",
  "orangered": "#ff4500",
  "orchid": "#da70d6",
  "palegoldenrod": "#eee8aa",
  "palegreen": "#98fb98",
  "paleturquoise": "#afeeee",
  "palevioletred": "#db7093",
  "papayawhip": "#ffefd5",
  "peachpuff": "#ffdab9",
  "peru": "#cd853f",
  "pink": "#ffc0cb",
  "plum": "#dda0dd",
  "powderblue": "#b0e0e6",
  "purple": "#800080",
  "rebeccapurple": "#663399",
  "red": "#ff0000",
  "rosybrown": "#bc8f8f",
  "royalblue": "#4169e1",
  "saddlebrown": "#8b4513",
  "salmon": "#fa8072",
  "sandybrown": "#f4a460",
  "seagreen": "#2e8b57",
  "seashell": "#fff5ee",
  "sienna": "#a0522d",
  "silver": "#c0c0c0",
  "skyblue": "#87ceeb",
  "slateblue": "#6a5acd",
  "slategray": "#708090",
  "slategrey": "#708090",
  "snow": "#fffafa",
  "springgreen": "#00ff7f",
  "steelblue": "#4682b4",
  "tan": "#d2b48c",
  "teal": "#008080",
  "thistle": "#d8bfd8",
  "tomato": "#ff6347",
  "turquoise": "#40e0d0",
  "violet": "#ee82ee",
  "wheat": "#f5deb3",
  "white": "#ffffff",
  "whitesmoke": "#f5f5f5",
  "yellow": "#ffff00",
  "yellowgreen": "#9acd32"
}

LANGUAGE_FLAG_SVG_BLOBS = {
  "english": "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2MCA0MCI+PHJlY3Qgd2lkdGg9IjYwIiBoZWlnaHQ9IjQwIiBmaWxsPSIjMDEyMTY5Ii8+PHBhdGggZD0iTTAgMCA2MCA0ME02MCAwIDAgNDAiIHN0cm9rZT0iI2ZmZiIgc3Ryb2tlLXdpZHRoPSI4Ii8+PHBhdGggZD0iTTAgMCA2MCA0ME02MCAwIDAgNDAiIHN0cm9rZT0iI2M4MTAyZSIgc3Ryb2tlLXdpZHRoPSI0Ii8+PHBhdGggZD0iTTMwIDB2NDBNMCAyMGg2MCIgc3Ryb2tlPSIjZmZmIiBzdHJva2Utd2lkdGg9IjEyIi8+PHBhdGggZD0iTTMwIDB2NDBNMCAyMGg2MCIgc3Ryb2tlPSIjYzgxMDJlIiBzdHJva2Utd2lkdGg9IjYiLz48L3N2Zz4=",
  "spanish": "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2MCA0MCI+PHJlY3Qgd2lkdGg9IjYwIiBoZWlnaHQ9IjQwIiBmaWxsPSIjYWExNTFiIi8+PHJlY3QgeT0iMTAiIHdpZHRoPSI2MCIgaGVpZ2h0PSIyMCIgZmlsbD0iI2YxYmYwMCIvPjwvc3ZnPg==",
  "french": "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2MCA0MCI+PHJlY3Qgd2lkdGg9IjIwIiBoZWlnaHQ9IjQwIiBmaWxsPSIjMDA1NWE0Ii8+PHJlY3QgeD0iMjAiIHdpZHRoPSIyMCIgaGVpZ2h0PSI0MCIgZmlsbD0iI2ZmZiIvPjxyZWN0IHg9IjQwIiB3aWR0aD0iMjAiIGhlaWdodD0iNDAiIGZpbGw9IiNlZjQxMzUiLz48L3N2Zz4=",
  "german": "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2MCA0MCI+PHJlY3Qgd2lkdGg9IjYwIiBoZWlnaHQ9IjEzLjM0IiBmaWxsPSIjMDAwIi8+PHJlY3QgeT0iMTMuMzMiIHdpZHRoPSI2MCIgaGVpZ2h0PSIxMy4zNCIgZmlsbD0iI2RkMDAwMCIvPjxyZWN0IHk9IjI2LjY2IiB3aWR0aD0iNjAiIGhlaWdodD0iMTMuMzQiIGZpbGw9IiNmZmNlMDAiLz48L3N2Zz4=",
  "portuguese": "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2MCA0MCI+PHJlY3Qgd2lkdGg9IjI0IiBoZWlnaHQ9IjQwIiBmaWxsPSIjMDA2NjAwIi8+PHJlY3QgeD0iMjQiIHdpZHRoPSIzNiIgaGVpZ2h0PSI0MCIgZmlsbD0iI2ZmMDAwMCIvPjxjaXJjbGUgY3g9IjI0IiBjeT0iMjAiIHI9IjciIGZpbGw9IiNmZmNjMDAiLz48L3N2Zz4=",
  "russian": "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2MCA0MCI+PHJlY3Qgd2lkdGg9IjYwIiBoZWlnaHQ9IjEzLjM0IiBmaWxsPSIjZmZmIi8+PHJlY3QgeT0iMTMuMzMiIHdpZHRoPSI2MCIgaGVpZ2h0PSIxMy4zNCIgZmlsbD0iIzAwMzlhNiIvPjxyZWN0IHk9IjI2LjY2IiB3aWR0aD0iNjAiIGhlaWdodD0iMTMuMzQiIGZpbGw9IiNkNTJiMWUiLz48L3N2Zz4=",
  "ukrainian": "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2MCA0MCI+PHJlY3Qgd2lkdGg9IjYwIiBoZWlnaHQ9IjIwIiBmaWxsPSIjMDA1N2I3Ii8+PHJlY3QgeT0iMjAiIHdpZHRoPSI2MCIgaGVpZ2h0PSIyMCIgZmlsbD0iI2ZmZDcwMCIvPjwvc3ZnPg==",
  "italian": "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2MCA0MCI+PHJlY3Qgd2lkdGg9IjIwIiBoZWlnaHQ9IjQwIiBmaWxsPSIjMDA5MjQ2Ii8+PHJlY3QgeD0iMjAiIHdpZHRoPSIyMCIgaGVpZ2h0PSI0MCIgZmlsbD0iI2ZmZiIvPjxyZWN0IHg9IjQwIiB3aWR0aD0iMjAiIGhlaWdodD0iNDAiIGZpbGw9IiNjZTJiMzciLz48L3N2Zz4=",
  "gaelic": "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2MCA0MCI+PHJlY3Qgd2lkdGg9IjIwIiBoZWlnaHQ9IjQwIiBmaWxsPSIjMTY5YjYyIi8+PHJlY3QgeD0iMjAiIHdpZHRoPSIyMCIgaGVpZ2h0PSI0MCIgZmlsbD0iI2ZmZiIvPjxyZWN0IHg9IjQwIiB3aWR0aD0iMjAiIGhlaWdodD0iNDAiIGZpbGw9IiNmZjg4M2UiLz48L3N2Zz4=",
  "japanese": "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2MCA0MCI+PHJlY3Qgd2lkdGg9IjYwIiBoZWlnaHQ9IjQwIiBmaWxsPSIjZmZmIi8+PGNpcmNsZSBjeD0iMzAiIGN5PSIyMCIgcj0iMTEiIGZpbGw9IiNiYzAwMmQiLz48L3N2Zz4=",
  "chinese": "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2MCA0MCI+PHJlY3Qgd2lkdGg9IjYwIiBoZWlnaHQ9IjQwIiBmaWxsPSIjZGUyOTEwIi8+PHBvbHlnb24gcG9pbnRzPSIxMiw2IDE0LDEyIDIwLDEyIDE1LDE2IDE3LDIyIDEyLDE4IDcsMjIgOSwxNiA0LDEyIDEwLDEyIiBmaWxsPSIjZmZkZTAwIi8+PC9zdmc+",
  "vietnamese": "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2MCA0MCI+PHJlY3Qgd2lkdGg9IjYwIiBoZWlnaHQ9IjQwIiBmaWxsPSIjZGEyNTFkIi8+PHBvbHlnb24gcG9pbnRzPSIzMCw4IDMzLDE3IDQzLDE3IDM1LDIzIDM4LDMyIDMwLDI2IDIyLDMyIDI1LDIzIDE3LDE3IDI3LDE3IiBmaWxsPSIjZmZkZTAwIi8+PC9zdmc+"
}


LANGUAGE_FLAG_SVG_BLOBS.update({
    "en": LANGUAGE_FLAG_SVG_BLOBS.get("english", ""),
    "es": LANGUAGE_FLAG_SVG_BLOBS.get("spanish", ""),
    "fr": LANGUAGE_FLAG_SVG_BLOBS.get("french", ""),
    "de": LANGUAGE_FLAG_SVG_BLOBS.get("german", ""),
    "ru": LANGUAGE_FLAG_SVG_BLOBS.get("russian", ""),
    "uk": LANGUAGE_FLAG_SVG_BLOBS.get("ukrainian", ""),
    "ua": LANGUAGE_FLAG_SVG_BLOBS.get("ukrainian", ""),
    "zh": LANGUAGE_FLAG_SVG_BLOBS.get("chinese", ""),
    "cn": LANGUAGE_FLAG_SVG_BLOBS.get("chinese", ""),
    "hi": "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2MCA0MCI+PHJlY3Qgd2lkdGg9IjYwIiBoZWlnaHQ9IjEzLjMzMyIgZmlsbD0iI2ZmOTkzMyIvPjxyZWN0IHk9IjEzLjMzMyIgd2lkdGg9IjYwIiBoZWlnaHQ9IjEzLjMzNCIgZmlsbD0iI2ZmZiIvPjxyZWN0IHk9IjI2LjY2NyIgd2lkdGg9IjYwIiBoZWlnaHQ9IjEzLjMzMyIgZmlsbD0iIzEzODgwOCIvPjxjaXJjbGUgY3g9IjMwIiBjeT0iMjAiIHI9IjUuMiIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjMDAwMDgwIiBzdHJva2Utd2lkdGg9IjEuMiIvPjxjaXJjbGUgY3g9IjMwIiBjeT0iMjAiIHI9IjEiIGZpbGw9IiMwMDAwODAiLz48L3N2Zz4=",
    "hindi": "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2MCA0MCI+PHJlY3Qgd2lkdGg9IjYwIiBoZWlnaHQ9IjEzLjMzMyIgZmlsbD0iI2ZmOTkzMyIvPjxyZWN0IHk9IjEzLjMzMyIgd2lkdGg9IjYwIiBoZWlnaHQ9IjEzLjMzNCIgZmlsbD0iI2ZmZiIvPjxyZWN0IHk9IjI2LjY2NyIgd2lkdGg9IjYwIiBoZWlnaHQ9IjEzLjMzMyIgZmlsbD0iIzEzODgwOCIvPjxjaXJjbGUgY3g9IjMwIiBjeT0iMjAiIHI9IjUuMiIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjMDAwMDgwIiBzdHJva2Utd2lkdGg9IjEuMiIvPjxjaXJjbGUgY3g9IjMwIiBjeT0iMjAiIHI9IjEiIGZpbGw9IiMwMDAwODAiLz48L3N2Zz4=",
})


class PromptLanguage(IntEnum):
    EN = 1
    ES = 2
    HI = 3
    ZH = 4
    RU = 5
    UK = 6
    DE = 7
    FR = 8


PROMPT_LANGUAGE_CODES: dict[PromptLanguage, str] = {
    PromptLanguage.EN: 'EN',
    PromptLanguage.ES: 'ES',
    PromptLanguage.HI: 'HI',
    PromptLanguage.ZH: 'ZH',
    PromptLanguage.RU: 'RU',
    PromptLanguage.UK: 'UK',
    PromptLanguage.DE: 'DE',
    PromptLanguage.FR: 'FR',
}

PROMPT_LANGUAGE_FROM_CODE: dict[str, PromptLanguage] = {value: key for key, value in PROMPT_LANGUAGE_CODES.items()}
PROMPT_LANGUAGE_FROM_CODE.update({'CN': PromptLanguage.ZH, 'ZH-CN': PromptLanguage.ZH, 'UA': PromptLanguage.UK})

PROMPT_LANGUAGE_NAMES: dict[PromptLanguage, str] = {
    PromptLanguage.EN: 'English',
    PromptLanguage.ES: 'Español',
    PromptLanguage.HI: 'हिन्दी',
    PromptLanguage.ZH: '简体中文',
    PromptLanguage.RU: 'Русский',
    PromptLanguage.UK: 'Українська',
    PromptLanguage.DE: 'Deutsch',
    PromptLanguage.FR: 'Français',
}

PROMPT_LANGUAGE_FLAG_KEYS: dict[PromptLanguage, str] = {
    PromptLanguage.EN: 'en',
    PromptLanguage.ES: 'es',
    PromptLanguage.HI: 'hi',
    PromptLanguage.ZH: 'zh',
    PromptLanguage.RU: 'ru',
    PromptLanguage.UK: 'uk',
    PromptLanguage.DE: 'de',
    PromptLanguage.FR: 'fr',
}

LANGUAGE_PICKER_ORDER: list[tuple[PromptLanguage, str]] = [
    (PromptLanguage.EN, PROMPT_LANGUAGE_NAMES[PromptLanguage.EN]),
    (PromptLanguage.ES, PROMPT_LANGUAGE_NAMES[PromptLanguage.ES]),
    (PromptLanguage.HI, PROMPT_LANGUAGE_NAMES[PromptLanguage.HI]),
    (PromptLanguage.ZH, PROMPT_LANGUAGE_NAMES[PromptLanguage.ZH]),
    (PromptLanguage.RU, PROMPT_LANGUAGE_NAMES[PromptLanguage.RU]),
    (PromptLanguage.UK, PROMPT_LANGUAGE_NAMES[PromptLanguage.UK]),
    (PromptLanguage.DE, PROMPT_LANGUAGE_NAMES[PromptLanguage.DE]),
    (PromptLanguage.FR, PROMPT_LANGUAGE_NAMES[PromptLanguage.FR]),
]


PROMPT_TEXT_EN: dict[str, dict[str, str]] = {
    'app': {
        'title': 'Prompt 1.0.0',
    },
    'menus': {
        'file': 'File',
        'language': 'Language',
        'help': 'Help',
    },
    'actions': {
        'new_prompt': 'New Prompt',
        'open_prompt': 'Open Prompt...',
        'save_prompt': 'Save Prompt',
        'save_workflow': 'Save Workflow',
        'save_doctype': 'Save Doctype',
        'open_font': 'Open Font...',
        'reload_everything': 'Reload Everything',
        'about_prompt': 'About Prompt',
        'about_prompt_ellipsis': 'About Prompt...',
    },
    'toolbars': {
        'main': 'Main',
        'language': 'Language',
    },
    'tabs': {
        'prompts': 'Prompts',
        'doctypes': 'Doctypes',
        'fonts': 'Fonts',
        'help': 'Help',
        'settings': 'Settings',
    },
    'panes': {
        'prompt': 'Prompt',
        'doctype': 'Doctype',
        'workflow': 'Workflow',
        'fonts': '<b>Fonts</b>',
    },
    'buttons': {
        'save': 'Save',
        'home': 'Home',
        'reload': 'Reload',
        'close': 'Close',
        'save_db': 'Save DB Settings',
    },
    'groups': {
        'about_prompt': 'About Prompt',
        'db_connection': 'DB Connection',
    },
    'labels': {
        'prompt': 'Prompt',
        'workflow': 'Workflow',
        'doctype': 'Doctype',
    },
    'about': {
        'title': 'About Prompt',
        'badge': 'Prompt 1.0.0',
        'hero_title': 'Prompt 1.0.0',
        'description': 'A desktop prompt workbench for building real-world prompts, doctypes, and workflows that are powerful right out of the box.',
        'pet_caption': "Spry little buddy reporting for duty. Isn't he spry?",
        'version_heading': 'Version',
        'author_heading': 'Author',
        'app_label': 'App',
        'app_version': 'Prompt 1.0.0',
        'md5_label': 'MD5',
        'released_label': 'Released',
        'license_label': 'License',
        'license_text': 'Released under the MIT License',
        'free_estimate': 'Call for a free estimate on your next project! (PHP, Python, Mobile)',
        'footer': 'Coded with <span class="heart">♥</span> by ChatGPT',
    },
    'source': {
        'copy_selection': '📋 Copy Selection',
        'view_source': '📄 View Source',
        'view_source_title': 'View Source',
        'save_as': 'Save Source As...',
        'save_dialog': 'Save Source As',
        'save_filter': 'HTML Files (*.html *.htm);;Text Files (*.txt);;All Files (*)',
        'saved_status': 'Saved {name}',
        'copy_source': 'Copy Source',
        'find_menu': 'Find',
        'find_action': 'Find...',
        'find_replace': 'Find / Replace...',
        'find_next': 'Find Next',
        'find_previous': 'Find Previous',
        'show_plain_text': 'Show Plain Text',
        'find_label': 'Find:',
        'find_placeholder': 'Find in source…',
        'previous': 'Previous',
        'next': 'Next',
        'replace_label': 'Replace:',
        'replace_current': 'Replace',
        'replace_all': 'Replace All',
        'replace_placeholder': 'Replace with…',
        'close_symbol': '✕',
    },
    'fonts': {
        'open': 'Open Font...',
        'reload': 'Reload Fonts',
        'add_selected': 'Add Selected →',
        'remove_selected': 'Remove Selected',
        'clear': 'Clear',
        'export': 'Export Font...',
        'bundled_font': 'Bundled Font',
        'builder_hint': 'Double-click a glyph or drag it into this column.',
        'inventory': 'Bundled fonts: {count} | Drag or double-click glyphs to build a new font subset.',
        'none_found': 'No bundled fonts found in /fonts/.',
        'missing_tools': '{target} | fontTools is missing.',
        'load_failed': '{target} | failed to load glyphs: {error}',
        'loaded': 'Loaded {count} glyphs from {name}.',
        'no_glyphs': '{target} | no glyphs found.',
        'group.bundled': 'Bundled Fonts',
        'group.glyph_atlas': 'Glyph Atlas',
        'group.selected_glyph': 'Selected Glyph',
        'group.builder': 'New Font Builder',
        'field.preview': 'Preview',
        'field.plain_text': 'Plain Text',
        'field.int_value': 'Int Value',
        'field.uplus_code': 'U+ Code',
        'field.glyph_name': 'Glyph Name',
        'field.font_path': 'Font Path',
        'field.name': 'Name',
        'field.type': 'Type',
        'no_fonts_short': 'No fonts found in /fonts/.',
        'tools_missing_short': 'fontTools missing',
        'load_failed_short': 'Load failed',
        'no_glyphs_short': 'No glyphs found',
        'export_title': 'Export Font',
        'export_need_glyph': 'Add at least one glyph first.',
        'export_single_source': 'Selected glyphs must come from exactly one source font.',
        'export_failed': 'Export failed: {error}',
        'export_created': 'Created {path}',
        'open_title': 'Open Font',
    },
    'settings': {
        'host': 'Host',
        'port': 'Port',
        'database': 'Database',
        'user': 'User',
        'password': 'Password',
        'sound_effects': 'Sound Effects',
        'sound_effects_tip': 'Play small confirmation/error sounds. Safe no-op if audio is unavailable.',
    },
    'prompt': {
        'selector.prompt': 'Prompt',
        'selector.workflow': 'Workflow',
        'selector.doctype': 'Doctype',
        'generator.title': 'Prompt Generator',
        'generator.generate': 'Generate',
        'generator.generate_tip': 'Save the matching workflow MD and regenerate the prompt',
        'generator.copy': 'Copy',
        'generator.color': 'Text',
        'generator.color_tip': 'Generated prompt text color',
        'generator.copy_hex': 'Copy Hex',
        'generator.placeholder': 'Your generated prompt will appear here.',
        'form.title': 'Prompt Title',
        'form.task': 'Task',
        'form.context': 'Context',
        'form.extra_instruction': 'Extra Instruction',
        'workflow_source': 'Workflow Source',
        'previous.copy': 'Copy',
        'previous.title': 'Previous Prompt',
        'previous.placeholder': 'Previous generated prompt will appear here so you can keep chaining prompts.',
        'home.workflow_source': 'Workflow source: {source}',
        'home.saved_prompt': 'Saved Prompt',
        'home.workflow': 'Workflow',
        'home.none': 'None',
        'home.generated_prompt': 'Generated Prompt',
        'select_prompt': 'Select a Prompt...',
        'new_prompt': 'New Prompt',
        'home.instruction_lines': 'Instruction Lines',
        'home.doctype_text': 'Doctype Text',
        'home.workflows': 'Workflows',
        'home.saved_prompts': 'Saved Prompts',
        'home.no_workflows': 'No workflows found.',
        'home.no_prompts': 'No saved prompts found yet.',
        'home.description': 'Prompt is a workflow-based prompt language and generator for LLM sessions.',
        'loaded_status': 'Loaded prompt: {name}',
        'empty_doctype_html': '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Doctype</title></head><body><p>Select a doctype to preview it.</p></body></html>',
        'empty_workflow_html': '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Workflow</title></head><body><p>Select a workflow to preview it.</p></body></html>',
        'load_doctype_failed': 'Load Doctype Failed',
    },
    'editor': {
        'info.editing': 'Editing: {path}',
        'info.workflow_compiled': 'Editing: {source} | Compiled: {compiled}',
    },
    'help': {
        'prompt_help_title': 'Prompt Help',
        'full_help_missing': '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Prompt Help</title></head><body><h1>Prompt Help</h1><p>The full help file was not found at help/prompt_help.html.</p></body></html>',
    },
    'status': {
        'language_set': 'Language set: {language}',
        'compiled_cache': 'Compiled workflow cache in background.',
        'reloaded': 'Reloaded Prompt.',
        'saved_db': 'Saved DB settings.',
        'generate_rebuilt_only': 'Generate rebuilt prompt only; workflow MD not saved: {reason}',
        'generate_save_failed': 'Generate rebuilt prompt, but workflow save failed: {error}',
        'generate_saved_workflow': 'Generated prompt and saved workflow MD: {name}',
        'saved_prompt': 'Saved prompt: {prompt_name} (+ {html_name})',
        'sounds_enabled': 'Sound effects enabled.',
        'sounds_disabled': 'Sound effects disabled.',
    },
    'dialogs': {
        'open_prompt': 'Open Prompt',
        'save_prompt': 'Save Prompt',
        'save_workflow_failed': 'Save Workflow Failed',
    },
    'js': {
        'copied_suffix': 'Copied',
        'copy_failed': 'Copy failed',
        'instructions': 'Instructions',
        'workflow_markdown': 'Workflow Markdown',
        'ready': 'Ready',
        'lines': 'lines',
        'chars': 'chars',
        'generate_requested': 'Generate requested',
        'regenerated_locally': 'Regenerated locally',
        'nothing_to_copy': 'Nothing to copy',
    },
}


def _prompt_translate_text(overrides: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    merged = copy.deepcopy(PROMPT_TEXT_EN)
    for section, values in overrides.items():
        merged.setdefault(str(section), {}).update({str(key): str(value) for key, value in values.items()})
    return merged


PROMPT_TEXT_ES: dict[str, dict[str, str]] = _prompt_translate_text({
    'menus': {'file': 'Archivo', 'language': 'Idioma', 'help': 'Ayuda'},
    'actions': {'new_prompt': 'Nuevo prompt', 'open_prompt': 'Abrir prompt...', 'save_prompt': 'Guardar prompt', 'save_workflow': 'Guardar flujo de trabajo', 'save_doctype': 'Guardar tipo de documento', 'open_font': 'Abrir fuente...', 'reload_everything': 'Recargar todo', 'about_prompt': 'Acerca de Prompt', 'about_prompt_ellipsis': 'Acerca de Prompt...'},
    'toolbars': {'main': 'Principal', 'language': 'Idioma'},
    'tabs': {'prompts': 'Prompts', 'doctypes': 'Tipos de documento', 'fonts': 'Fuentes', 'help': 'Ayuda', 'settings': 'Ajustes'},
    'panes': {'prompt': 'Prompt', 'doctype': 'Tipo de documento', 'workflow': 'Flujo de trabajo', 'fonts': '<b>Fuentes</b>'},
    'buttons': {'save': 'Guardar', 'home': 'Inicio', 'reload': 'Recargar', 'close': 'Cerrar', 'save_db': 'Guardar ajustes de BD'},
    'groups': {'about_prompt': 'Acerca de Prompt', 'db_connection': 'Conexión de BD'},
    'labels': {'prompt': 'Prompt', 'workflow': 'Flujo de trabajo', 'doctype': 'Tipo de documento'},
    'about': {'title': 'Acerca de Prompt', 'description': 'Un banco de trabajo de escritorio para crear prompts, tipos de documento y flujos de trabajo reales que funcionan con fuerza desde el inicio.', 'pet_caption': 'Pequeño compañero ágil listo para trabajar. ¿No es ágil?', 'version_heading': 'Versión', 'author_heading': 'Autor', 'app_label': 'Aplicación', 'released_label': 'Publicado', 'license_label': 'Licencia', 'license_text': 'Publicado bajo la licencia MIT', 'free_estimate': '¡Llame para una estimación gratis de su próximo proyecto! (PHP, Python, móvil)', 'footer': 'Programado con <span class="heart">♥</span> por ChatGPT'},
    'source': {'copy_selection': '📋 Copiar selección', 'view_source': '📄 Ver código fuente', 'view_source_title': 'Ver código fuente', 'save_as': 'Guardar fuente como...', 'save_dialog': 'Guardar fuente como', 'save_filter': 'Archivos HTML (*.html *.htm);;Archivos de texto (*.txt);;Todos los archivos (*)', 'saved_status': 'Guardado {name}', 'copy_source': 'Copiar fuente', 'find_menu': 'Buscar', 'find_action': 'Buscar...', 'find_replace': 'Buscar / reemplazar...', 'find_next': 'Buscar siguiente', 'find_previous': 'Buscar anterior', 'show_plain_text': 'Mostrar texto plano', 'find_label': 'Buscar:', 'find_placeholder': 'Buscar en el código…', 'previous': 'Anterior', 'next': 'Siguiente', 'replace_label': 'Reemplazar:', 'replace_current': 'Reemplazar', 'replace_all': 'Reemplazar todo', 'replace_placeholder': 'Reemplazar con…'},
    'fonts': {'open': 'Abrir fuente...', 'reload': 'Recargar fuentes', 'add_selected': 'Agregar selección →', 'remove_selected': 'Quitar selección', 'clear': 'Limpiar', 'export': 'Exportar fuente...', 'bundled_font': 'Fuente incluida', 'builder_hint': 'Haga doble clic en un glifo o arrástrelo a esta columna.', 'inventory': 'Fuentes incluidas: {count} | Arrastre o haga doble clic en glifos para crear un nuevo subconjunto.', 'none_found': 'No se encontraron fuentes incluidas en /fonts/.', 'missing_tools': '{target} | falta fontTools.', 'load_failed': '{target} | no se pudieron cargar glifos: {error}', 'loaded': 'Se cargaron {count} glifos de {name}.', 'no_glyphs': '{target} | no se encontraron glifos.', 'group.bundled': 'Fuentes incluidas', 'group.glyph_atlas': 'Atlas de glifos', 'group.selected_glyph': 'Glifo seleccionado', 'group.builder': 'Constructor de fuente nueva', 'field.preview': 'Vista previa', 'field.plain_text': 'Texto plano', 'field.int_value': 'Valor entero', 'field.uplus_code': 'Código U+', 'field.glyph_name': 'Nombre del glifo', 'field.font_path': 'Ruta de fuente', 'field.name': 'Nombre', 'field.type': 'Tipo', 'no_fonts_short': 'No hay fuentes en /fonts/.', 'tools_missing_short': 'falta fontTools', 'load_failed_short': 'Error al cargar', 'no_glyphs_short': 'No hay glifos', 'export_title': 'Exportar fuente', 'export_need_glyph': 'Agregue al menos un glifo primero.', 'export_single_source': 'Los glifos seleccionados deben venir exactamente de una fuente.', 'export_failed': 'Error al exportar: {error}', 'export_created': 'Creado {path}', 'open_title': 'Abrir fuente'},
    'settings': {'host': 'Host', 'port': 'Puerto', 'database': 'Base de datos', 'user': 'Usuario', 'password': 'Contraseña', 'sound_effects': 'Efectos de sonido', 'sound_effects_tip': 'Reproducir sonidos breves de confirmación/error. No hace nada si el audio no está disponible.'},
    'prompt': {'selector.workflow': 'Flujo de trabajo', 'selector.doctype': 'Tipo de documento', 'generator.title': 'Generador de prompts', 'generator.generate': 'Generar', 'generator.generate_tip': 'Guarda el MD del flujo correspondiente y regenera el prompt', 'generator.copy': 'Copiar', 'generator.color': 'Texto', 'generator.color_tip': 'Color del texto del prompt generado', 'generator.copy_hex': 'Copiar hex', 'generator.placeholder': 'Su prompt generado aparecerá aquí.', 'form.title': 'Título del prompt', 'form.task': 'Tarea', 'form.context': 'Contexto', 'form.extra_instruction': 'Instrucción adicional', 'workflow_source': 'Fuente del flujo', 'previous.copy': 'Copiar', 'previous.title': 'Prompt anterior', 'previous.placeholder': 'El prompt generado anterior aparecerá aquí para que pueda encadenar prompts.', 'home.workflow_source': 'Fuente del flujo: {source}', 'home.saved_prompt': 'Prompt guardado', 'home.workflow': 'Flujo de trabajo', 'home.none': 'Ninguno', 'home.generated_prompt': 'Prompt generado', 'select_prompt': 'Seleccionar un prompt...', 'new_prompt': 'Nuevo prompt', 'home.instruction_lines': 'Líneas de instrucción', 'home.doctype_text': 'Texto del tipo de documento', 'home.workflows': 'Flujos de trabajo', 'home.saved_prompts': 'Prompts guardados', 'home.no_workflows': 'No se encontraron flujos de trabajo.', 'home.no_prompts': 'Todavía no hay prompts guardados.', 'home.description': 'Prompt es un lenguaje y generador de prompts basado en flujos de trabajo para sesiones de LLM.', 'loaded_status': 'Prompt cargado: {name}', 'empty_doctype_html': '<!DOCTYPE html><html lang="es"><head><meta charset="utf-8"><title>Tipo de documento</title></head><body><p>Seleccione un tipo de documento para previsualizarlo.</p></body></html>', 'empty_workflow_html': '<!DOCTYPE html><html lang="es"><head><meta charset="utf-8"><title>Flujo de trabajo</title></head><body><p>Seleccione un flujo de trabajo para previsualizarlo.</p></body></html>', 'load_doctype_failed': 'Error al cargar tipo de documento'},
    'editor': {'info.editing': 'Editando: {path}', 'info.workflow_compiled': 'Editando: {source} | Compilado: {compiled}'},
    'help': {'prompt_help_title': 'Ayuda de Prompt', 'full_help_missing': '<!DOCTYPE html><html lang="es"><head><meta charset="utf-8"><title>Ayuda de Prompt</title></head><body><h1>Ayuda de Prompt</h1><p>No se encontró el archivo de ayuda completo en help/prompt_help.html.</p></body></html>'},
    'status': {'language_set': 'Idioma seleccionado: {language}', 'compiled_cache': 'Caché del flujo compilado en segundo plano.', 'reloaded': 'Prompt recargado.', 'saved_db': 'Ajustes de BD guardados.', 'generate_rebuilt_only': 'Generate reconstruyó solo el prompt; el MD del flujo no se guardó: {reason}', 'generate_save_failed': 'Generate reconstruyó el prompt, pero falló guardar el flujo: {error}', 'generate_saved_workflow': 'Prompt generado y MD del flujo guardado: {name}', 'saved_prompt': 'Prompt guardado: {prompt_name} (+ {html_name})', 'sounds_enabled': 'Efectos de sonido activados.', 'sounds_disabled': 'Efectos de sonido desactivados.'},
    'dialogs': {'open_prompt': 'Abrir prompt', 'save_prompt': 'Guardar prompt', 'save_workflow_failed': 'Error al guardar flujo'},
    'js': {'copied_suffix': 'Copiado', 'copy_failed': 'Error al copiar', 'instructions': 'Instrucciones', 'workflow_markdown': 'Markdown del flujo', 'ready': 'Listo', 'lines': 'líneas', 'chars': 'caracteres', 'generate_requested': 'Generación solicitada', 'regenerated_locally': 'Regenerado localmente', 'nothing_to_copy': 'Nada que copiar'},
})

PROMPT_TEXT_HI: dict[str, dict[str, str]] = _prompt_translate_text({
    'menus': {'file': 'फ़ाइल', 'language': 'भाषा', 'help': 'सहायता'},
    'actions': {'new_prompt': 'नया प्रॉम्प्ट', 'open_prompt': 'प्रॉम्प्ट खोलें...', 'save_prompt': 'प्रॉम्प्ट सहेजें', 'save_workflow': 'वर्कफ़्लो सहेजें', 'save_doctype': 'दस्तावेज़ प्रकार सहेजें', 'open_font': 'फ़ॉन्ट खोलें...', 'reload_everything': 'सब कुछ फिर से लोड करें', 'about_prompt': 'Prompt के बारे में', 'about_prompt_ellipsis': 'Prompt के बारे में...'},
    'toolbars': {'main': 'मुख्य', 'language': 'भाषा'},
    'tabs': {'prompts': 'प्रॉम्प्ट', 'doctypes': 'दस्तावेज़ प्रकार', 'fonts': 'फ़ॉन्ट', 'help': 'सहायता', 'settings': 'सेटिंग्स'},
    'panes': {'prompt': 'प्रॉम्प्ट', 'doctype': 'दस्तावेज़ प्रकार', 'workflow': 'वर्कफ़्लो', 'fonts': '<b>फ़ॉन्ट</b>'},
    'buttons': {'save': 'सहेजें', 'home': 'होम', 'reload': 'रीलोड', 'close': 'बंद करें', 'save_db': 'DB सेटिंग्स सहेजें'},
    'groups': {'about_prompt': 'Prompt के बारे में', 'db_connection': 'DB कनेक्शन'},
    'labels': {'prompt': 'प्रॉम्प्ट', 'workflow': 'वर्कफ़्लो', 'doctype': 'दस्तावेज़ प्रकार'},
    'about': {'title': 'Prompt के बारे में', 'description': 'वास्तविक दुनिया के प्रॉम्प्ट, दस्तावेज़ प्रकार और वर्कफ़्लो बनाने के लिए एक डेस्कटॉप वर्कबेंच।', 'pet_caption': 'छोटा फुर्तीला साथी ड्यूटी के लिए तैयार है। सच में फुर्तीला है, है ना?', 'version_heading': 'संस्करण', 'author_heading': 'लेखक', 'app_label': 'ऐप', 'released_label': 'रिलीज़', 'license_label': 'लाइसेंस', 'license_text': 'MIT लाइसेंस के तहत जारी', 'free_estimate': 'अपने अगले प्रोजेक्ट के लिए मुफ्त अनुमान हेतु कॉल करें! (PHP, Python, Mobile)', 'footer': 'ChatGPT द्वारा <span class="heart">♥</span> के साथ कोड किया गया'},
    'source': {'copy_selection': '📋 चयन कॉपी करें', 'view_source': '📄 स्रोत देखें', 'view_source_title': 'स्रोत देखें', 'save_as': 'स्रोत ऐसे सहेजें...', 'save_dialog': 'स्रोत ऐसे सहेजें', 'save_filter': 'HTML फ़ाइलें (*.html *.htm);;टेक्स्ट फ़ाइलें (*.txt);;सभी फ़ाइलें (*)', 'saved_status': '{name} सहेजा गया', 'copy_source': 'स्रोत कॉपी करें', 'find_menu': 'ढूंढें', 'find_action': 'ढूंढें...', 'find_replace': 'ढूंढें / बदलें...', 'find_next': 'अगला ढूंढें', 'find_previous': 'पिछला ढूंढें', 'show_plain_text': 'सादा पाठ दिखाएं', 'find_label': 'ढूंढें:', 'find_placeholder': 'स्रोत में ढूंढें…', 'previous': 'पिछला', 'next': 'अगला', 'replace_label': 'बदलें:', 'replace_current': 'बदलें', 'replace_all': 'सब बदलें', 'replace_placeholder': 'इससे बदलें…'},
    'fonts': {'open': 'फ़ॉन्ट खोलें...', 'reload': 'फ़ॉन्ट रीलोड करें', 'add_selected': 'चयन जोड़ें →', 'remove_selected': 'चयन हटाएं', 'clear': 'साफ़ करें', 'export': 'फ़ॉन्ट निर्यात करें...', 'bundled_font': 'साथ दिया गया फ़ॉन्ट', 'builder_hint': 'किसी glyph पर डबल-क्लिक करें या उसे इस कॉलम में खींचें।', 'inventory': 'साथ दिए गए फ़ॉन्ट: {count} | नया subset बनाने के लिए glyphs खींचें या डबल-क्लिक करें।', 'none_found': '/fonts/ में कोई bundled font नहीं मिला।', 'missing_tools': '{target} | fontTools उपलब्ध नहीं है।', 'load_failed': '{target} | glyphs लोड नहीं हुए: {error}', 'loaded': '{name} से {count} glyphs लोड हुए।', 'no_glyphs': '{target} | कोई glyph नहीं मिला।', 'group.bundled': 'Bundled Fonts', 'group.glyph_atlas': 'Glyph Atlas', 'group.selected_glyph': 'चयनित glyph', 'group.builder': 'नया फ़ॉन्ट बिल्डर', 'field.preview': 'पूर्वावलोकन', 'field.plain_text': 'सादा पाठ', 'field.int_value': 'पूर्णांक मान', 'field.uplus_code': 'U+ कोड', 'field.glyph_name': 'Glyph नाम', 'field.font_path': 'फ़ॉन्ट पथ', 'field.name': 'नाम', 'field.type': 'प्रकार', 'no_fonts_short': '/fonts/ में कोई फ़ॉन्ट नहीं मिला।', 'tools_missing_short': 'fontTools उपलब्ध नहीं', 'load_failed_short': 'लोड विफल', 'no_glyphs_short': 'कोई glyph नहीं मिला', 'export_title': 'फ़ॉन्ट निर्यात करें', 'export_need_glyph': 'पहले कम से कम एक glyph जोड़ें।', 'export_single_source': 'चयनित glyphs केवल एक स्रोत फ़ॉन्ट से होने चाहिए।', 'export_failed': 'निर्यात विफल: {error}', 'export_created': '{path} बनाया गया', 'open_title': 'फ़ॉन्ट खोलें'},
    'settings': {'host': 'होस्ट', 'port': 'पोर्ट', 'database': 'डेटाबेस', 'user': 'उपयोगकर्ता', 'password': 'पासवर्ड', 'sound_effects': 'ध्वनि प्रभाव', 'sound_effects_tip': 'छोटे confirmation/error sounds चलाएं। ऑडियो उपलब्ध न हो तो सुरक्षित no-op।'},
    'prompt': {'selector.workflow': 'वर्कफ़्लो', 'selector.doctype': 'दस्तावेज़ प्रकार', 'generator.title': 'प्रॉम्प्ट जनरेटर', 'generator.generate': 'जनरेट करें', 'generator.generate_tip': 'मिलता हुआ workflow MD सहेजें और prompt फिर से बनाएं', 'generator.copy': 'कॉपी करें', 'generator.color': 'टेक्स्ट', 'generator.color_tip': 'Generated prompt text color', 'generator.copy_hex': 'Hex कॉपी करें', 'generator.placeholder': 'आपका generated prompt यहाँ दिखाई देगा।', 'form.title': 'Prompt शीर्षक', 'form.task': 'कार्य', 'form.context': 'संदर्भ', 'form.extra_instruction': 'अतिरिक्त निर्देश', 'workflow_source': 'Workflow स्रोत', 'previous.copy': 'कॉपी करें', 'previous.title': 'पिछला Prompt', 'previous.placeholder': 'पिछला generated prompt यहाँ दिखेगा ताकि आप prompts chain कर सकें।', 'home.workflow_source': 'Workflow स्रोत: {source}', 'home.saved_prompt': 'सहेजा गया Prompt', 'home.workflow': 'वर्कफ़्लो', 'home.none': 'कोई नहीं', 'home.generated_prompt': 'Generated Prompt', 'select_prompt': 'एक Prompt चुनें...', 'new_prompt': 'नया Prompt', 'home.instruction_lines': 'निर्देश पंक्तियाँ', 'home.doctype_text': 'दस्तावेज़ प्रकार पाठ', 'home.workflows': 'वर्कफ़्लो', 'home.saved_prompts': 'सहेजे गए Prompts', 'home.no_workflows': 'कोई workflow नहीं मिला।', 'home.no_prompts': 'अभी कोई saved prompt नहीं है।', 'home.description': 'Prompt LLM sessions के लिए workflow-based prompt language और generator है।', 'loaded_status': 'Prompt लोड हुआ: {name}', 'empty_doctype_html': '<!DOCTYPE html><html lang="hi"><head><meta charset="utf-8"><title>दस्तावेज़ प्रकार</title></head><body><p>Preview के लिए document type चुनें।</p></body></html>', 'empty_workflow_html': '<!DOCTYPE html><html lang="hi"><head><meta charset="utf-8"><title>वर्कफ़्लो</title></head><body><p>Preview के लिए workflow चुनें।</p></body></html>', 'load_doctype_failed': 'दस्तावेज़ प्रकार लोड विफल'},
    'editor': {'info.editing': 'संपादन: {path}', 'info.workflow_compiled': 'संपादन: {source} | संकलित: {compiled}'},
    'help': {'prompt_help_title': 'Prompt सहायता', 'full_help_missing': '<!DOCTYPE html><html lang="hi"><head><meta charset="utf-8"><title>Prompt सहायता</title></head><body><h1>Prompt सहायता</h1><p>पूरा help file help/prompt_help.html पर नहीं मिला।</p></body></html>'},
    'status': {'language_set': 'भाषा सेट की गई: {language}', 'compiled_cache': 'Compiled workflow cache background में बनाया गया।', 'reloaded': 'Prompt reloaded.', 'saved_db': 'DB settings saved.', 'generate_rebuilt_only': 'Generate ने केवल prompt rebuilt किया; workflow MD saved नहीं हुआ: {reason}', 'generate_save_failed': 'Generate ने prompt rebuilt किया, लेकिन workflow save failed: {error}', 'generate_saved_workflow': 'Prompt generated और workflow MD saved: {name}', 'saved_prompt': 'Prompt saved: {prompt_name} (+ {html_name})', 'sounds_enabled': 'ध्वनि प्रभाव चालू।', 'sounds_disabled': 'ध्वनि प्रभाव बंद।'},
    'dialogs': {'open_prompt': 'Prompt खोलें', 'save_prompt': 'Prompt सहेजें', 'save_workflow_failed': 'Workflow save failed'},
    'js': {'copied_suffix': 'कॉपी हुआ', 'copy_failed': 'कॉपी विफल', 'instructions': 'निर्देश', 'workflow_markdown': 'Workflow Markdown', 'ready': 'तैयार', 'lines': 'पंक्तियाँ', 'chars': 'अक्षर', 'generate_requested': 'Generate requested', 'regenerated_locally': 'Locally regenerated', 'nothing_to_copy': 'कॉपी करने को कुछ नहीं'},
})

PROMPT_TEXT_ZH: dict[str, dict[str, str]] = _prompt_translate_text({
    'menus': {'file': '文件', 'language': '语言', 'help': '帮助'},
    'actions': {'new_prompt': '新建提示词', 'open_prompt': '打开提示词...', 'save_prompt': '保存提示词', 'save_workflow': '保存工作流', 'save_doctype': '保存文档类型', 'open_font': '打开字体...', 'reload_everything': '重新加载全部', 'about_prompt': '关于 Prompt', 'about_prompt_ellipsis': '关于 Prompt...'},
    'toolbars': {'main': '主工具栏', 'language': '语言'},
    'tabs': {'prompts': '提示词', 'doctypes': '文档类型', 'fonts': '字体', 'help': '帮助', 'settings': '设置'},
    'panes': {'prompt': '提示词', 'doctype': '文档类型', 'workflow': '工作流', 'fonts': '<b>字体</b>'},
    'buttons': {'save': '保存', 'home': '主页', 'reload': '重新加载', 'close': '关闭', 'save_db': '保存数据库设置'},
    'groups': {'about_prompt': '关于 Prompt', 'db_connection': '数据库连接'},
    'labels': {'prompt': '提示词', 'workflow': '工作流', 'doctype': '文档类型'},
    'about': {'title': '关于 Prompt', 'description': '一个桌面提示词工作台，用于构建真实可用的提示词、文档类型和工作流。', 'pet_caption': '机灵的小伙伴准备上岗。他是不是很机灵？', 'version_heading': '版本', 'author_heading': '作者', 'app_label': '应用', 'released_label': '发布', 'license_label': '许可证', 'license_text': '基于 MIT 许可证发布', 'free_estimate': '欢迎来电获取下一个项目的免费估价！(PHP、Python、移动端)', 'footer': '由 ChatGPT 用 <span class="heart">♥</span> 编写'},
    'source': {'copy_selection': '📋 复制所选内容', 'view_source': '📄 查看源代码', 'view_source_title': '查看源代码', 'save_as': '源代码另存为...', 'save_dialog': '源代码另存为', 'save_filter': 'HTML 文件 (*.html *.htm);;文本文件 (*.txt);;所有文件 (*)', 'saved_status': '已保存 {name}', 'copy_source': '复制源代码', 'find_menu': '查找', 'find_action': '查找...', 'find_replace': '查找 / 替换...', 'find_next': '查找下一个', 'find_previous': '查找上一个', 'show_plain_text': '显示纯文本', 'find_label': '查找:', 'find_placeholder': '在源代码中查找…', 'previous': '上一个', 'next': '下一个', 'replace_label': '替换:', 'replace_current': '替换', 'replace_all': '全部替换', 'replace_placeholder': '替换为…'},
    'fonts': {'open': '打开字体...', 'reload': '重新加载字体', 'add_selected': '添加所选 →', 'remove_selected': '移除所选', 'clear': '清除', 'export': '导出字体...', 'bundled_font': '内置字体', 'builder_hint': '双击一个字形，或将它拖到此列。', 'inventory': '内置字体: {count} | 拖动或双击字形以构建新的字体子集。', 'none_found': '在 /fonts/ 中未找到内置字体。', 'missing_tools': '{target} | 缺少 fontTools。', 'load_failed': '{target} | 加载字形失败: {error}', 'loaded': '已从 {name} 加载 {count} 个字形。', 'no_glyphs': '{target} | 未找到字形。', 'group.bundled': '内置字体', 'group.glyph_atlas': '字形图集', 'group.selected_glyph': '所选字形', 'group.builder': '新字体构建器', 'field.preview': '预览', 'field.plain_text': '纯文本', 'field.int_value': '整数值', 'field.uplus_code': 'U+ 代码', 'field.glyph_name': '字形名称', 'field.font_path': '字体路径', 'field.name': '名称', 'field.type': '类型', 'no_fonts_short': '在 /fonts/ 中未找到字体。', 'tools_missing_short': '缺少 fontTools', 'load_failed_short': '加载失败', 'no_glyphs_short': '未找到字形', 'export_title': '导出字体', 'export_need_glyph': '请先添加至少一个字形。', 'export_single_source': '所选字形必须来自同一个源字体。', 'export_failed': '导出失败: {error}', 'export_created': '已创建 {path}', 'open_title': '打开字体'},
    'settings': {'host': '主机', 'port': '端口', 'database': '数据库', 'user': '用户', 'password': '密码', 'sound_effects': '音效', 'sound_effects_tip': '播放简短确认/错误音效。若音频不可用则安全跳过。'},
    'prompt': {'selector.prompt': '提示词', 'selector.workflow': '工作流', 'selector.doctype': '文档类型', 'generator.title': '提示词生成器', 'generator.generate': '生成', 'generator.generate_tip': '保存匹配的工作流 MD 并重新生成提示词', 'generator.copy': '复制', 'generator.color': '文本', 'generator.color_tip': '生成提示词的文本颜色', 'generator.copy_hex': '复制十六进制', 'generator.placeholder': '生成的提示词将显示在这里。', 'form.title': '提示词标题', 'form.task': '任务', 'form.context': '上下文', 'form.extra_instruction': '附加指令', 'workflow_source': '工作流来源', 'previous.copy': '复制', 'previous.title': '上一个提示词', 'previous.placeholder': '上一个生成的提示词会显示在这里，方便继续串联。', 'home.workflow_source': '工作流来源: {source}', 'home.saved_prompt': '已保存提示词', 'home.workflow': '工作流', 'home.none': '无', 'home.generated_prompt': '生成的提示词', 'select_prompt': '选择提示词...', 'new_prompt': '新建提示词', 'home.instruction_lines': '指令行', 'home.doctype_text': '文档类型文本', 'home.workflows': '工作流', 'home.saved_prompts': '已保存提示词', 'home.no_workflows': '未找到工作流。', 'home.no_prompts': '尚未保存任何提示词。', 'home.description': 'Prompt 是用于 LLM 会话的工作流式提示词语言和生成器。', 'loaded_status': '已加载提示词: {name}', 'empty_doctype_html': '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>文档类型</title></head><body><p>选择一个文档类型进行预览。</p></body></html>', 'empty_workflow_html': '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>工作流</title></head><body><p>选择一个工作流进行预览。</p></body></html>', 'load_doctype_failed': '加载文档类型失败'},
    'editor': {'info.editing': '正在编辑: {path}', 'info.workflow_compiled': '正在编辑: {source} | 已编译: {compiled}'},
    'help': {'prompt_help_title': 'Prompt 帮助', 'full_help_missing': '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>Prompt 帮助</title></head><body><h1>Prompt 帮助</h1><p>未在 help/prompt_help.html 找到完整帮助文件。</p></body></html>'},
    'status': {'language_set': '语言已设置: {language}', 'compiled_cache': '已在后台编译工作流缓存。', 'reloaded': '已重新加载 Prompt。', 'saved_db': '已保存数据库设置。', 'generate_rebuilt_only': 'Generate 仅重建了提示词；未保存工作流 MD: {reason}', 'generate_save_failed': 'Generate 已重建提示词，但保存工作流失败: {error}', 'generate_saved_workflow': '已生成提示词并保存工作流 MD: {name}', 'saved_prompt': '已保存提示词: {prompt_name} (+ {html_name})', 'sounds_enabled': '音效已开启。', 'sounds_disabled': '音效已关闭。'},
    'dialogs': {'open_prompt': '打开提示词', 'save_prompt': '保存提示词', 'save_workflow_failed': '保存工作流失败'},
    'js': {'copied_suffix': '已复制', 'copy_failed': '复制失败', 'instructions': '指令', 'workflow_markdown': '工作流 Markdown', 'ready': '就绪', 'lines': '行', 'chars': '字符', 'generate_requested': '已请求生成', 'regenerated_locally': '已本地重新生成', 'nothing_to_copy': '没有可复制内容'},
})

PROMPT_TEXT_RU: dict[str, dict[str, str]] = _prompt_translate_text({
    'menus': {'file': 'Файл', 'language': 'Язык', 'help': 'Справка'},
    'actions': {'new_prompt': 'Новый промпт', 'open_prompt': 'Открыть промпт...', 'save_prompt': 'Сохранить промпт', 'save_workflow': 'Сохранить рабочий процесс', 'save_doctype': 'Сохранить тип документа', 'open_font': 'Открыть шрифт...', 'reload_everything': 'Перезагрузить всё', 'about_prompt': 'О Prompt', 'about_prompt_ellipsis': 'О Prompt...'},
    'toolbars': {'main': 'Главная', 'language': 'Язык'},
    'tabs': {'prompts': 'Промпты', 'doctypes': 'Типы документов', 'fonts': 'Шрифты', 'help': 'Справка', 'settings': 'Настройки'},
    'panes': {'prompt': 'Промпт', 'doctype': 'Тип документа', 'workflow': 'Рабочий процесс', 'fonts': '<b>Шрифты</b>'},
    'buttons': {'save': 'Сохранить', 'home': 'Домой', 'reload': 'Перезагрузить', 'close': 'Закрыть', 'save_db': 'Сохранить настройки БД'},
    'groups': {'about_prompt': 'О Prompt', 'db_connection': 'Подключение к БД'},
    'labels': {'prompt': 'Промпт', 'workflow': 'Рабочий процесс', 'doctype': 'Тип документа'},
    'about': {'title': 'О Prompt', 'description': 'Настольная рабочая среда для создания реальных промптов, типов документов и рабочих процессов.', 'pet_caption': 'Шустрый маленький помощник готов к работе. Правда шустрый?', 'version_heading': 'Версия', 'author_heading': 'Автор', 'app_label': 'Приложение', 'released_label': 'Выпуск', 'license_label': 'Лицензия', 'license_text': 'Выпущено по лицензии MIT', 'free_estimate': 'Позвоните для бесплатной оценки вашего следующего проекта! (PHP, Python, Mobile)', 'footer': 'Создано с <span class="heart">♥</span> от ChatGPT'},
    'source': {'copy_selection': '📋 Копировать выделение', 'view_source': '📄 Показать исходник', 'view_source_title': 'Показать исходник', 'save_as': 'Сохранить исходник как...', 'save_dialog': 'Сохранить исходник как', 'save_filter': 'HTML-файлы (*.html *.htm);;Текстовые файлы (*.txt);;Все файлы (*)', 'saved_status': 'Сохранено {name}', 'copy_source': 'Копировать исходник', 'find_menu': 'Найти', 'find_action': 'Найти...', 'find_replace': 'Найти / заменить...', 'find_next': 'Найти следующее', 'find_previous': 'Найти предыдущее', 'show_plain_text': 'Показать обычный текст', 'find_label': 'Найти:', 'find_placeholder': 'Поиск в исходнике…', 'previous': 'Предыдущее', 'next': 'Следующее', 'replace_label': 'Заменить:', 'replace_current': 'Заменить', 'replace_all': 'Заменить всё', 'replace_placeholder': 'Заменить на…'},
    'settings': {'host': 'Хост', 'port': 'Порт', 'database': 'База данных', 'user': 'Пользователь', 'password': 'Пароль', 'sound_effects': 'Звуковые эффекты', 'sound_effects_tip': 'Проигрывать короткие звуки подтверждения/ошибки. Безопасно ничего не делает, если аудио недоступно.'},
    'prompt': {'selector.prompt': 'Промпт', 'selector.workflow': 'Рабочий процесс', 'selector.doctype': 'Тип документа', 'generator.title': 'Генератор промптов', 'generator.generate': 'Сгенерировать', 'generator.generate_tip': 'Сохранить подходящий workflow MD и заново сгенерировать промпт', 'generator.copy': 'Копировать', 'generator.color': 'Текст', 'generator.color_tip': 'Цвет текста сгенерированного промпта', 'generator.copy_hex': 'Копировать Hex', 'generator.placeholder': 'Ваш сгенерированный промпт появится здесь.', 'form.title': 'Заголовок промпта', 'form.task': 'Задача', 'form.context': 'Контекст', 'form.extra_instruction': 'Дополнительная инструкция', 'workflow_source': 'Источник рабочего процесса', 'previous.copy': 'Копировать', 'previous.title': 'Предыдущий промпт', 'previous.placeholder': 'Предыдущий сгенерированный промпт появится здесь для продолжения цепочки.', 'home.workflow_source': 'Источник workflow: {source}', 'home.saved_prompt': 'Сохранённый промпт', 'home.workflow': 'Рабочий процесс', 'home.none': 'Нет', 'home.generated_prompt': 'Сгенерированный промпт', 'select_prompt': 'Выберите промпт...', 'new_prompt': 'Новый промпт', 'home.instruction_lines': 'Строки инструкций', 'home.doctype_text': 'Текст типа документа', 'home.workflows': 'Рабочие процессы', 'home.saved_prompts': 'Сохранённые промпты', 'home.no_workflows': 'Рабочие процессы не найдены.', 'home.no_prompts': 'Сохранённых промптов пока нет.', 'home.description': 'Prompt — это workflow-язык промптов и генератор для LLM-сессий.', 'loaded_status': 'Загружен промпт: {name}', 'load_doctype_failed': 'Не удалось загрузить тип документа'},
    'status': {'language_set': 'Язык установлен: {language}', 'compiled_cache': 'Кэш workflow скомпилирован в фоне.', 'reloaded': 'Prompt перезагружен.', 'saved_db': 'Настройки БД сохранены.', 'generate_rebuilt_only': 'Generate только пересобрал промпт; workflow MD не сохранён: {reason}', 'generate_save_failed': 'Generate пересобрал промпт, но сохранение workflow не удалось: {error}', 'generate_saved_workflow': 'Промпт сгенерирован, workflow MD сохранён: {name}', 'saved_prompt': 'Промпт сохранён: {prompt_name} (+ {html_name})', 'sounds_enabled': 'Звуковые эффекты включены.', 'sounds_disabled': 'Звуковые эффекты отключены.'},
    'dialogs': {'open_prompt': 'Открыть промпт', 'save_prompt': 'Сохранить промпт', 'save_workflow_failed': 'Не удалось сохранить workflow'},
    'js': {'copied_suffix': 'Скопировано', 'copy_failed': 'Не удалось скопировать', 'instructions': 'Инструкции', 'workflow_markdown': 'Workflow Markdown', 'ready': 'Готово', 'lines': 'строк', 'chars': 'символов', 'generate_requested': 'Генерация запрошена', 'regenerated_locally': 'Перегенерировано локально', 'nothing_to_copy': 'Нечего копировать'},
})

PROMPT_TEXT_UK: dict[str, dict[str, str]] = _prompt_translate_text({
    'menus': {'file': 'Файл', 'language': 'Мова', 'help': 'Довідка'},
    'actions': {'new_prompt': 'Новий промпт', 'open_prompt': 'Відкрити промпт...', 'save_prompt': 'Зберегти промпт', 'save_workflow': 'Зберегти робочий процес', 'save_doctype': 'Зберегти тип документа', 'open_font': 'Відкрити шрифт...', 'reload_everything': 'Перезавантажити все', 'about_prompt': 'Про Prompt', 'about_prompt_ellipsis': 'Про Prompt...'},
    'toolbars': {'main': 'Головна', 'language': 'Мова'},
    'tabs': {'prompts': 'Промпти', 'doctypes': 'Типи документів', 'fonts': 'Шрифти', 'help': 'Довідка', 'settings': 'Налаштування'},
    'panes': {'prompt': 'Промпт', 'doctype': 'Тип документа', 'workflow': 'Робочий процес', 'fonts': '<b>Шрифти</b>'},
    'buttons': {'save': 'Зберегти', 'home': 'Головна', 'reload': 'Перезавантажити', 'close': 'Закрити', 'save_db': 'Зберегти налаштування БД'},
    'groups': {'about_prompt': 'Про Prompt', 'db_connection': 'Підключення до БД'},
    'labels': {'prompt': 'Промпт', 'workflow': 'Робочий процес', 'doctype': 'Тип документа'},
    'settings': {'host': 'Хост', 'port': 'Порт', 'database': 'База даних', 'user': 'Користувач', 'password': 'Пароль', 'sound_effects': 'Звукові ефекти', 'sound_effects_tip': 'Відтворювати короткі звуки підтвердження/помилки. Безпечно нічого не робить, якщо аудіо недоступне.'},
    'prompt': {'selector.prompt': 'Промпт', 'selector.workflow': 'Робочий процес', 'selector.doctype': 'Тип документа', 'generator.title': 'Генератор промптів', 'generator.generate': 'Згенерувати', 'generator.copy': 'Копіювати', 'generator.placeholder': 'Ваш згенерований промпт з’явиться тут.', 'form.title': 'Назва промпта', 'form.task': 'Завдання', 'form.context': 'Контекст', 'form.extra_instruction': 'Додаткова інструкція', 'previous.title': 'Попередній промпт', 'home.workflow_source': 'Джерело workflow: {source}', 'home.saved_prompt': 'Збережений промпт', 'home.workflow': 'Робочий процес', 'home.none': 'Немає', 'home.generated_prompt': 'Згенерований промпт', 'select_prompt': 'Виберіть промпт...', 'new_prompt': 'Новий промпт', 'home.no_workflows': 'Робочі процеси не знайдені.', 'home.no_prompts': 'Збережених промптів поки немає.', 'loaded_status': 'Завантажено промпт: {name}', 'load_doctype_failed': 'Не вдалося завантажити тип документа'},
    'source': {'copy_selection': '📋 Копіювати виділення', 'view_source': '📄 Переглянути джерело', 'save_as': 'Зберегти джерело як...', 'copy_source': 'Копіювати джерело', 'find_menu': 'Знайти', 'find_action': 'Знайти...', 'find_replace': 'Знайти / замінити...', 'find_next': 'Знайти далі', 'find_previous': 'Знайти попереднє', 'previous': 'Попереднє', 'next': 'Далі', 'replace_current': 'Замінити', 'replace_all': 'Замінити все'},
    'status': {'language_set': 'Мову встановлено: {language}', 'compiled_cache': 'Кеш workflow скомпільовано у фоні.', 'reloaded': 'Prompt перезавантажено.', 'saved_db': 'Налаштування БД збережено.', 'sounds_enabled': 'Звукові ефекти увімкнено.', 'sounds_disabled': 'Звукові ефекти вимкнено.'},
    'dialogs': {'open_prompt': 'Відкрити промпт', 'save_prompt': 'Зберегти промпт', 'save_workflow_failed': 'Не вдалося зберегти workflow'},
    'js': {'copied_suffix': 'Скопійовано', 'copy_failed': 'Не вдалося скопіювати', 'instructions': 'Інструкції', 'ready': 'Готово', 'lines': 'рядків', 'chars': 'символів', 'nothing_to_copy': 'Нічого копіювати'},
})

PROMPT_TEXT_DE: dict[str, dict[str, str]] = _prompt_translate_text({
    'menus': {'file': 'Datei', 'language': 'Sprache', 'help': 'Hilfe'},
    'actions': {'new_prompt': 'Neuer Prompt', 'open_prompt': 'Prompt öffnen...', 'save_prompt': 'Prompt speichern', 'save_workflow': 'Workflow speichern', 'save_doctype': 'Doctype speichern', 'open_font': 'Schrift öffnen...', 'reload_everything': 'Alles neu laden', 'about_prompt': 'Über Prompt', 'about_prompt_ellipsis': 'Über Prompt...'},
    'toolbars': {'main': 'Hauptleiste', 'language': 'Sprache'},
    'tabs': {'prompts': 'Prompts', 'doctypes': 'Doctypes', 'fonts': 'Schriften', 'help': 'Hilfe', 'settings': 'Einstellungen'},
    'panes': {'prompt': 'Prompt', 'doctype': 'Doctype', 'workflow': 'Workflow', 'fonts': '<b>Schriften</b>'},
    'buttons': {'save': 'Speichern', 'home': 'Start', 'reload': 'Neu laden', 'close': 'Schließen', 'save_db': 'DB-Einstellungen speichern'},
    'groups': {'about_prompt': 'Über Prompt', 'db_connection': 'DB-Verbindung'},
    'labels': {'prompt': 'Prompt', 'workflow': 'Workflow', 'doctype': 'Doctype'},
    'settings': {'host': 'Host', 'port': 'Port', 'database': 'Datenbank', 'user': 'Benutzer', 'password': 'Passwort', 'sound_effects': 'Soundeffekte', 'sound_effects_tip': 'Kurze Bestätigungs-/Fehlertöne abspielen. Sicherer No-op, wenn Audio nicht verfügbar ist.'},
    'prompt': {'selector.workflow': 'Workflow', 'selector.doctype': 'Doctype', 'generator.title': 'Prompt-Generator', 'generator.generate': 'Generieren', 'generator.copy': 'Kopieren', 'generator.placeholder': 'Ihr generierter Prompt erscheint hier.', 'form.title': 'Prompt-Titel', 'form.task': 'Aufgabe', 'form.context': 'Kontext', 'form.extra_instruction': 'Zusätzliche Anweisung', 'previous.title': 'Vorheriger Prompt', 'home.workflow_source': 'Workflow-Quelle: {source}', 'home.saved_prompt': 'Gespeicherter Prompt', 'home.workflow': 'Workflow', 'home.none': 'Keine', 'home.generated_prompt': 'Generierter Prompt', 'select_prompt': 'Prompt auswählen...', 'new_prompt': 'Neuer Prompt', 'home.no_workflows': 'Keine Workflows gefunden.', 'home.no_prompts': 'Noch keine gespeicherten Prompts.', 'loaded_status': 'Prompt geladen: {name}', 'load_doctype_failed': 'Doctype konnte nicht geladen werden'},
    'source': {'copy_selection': '📋 Auswahl kopieren', 'view_source': '📄 Quelle anzeigen', 'save_as': 'Quelle speichern unter...', 'copy_source': 'Quelle kopieren', 'find_menu': 'Suchen', 'find_action': 'Suchen...', 'find_replace': 'Suchen / Ersetzen...', 'find_next': 'Weiter suchen', 'find_previous': 'Vorherige suchen', 'previous': 'Zurück', 'next': 'Weiter', 'replace_current': 'Ersetzen', 'replace_all': 'Alle ersetzen'},
    'status': {'language_set': 'Sprache eingestellt: {language}', 'compiled_cache': 'Workflow-Cache im Hintergrund kompiliert.', 'reloaded': 'Prompt neu geladen.', 'saved_db': 'DB-Einstellungen gespeichert.', 'sounds_enabled': 'Soundeffekte aktiviert.', 'sounds_disabled': 'Soundeffekte deaktiviert.'},
    'dialogs': {'open_prompt': 'Prompt öffnen', 'save_prompt': 'Prompt speichern', 'save_workflow_failed': 'Workflow konnte nicht gespeichert werden'},
    'js': {'copied_suffix': 'Kopiert', 'copy_failed': 'Kopieren fehlgeschlagen', 'instructions': 'Anweisungen', 'ready': 'Bereit', 'lines': 'Zeilen', 'chars': 'Zeichen', 'nothing_to_copy': 'Nichts zu kopieren'},
})

PROMPT_TEXT_FR: dict[str, dict[str, str]] = _prompt_translate_text({
    'menus': {'file': 'Fichier', 'language': 'Langue', 'help': 'Aide'},
    'actions': {'new_prompt': 'Nouveau prompt', 'open_prompt': 'Ouvrir un prompt...', 'save_prompt': 'Enregistrer le prompt', 'save_workflow': 'Enregistrer le workflow', 'save_doctype': 'Enregistrer le doctype', 'open_font': 'Ouvrir une police...', 'reload_everything': 'Tout recharger', 'about_prompt': 'À propos de Prompt', 'about_prompt_ellipsis': 'À propos de Prompt...'},
    'toolbars': {'main': 'Principal', 'language': 'Langue'},
    'tabs': {'prompts': 'Prompts', 'doctypes': 'Doctypes', 'fonts': 'Polices', 'help': 'Aide', 'settings': 'Paramètres'},
    'panes': {'prompt': 'Prompt', 'doctype': 'Doctype', 'workflow': 'Workflow', 'fonts': '<b>Polices</b>'},
    'buttons': {'save': 'Enregistrer', 'home': 'Accueil', 'reload': 'Recharger', 'close': 'Fermer', 'save_db': 'Enregistrer les paramètres BD'},
    'groups': {'about_prompt': 'À propos de Prompt', 'db_connection': 'Connexion BD'},
    'labels': {'prompt': 'Prompt', 'workflow': 'Workflow', 'doctype': 'Doctype'},
    'settings': {'host': 'Hôte', 'port': 'Port', 'database': 'Base de données', 'user': 'Utilisateur', 'password': 'Mot de passe', 'sound_effects': 'Effets sonores', 'sound_effects_tip': 'Joue de courts sons de confirmation/erreur. Sans effet si l’audio est indisponible.'},
    'prompt': {'selector.workflow': 'Workflow', 'selector.doctype': 'Doctype', 'generator.title': 'Générateur de prompts', 'generator.generate': 'Générer', 'generator.copy': 'Copier', 'generator.placeholder': 'Votre prompt généré apparaîtra ici.', 'form.title': 'Titre du prompt', 'form.task': 'Tâche', 'form.context': 'Contexte', 'form.extra_instruction': 'Instruction supplémentaire', 'previous.title': 'Prompt précédent', 'home.workflow_source': 'Source du workflow : {source}', 'home.saved_prompt': 'Prompt enregistré', 'home.workflow': 'Workflow', 'home.none': 'Aucun', 'home.generated_prompt': 'Prompt généré', 'select_prompt': 'Sélectionner un prompt...', 'new_prompt': 'Nouveau prompt', 'home.no_workflows': 'Aucun workflow trouvé.', 'home.no_prompts': 'Aucun prompt enregistré pour le moment.', 'loaded_status': 'Prompt chargé : {name}', 'load_doctype_failed': 'Échec du chargement du doctype'},
    'source': {'copy_selection': '📋 Copier la sélection', 'view_source': '📄 Voir la source', 'save_as': 'Enregistrer la source sous...', 'copy_source': 'Copier la source', 'find_menu': 'Rechercher', 'find_action': 'Rechercher...', 'find_replace': 'Rechercher / remplacer...', 'find_next': 'Suivant', 'find_previous': 'Précédent', 'previous': 'Précédent', 'next': 'Suivant', 'replace_current': 'Remplacer', 'replace_all': 'Tout remplacer'},
    'status': {'language_set': 'Langue définie : {language}', 'compiled_cache': 'Cache du workflow compilé en arrière-plan.', 'reloaded': 'Prompt rechargé.', 'saved_db': 'Paramètres BD enregistrés.', 'sounds_enabled': 'Effets sonores activés.', 'sounds_disabled': 'Effets sonores désactivés.'},
    'dialogs': {'open_prompt': 'Ouvrir un prompt', 'save_prompt': 'Enregistrer le prompt', 'save_workflow_failed': 'Échec de l’enregistrement du workflow'},
    'js': {'copied_suffix': 'Copié', 'copy_failed': 'Échec de la copie', 'instructions': 'Instructions', 'ready': 'Prêt', 'lines': 'lignes', 'chars': 'caractères', 'nothing_to_copy': 'Rien à copier'},
})


PROMPT_TEXT_COMPLETION_TRANSLATIONS: dict[PromptLanguage, dict[str, str]] = {
    PromptLanguage.ES: {
        'app.title': 'Prompt 1.0.0',
        'about.badge': 'Prompt 1.0.0',
        'about.hero_title': 'Prompt 1.0.0',
        'about.app_version': 'Prompt 1.0.0',
        'about.md5_label': 'MD5',
        'prompt.selector.prompt': 'Prompt',
        'source.close_symbol': '✕',
    },
    PromptLanguage.HI: {
        'app.title': 'Prompt 1.0.0',
        'about.badge': 'Prompt 1.0.0',
        'about.hero_title': 'Prompt 1.0.0',
        'about.app_version': 'Prompt 1.0.0',
        'about.md5_label': 'MD5',
        'prompt.selector.prompt': 'प्रॉम्प्ट',
        'source.close_symbol': '✕',
    },
    PromptLanguage.ZH: {
        'app.title': 'Prompt 1.0.0',
        'about.badge': 'Prompt 1.0.0',
        'about.hero_title': 'Prompt 1.0.0',
        'about.app_version': 'Prompt 1.0.0',
        'about.md5_label': 'MD5',
        'source.close_symbol': '✕',
    },
    PromptLanguage.RU: {
        'app.title': 'Prompt 1.0.0',
        'about.badge': 'Prompt 1.0.0',
        'about.hero_title': 'Prompt 1.0.0',
        'about.app_version': 'Prompt 1.0.0',
        'about.md5_label': 'MD5',
        'editor.info.editing': 'Редактируется: {path}',
        'editor.info.workflow_compiled': 'Редактируется: {source} | Скомпилировано: {compiled}',
        'fonts.open': 'Открыть шрифт...',
        'fonts.reload': 'Перезагрузить шрифты',
        'fonts.add_selected': 'Добавить выбранное →',
        'fonts.remove_selected': 'Удалить выбранное',
        'fonts.clear': 'Очистить',
        'fonts.export': 'Экспорт шрифта...',
        'fonts.bundled_font': 'Встроенный шрифт',
        'fonts.builder_hint': 'Дважды щёлкните глиф или перетащите его в этот столбец.',
        'fonts.inventory': 'Встроенные шрифты: {count} | Перетащите или дважды щёлкните глифы, чтобы создать новый поднабор шрифта.',
        'fonts.none_found': 'Встроенные шрифты в /fonts/ не найдены.',
        'fonts.missing_tools': '{target} | fontTools отсутствует.',
        'fonts.load_failed': '{target} | не удалось загрузить глифы: {error}',
        'fonts.loaded': 'Загружено глифов: {count} из {name}.',
        'fonts.no_glyphs': '{target} | глифы не найдены.',
        'fonts.group.bundled': 'Встроенные шрифты',
        'fonts.group.glyph_atlas': 'Атлас глифов',
        'fonts.group.selected_glyph': 'Выбранный глиф',
        'fonts.group.builder': 'Создание нового шрифта',
        'fonts.field.preview': 'Предпросмотр',
        'fonts.field.plain_text': 'Обычный текст',
        'fonts.field.int_value': 'Целое значение',
        'fonts.field.uplus_code': 'Код U+',
        'fonts.field.glyph_name': 'Имя глифа',
        'fonts.field.font_path': 'Путь к шрифту',
        'fonts.field.name': 'Имя',
        'fonts.field.type': 'Тип',
        'fonts.no_fonts_short': 'Шрифты в /fonts/ не найдены.',
        'fonts.tools_missing_short': 'fontTools отсутствует',
        'fonts.load_failed_short': 'Ошибка загрузки',
        'fonts.no_glyphs_short': 'Глифы не найдены',
        'fonts.export_title': 'Экспорт шрифта',
        'fonts.export_need_glyph': 'Сначала добавьте хотя бы один глиф.',
        'fonts.export_single_source': 'Выбранные глифы должны быть ровно из одного исходного шрифта.',
        'fonts.export_failed': 'Ошибка экспорта: {error}',
        'fonts.export_created': 'Создано: {path}',
        'fonts.open_title': 'Открыть шрифт',
        'help.prompt_help_title': 'Справка Prompt',
        'help.full_help_missing': '<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Справка Prompt</title></head><body><h1>Справка Prompt</h1><p>Полный файл справки не найден по пути help/prompt_help.html.</p></body></html>',
        'prompt.empty_doctype_html': '<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Тип документа</title></head><body><p>Выберите тип документа для предпросмотра.</p></body></html>',
        'prompt.empty_workflow_html': '<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Рабочий процесс</title></head><body><p>Выберите рабочий процесс для предпросмотра.</p></body></html>',
        'source.close_symbol': '✕',
    },
    PromptLanguage.UK: {
        'app.title': 'Prompt 1.0.0',
        'about.title': 'Про Prompt',
        'about.badge': 'Prompt 1.0.0',
        'about.hero_title': 'Prompt 1.0.0',
        'about.description': 'Настільна майстерня промптів для створення практичних промптів, типів документів і робочих процесів, які потужно працюють одразу.',
        'about.pet_caption': 'Спритний маленький помічник готовий до роботи. Правда ж, спритний?',
        'about.version_heading': 'Версія',
        'about.author_heading': 'Автор',
        'about.app_label': 'Застосунок',
        'about.app_version': 'Prompt 1.0.0',
        'about.md5_label': 'MD5',
        'about.released_label': 'Випущено',
        'about.license_label': 'Ліцензія',
        'about.license_text': 'Випущено за ліцензією MIT',
        'about.free_estimate': 'Зателефонуйте для безкоштовної оцінки вашого наступного проєкту! (PHP, Python, Mobile)',
        'about.footer': 'Створено з <span class="heart">♥</span> за допомогою ChatGPT',
        'editor.info.editing': 'Редагується: {path}',
        'editor.info.workflow_compiled': 'Редагується: {source} | Скомпільовано: {compiled}',
        'fonts.open': 'Відкрити шрифт...',
        'fonts.reload': 'Перезавантажити шрифти',
        'fonts.add_selected': 'Додати вибране →',
        'fonts.remove_selected': 'Вилучити вибране',
        'fonts.clear': 'Очистити',
        'fonts.export': 'Експорт шрифту...',
        'fonts.bundled_font': 'Вбудований шрифт',
        'fonts.builder_hint': 'Двічі клацніть гліф або перетягніть його в цей стовпець.',
        'fonts.inventory': 'Вбудовані шрифти: {count} | Перетягніть або двічі клацніть гліфи, щоб створити новий піднабір шрифту.',
        'fonts.none_found': 'Вбудовані шрифти у /fonts/ не знайдено.',
        'fonts.missing_tools': '{target} | fontTools відсутній.',
        'fonts.load_failed': '{target} | не вдалося завантажити гліфи: {error}',
        'fonts.loaded': 'Завантажено {count} гліфів із {name}.',
        'fonts.no_glyphs': '{target} | гліфи не знайдено.',
        'fonts.group.bundled': 'Вбудовані шрифти',
        'fonts.group.glyph_atlas': 'Атлас гліфів',
        'fonts.group.selected_glyph': 'Вибраний гліф',
        'fonts.group.builder': 'Конструктор нового шрифту',
        'fonts.field.preview': 'Попередній перегляд',
        'fonts.field.plain_text': 'Звичайний текст',
        'fonts.field.int_value': 'Ціле значення',
        'fonts.field.uplus_code': 'Код U+',
        'fonts.field.glyph_name': 'Назва гліфа',
        'fonts.field.font_path': 'Шлях до шрифту',
        'fonts.field.name': 'Назва',
        'fonts.field.type': 'Тип',
        'fonts.no_fonts_short': 'Шрифти у /fonts/ не знайдено.',
        'fonts.tools_missing_short': 'fontTools відсутній',
        'fonts.load_failed_short': 'Помилка завантаження',
        'fonts.no_glyphs_short': 'Гліфи не знайдено',
        'fonts.export_title': 'Експорт шрифту',
        'fonts.export_need_glyph': 'Спочатку додайте хоча б один гліф.',
        'fonts.export_single_source': 'Вибрані гліфи мають походити рівно з одного вихідного шрифту.',
        'fonts.export_failed': 'Помилка експорту: {error}',
        'fonts.export_created': 'Створено {path}',
        'fonts.open_title': 'Відкрити шрифт',
        'help.prompt_help_title': 'Довідка Prompt',
        'help.full_help_missing': '<!DOCTYPE html><html lang="uk"><head><meta charset="utf-8"><title>Довідка Prompt</title></head><body><h1>Довідка Prompt</h1><p>Повний файл довідки не знайдено за шляхом help/prompt_help.html.</p></body></html>',
        'js.workflow_markdown': 'Markdown робочого процесу',
        'js.generate_requested': 'Запит на генерацію надіслано',
        'js.regenerated_locally': 'Перегенеровано локально',
        'prompt.generator.generate_tip': 'Зберегти відповідний MD робочого процесу і перегенерувати промпт',
        'prompt.generator.color': 'Текст',
        'prompt.generator.color_tip': 'Колір тексту згенерованого промпта',
        'prompt.generator.copy_hex': 'Копіювати hex',
        'prompt.workflow_source': 'Джерело робочого процесу',
        'prompt.previous.copy': 'Копіювати',
        'prompt.previous.placeholder': 'Попередній згенерований промпт з’явиться тут, щоб ви могли продовжувати ланцюжок промптів.',
        'prompt.home.instruction_lines': 'Рядки інструкцій',
        'prompt.home.doctype_text': 'Текст типу документа',
        'prompt.home.workflows': 'Робочі процеси',
        'prompt.home.saved_prompts': 'Збережені промпти',
        'prompt.home.description': 'Prompt — це мова і генератор промптів на основі робочих процесів для LLM-сесій.',
        'prompt.empty_doctype_html': '<!DOCTYPE html><html lang="uk"><head><meta charset="utf-8"><title>Тип документа</title></head><body><p>Виберіть тип документа для попереднього перегляду.</p></body></html>',
        'prompt.empty_workflow_html': '<!DOCTYPE html><html lang="uk"><head><meta charset="utf-8"><title>Робочий процес</title></head><body><p>Виберіть робочий процес для попереднього перегляду.</p></body></html>',
        'source.view_source_title': 'Переглянути джерело',
        'source.save_dialog': 'Зберегти джерело як',
        'source.save_filter': 'HTML-файли (*.html *.htm);;Текстові файли (*.txt);;Усі файли (*)',
        'source.saved_status': 'Збережено {name}',
        'source.show_plain_text': 'Показати звичайний текст',
        'source.find_label': 'Знайти:',
        'source.find_placeholder': 'Знайти у джерелі…',
        'source.replace_label': 'Замінити:',
        'source.replace_placeholder': 'Замінити на…',
        'source.close_symbol': '✕',
        'status.generate_rebuilt_only': 'Generate лише перебудував промпт; MD робочого процесу не збережено: {reason}',
        'status.generate_save_failed': 'Generate перебудував промпт, але збереження робочого процесу не вдалося: {error}',
        'status.generate_saved_workflow': 'Промпт згенеровано і MD робочого процесу збережено: {name}',
        'status.saved_prompt': 'Промпт збережено: {prompt_name} (+ {html_name})',
    },
    PromptLanguage.DE: {
        'app.title': 'Prompt 1.0.0',
        'about.title': 'Über Prompt',
        'about.badge': 'Prompt 1.0.0',
        'about.hero_title': 'Prompt 1.0.0',
        'about.description': 'Eine Desktop-Prompt-Werkbank zum Erstellen praxistauglicher Prompts, Doctypes und Workflows, die sofort leistungsfähig sind.',
        'about.pet_caption': 'Der flinke kleine Kumpel meldet sich zum Dienst. Ist er nicht flink?',
        'about.version_heading': 'Version',
        'about.author_heading': 'Autor',
        'about.app_label': 'App',
        'about.app_version': 'Prompt 1.0.0',
        'about.md5_label': 'MD5',
        'about.released_label': 'Veröffentlicht',
        'about.license_label': 'Lizenz',
        'about.license_text': 'Veröffentlicht unter der MIT-Lizenz',
        'about.free_estimate': 'Rufen Sie für eine kostenlose Schätzung Ihres nächsten Projekts an! (PHP, Python, Mobile)',
        'about.footer': 'Mit <span class="heart">♥</span> von ChatGPT programmiert',
        'editor.info.editing': 'Bearbeitung: {path}',
        'editor.info.workflow_compiled': 'Bearbeitung: {source} | Kompiliert: {compiled}',
        'fonts.open': 'Schrift öffnen...',
        'fonts.reload': 'Schriften neu laden',
        'fonts.add_selected': 'Auswahl hinzufügen →',
        'fonts.remove_selected': 'Auswahl entfernen',
        'fonts.clear': 'Leeren',
        'fonts.export': 'Schrift exportieren...',
        'fonts.bundled_font': 'Mitgelieferte Schrift',
        'fonts.builder_hint': 'Doppelklicken Sie auf ein Glyph oder ziehen Sie es in diese Spalte.',
        'fonts.inventory': 'Mitgelieferte Schriften: {count} | Ziehen oder doppelklicken Sie Glyphen, um eine neue Teilmenge zu erstellen.',
        'fonts.none_found': 'Keine mitgelieferten Schriften in /fonts/ gefunden.',
        'fonts.missing_tools': '{target} | fontTools fehlt.',
        'fonts.load_failed': '{target} | Glyphen konnten nicht geladen werden: {error}',
        'fonts.loaded': '{count} Glyphen aus {name} geladen.',
        'fonts.no_glyphs': '{target} | keine Glyphen gefunden.',
        'fonts.group.bundled': 'Mitgelieferte Schriften',
        'fonts.group.glyph_atlas': 'Glyph-Atlas',
        'fonts.group.selected_glyph': 'Ausgewähltes Glyph',
        'fonts.group.builder': 'Neuer Schrift-Builder',
        'fonts.field.preview': 'Vorschau',
        'fonts.field.plain_text': 'Klartext',
        'fonts.field.int_value': 'Ganzzahlwert',
        'fonts.field.uplus_code': 'U+-Code',
        'fonts.field.glyph_name': 'Glyph-Name',
        'fonts.field.font_path': 'Schriftpfad',
        'fonts.field.name': 'Name',
        'fonts.field.type': 'Typ',
        'fonts.no_fonts_short': 'Keine Schriften in /fonts/ gefunden.',
        'fonts.tools_missing_short': 'fontTools fehlt',
        'fonts.load_failed_short': 'Laden fehlgeschlagen',
        'fonts.no_glyphs_short': 'Keine Glyphen gefunden',
        'fonts.export_title': 'Schrift exportieren',
        'fonts.export_need_glyph': 'Fügen Sie zuerst mindestens ein Glyph hinzu.',
        'fonts.export_single_source': 'Ausgewählte Glyphen müssen genau aus einer Quellschrift stammen.',
        'fonts.export_failed': 'Export fehlgeschlagen: {error}',
        'fonts.export_created': '{path} erstellt',
        'fonts.open_title': 'Schrift öffnen',
        'help.prompt_help_title': 'Prompt-Hilfe',
        'help.full_help_missing': '<!DOCTYPE html><html lang="de"><head><meta charset="utf-8"><title>Prompt-Hilfe</title></head><body><h1>Prompt-Hilfe</h1><p>Die vollständige Hilfedatei wurde unter help/prompt_help.html nicht gefunden.</p></body></html>',
        'js.workflow_markdown': 'Workflow-Markdown',
        'js.generate_requested': 'Generierung angefordert',
        'js.regenerated_locally': 'Lokal neu generiert',
        'prompt.selector.prompt': 'Prompt',
        'prompt.generator.generate_tip': 'Passendes Workflow-MD speichern und den Prompt neu generieren',
        'prompt.generator.color': 'Text',
        'prompt.generator.color_tip': 'Textfarbe des generierten Prompts',
        'prompt.generator.copy_hex': 'Hex kopieren',
        'prompt.workflow_source': 'Workflow-Quelle',
        'prompt.previous.copy': 'Kopieren',
        'prompt.previous.placeholder': 'Der vorherige generierte Prompt erscheint hier, damit Sie Prompts weiter verketten können.',
        'prompt.home.instruction_lines': 'Anweisungszeilen',
        'prompt.home.doctype_text': 'Doctype-Text',
        'prompt.home.workflows': 'Workflows',
        'prompt.home.saved_prompts': 'Gespeicherte Prompts',
        'prompt.home.description': 'Prompt ist eine workflowbasierte Prompt-Sprache und ein Generator für LLM-Sitzungen.',
        'prompt.empty_doctype_html': '<!DOCTYPE html><html lang="de"><head><meta charset="utf-8"><title>Doctype</title></head><body><p>Wählen Sie einen Doctype aus, um ihn in der Vorschau anzuzeigen.</p></body></html>',
        'prompt.empty_workflow_html': '<!DOCTYPE html><html lang="de"><head><meta charset="utf-8"><title>Workflow</title></head><body><p>Wählen Sie einen Workflow aus, um ihn in der Vorschau anzuzeigen.</p></body></html>',
        'source.view_source_title': 'Quelle anzeigen',
        'source.save_dialog': 'Quelle speichern unter',
        'source.save_filter': 'HTML-Dateien (*.html *.htm);;Textdateien (*.txt);;Alle Dateien (*)',
        'source.saved_status': '{name} gespeichert',
        'source.show_plain_text': 'Klartext anzeigen',
        'source.find_label': 'Suchen:',
        'source.find_placeholder': 'In Quelle suchen…',
        'source.replace_label': 'Ersetzen:',
        'source.replace_placeholder': 'Ersetzen durch…',
        'source.close_symbol': '✕',
        'status.generate_rebuilt_only': 'Generate hat nur den Prompt neu aufgebaut; Workflow-MD nicht gespeichert: {reason}',
        'status.generate_save_failed': 'Generate hat den Prompt neu aufgebaut, aber das Speichern des Workflows ist fehlgeschlagen: {error}',
        'status.generate_saved_workflow': 'Prompt generiert und Workflow-MD gespeichert: {name}',
        'status.saved_prompt': 'Prompt gespeichert: {prompt_name} (+ {html_name})',
    },
    PromptLanguage.FR: {
        'app.title': 'Prompt 1.0.0',
        'about.title': 'À propos de Prompt',
        'about.badge': 'Prompt 1.0.0',
        'about.hero_title': 'Prompt 1.0.0',
        'about.description': 'Un atelier de bureau pour créer des prompts, des doctypes et des workflows concrets, puissants dès le départ.',
        'about.pet_caption': 'Petit compagnon vif au rapport. Il est vif, non ?',
        'about.version_heading': 'Version',
        'about.author_heading': 'Auteur',
        'about.app_label': 'Application',
        'about.app_version': 'Prompt 1.0.0',
        'about.md5_label': 'MD5',
        'about.released_label': 'Publié',
        'about.license_label': 'Licence',
        'about.license_text': 'Publié sous licence MIT',
        'about.free_estimate': 'Appelez pour une estimation gratuite de votre prochain projet ! (PHP, Python, Mobile)',
        'about.footer': 'Codé avec <span class="heart">♥</span> par ChatGPT',
        'editor.info.editing': 'Modification : {path}',
        'editor.info.workflow_compiled': 'Modification : {source} | Compilé : {compiled}',
        'fonts.open': 'Ouvrir une police...',
        'fonts.reload': 'Recharger les polices',
        'fonts.add_selected': 'Ajouter la sélection →',
        'fonts.remove_selected': 'Supprimer la sélection',
        'fonts.clear': 'Effacer',
        'fonts.export': 'Exporter la police...',
        'fonts.bundled_font': 'Police incluse',
        'fonts.builder_hint': 'Double-cliquez sur un glyphe ou faites-le glisser dans cette colonne.',
        'fonts.inventory': 'Polices incluses : {count} | Faites glisser ou double-cliquez sur des glyphes pour créer un nouveau sous-ensemble.',
        'fonts.none_found': 'Aucune police incluse trouvée dans /fonts/.',
        'fonts.missing_tools': '{target} | fontTools est manquant.',
        'fonts.load_failed': '{target} | échec du chargement des glyphes : {error}',
        'fonts.loaded': '{count} glyphes chargés depuis {name}.',
        'fonts.no_glyphs': '{target} | aucun glyphe trouvé.',
        'fonts.group.bundled': 'Polices incluses',
        'fonts.group.glyph_atlas': 'Atlas des glyphes',
        'fonts.group.selected_glyph': 'Glyphe sélectionné',
        'fonts.group.builder': 'Constructeur de nouvelle police',
        'fonts.field.preview': 'Aperçu',
        'fonts.field.plain_text': 'Texte brut',
        'fonts.field.int_value': 'Valeur entière',
        'fonts.field.uplus_code': 'Code U+',
        'fonts.field.glyph_name': 'Nom du glyphe',
        'fonts.field.font_path': 'Chemin de police',
        'fonts.field.name': 'Nom',
        'fonts.field.type': 'Type',
        'fonts.no_fonts_short': 'Aucune police trouvée dans /fonts/.',
        'fonts.tools_missing_short': 'fontTools manquant',
        'fonts.load_failed_short': 'Échec du chargement',
        'fonts.no_glyphs_short': 'Aucun glyphe trouvé',
        'fonts.export_title': 'Exporter la police',
        'fonts.export_need_glyph': 'Ajoutez d’abord au moins un glyphe.',
        'fonts.export_single_source': 'Les glyphes sélectionnés doivent provenir d’une seule police source.',
        'fonts.export_failed': 'Échec de l’export : {error}',
        'fonts.export_created': '{path} créé',
        'fonts.open_title': 'Ouvrir une police',
        'help.prompt_help_title': 'Aide de Prompt',
        'help.full_help_missing': '<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8"><title>Aide de Prompt</title></head><body><h1>Aide de Prompt</h1><p>Le fichier d’aide complet est introuvable à help/prompt_help.html.</p></body></html>',
        'js.workflow_markdown': 'Markdown du workflow',
        'js.generate_requested': 'Génération demandée',
        'js.regenerated_locally': 'Régénéré localement',
        'prompt.selector.prompt': 'Prompt',
        'prompt.generator.generate_tip': 'Enregistrer le MD du workflow correspondant et régénérer le prompt',
        'prompt.generator.color': 'Texte',
        'prompt.generator.color_tip': 'Couleur du texte du prompt généré',
        'prompt.generator.copy_hex': 'Copier hex',
        'prompt.workflow_source': 'Source du workflow',
        'prompt.previous.copy': 'Copier',
        'prompt.previous.placeholder': 'Le prompt généré précédent apparaîtra ici pour continuer à enchaîner les prompts.',
        'prompt.home.instruction_lines': 'Lignes d’instruction',
        'prompt.home.doctype_text': 'Texte du doctype',
        'prompt.home.workflows': 'Workflows',
        'prompt.home.saved_prompts': 'Prompts enregistrés',
        'prompt.home.description': 'Prompt est un langage et générateur de prompts basé sur les workflows pour les sessions LLM.',
        'prompt.empty_doctype_html': '<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8"><title>Doctype</title></head><body><p>Sélectionnez un doctype pour le prévisualiser.</p></body></html>',
        'prompt.empty_workflow_html': '<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8"><title>Workflow</title></head><body><p>Sélectionnez un workflow pour le prévisualiser.</p></body></html>',
        'source.view_source_title': 'Voir la source',
        'source.save_dialog': 'Enregistrer la source sous',
        'source.save_filter': 'Fichiers HTML (*.html *.htm);;Fichiers texte (*.txt);;Tous les fichiers (*)',
        'source.saved_status': '{name} enregistré',
        'source.show_plain_text': 'Afficher le texte brut',
        'source.find_label': 'Rechercher :',
        'source.find_placeholder': 'Rechercher dans la source…',
        'source.replace_label': 'Remplacer :',
        'source.replace_placeholder': 'Remplacer par…',
        'source.close_symbol': '✕',
        'status.generate_rebuilt_only': 'Generate a seulement reconstruit le prompt ; le MD du workflow n’a pas été enregistré : {reason}',
        'status.generate_save_failed': 'Generate a reconstruit le prompt, mais l’enregistrement du workflow a échoué : {error}',
        'status.generate_saved_workflow': 'Prompt généré et MD du workflow enregistré : {name}',
        'status.saved_prompt': 'Prompt enregistré : {prompt_name} (+ {html_name})',
    },
}


def _prompt_apply_flat_translations(target: dict[str, dict[str, str]], translations: dict[str, str]) -> None:
    category_aliases = {
        'menu': 'menus',
        'action': 'actions',
        'toolbar': 'toolbars',
        'tab': 'tabs',
        'pane': 'panes',
        'button': 'buttons',
        'group': 'groups',
        'label': 'labels',
        'dialog': 'dialogs',
    }
    for flat_key, text in dict(translations or {}).items():
        key_text = str(flat_key or '').strip()
        if '.' not in key_text:
            continue
        category, item_key = key_text.split('.', 1)
        section = category_aliases.get(category, category)
        target.setdefault(section, {})[item_key] = str(text)


for _prompt_language, _prompt_translations in PROMPT_TEXT_COMPLETION_TRANSLATIONS.items():
    _prompt_target = {
        PromptLanguage.ES: PROMPT_TEXT_ES,
        PromptLanguage.HI: PROMPT_TEXT_HI,
        PromptLanguage.ZH: PROMPT_TEXT_ZH,
        PromptLanguage.RU: PROMPT_TEXT_RU,
        PromptLanguage.UK: PROMPT_TEXT_UK,
        PromptLanguage.DE: PROMPT_TEXT_DE,
        PromptLanguage.FR: PROMPT_TEXT_FR,
    }.get(_prompt_language)
    if _prompt_target is not None:
        _prompt_apply_flat_translations(_prompt_target, _prompt_translations)



def _prompt_deepcopy_text(value: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    return {str(category): {str(key): str(text) for key, text in items.items()} for category, items in value.items()}


def _prompt_flatten_text(value: dict[str, dict[str, str]]) -> dict[str, str]:
    flattened: dict[str, str] = {}
    category_aliases = {
        'menus': 'menu',
        'actions': 'action',
        'toolbars': 'toolbar',
        'tabs': 'tab',
        'panes': 'pane',
        'buttons': 'button',
        'groups': 'group',
        'labels': 'label',
        'dialogs': 'dialog',
    }
    for category, items in value.items():
        prefix = category_aliases.get(category, category)
        for key, text in items.items():
            flattened[f'{prefix}.{key}'] = text
    return flattened


class Languages:
    activeLanguage: PromptLanguage = PromptLanguage.EN

    @classmethod
    def normalize(cls, language: Any, default: PromptLanguage = PromptLanguage.EN) -> PromptLanguage:
        if isinstance(language, PromptLanguage):
            return language
        if isinstance(language, int):
            try:
                return PromptLanguage(language)
            except Exception:
                captureException(None, source='prompt_app.py', context='except@1508')
                return default
        raw = str(language or '').strip().lower()
        aliases = {
            'en': PromptLanguage.EN, 'eng': PromptLanguage.EN, 'english': PromptLanguage.EN, 'ingles': PromptLanguage.EN, 'inglés': PromptLanguage.EN,
            'es': PromptLanguage.ES, 'esp': PromptLanguage.ES, 'spanish': PromptLanguage.ES, 'espanol': PromptLanguage.ES, 'español': PromptLanguage.ES, 'castellano': PromptLanguage.ES,
            'de': PromptLanguage.DE, 'ger': PromptLanguage.DE, 'german': PromptLanguage.DE, 'deutsch': PromptLanguage.DE,
            'ru': PromptLanguage.RU, 'rus': PromptLanguage.RU, 'russian': PromptLanguage.RU, 'русский': PromptLanguage.RU,
            'fr': PromptLanguage.FR, 'fre': PromptLanguage.FR, 'fresh': PromptLanguage.FR, 'french': PromptLanguage.FR, 'français': PromptLanguage.FR, 'francais': PromptLanguage.FR,
            'hi': PromptLanguage.HI, 'hin': PromptLanguage.HI, 'hindi': PromptLanguage.HI, 'indian': PromptLanguage.HI, 'india': PromptLanguage.HI, 'हिन्दी': PromptLanguage.HI,
            'zh': PromptLanguage.ZH, 'cn': PromptLanguage.ZH, 'zh-cn': PromptLanguage.ZH, 'chinese': PromptLanguage.ZH, 'china': PromptLanguage.ZH, 'simplified chinese': PromptLanguage.ZH, '简体中文': PromptLanguage.ZH, '中文': PromptLanguage.ZH,
            'uk': PromptLanguage.UK, 'ua': PromptLanguage.UK, 'ukr': PromptLanguage.UK, 'ukrainian': PromptLanguage.UK, 'українська': PromptLanguage.UK,
        }
        return aliases.get(raw, default)

    @classmethod
    def code(cls, language: Any, default: PromptLanguage = PromptLanguage.EN) -> str:
        return PROMPT_LANGUAGE_CODES[cls.normalize(language, default=default)]

    @classmethod
    def changeLanguage(cls, language: Any) -> PromptLanguage:
        cls.activeLanguage = cls.normalize(language, default=cls.activeLanguage)
        os.environ['PROMPT_LANGUAGE'] = PROMPT_LANGUAGE_CODES[cls.activeLanguage]
        return cls.activeLanguage


class PromptLocalization:
    ENGLISH = 'EN'
    SPANISH = 'ES'
    GERMAN = 'DE'
    RUSSIAN = 'RU'
    FRENCH = 'FR'
    HINDI = 'HI'
    CHINESE = 'ZH'
    UKRAINIAN = 'UK'
    VALID = set(PROMPT_LANGUAGE_FROM_CODE.keys())
    TEXT: dict[PromptLanguage, dict[str, dict[str, str]]] = {
        PromptLanguage.EN: _prompt_deepcopy_text(PROMPT_TEXT_EN),
        PromptLanguage.ES: _prompt_deepcopy_text(PROMPT_TEXT_ES),
        PromptLanguage.HI: _prompt_deepcopy_text(PROMPT_TEXT_HI),
        PromptLanguage.ZH: _prompt_deepcopy_text(PROMPT_TEXT_ZH),
        PromptLanguage.RU: _prompt_deepcopy_text(PROMPT_TEXT_RU),
        PromptLanguage.UK: _prompt_deepcopy_text(PROMPT_TEXT_UK),
        PromptLanguage.DE: _prompt_deepcopy_text(PROMPT_TEXT_DE),
        PromptLanguage.FR: _prompt_deepcopy_text(PROMPT_TEXT_FR),
    }
    STRINGS: dict[str, dict[str, str]] = {PROMPT_LANGUAGE_CODES[language]: _prompt_flatten_text(text) for language, text in TEXT.items()}

    @classmethod
    def language_enum(cls, value: Any, default: PromptLanguage = PromptLanguage.EN) -> PromptLanguage:
        if isinstance(value, str) and value.strip().upper() in PROMPT_LANGUAGE_FROM_CODE:
            return PROMPT_LANGUAGE_FROM_CODE[value.strip().upper()]
        return Languages.normalize(value, default=default)

    @classmethod
    def normalize(cls, value: Any, default: str = ENGLISH) -> str:
        raw = str(value or '').strip()
        default_raw = str(default or '').strip().upper()
        if not raw:
            if default_raw in PROMPT_LANGUAGE_FROM_CODE:
                return PROMPT_LANGUAGE_CODES[PROMPT_LANGUAGE_FROM_CODE[default_raw]]
            return EMPTY_STRING if default_raw == EMPTY_STRING else cls.ENGLISH
        upper = raw.upper()
        if upper in PROMPT_LANGUAGE_FROM_CODE:
            return PROMPT_LANGUAGE_CODES[PROMPT_LANGUAGE_FROM_CODE[upper]]
        default_language = PROMPT_LANGUAGE_FROM_CODE.get(default_raw, PromptLanguage.EN)
        normalized = cls.language_enum(raw, default=default_language)
        if normalized == default_language and default_raw == EMPTY_STRING:
            return EMPTY_STRING
        return PROMPT_LANGUAGE_CODES[normalized]

    @classmethod
    def cli_language(cls, argv: Any = None) -> str:
        tokens = [str(token or '').strip() for token in list(argv or sys.argv or []) if str(token or '').strip()]
        lowered = [token.lower() for token in tokens]
        for token in lowered:
            if token in {'en', '-en', '--en', '/en', 'english', '-english', '--english', '/english'}:
                return cls.ENGLISH
            if token in {'es', '-es', '--es', '/es', 'spanish', '-spanish', '--spanish', '/spanish'}:
                return cls.SPANISH
            if token in {'de', '-de', '--de', '/de', 'german', '-german', '--german', '/german'}:
                return cls.GERMAN
            if token in {'ru', '-ru', '--ru', '/ru', 'russian', '-russian', '--russian', '/russian'}:
                return cls.RUSSIAN
            if token in {'fr', '-fr', '--fr', '/fr', 'fresh', '-fresh', '--fresh', '/fresh', 'french', '-french', '--french', '/french'}:
                return cls.FRENCH
            if token in {'hi', '-hi', '--hi', '/hi', 'hindi', '-hindi', '--hindi', '/hindi', 'indian', '-indian', '--indian', '/indian'}:
                return cls.HINDI
            if token in {'zh', '-zh', '--zh', '/zh', 'cn', '-cn', '--cn', '/cn', 'chinese', '-chinese', '--chinese', '/chinese'}:
                return cls.CHINESE
            if token in {'uk', '-uk', '--uk', '/uk', 'ua', '-ua', '--ua', '/ua', 'ukrainian', '-ukrainian', '--ukrainian', '/ukrainian'}:
                return cls.UKRAINIAN
            for prefix in ('--language=', '-language=', '/language=', 'language=', '--lang=', '-lang=', '/lang=', 'lang='):
                if token.startswith(prefix):
                    return cls.normalize(token.split('=', 1)[1])
        return ''

    @classmethod
    def config_language(cls, root: Path) -> str:
        config_path = Path(root) / 'config.ini'
        parser = configparser.ConfigParser()
        if not config_path.exists():
            return ''
        try:
            parser.read(config_path, encoding='utf-8')
            for section, option in (('ui', 'language'), ('prompt', 'language'), ('language', 'current')):
                if parser.has_option(section, option):
                    return cls.normalize(parser.get(section, option, fallback=''))
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@1594')
            _log_exception('PROMPT:LOCALIZATION-CONFIG-READ-FAILED', exc, include_traceback=False)
        return ''

    @classmethod
    def save_config_language(cls, root: Path, language: str) -> None:
        config_path = Path(root) / 'config.ini'
        parser = configparser.ConfigParser()
        try:
            if config_path.exists():
                parser.read(config_path, encoding='utf-8')
            if not parser.has_section('ui'):
                parser.add_section('ui')
            parser.set('ui', 'language', cls.normalize(language))
            with File.tracedOpen(config_path, 'w', encoding='utf-8') as handle:
                parser.write(handle)
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@1610')
            _log_exception('PROMPT:LOCALIZATION-CONFIG-WRITE-FAILED', exc, include_traceback=False)

    @classmethod
    def resolve(cls, root: Path, settings: Any = None, argv: Any = None) -> str:
        cli = cls.cli_language(argv)
        if cli:
            return cli
        env_value = cls.normalize(os.environ.get('PROMPT_LANGUAGE', ''), default='')
        if env_value:
            return env_value
        cfg = cls.config_language(root)
        if cfg:
            return cfg
        try:
            if settings is not None:
                stored = str(settings.get_value('ui.language', '') or '').strip()
                if stored:
                    return cls.normalize(stored)
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@1629')
            _log_exception('PROMPT:LOCALIZATION-SETTINGS-READ-FAILED', exc, include_traceback=False)
        return cls.ENGLISH

    def __init__(self, language: str = ENGLISH) -> None:
        self.language = self.normalize(language)
        Languages.changeLanguage(self.language)

    def set_language(self, language: str) -> str:
        self.language = self.normalize(language)
        Languages.changeLanguage(self.language)
        return self.language

    def t(self, key: str, **kwargs: Any) -> str:
        value = self.STRINGS.get(self.language, self.STRINGS[self.ENGLISH]).get(key)
        if value is None:
            value = self.STRINGS[self.ENGLISH].get(key, key)
        try:
            return str(value).format(**kwargs)
        except Exception:
            captureException(None, source='prompt_app.py', context='except@1648')
            return str(value)

    def js_text_map(self) -> dict[str, str]:
        return {
            'copiedSuffix': self.t('js.copied_suffix'),
            'text': self.t('prompt.generator.color'),
            'copyFailed': self.t('js.copy_failed'),
            'instructionsBlock': self.t('js.instructions'),
            'workflowMarkdownBlock': self.t('js.workflow_markdown'),
            'doctypeBlock': self.t('label.doctype'),
            'generatedPrompt': self.t('prompt.home.generated_prompt'),
            'ready': self.t('js.ready'),
            'lines': self.t('js.lines'),
            'chars': self.t('js.chars'),
            'generateRequested': self.t('js.generate_requested'),
            'regeneratedLocally': self.t('js.regenerated_locally'),
            'nothingToCopy': self.t('js.nothing_to_copy'),
            'generatedPromptLabel': self.t('prompt.home.generated_prompt'),
            'colorHexLabel': self.t('prompt.generator.copy_hex'),
        }

def prompt_ui_text(key: str, **kwargs: Any) -> str:
    """English-first central UI string lookup for non-window helpers.

    This keeps generated HTML, source viewers, modal helpers, and standalone panes
    on the same associative PromptLocalization table as the main window.
    """
    try:
        root = Path(os.environ.get('PROMPT_RUNTIME_ROOT') or os.environ.get('PROMPT_BUNDLE_ROOT') or Path(__file__).resolve().parent)
        return PromptLocalization(PromptLocalization.resolve(root)).t(key, **kwargs)
    except Exception:
        captureException(None, source='prompt_app.py', context='except@1679')
        try:
            return PromptLocalization(PromptLocalization.ENGLISH).t(key, **kwargs)
        except Exception:
            captureException(None, source='prompt_app.py', context='except@1682')
            return str(key)


class PromptColor:
    def __init__(self, raw_value: str = '') -> None:
        self.raw_value = str(raw_value or '').strip()  # noqa: nonconform
        self.hex_value = self._normalize(self.raw_value)  # noqa: nonconform

    def __str__(self) -> str:
        return self.hex_value

    @classmethod
    def from_value(cls, value: str) -> 'PromptColor':
        return cls(value)

    @classmethod
    def _normalize(cls, value: str) -> str:
        raw = str(value or '').strip()
        if not raw:
            return '#000000'
        lowered = raw.lower().strip()
        if lowered in CSS_NAMED_COLORS:
            return CSS_NAMED_COLORS[lowered]
        if lowered.startswith('rgb'):
            nums = [max(0, min(255, int(float(part)))) for part in re.findall(r'[-+]?\d+(?:\.\d+)?', lowered)[:3]]
            while len(nums) < 3:
                nums.append(0)
            return '#{:02x}{:02x}{:02x}'.format(*nums[:3])
        if any(token in lowered for token in ('r=', 'g=', 'b=')):
            pairs = dict((k.lower(), v) for k, v in re.findall(r'([rgbRGB])\s*=\s*([#0-9xa-fA-F]+)', raw))
            nums = [cls._parse_component(str(pairs.get(key, '0'))) for key in ('r', 'g', 'b')]
            return '#{:02x}{:02x}{:02x}'.format(*nums[:3])
        compact = lowered.replace('0x', '').replace('#', '').replace(',', '').replace(';', '').replace(' ', '')
        if re.fullmatch(r'[0-9a-f]+', compact):
            if len(compact) == 3:
                return '#' + ''.join(ch * 2 for ch in compact)
            if len(compact) == 4:
                return '#' + ''.join(ch * 2 for ch in compact[:3])
            if len(compact) >= 6:
                return '#' + compact[:6]
        numbers = re.findall(r'[-+]?\d+', raw)
        if len(numbers) >= 3:
            comps = [cls._parse_component(value) for value in numbers[:3]]
            return '#{:02x}{:02x}{:02x}'.format(*comps)
        if len(numbers) == 1:
            component = cls._parse_component(numbers[0])
            return '#{:02x}{:02x}{:02x}'.format(component, component, component)
        return '#000000'

    @classmethod
    def _parse_component(cls, token: str) -> int:
        text = str(token or '0').strip().lower()
        if not text:
            return 0
        base = 10
        if text.startswith('0x'):
            text = text[2:]
            base = 16
        elif text.startswith('#'):
            text = text[1:]
            base = 16
        elif text.startswith('0') and len(text) > 1 and re.fullmatch(r'[0-9a-f]+', text):
            base = 16
        elif re.fullmatch(r'[0-9a-f]+', text) and any(ch in 'abcdef' for ch in text):
            base = 16
        try:
            value = int(text, base)
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@1750')
            print(f"[WARN:swallowed-exception] prompt_app.py:1171 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            try:
                value = int(float(text))
            except Exception as error:
                captureException(None, source='prompt_app.py', context='except@1754')
                print(f"[WARN:swallowed-exception] prompt_app.py:1174 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                value = 0
        return max(0, min(255, value))


def _language_flag_icon(language_key: str) -> QIcon:
    blob = str(LANGUAGE_FLAG_SVG_BLOBS.get(str(language_key or '').lower(), '') or '')
    if not blob:
        return QIcon()
    pixmap = QPixmap()
    try:
        pixmap.loadFromData(base64.b64decode(blob), 'SVG')
    except Exception as error:
        captureException(None, source='prompt_app.py', context='except@1767')
        print(f"[WARN:swallowed-exception] prompt_app.py:1186 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        return QIcon()
    return QIcon(pixmap)


def _keep_modeless_dialog_alive(dialog: QDialog) -> QDialog:
    try:
        _MODELLESS_SOURCE_DIALOGS.append(dialog)
    except Exception as error:
        captureException(None, source='prompt_app.py', context='except@1776')
        print(f"[WARN:swallowed-exception] prompt_app.py:1194 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        pass
    def _cleanup(*args):
        try:
            while dialog in _MODELLESS_SOURCE_DIALOGS:
                _MODELLESS_SOURCE_DIALOGS.remove(dialog)
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@1783')
            print(f"[WARN:swallowed-exception] prompt_app.py:1200 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
    try:
        dialog.destroyed.connect(_cleanup)
    except Exception as error:
        captureException(None, source='prompt_app.py', context='except@1788')
        print(f"[WARN:swallowed-exception] prompt_app.py:1204 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        pass
    return dialog


def _show_source_view_dialog_modeless(source_text: str, title: str = 'View Source', language_hint: str = 'html', parent: Any = None) -> QDialog:
    dialog = SourceViewDialog(source_text=source_text, title=title, language_hint=language_hint, parent=parent)
    try:
        dialog.setWindowModality(Qt.NonModal)
    except Exception as error:
        captureException(None, source='prompt_app.py', context='except@1798')
        print(f"[WARN:swallowed-exception] prompt_app.py:1213 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        pass
    try:
        dialog.setModal(False)
    except Exception as error:
        captureException(None, source='prompt_app.py', context='except@1803')
        print(f"[WARN:swallowed-exception] prompt_app.py:1217 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        pass
    try:
        dialog.setAttribute(Qt.WA_DeleteOnClose, True)
    except Exception as error:
        captureException(None, source='prompt_app.py', context='except@1808')
        print(f"[WARN:swallowed-exception] prompt_app.py:1221 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        pass
    try:
        dialog.setWindowFlag(Qt.Tool, True)
    except Exception as error:
        captureException(None, source='prompt_app.py', context='except@1813')
        print(f"[WARN:swallowed-exception] prompt_app.py:1225 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        pass
    try:
        dialog.setWindowFlag(Qt.WindowStaysOnTopHint, True)
    except Exception as error:
        captureException(None, source='prompt_app.py', context='except@1818')
        print(f"[WARN:swallowed-exception] prompt_app.py:1229 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        pass
    try:
        dialog.show()
        dialog.raise_()
    except Exception as error:
        captureException(None, source='prompt_app.py', context='except@1824')
        print(f"[WARN:swallowed-exception] prompt_app.py:1234 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        pass
    return _keep_modeless_dialog_alive(dialog)


def _debug_enabled() -> bool:
    return any(flag in sys.argv for flag in DEBUG_CLI_FLAGS)




def _prompt_run_log_path() -> Path:
    override = str(os.environ.get('PROMPT_RUN_LOG', '') or '').strip()
    if override:
        return Path(override).expanduser()
    try:
        if bool(getattr(sys, 'frozen', False)):
            exe_dir = Path(sys.executable).resolve().parent
            root = exe_dir.parent if exe_dir.name.lower() == 'dist' else exe_dir
            return root / 'run.log'
        return Path(__file__).resolve().parent / 'run.log'
    except Exception as error:
        captureException(error, source='prompt_app.py', context='prompt-run-log-path', handled=True)
        return Path.cwd() / 'run.log'


def _append_prompt_run_log(line: str) -> None:
    try:
        target = _prompt_run_log_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().isoformat(timespec='seconds')
        with File.tracedOpen(target, 'a', encoding='utf-8', errors='replace') as handle:
            for raw in (str(line or '').splitlines() or ['']):
                if raw.strip():
                    handle.write(f'{stamp} [pid={os.getpid()}] {raw.rstrip()}\n')
    except Exception as log_error:
        captureException(log_error, source='prompt_app.py', context='append-prompt-run-log', handled=True)
        print(f"[WARN:swallowed-exception] append-prompt-run-log {type(log_error).__name__}: {log_error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)

def _prompt_errors_log_path() -> Path:
    override = str(os.environ.get('PROMPT_ERRORS_LOG', '') or '').strip()
    if override:
        return Path(override).expanduser()
    if os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA') or str(Path.home() / 'AppData' / 'Local')
        return Path(base) / 'Prompt' / 'errors.txt'
    return Path.cwd() / 'errors.txt'


def _append_prompt_error_log(line: str) -> None:
    _append_prompt_run_log(line)
    try:
        target = _prompt_errors_log_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        with File.tracedOpen(target, 'a', encoding='utf-8', errors='replace') as handle:
            handle.write(line.rstrip() + '\n')
    except Exception:
        captureException(None, source='prompt_app.py', context='except@1850')
        pass


class _PromptErrorLogStream:
    """Mirror hidden-window stdout/stderr into errors.txt for installed EXE debugging."""

    def __init__(self, stream: object, label: str) -> None:
        self.stream = stream  # noqa: nonconform
        self.label = str(label or 'STREAM')  # noqa: nonconform
        self._buffer = EMPTY_STRING

    def write(self, value: object) -> int:
        text = str(value or EMPTY_STRING)
        try:
            if self.stream is not None and hasattr(self.stream, 'write'):
                cast(Any, self.stream).write(text)
        except Exception:
            captureException(None, source='prompt_app.py', context='except@1867')
            pass
        if text:
            self._buffer += text
            while '\n' in self._buffer:
                line, self._buffer = self._buffer.split('\n', 1)
                if line.strip():
                    _append_prompt_error_log(f'{datetime.datetime.now().isoformat(timespec="seconds")} [{self.label}] {line.rstrip()}')
        return len(text)

    def flush(self) -> None:
        try:
            if self._buffer.strip():
                _append_prompt_error_log(f'{datetime.datetime.now().isoformat(timespec="seconds")} [{self.label}] {self._buffer.rstrip()}')
                self._buffer = EMPTY_STRING
        except Exception:
            captureException(None, source='prompt_app.py', context='except@1882')
            pass
        try:
            if self.stream is not None and hasattr(self.stream, 'flush'):
                cast(Any, self.stream).flush()
        except Exception:
            captureException(None, source='prompt_app.py', context='except@1887')
            pass

    def isatty(self) -> bool:
        try:
            return bool(self.stream is not None and hasattr(self.stream, 'isatty') and cast(Any, self.stream).isatty())
        except Exception:
            captureException(None, source='prompt_app.py', context='except@1893')
            return False


def _install_prompt_error_log_streams() -> None:
    if getattr(sys, '_prompt_error_log_streams_installed', False):
        return
    try:
        setattr(sys, '_prompt_error_log_streams_installed', True)
        sys.stdout = cast(Any, _PromptErrorLogStream(getattr(sys, 'stdout', None), 'STDOUT'))
        sys.stderr = cast(Any, _PromptErrorLogStream(getattr(sys, 'stderr', None), 'STDERR'))
        def _excepthook(exc_type, exc_value, exc_tb):
            _append_prompt_error_log(f'{datetime.datetime.now().isoformat(timespec="seconds")} [UNHANDLED] {exc_type.__name__}: {exc_value}')
            for line in traceback.format_exception(exc_type, exc_value, exc_tb):
                _append_prompt_error_log(f'{datetime.datetime.now().isoformat(timespec="seconds")} [UNHANDLED:TRACEBACK] {line.rstrip()}')
        sys.excepthook = _excepthook
    except Exception:
        captureException(None, source='prompt_app.py', context='except@1909')
        pass


def _trace_event(tag: str, message: str, *, stderr: bool = False, screen: bool | None = None) -> None:
    text = f'[{tag}] {message}'
    timestamp = datetime.datetime.now().isoformat(timespec='seconds')
    _append_prompt_error_log(f'{timestamp} {text}')
    tag_upper = str(tag or '').upper()
    level = 'TRACE'
    if any(marker in tag_upper for marker in ('FATAL', 'ERROR', 'FAILED', 'FAULT', 'CRASH')):
        level = 'ERROR'
    elif any(marker in tag_upper for marker in ('WARN', 'WARNING', 'MISSING')):
        level = 'WARN'
    quiet_prefixes = (
        'PROMPT:ASSETS:SEED-SKIP-MD5-OK',
        'PROMPT:ASSETS:BUNDLE-SNAPSHOT',
        'PROMPT:PATHS:BUNDLE-CANDIDATES',
        'PROMPT:PATHS:BUNDLE-PROBE',
    )
    if screen is None:
        screen = not str(tag or '').startswith(quiet_prefixes)
    stream = 'stderr' if (stderr or level in {'WARN', 'ERROR', 'FATAL'}) else 'stdout'
    DebugLog.trace(text, level=level, source='prompt_app.py', stream=stream, to_screen=bool(screen))


def _md5_for_bytes(payload: bytes) -> str:
    try:
        return hashlib.md5(payload).hexdigest()
    except Exception:
        captureException(None, source='prompt_app.py', context='except@1929')
        return EMPTY_STRING


def _describe_local_file_for_trace(path: Path | None) -> str:
    if path is None:
        return 'local_path=None exists=False is_file=False parent_exists=False size=0 bytes=0 md5='
    try:
        candidate = Path(path)
        exists = candidate.exists()
        is_file = exists and candidate.is_file()
        parent_exists = candidate.parent.exists() if candidate.parent else False
        size = candidate.stat().st_size if is_file else 0
        md5 = EMPTY_STRING
        if is_file and size >= 0:
            try:
                md5 = hashlib.md5(candidate.read_bytes()).hexdigest()
            except Exception as digest_exc:
                captureException(None, source='prompt_app.py', context='except@1946')
                md5 = f'digest-error:{type(digest_exc).__name__}:{digest_exc}'
        return f'local_path={candidate} exists={exists} is_file={is_file} parent_exists={parent_exists} size={size} bytes={size} md5={md5}'
    except Exception as exc:
        captureException(None, source='prompt_app.py', context='except@1949')
        return f'local_path={path} describe_error={type(exc).__name__}:{exc}'


def _trace_html_asset(tag: str, path: Path, *, role: str, required: bool = False) -> bool:
    try:
        candidate = Path(path)
        exists = candidate.exists()
        level = tag if exists or not required else f'{tag}:MISSING'
        _trace_event(level, f'role={role} {_describe_local_file_for_trace(candidate)}')
        return bool(exists)
    except Exception as exc:
        captureException(None, source='prompt_app.py', context='except@1960')
        _trace_event(f'{tag}:ERROR', f'role={role} path={path} error={type(exc).__name__}:{exc}')
        return False


def _trace_prompt_bundle_snapshot(label: str) -> None:
    try:
        if prompt_embedded_data is None:
            _trace_event('PROMPT:ASSETS:BUNDLE-SNAPSHOT', f'label={label} import_ok=False error={PROMPT_EMBEDDED_DATA_IMPORT_ERROR!r}')
            return
        if hasattr(prompt_embedded_data, 'bundle_debug_snapshot'):
            snapshot = prompt_embedded_data.bundle_debug_snapshot()
            _trace_event('PROMPT:ASSETS:BUNDLE-SNAPSHOT', f'label={label} {json.dumps(snapshot, sort_keys=True, default=str)}')
            return
        _trace_event('PROMPT:ASSETS:BUNDLE-SNAPSHOT', f'label={label} import_ok=True file_count={getattr(prompt_embedded_data, "EMBEDDED_FILE_COUNT", "?")} raw_bytes={getattr(prompt_embedded_data, "EMBEDDED_RAW_BYTES", "?")}')
    except Exception as exc:
        captureException(None, source='prompt_app.py', context='except@1975')
        _trace_event('PROMPT:ASSETS:BUNDLE-SNAPSHOT-FAILED', f'label={label} error={type(exc).__name__}:{exc}')


def _trace_prompt_runtime_environment(label: str, *, bundle_root: Path | None = None, runtime_root: Path | None = None) -> None:
    try:
        payload = {
            'label': label,
            'cwd': str(Path.cwd()),
            'file': str(Path(__file__).resolve()),
            'executable': str(sys.executable),
            'argv': list(sys.argv),
            'frozen': bool(getattr(sys, 'frozen', False)),
            '_MEIPASS': str(getattr(sys, '_MEIPASS', '')),
            'bundle_root': str(bundle_root or EMPTY_STRING),
            'runtime_root': str(runtime_root or EMPTY_STRING),
            'errors_log': str(_prompt_errors_log_path()),
            'PROMPT_USER_ROOT': os.environ.get('PROMPT_USER_ROOT', EMPTY_STRING),
            'PROMPT_BUNDLE_ROOT': os.environ.get('PROMPT_BUNDLE_ROOT', EMPTY_STRING),
            'PROMPT_ASSETS_ROOT': os.environ.get('PROMPT_ASSETS_ROOT', EMPTY_STRING),
        }
        _trace_event('PROMPT:RUNTIME:ENV', json.dumps(payload, sort_keys=True, default=str))
    except Exception as exc:
        captureException(None, source='prompt_app.py', context='except@1997')
        _trace_event('PROMPT:RUNTIME:ENV-FAILED', f'label={label} error={type(exc).__name__}:{exc}')
    _trace_prompt_bundle_snapshot(label)


PROMPT_EMBEDDED_RUNTIME_PATCH = 'V164_INSTALL_RUNTIME_HTML_AND_WRITABLE_USERDATA'
PROMPT_EMBEDDED_ANCHOR_DIRS = ('assets', 'doctypes', 'help', 'js', 'prompts', 'vendor', 'workflows')
PROMPT_EMBEDDED_STATIC_PREFIXES = ('assets/help/', 'help/', 'js/', 'vendor/jquery/', 'vendor/prism/')


def _prompt_embedded_available() -> bool:
    if prompt_embedded_data is None or not hasattr(prompt_embedded_data, 'EMBEDDED_FILES'):
        return False
    try:
        if hasattr(prompt_embedded_data, 'BUNDLE') and hasattr(prompt_embedded_data.BUNDLE, 'available') and not prompt_embedded_data.BUNDLE.available():
            return False
    except Exception:
        captureException(None, source='prompt_app.py', context='except@2013')
        return False
    try:
        return int(getattr(prompt_embedded_data, 'EMBEDDED_FILE_COUNT', len(getattr(prompt_embedded_data, 'EMBEDDED_FILES', {}) or {})) or 0) > 0
    except Exception:
        captureException(None, source='prompt_app.py', context='except@2017')
        return bool(getattr(prompt_embedded_data, 'EMBEDDED_FILES', None))


def _prompt_embedded_file_names(prefix: str = '') -> list[str]:
    if not _prompt_embedded_available():
        return []
    try:
        return list(prompt_embedded_data.list_files(prefix))
    except Exception as exc:
        captureException(None, source='prompt_app.py', context='except@2026')
        _trace_event('PROMPT:EMBEDDED:LIST-FAILED', f'prefix={prefix} error={type(exc).__name__}:{exc}')
        return []


def _prompt_embedded_rel_for_path(path: Path | str) -> str:
    try:
        candidate = Path(path)
        parts = list(candidate.parts)
    except Exception:
        captureException(None, source='prompt_app.py', context='except@2035')
        return EMPTY_STRING
    for anchor in PROMPT_EMBEDDED_ANCHOR_DIRS:
        if anchor in parts:
            index = parts.index(anchor)
            rel = '/'.join(parts[index:])
            try:
                if _prompt_embedded_available() and prompt_embedded_data.has_file(rel):
                    return rel
            except Exception:
                captureException(None, source='prompt_app.py', context='except@2044')
                return EMPTY_STRING
    return EMPTY_STRING


def _prompt_embedded_read_text(rel_path: str) -> str:
    if not _prompt_embedded_available():
        raise FileNotFoundError(f'Prompt embedded data is unavailable: {PROMPT_EMBEDDED_DATA_IMPORT_ERROR!r}')
    return str(prompt_embedded_data.tracedReadText(rel_path))


def _read_prompt_text_with_embedded_fallback(path: Path | str, *, encoding: str = 'utf-8', errors: str = 'replace') -> str:
    candidate = Path(path)
    rel = _prompt_embedded_rel_for_path(candidate)
    exists = candidate.exists()
    size = candidate.stat().st_size if exists and candidate.is_file() else 0
    if exists and size > 0:
        payload = candidate.read_bytes()
        text = payload.decode(encoding, errors)
        _trace_event('PROMPT:FILE-LOAD:EXTERNAL', f'path={candidate} rel={rel or "-"} exists=True bytes={len(payload)} chars={len(text)} md5={_md5_for_bytes(payload)} {_describe_local_file_for_trace(candidate)}')
        return text
    if rel:
        payload = prompt_embedded_data.read_bytes(rel) if hasattr(prompt_embedded_data, 'read_bytes') else _prompt_embedded_read_text(rel).encode(encoding, errors='replace')
        text = payload.decode(encoding, errors)
        _trace_event('PROMPT:FILE-LOAD:EMBEDDED', f'path={candidate} rel={rel} external_exists={exists} external_size={size} bytes={len(payload)} chars={len(text)} md5={_md5_for_bytes(payload)}')
        return text
    _trace_event('PROMPT:FILE-LOAD:MISSING', f'path={candidate} rel=- exists={exists} size={size} {_describe_local_file_for_trace(candidate)}')
    if candidate.name == 'prompt_app.py' and _is_frozen_runtime():
        text = (
            '# Prompt source is not available in this bundled executable runtime.\n'
            f'# requested={candidate}\n'
            f'# executable={_prompt_executable_path()}\n'
            '# Re-run from the source repo to view prompt_app.py source.\n'
        )
        _trace_event('PROMPT:FILE-LOAD:BUNDLED-SOURCE-PLACEHOLDER', f'path={candidate} chars={len(text)}')
        return text
    return File.readText(candidate, encoding=encoding, errors=errors)


def _write_prompt_embedded_file(runtime_root: Path, rel: str, *, overwrite: bool = False) -> bool:
    target = Path(runtime_root) / rel
    try:
        if not _prompt_embedded_available():
            _trace_event('PROMPT:EMBEDDED:SEED-UNAVAILABLE', f'rel={rel} error={PROMPT_EMBEDDED_DATA_IMPORT_ERROR!r}')
            return False
        info = prompt_embedded_data.file_info(rel) if hasattr(prompt_embedded_data, 'file_info') else {}
        target_exists = target.exists()
        target_size = target.stat().st_size if target_exists and target.is_file() else 0
        target_matches = False
        if target_exists and target.is_file() and not overwrite and hasattr(prompt_embedded_data, 'target_matches'):
            target_matches = bool(prompt_embedded_data.target_matches(rel, target))
        elif target_exists and target.is_file() and not overwrite:
            expected_size = int(info.get('size', -1) or -1)
            target_matches = bool(target_size > 0 and (expected_size < 0 or target_size == expected_size))
        if target_matches and not overwrite:
            _trace_event('PROMPT:ASSETS:SEED-SKIP-MD5-OK', f'rel={rel} target={target} expected_size={info.get("size", "?")} expected_md5={info.get("md5", "?")} {_describe_local_file_for_trace(target)}')
            return False
        wrote = bool(prompt_embedded_data.write_file(rel, target, overwrite=True))
        if wrote:
            _prompt_make_user_writable(target)
        reason = 'overwrite' if overwrite else ('missing' if not target_exists else 'md5-or-size-mismatch')
        _trace_event('PROMPT:ASSETS:SEED-WRITE', f'rel={rel} target={target} reason={reason} wrote={wrote} expected_size={info.get("size", "?")} expected_md5={info.get("md5", "?")} {_describe_local_file_for_trace(target)}')
        return wrote
    except Exception as exc:
        captureException(None, source='prompt_app.py', context='except@2096')
        _log_exception('PROMPT:ASSETS:SEED-FAILED', exc, include_traceback=True)
        return False


def _seed_prompt_embedded_runtime_files(runtime_root: Path, *, overwrite_static: bool = False) -> tuple[int, int]:
    if not _prompt_embedded_available():
        _trace_event('PROMPT:EMBEDDED:UNAVAILABLE', f'error={PROMPT_EMBEDDED_DATA_IMPORT_ERROR!r}')
        return 0, 0
    written = 0
    skipped = 0
    files = _prompt_embedded_file_names()
    _trace_prompt_bundle_snapshot('embedded-seed')
    _trace_event('PROMPT:EMBEDDED:SEED-BEGIN', f'root={runtime_root} files={len(files)} raw_bytes={getattr(prompt_embedded_data, "EMBEDDED_RAW_BYTES", "?")} bundle_path={getattr(getattr(prompt_embedded_data, "BUNDLE", None), "bundle_path", "?")}')
    for rel in files:
        is_static = rel.startswith(PROMPT_EMBEDDED_STATIC_PREFIXES)
        if _write_prompt_embedded_file(runtime_root, rel, overwrite=bool(overwrite_static and is_static)):
            written += 1
        else:
            skipped += 1
    _trace_event('PROMPT:EMBEDDED:SEED-END', f'root={runtime_root} written={written} skipped={skipped}')
    return written, skipped

def _copy_missing_tree(source: Path, target: Path, *, overwrite: bool) -> tuple[int, int]:
    copied = 0
    skipped = 0
    source = Path(source)
    target = Path(target)
    if not source.exists():
        return copied, skipped
    if source.is_file():
        if overwrite or not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            File.copy2(source, target)
            _prompt_make_user_writable(target)
            copied += 1
        else:
            skipped += 1
        return copied, skipped
    for item in source.rglob('*'):
        if not item.is_file():
            continue
        rel = item.relative_to(source)
        dest = target / rel
        if overwrite or not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            File.copy2(item, dest)
            _prompt_make_user_writable(dest)
            copied += 1
        else:
            skipped += 1
    return copied, skipped


def _debug_log(tag: str, message: str) -> None:
    if _debug_enabled():
        _trace_event(tag, message)


def _warn_log(tag: str, message: str) -> None:
    _trace_event(tag, message)


def _qt_flag_value(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except Exception as error:
        captureException(None, source='prompt_app.py', context='except@2160')
        print(f"[WARN:swallowed-exception] prompt_app.py:1255 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        pass
    try:
        raw = getattr(value, 'value', None)
        if raw is not None:
            return int(raw)
    except Exception as error:
        captureException(None, source='prompt_app.py', context='except@2167')
        print(f"[WARN:swallowed-exception] prompt_app.py:1261 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        pass
    try:
        raw = getattr(value, '__index__', None)
        if callable(raw):
            return int(cast(Any, raw)())
    except Exception as error:
        captureException(None, source='prompt_app.py', context='except@2174')
        print(f"[WARN:swallowed-exception] prompt_app.py:1267 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        pass
    return int(default)


def _log_exception(tag: str, exc: BaseException, *, include_traceback: bool = True) -> None:
    _warn_log(tag, f'{type(exc).__name__}: {exc}')
    if include_traceback:
        trace_text = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        for line in trace_text.rstrip().splitlines():
            _append_prompt_error_log(f'{datetime.datetime.now().isoformat(timespec="seconds")} [{tag}:TRACEBACK] {line}')
        if _debug_enabled():
            try:
                stream = getattr(sys, 'stderr', None)
                if stream is not None and hasattr(stream, 'write'):
                    traceback.print_exc(file=stream)
            except Exception:
                captureException(None, source='prompt_app.py', context='except@2191')
                pass


def _settings_db_path(root: Path) -> Path:
    return root / 'workspaces' / 'prompt_cwv_settings.sqlite3'


def _prism_assets_root() -> Path:
    return Path(__file__).resolve().parent / 'vendor' / 'prism'


def _read_text_file(path: Path) -> str:
    try:
        return _read_prompt_text_with_embedded_fallback(path, encoding='utf-8', errors='replace')
    except Exception as error:
        captureException(None, source='prompt_app.py', context='except@2206')
        print(f"[WARN:swallowed-exception] prompt_app.py:1289 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        return ''


def _prism_asset_text(file_name: str) -> str:
    return _read_text_file(_prism_assets_root() / str(file_name or ''))


def _source_viewer_html(source_text: str, language_hint: str = 'html', title: str = 'View Source') -> str:
    source = html_lib.escape(str(source_text or ''))
    safe_title = html_lib.escape(str(title or 'View Source'))
    raw_language = re.sub(r'[^a-z0-9_-]+', '', str(language_hint or 'html').lower()) or 'html'
    language_aliases = {
        'html': 'markup',
        'htm': 'markup',
        'xml': 'markup',
        'svg': 'markup',
        'js': 'javascript',
        'mjs': 'javascript',
        'py': 'python',
        'md': 'markdown',
    }
    safe_language = language_aliases.get(raw_language, raw_language)
    prism_css = _prism_asset_text('prism-tomorrow.min.css')
    prism_js = _prism_asset_text('prism.min.js')
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_title}</title>
<style>{prism_css}</style>
<style>
html, body {{ height:100%; margin:0; background:#111; color:#eee; overflow:hidden; }}
body {{ font-family: sans-serif; }}
* {{ box-sizing:border-box; }}
#wrap {{ display:flex; flex-direction:column; height:100vh; min-height:100vh; }}
#header {{ flex:0 0 auto; padding:10px 14px; background:#1b1b1b; border-bottom:1px solid #333; font-weight:700; }}
#scroll {{ flex:1 1 auto; min-height:0; overflow:auto; background:#111; }}
#editor-shell {{ display:grid; grid-template-columns:max-content minmax(0, 1fr); align-items:start; min-height:100%; }}
#line-gutter,
#code-pre,
#code-source {{ font-family: Consolas, 'Courier New', monospace; font-size:13px; line-height:1.45; }}
#line-gutter {{ margin:0; padding:16px 10px 16px 14px; min-width:64px; text-align:right; user-select:none; -webkit-user-select:none; -moz-user-select:none; pointer-events:none; color:#7f848e; background:#181818; border-right:1px solid #2b2b2b; white-space:pre; }}
#line-gutter::selection {{ background:transparent; color:#7f848e; }}
#code-pre {{ margin:0; padding:16px 18px; min-width:0; background:#111 !important; border-radius:0; overflow:visible; white-space:pre; tab-size:4; -moz-tab-size:4; }}
#code-wrap {{ min-width:0; overflow:visible; }}
#code-source {{ display:block; white-space:pre; min-height:100%; }}
pre[class*="language-"], code[class*="language-"] {{ text-shadow:none !important; }}
.token.operator, .token.entity, .token.url, .language-css .token.string, .style .token.string {{ background:transparent !important; }}
</style>
<script>window.Prism = window.Prism || {{}}; window.Prism.manual = true;</script>
<script>{prism_js}</script>
</head>
<body>
<div id="wrap">
  <div id="header">{safe_title}</div>
  <div id="scroll">
    <div id="editor-shell">
      <pre id="line-gutter" aria-hidden="true">1</pre>
      <div id="code-wrap"><pre id="code-pre" class="language-{safe_language}"><code id="code-source" class="language-{safe_language}">{source}</code></pre></div>
    </div>
  </div>
</div>
<script>
(function() {{
  console.info('[SourceViewer] bootstrap title={safe_title} language={safe_language} sourceLen=' + String((document.getElementById('code-source') && document.getElementById('code-source').textContent || '').length));
  function updateLines() {{
    var code = document.getElementById('code-source');
    var gutter = document.getElementById('line-gutter');
    if (!code || !gutter) return;
    var sourceText = String(code.textContent || '');
    var count = sourceText.length ? sourceText.split('\\n').length : 1;
    var rows = [];
    for (var i = 1; i <= count; i += 1) rows.push(String(i));
    gutter.textContent = rows.join('\\n');
  }}
  function render() {{
    updateLines();
    var code = document.getElementById('code-source');
    var pre = document.getElementById('code-pre');
    console.info('[SourceViewer] render prism=' + String(!!window.Prism) + ' code=' + String(!!code));
    if (window.Prism && code && typeof window.Prism.highlightElement === 'function') {{
      window.Prism.highlightElement(code);
      if (pre) pre.className = 'language-{safe_language}';
      console.info('[SourceViewer] highlight complete');
    }}
    updateLines();
  }}
  if (document.readyState === 'loading') {{
    document.addEventListener('DOMContentLoaded', render, {{ once: true }});
  }} else {{
    render();
  }}
}})();
</script>
</body>
</html>"""


def _dump_source_viewer_payload(source_text: str, html_text: str, title: str = 'View Source') -> tuple[Path, Path]:
    debug_dir = Path(__file__).resolve().parent / 'generated' / 'source_viewer_debug'
    debug_dir.mkdir(parents=True, exist_ok=True)
    html_path = debug_dir / '_source_viewer_last.html'
    source_path = debug_dir / '_source_viewer_last_source.txt'
    File.writeText(html_path, str(html_text or ''), encoding='utf-8', errors='replace')
    File.writeText(source_path, str(source_text or ''), encoding='utf-8', errors='replace')
    _warn_log('PROMPT:SOURCE-VIEWER-HTML-PATH', str(html_path))
    _warn_log('PROMPT:SOURCE-VIEWER-SOURCE-PATH', str(source_path))
    if _debug_enabled():
        _warn_log('PROMPT:SOURCE-VIEWER-HTML', str(html_text or ''))
    return html_path, source_path

def _debugger_db_path() -> Optional[Path]:
    raw = str(os.environ.get('TRIO_SQLITE_PATH', '') or '').strip()
    return Path(raw) if raw else None


def _debugger_enabled() -> bool:
    return bool(_debugger_db_path()) and any(flag in sys.argv for flag in ('--debug', '--trace', '--verbose-trace', '--offscreen', '--xdummy', '--xpra'))


def _debugger_orm_store() -> PromptSqlAlchemyStore | None:
    db_path = _debugger_db_path()
    if not db_path:
        return None
    return prompt_orm_store(db_path)




def _write_debugger_heartbeat(event_kind: str = 'heartbeat', reason: str = '', caller: str = '', phase: str = '', var_dump: str = '', process_snapshot: str = '') -> None:
    store = _debugger_orm_store()
    if store is None:
        return
    try:
        import datetime
        now = time.time()
        created = datetime.datetime.fromtimestamp(now).isoformat(sep=' ', timespec='microseconds')
        store.insert_heartbeat(
            created=created,
            heartbeat_microtime=now,
            source=Path(__file__).name,
            event_kind=str(event_kind or ''),
            reason=str(reason or ''),
            caller=str(caller or ''),
            phase=str(phase or ''),
            pid=int(os.getpid()),
            stack_trace='',
            var_dump=str(var_dump or ''),
            process_snapshot=str(process_snapshot or ''),
            processed=0,
        )
    except Exception as exc:
        captureException(None, source='prompt_app.py', context='except@2360')
        print(f"[WARN:swallowed-exception] prompt_app.py:1444 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        _log_exception('PROMPT:DEBUGGER-HEARTBEAT-WRITE-FAILED', exc, include_traceback=False)


def _current_debugger_namespace(window=None) -> dict:
    app = None
    try:
        app = QApplication.instance()
    except Exception as error:
        captureException(None, source='prompt_app.py', context='except@2369')
        print(f"[WARN:swallowed-exception] prompt_app.py:1452 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        app = None
    return {
        '__builtins__': __builtins__,
        'APP_ROOT': str(Path(__file__).resolve().parent),
        'PROMPT_APP': app,
        'PROMPT_MAIN_WINDOW': window,
        'app': app,
        'window': window,
        'Path': Path,
        'json': json,
        'os': os,
        'sys': sys,
        'time': time,
        'traceback': traceback,
    }


def _debugger_var_dump_payload(window=None) -> dict:
    payload = {
        'pid': int(os.getpid()),
        'argv': list(normalized_cli_tokens(sys.argv)),
        'debugger_db_path': str(_debugger_db_path() or EMPTY_STRING),
        'current_time': float(time.time()),
    }
    if window is not None:
        try:
            payload.update({
                'window_title': str(window.windowTitle() or EMPTY_STRING),
                'window_state': str(window._current_window_state_name() or EMPTY_STRING),
                'visible': bool(window.isVisible()),
                'geometry': list(window._rect_to_tuple(window.geometry())),
                'normal_geometry': list(window._rect_to_tuple(window.normalGeometry())),
                'current_prompt_path': str(window.currentPromptPath or EMPTY_STRING),
                'current_workflow_slug': str(window.current_workflow_slug() or EMPTY_STRING),
                'current_doctype_path': str(window.current_doctype_path() or EMPTY_STRING),
            })
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@2407')
            print(f"[WARN:swallowed-exception] prompt_app.py:1489 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            payload['window_error'] = f'{type(exc).__name__}: {exc}'
    return payload


def _debugger_process_snapshot_payload(window=None) -> dict:
    payload = {
        'pid': int(os.getpid()),
        'cwd': str(Path.cwd()),
        'file': str(Path(__file__).resolve()),
        'db_path': str(_debugger_db_path() or EMPTY_STRING),
        'qt_available': True,
    }
    if window is not None:
        try:
            payload['window_state'] = str(window._current_window_state_name() or EMPTY_STRING)
            payload['geometry'] = list(window._rect_to_tuple(window.geometry()))
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@2425')
            print(f"[WARN:swallowed-exception] prompt_app.py:1506 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            payload['window_error'] = f'{type(exc).__name__}: {exc}'
    return payload


def _write_debugger_result_row(event_kind: str = 'debugger-exec-result', reason: str = EMPTY_STRING, output_text: str = EMPTY_STRING, traceback_text: str = EMPTY_STRING, metadata=None) -> bool:
    store = _debugger_orm_store()
    if store is None:
        return False
    try:
        import datetime
        now = time.time()
        created = datetime.datetime.fromtimestamp(now).isoformat(sep=' ', timespec='microseconds')
        process_snapshot = json.dumps(metadata or {}, ensure_ascii=False, default=str)
        store.insert_heartbeat(
            created=created,
            heartbeat_microtime=now,
            source=Path(__file__).name,
            event_kind=str(event_kind or 'debugger-exec-result'),
            reason=str(reason or EMPTY_STRING),
            caller='PromptDebugger',
            phase='DEBUGGER',
            pid=int(os.getpid()),
            stack_trace=str(traceback_text or EMPTY_STRING),
            var_dump=str(output_text or EMPTY_STRING),
            process_snapshot=process_snapshot,
            processed=0,
        )
        return True
    except Exception as exc:
        captureException(None, source='prompt_app.py', context='except@2455')
        print(f"[WARN:swallowed-exception] prompt_app.py:1535 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        _log_exception('PROMPT:DEBUGGER-RESULT-WRITE-FAILED', exc, include_traceback=False)
        return False



def _clear_debugger_exec_row(row_id: int = 0) -> bool:
    store = _debugger_orm_store()
    if store is None or int(row_id or 0) <= 0:
        return False
    try:
        return bool(store.clear_heartbeat_exec(int(row_id)))
    except Exception as exc:
        captureException(None, source='prompt_app.py', context='except@2468')
        print(f"[WARN:swallowed-exception] prompt_app.py:1547 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        _log_exception('PROMPT:DEBUGGER-CLEAR-EXEC-FAILED', exc, include_traceback=False)
        return False



def _fetch_pending_debugger_command_rows() -> list[dict]:
    store = _debugger_orm_store()
    if store is None:
        return []
    try:
        return store.pending_debugger_commands('start.py', int(os.getpid()))
    except Exception as exc:
        captureException(None, source='prompt_app.py', context='except@2481')
        print(f"[WARN:swallowed-exception] prompt_app.py:1559 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        _log_exception('PROMPT:DEBUGGER-FETCH-ROWS-FAILED', exc, include_traceback=False)
        return []



class PromptDebuggerHook:
    SURFACES = DEBUGGER_QUERY_SURFACES

    def __init__(self, window) -> None:
        import threading  # thread-ok
        self.window = window  # noqa: nonconform
        self.cron_lock = threading.RLock()  # noqa: nonconform
        self.cron_tasks: dict[str, dict] = {}
        self.cron_counter = 0  # noqa: nonconform
        self.cron_row_state: dict[int, float] = {}

    def querySurfacesList(self) -> list[str]:
        return list(self.SURFACES)


    def buildLiveVarDump(self, reason: str = EMPTY_STRING) -> str:
        payload = _debugger_var_dump_payload(self.window)
        payload['reason'] = str(reason or EMPTY_STRING)
        return json.dumps(payload, ensure_ascii=False, indent=2, default=str)

    def buildLiveProcessSnapshot(self, reason: str = EMPTY_STRING) -> str:
        payload = _debugger_process_snapshot_payload(self.window)
        payload['reason'] = str(reason or EMPTY_STRING)
        return json.dumps(payload, ensure_ascii=False, indent=2, default=str)

    def buildExecutionGlobals(self) -> dict:
        namespace = _current_debugger_namespace(self.window)
        namespace['PromptDebuggerHook'] = PromptDebuggerHook
        return namespace

    def executeCommand(self, text: str = EMPTY_STRING, file_path: str = EMPTY_STRING) -> dict:
        return _run_debugger_code_payload(text=text, file_path=file_path, namespace=self.buildExecutionGlobals())

    def listCronTasks(self) -> list[dict]:
        rows = []
        with self.cron_lock:
            for task_id, task in sorted(self.cron_tasks.items(), key=lambda item: str(item[0])):
                thread = task.get('thread')
                rows.append({
                    'task_id': str(task_id),
                    'interval': float(task.get('interval', 1.0) or 1.0),
                    'count': int(task.get('count', 0) or 0),
                    'run_count': int(task.get('run_count', 0) or 0),
                    'source_path': str(task.get('source_path', EMPTY_STRING) or EMPTY_STRING),
                    'label': str(task.get('label', EMPTY_STRING) or EMPTY_STRING),
                    'alive': bool(getattr(thread, 'is_alive', lambda: False)()),
                })
        return rows

    def stopCronTask(self, task_id: str = EMPTY_STRING) -> dict:
        target = str(task_id or EMPTY_STRING).strip()
        if not target:
            return {'ok': False, 'error': 'missing cron task id'}
        with self.cron_lock:
            task = self.cron_tasks.get(target)
            if task is None:
                return {'ok': False, 'error': f'unknown cron task: {target}'}
            stop_event = task.get('stop_event')
            if stop_event is not None:
                stop_event.set()
        return {'ok': True, 'text': f'cron task stopped: {target}', 'task_id': target}

    def startCronCommand(self, text: str = EMPTY_STRING, file_path: str = EMPTY_STRING, interval: float = 1.0, count: int = 0) -> dict:
        import threading  # thread-ok
        try:
            source_text, label, resolved_path = _resolve_debugger_code_payload(text=text, file_path=file_path)
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@2554')
            print(f"[WARN:swallowed-exception] prompt_app.py:1633 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return {'ok': False, 'error': f'{type(error).__name__}: {error}'}
        if not str(source_text or EMPTY_STRING).strip():
            return {'ok': False, 'error': 'empty debugger cron command'}
        try:
            interval_value = max(0.05, float(interval or 1.0))
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@2561')
            print(f"[WARN:swallowed-exception] prompt_app.py:1639 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            interval_value = 1.0
        try:
            count_value = max(0, int(count or 0))
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@2566')
            print(f"[WARN:swallowed-exception] prompt_app.py:1643 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            count_value = 0
        with self.cron_lock:
            self.cron_counter += 1
            task_id = f'prompt-debugger-cron-{int(time.time() * 1000)}-{self.cron_counter}'
            stop_event = threading.Event()
            task = {
                'task_id': task_id,
                'interval': interval_value,
                'count': count_value,
                'run_count': 0,
                'label': str(label or EMPTY_STRING),
                'source_text': str(source_text or EMPTY_STRING),
                'source_path': str(resolved_path or EMPTY_STRING),
                'stop_event': stop_event,
                'last_result': None,
            }
            self.cron_tasks[task_id] = task

        def runner():
            first = True
            try:
                while not stop_event.is_set():
                    if not first and stop_event.wait(interval_value):
                        break
                    first = False
                    result = self.executeCommand(text=task['source_text'], file_path=task.get('source_path', EMPTY_STRING))
                    with self.cron_lock:
                        task['run_count'] = int(task.get('run_count', 0) or 0) + 1
                        task['last_result'] = result
                        if count_value > 0 and int(task['run_count']) >= count_value:
                            stop_event.set()
                            break
            finally:
                with self.cron_lock:
                    task['stopped'] = True

        lifecycle = getattr(getattr(self, 'window', None), 'AppLifecycle', None)
        if lifecycle is None or not hasattr(lifecycle, 'startThreadCallbackPhase'):
            return {'ok': False, 'error': 'AppLifecycle unavailable for debugger cron thread'}
        thread = lifecycle.startThreadCallbackPhase(PHASE_RUNTIME_THREADS + 10 + int(self.cron_counter or 0), f'Prompt Debugger Cron {task_id}', runner)
        task['thread'] = thread
        return {
            'ok': True,
            'text': f'cron task started: {task_id} interval={interval_value:.2f}s count={count_value or 0}',
            'task_id': task_id,
            'interval': interval_value,
            'count': count_value,
            'label': str(label or EMPTY_STRING),
            'source_path': str(resolved_path or EMPTY_STRING),
            'type': 'debugger-cron-command',
        }

    def runDebuggerExecRow(self, row: dict) -> bool:
        row_id = int(row.get('id') or 0)
        code_text = str(row.get('exec') or EMPTY_STRING)
        is_file = bool(int(row.get('exec_is_file') or 0))
        if not code_text.strip():
            return False
        _clear_debugger_exec_row(row_id)
        result = self.executeCommand(text=EMPTY_STRING if is_file else code_text, file_path=code_text if is_file else EMPTY_STRING)
        metadata = {
            'row_id': row_id,
            'is_file': int(is_file),
            'label': str(result.get('label') or EMPTY_STRING),
            'source_path': str(result.get('source_path') or EMPTY_STRING),
            'ok': bool(result.get('ok')),
        }
        _write_debugger_result_row(
            event_kind='debugger-exec-result',
            reason=f'DEBUGGER-EXEC:{row_id}',
            output_text=str(result.get('text') or EMPTY_STRING),
            traceback_text=str(result.get('traceback_text') or result.get('error') or EMPTY_STRING),
            metadata=metadata,
        )
        return bool(result.get('ok'))

    def runDebuggerCronRow(self, row: dict) -> bool:
        row_id = int(row.get('id') or 0)
        code_text = str(row.get('cron') or EMPTY_STRING)
        is_file = bool(int(row.get('cron_is_file') or 0))
        try:
            interval_seconds = max(0.05, float(row.get('cron_interval_seconds') or 0.0))
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@2650')
            print(f"[WARN:swallowed-exception] prompt_app.py:1726 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            interval_seconds = 1.0
        if not code_text.strip():
            return False
        now = time.time()
        next_run = float(self.cron_row_state.get(row_id, 0.0) or 0.0)
        if next_run > now:
            return False
        self.cron_row_state[row_id] = now + interval_seconds
        result = self.executeCommand(text=EMPTY_STRING if is_file else code_text, file_path=code_text if is_file else EMPTY_STRING)
        metadata = {
            'row_id': row_id,
            'is_file': int(is_file),
            'interval_seconds': float(interval_seconds),
            'label': str(result.get('label') or EMPTY_STRING),
            'source_path': str(result.get('source_path') or EMPTY_STRING),
            'ok': bool(result.get('ok')),
        }
        _write_debugger_result_row(
            event_kind='debugger-cron-result',
            reason=f'DEBUGGER-CRON:{row_id}',
            output_text=str(result.get('text') or EMPTY_STRING),
            traceback_text=str(result.get('traceback_text') or result.get('error') or EMPTY_STRING),
            metadata=metadata,
        )
        return bool(result.get('ok'))

    def pollDebuggerHeartbeatCommands(self) -> bool:
        any_ran = False
        for row in list(_fetch_pending_debugger_command_rows() or []):
            try:
                if str(row.get('exec') or EMPTY_STRING).strip():
                    any_ran = bool(self.runDebuggerExecRow(row)) or any_ran
                if str(row.get('cron') or EMPTY_STRING).strip():
                    any_ran = bool(self.runDebuggerCronRow(row)) or any_ran
            except Exception as exc:
                captureException(None, source='prompt_app.py', context='except@2686')
                print(f"[WARN:swallowed-exception] prompt_app.py:1761 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                _log_exception('PROMPT:DEBUGGER-POLL-COMMANDS-FAILED', exc, include_traceback=False)
        return any_ran


class PromptRemoteDebuggerProxy:
    def __init__(self, hook: PromptDebuggerHook) -> None:
        self.hook = hook  # noqa: nonconform
        self.enabled = str(os.environ.get('TRIO_DEBUGGER_ENABLED', EMPTY_STRING) or EMPTY_STRING).strip() == '1'
        self.host = str(os.environ.get('TRIO_DEBUGGER_HOST', '127.0.0.1') or '127.0.0.1').strip() or '127.0.0.1'  # noqa: nonconform
        try:
            self.port = int(str(os.environ.get('TRIO_DEBUGGER_PORT', '0') or '0'))
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@2699')
            print(f"[WARN:swallowed-exception] prompt_app.py:1773 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            self.port = 0  # noqa: nonconform
        self.token = str(os.environ.get('TRIO_DEBUGGER_TOKEN', EMPTY_STRING) or EMPTY_STRING).strip()
        self.controlServer = None  # noqa: nonconform
        self.controlThread = None
        self.controlPort = 0  # noqa: nonconform
        if self.enabled and self.port > 0 and self.token:
            self.startControlServer()
            self.sendAttach()

    def window(self):
        return getattr(self.hook, 'window', None)

    def relayPayload(self, kind: str, payload: dict | None = None) -> None:
        if not (self.enabled and self.port > 0 and self.token):
            return
        message = {'token': self.token, 'kind': str(kind or EMPTY_STRING), 'payload': dict(payload or {})}
        try:
            data = (json.dumps(message, ensure_ascii=False) + '\n').encode('utf-8', 'replace')
            with socket.create_connection((self.host, int(self.port)), timeout=0.75) as sock:
                sock.sendall(data)
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@2721')
            print(f"[WARN:swallowed-exception] prompt_app.py:1794 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            _log_exception('PROMPT:DEBUGGER-RELAY-SEND-FAILED', exc, include_traceback=False)

    def sendAttach(self) -> None:
        self.relayPayload('attach', {
            'pid': int(os.getpid()),
            'control_port': int(self.controlPort or 0),
            'surfaces': list(self.hook.querySurfacesList()),
        })

    def emitHeartbeat(self, reason: str = 'UI', caller: str = 'PromptMainWindow') -> None:
        self.relayPayload('heartbeat', {
            'pid': int(os.getpid()),
            'reason': str(reason or 'UI'),
            'caller': str(caller or EMPTY_STRING),
            'timestamp': float(time.time()),
        })

    def emitProcess(self, reason: str = 'PROCESS') -> None:
        self.relayPayload('process', {
            'pid': int(os.getpid()),
            'reason': str(reason or 'PROCESS'),
            'timestamp': float(time.time()),
        })

    def captureSnapshot(self, reason: str = 'CONTROL:SNAPSHOT', caller: str = 'PromptRemoteDebuggerProxy') -> dict:
        var_text = self.hook.buildLiveVarDump(reason)
        process_text = self.hook.buildLiveProcessSnapshot(reason)
        _write_debugger_heartbeat(event_kind='snapshot', reason=str(reason or 'CONTROL:SNAPSHOT'), caller=str(caller or 'PromptRemoteDebuggerProxy'), phase='DEBUGGER', var_dump=var_text, process_snapshot=process_text)
        return {'var_dump': var_text, 'process_snapshot': process_text}

    def handleControlCommand(self, command: str, payload: dict | None = None) -> dict:
        cmd = str(command or EMPTY_STRING).strip().lower()
        payload = dict(payload or {})
        if not cmd:
            return {'ok': False, 'error': 'empty command'}
        if cmd == 'close':
            try:
                target_window = self.window()
                if target_window is not None:
                    QMetaObject.invokeMethod(target_window, 'close', Qt.QueuedConnection)
                app = QApplication.instance()
                if app is not None:
                    QMetaObject.invokeMethod(app, 'quit', Qt.QueuedConnection)
            except Exception as error:
                captureException(None, source='prompt_app.py', context='except@2766')
                print(f"[WARN:swallowed-exception] prompt_app.py:1838 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                try:
                    app = QApplication.instance()
                    if app is not None:
                        app.quit()  # lifecycle-ok: fallback close path after queued lifecycle close request
                except Exception as error:
                    captureException(None, source='prompt_app.py', context='except@2772')
                    print(f"[WARN:swallowed-exception] prompt_app.py:1843 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    pass
            return {'ok': True, 'text': 'close requested'}
        if cmd == 'ping':
            self.emitHeartbeat('CONTROL:PING', 'PromptRemoteDebuggerProxy')
            return {'ok': True, 'text': 'pong'}
        if cmd in {'debugger-query-surfaces', 'query-surfaces', 'surfaces'}:
            surfaces = list(self.hook.querySurfacesList())
            return {'ok': True, 'surfaces': surfaces, 'text': ' '.join(surfaces)}
        if cmd in {'vars', 'vardump'}:
            var_text = self.hook.buildLiveVarDump('CONTROL:VARDUMP')
            process_text = self.hook.buildLiveProcessSnapshot('CONTROL:VARDUMP')
            _write_debugger_heartbeat(event_kind='snapshot', reason='CONTROL:VARDUMP', caller='PromptRemoteDebuggerProxy', phase='DEBUGGER', var_dump=var_text, process_snapshot=process_text)
            return {'ok': True, 'text': str(var_text or EMPTY_STRING), 'process_snapshot': str(process_text or EMPTY_STRING)}
        if cmd in {'snapshot', 'dump', 'freeze_dump'}:
            reason = 'CONTROL:FREEZE' if cmd == 'freeze_dump' else 'CONTROL:SNAPSHOT'
            self.captureSnapshot(reason, caller='PromptRemoteDebuggerProxy')
            return {'ok': True, 'text': 'snapshot requested'}
        if cmd in {'debugger-exec-command', 'exec-command', 'exec'}:
            result = self.hook.executeCommand(text=str(payload.get('text') or payload.get('args') or EMPTY_STRING), file_path=str(payload.get('file') or EMPTY_STRING))
            result['scope'] = 'child-debugger'
            return result
        if cmd in {'debugger-cron-command', 'cron-command', 'cron'}:
            action = str(payload.get('action') or EMPTY_STRING).strip().lower()
            if action in {'list', 'status'}:
                rows = list(self.hook.listCronTasks() or [])
                return {'ok': True, 'tasks': rows, 'text': json.dumps(rows, ensure_ascii=False, indent=2, default=str)}
            if action == 'stop':
                result = self.hook.stopCronTask(str(payload.get('task_id') or payload.get('id') or EMPTY_STRING))
                result['scope'] = 'child-debugger'
                return result
            result = self.hook.startCronCommand(
                text=str(payload.get('text') or payload.get('args') or EMPTY_STRING),
                file_path=str(payload.get('file') or EMPTY_STRING),
                interval=float(payload.get('interval_seconds') or payload.get('interval') or 1.0),
                count=int(payload.get('count') or 0),
            )
            result['scope'] = 'child-debugger'
            return result
        if cmd == 'proxy':
            endpoint = str(payload.get('text') or payload.get('endpoint') or payload.get('args') or EMPTY_STRING).strip()
            return {'ok': True, 'text': endpoint or 'proxy accepted', 'scope': 'child-debugger'}
        return {'ok': False, 'error': f'unsupported command: {cmd}'}

    def startControlServer(self) -> None:
        proxy = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                while True:  # noqa: badcode reviewed detector-style finding
                    try:
                        raw = self.rfile.readline()
                    except (ConnectionResetError, OSError):
                        captureException(None, source='prompt_app.py', context='except@2825')
                        break
                    if not raw:
                        break
                    try:
                        payload = json.loads(raw.decode('utf-8', 'replace'))
                    except Exception as error:
                        captureException(None, source='prompt_app.py', context='except@2831')
                        print(f"[WARN:swallowed-exception] prompt_app.py:1901 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                        continue
                    if str(payload.get('token') or EMPTY_STRING) != proxy.token:
                        continue
                    response = proxy.handleControlCommand(str(payload.get('command') or EMPTY_STRING).strip(), payload)
                    try:
                        if response is None:
                            response = {'ok': True}
                        self.wfile.write((json.dumps(response, ensure_ascii=False) + '\n').encode('utf-8', 'replace'))
                        self.wfile.flush()
                    except Exception as error:
                        captureException(None, source='prompt_app.py', context='except@2842')
                        print(f"[WARN:swallowed-exception] prompt_app.py:1911 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                        pass

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        try:
            self.controlServer = Server(('127.0.0.1', 0), Handler)
            self.controlPort = int(self.controlServer.server_address[1])
            thread_class = getattr(threading, 'Thread')
            self.controlThread = thread_class(target=self.controlServer.serve_forever, name='PromptRemoteDebuggerControl', daemon=True)
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@2855')
            print(f"[WARN:swallowed-exception] prompt_app.py:1923 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            self.controlServer = None
            self.controlThread = None
            self.controlPort = 0
            _log_exception('PROMPT:DEBUGGER-CONTROL-SERVER-FAILED', exc, include_traceback=False)

    def shutdown(self) -> None:
        try:
            if self.controlServer is not None:
                self.controlServer.shutdown()
                self.controlServer.server_close()
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@2867')
            print(f"[WARN:swallowed-exception] prompt_app.py:1934 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            _log_exception('PROMPT:DEBUGGER-CONTROL-SHUTDOWN-FAILED', exc, include_traceback=False)
        finally:
            self.controlServer = None
            self.controlThread = None
            self.controlPort = 0


def _relative_url(from_dir: Path, target_path: Path) -> str:
    try:
        relative = os.path.relpath(str(target_path), str(from_dir))
    except Exception as error:
        captureException(None, source='prompt_app.py', context='except@2879')
        print(f"[WARN:swallowed-exception] prompt_app.py:1945 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        relative = str(target_path)
    return relative.replace('\\', '/')


def _local_asset_url(target_path: Path, from_dir: Path | None = None) -> str:
    """Return a robust URL for bundled local HTML assets.

    Runtime workflow HTML is written inside workflow folders, while some
    generated previews are written under generated/. Relative URLs based on
    the wrong folder make prompt_generator.js fail to load, leaving raw
    Markdown instead of live checkboxes, radio buttons, and input boxes.
    Prefer file:// URLs so every rendered pane can load the same assets.
    """
    try:
        candidate = Path(target_path).resolve()
        if candidate.exists():
            return candidate.as_uri()
    except Exception as error:
        captureException(None, source='prompt_app.py', context='except@2898')
        print(f"[WARN:swallowed-exception] prompt_app.py:1956 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
    if from_dir is not None:
        return _relative_url(Path(from_dir), Path(target_path))
    return str(target_path).replace('\\', '/')


def _clean_rendered_html_artifacts(html_text: str) -> str:
    cleaned = str(html_text or EMPTY_STRING)
    if not cleaned:
        return cleaned
    cleaned = cleaned.replace('\\r\\n', '\n').replace('\\n', '\n').replace('\\t', '\t')
    cleaned = cleaned.replace('\\&quot;', '&quot;')
    cleaned = cleaned.replace('\\"', '"')
    cleaned = cleaned.replace("\\'", "'")
    cleaned = re.sub(r"([\"\\\'])&quot;([^\"\\\']*?)&quot;([\"\\\'])", lambda match: match.group(1) + html_lib.escape(match.group(2), quote=True) + match.group(3), cleaned)
    cleaned = cleaned.replace('id="&quot;', 'id="').replace('&quot;"', '"')
    cleaned = cleaned.replace('class="&quot;', 'class="').replace('src="&quot;', 'src="').replace('alt="&quot;', 'alt="')
    cleaned = re.sub(
        r'<img\b[^>]*(?:resources/|openai|logo)[^>]*>',
        '<span class="missing-handbook-image" title="Original handbook image omitted from bundled runtime preview">[handbook image]</span>',
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned


def _comment_lines_to_blockquote(text: str) -> str:
    lines = str(text or EMPTY_STRING).replace('\r\n', '\n').split('\n')
    rendered: list[str] = []
    for raw_line in lines:
        stripped = str(raw_line or EMPTY_STRING).strip()
        rendered.append('> ' + stripped if stripped else '>')
    return '\n'.join(rendered)


def _replace_comment_tag_blocks(text: str, open_token: str = '[comment]', close_token: str = '[/comment]') -> str:
    source_text = str(text or EMPTY_STRING)
    if not source_text or open_token not in source_text:
        return source_text
    chunks = source_text.split(open_token)
    rebuilt: list[str] = [chunks[0]]
    index = 1
    while index < len(chunks):
        merged_chunk = chunks[index]
        while close_token not in merged_chunk and index + 1 < len(chunks):
            index += 1
            merged_chunk += open_token + chunks[index]
        if close_token in merged_chunk:
            close_index = merged_chunk.rfind(close_token)
            comment_body = merged_chunk[:close_index]
            trailing_text = merged_chunk[close_index + len(close_token):]
            rebuilt.append(_comment_lines_to_blockquote(comment_body))
            rebuilt.append(trailing_text)
        else:
            rebuilt.append(open_token + merged_chunk)
        index += 1
    return ''.join(rebuilt)


def _replace_block_comment_patterns(text: str) -> str:
    normalized = _replace_comment_tag_blocks(str(text or EMPTY_STRING))
    normalized = re.sub(r'<!--([\s\S]*?)-->', lambda match: _comment_lines_to_blockquote(match.group(1)), normalized)
    normalized = re.sub(r'/\*([\s\S]*?)\*/', lambda match: _comment_lines_to_blockquote(match.group(1)), normalized)
    return normalized


def _replace_line_comment_patterns(text: str, *, allow_single_hash_comments: bool = False) -> str:
    rendered_lines: list[str] = []
    for raw_line in str(text or EMPTY_STRING).replace('\r\n', '\n').split('\n'):
        leading = raw_line[:len(raw_line) - len(raw_line.lstrip())]
        stripped = raw_line.lstrip()
        if stripped.startswith('//'):
            rendered_lines.append(leading + '> ' + stripped[2:].lstrip())
            continue
        if allow_single_hash_comments and re.match(r'^#(?!#)\s+', stripped):
            rendered_lines.append(leading + '> ' + re.sub(r'^#(?!#)\s*', EMPTY_STRING, stripped, count=1))
            continue
        rendered_lines.append(raw_line)
    return '\n'.join(rendered_lines)


def _normalize_markdown_comments(text: str, *, allow_single_hash_comments: bool = False) -> str:
    normalized = _replace_block_comment_patterns(str(text or EMPTY_STRING))
    normalized = _replace_line_comment_patterns(normalized, allow_single_hash_comments=allow_single_hash_comments)
    return normalized


def _title_case_prompt_label(value: str) -> str:
    def render_token(match: re.Match[str]) -> str:
        token = str(match.group(0) or EMPTY_STRING)
        upper_token = token.upper()
        if upper_token in PROMPT_TITLE_ACRONYMS:
            return upper_token
        if token.isdigit():
            return token
        return token[:1].upper() + token[1:].lower()

    return re.sub(r'[A-Za-z0-9]+', render_token, str(value or EMPTY_STRING)).strip()


def _slugify(value: str, fallback: str = 'item') -> str:
    text_value = html_lib.unescape(str(value or EMPTY_STRING)).strip().lower()
    text_value = re.sub(r'[^a-z0-9]+', '_', text_value)
    text_value = re.sub(r'_+', '_', text_value).strip('_')
    return text_value or str(fallback or 'item')


def _friendly_prompt_title(value: str) -> str:
    cleaned = re.sub(r'(?i)\.prompt$', EMPTY_STRING, str(value or EMPTY_STRING))
    cleaned = cleaned.replace('_', ' ').replace('-', ' ').replace('/', ' > ')
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return _title_case_prompt_label(cleaned or 'Untitled Prompt')


def _normalize_prompt_bucket_name(value: str) -> str:
    text_value = str(value or EMPTY_STRING).strip().strip('/\\')
    text_value = re.sub(r'\s+', ' ', text_value)
    if not text_value:
        return EMPTY_STRING
    return _title_case_prompt_label(text_value.replace('_', ' ').replace('-', ' '))


def _split_prompt_bucket_title(title: str) -> tuple[str, str]:
    text_value = str(title or EMPTY_STRING).strip()
    if not text_value:
        return EMPTY_STRING, EMPTY_STRING
    for separator in (' > ', ' / ', '::'):
        if separator in text_value:
            left, right = text_value.rsplit(separator, 1)
            return _normalize_prompt_bucket_name(left), right.strip()
    return EMPTY_STRING, text_value


def _fallback_preview_html(view_name: str, html_text: str, source_url: str = EMPTY_STRING) -> str:
    source = str(html_text or EMPTY_STRING)
    body_match = re.search(r'<body\b[^>]*>(.*?)</body>', source, flags=re.IGNORECASE | re.DOTALL)
    body = body_match.group(1) if body_match else source
    title = html_lib.escape(str(view_name or 'Preview'))
    url_note = f'<p class="muted">{html_lib.escape(str(source_url or EMPTY_STRING))}</p>' if source_url else EMPTY_STRING
    return f'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<style>
body {{ background:#111820; color:#eef3f8; font-family:Segoe UI, Arial, sans-serif; padding:18px; line-height:1.55; }}
a {{ color:#8ecbff; }} code, pre {{ background:#0b1016; color:#f7f7f7; border-radius:8px; }} pre {{ padding:12px; overflow:auto; }}
.muted {{ color:#9aa8b5; font-size:12px; }} table {{ border-collapse:collapse; width:100%; }} td, th {{ border:1px solid #334456; padding:6px 8px; }}
</style></head><body><h1>{title}</h1>{url_note}<div class="fallback-preview-body">{body}</div></body></html>'''


def _read_local_asset_text_for_html(path: Path, role: str, *, required: bool = False) -> str:
    candidate = Path(path)
    try:
        if not candidate.exists() or not candidate.is_file():
            _trace_event('PROMPT:HTML:INLINE-ASSET-MISSING' if required else 'PROMPT:HTML:INLINE-ASSET-SKIP', f'role={role} {_describe_local_file_for_trace(candidate)}')
            return EMPTY_STRING
        payload = candidate.read_bytes()
        text = payload.decode('utf-8', errors='replace')
        _trace_event('PROMPT:HTML:INLINE-ASSET-READ', f'role={role} path={candidate} bytes={len(payload)} chars={len(text)} md5={_md5_for_bytes(payload)}')
        return text
    except Exception as exc:
        captureException(None, source='prompt_app.py', context='except@3057')
        _trace_event('PROMPT:HTML:INLINE-ASSET-FAILED', f'role={role} path={candidate} required={required} error={type(exc).__name__}:{exc}')
        return EMPTY_STRING


def _inline_script_tag(role: str, source_path: Path, script_text: str) -> str:
    if not script_text:
        return EMPTY_STRING
    safe_role = html_lib.escape(str(role or 'script'), quote=True)
    source_url = str(source_path.resolve()).replace('\\', '/')
    return f'<script data-inline-asset="{safe_role}">\n{script_text}\n//# sourceURL=file:///{source_url}\n</script>'


def _inline_style_tag(role: str, style_text: str) -> str:
    if not style_text:
        return EMPTY_STRING
    safe_role = html_lib.escape(str(role or 'style'), quote=True)
    return f'<style data-inline-asset="{safe_role}">\n{style_text}\n</style>'


def _prompt_html_shell(root: Path, title: str, body_html: str, *, runtime_data: dict[str, object] | None = None, extra_head: str = EMPTY_STRING) -> str:
    vendor_jquery = Path(root) / 'vendor' / 'jquery' / 'jquery-4.0.0.min.js'
    vendor_prism_js = Path(root) / 'vendor' / 'prism' / 'prism.min.js'
    vendor_prism_css = Path(root) / 'vendor' / 'prism' / 'prism-tomorrow.min.css'
    generator_js = Path(root) / 'js' / 'prompt_generator.js'
    jquery_exists = _trace_html_asset('PROMPT:HTML:ASSET-CHECK', vendor_jquery, role='jquery', required=True)
    prism_js_exists = _trace_html_asset('PROMPT:HTML:ASSET-CHECK', vendor_prism_js, role='prism-js', required=False)
    prism_css_exists = _trace_html_asset('PROMPT:HTML:ASSET-CHECK', vendor_prism_css, role='prism-css', required=False)
    generator_exists = _trace_html_asset('PROMPT:HTML:ASSET-CHECK', generator_js, role='prompt-generator-js', required=True)
    jquery_text = _read_local_asset_text_for_html(vendor_jquery, 'jquery', required=True) if jquery_exists else EMPTY_STRING
    prism_js_text = _read_local_asset_text_for_html(vendor_prism_js, 'prism-js', required=False) if prism_js_exists else EMPTY_STRING
    prism_css_text = _read_local_asset_text_for_html(vendor_prism_css, 'prism-css', required=False) if prism_css_exists else EMPTY_STRING
    generator_text = _read_local_asset_text_for_html(generator_js, 'prompt-generator-js', required=True) if generator_exists else EMPTY_STRING
    _trace_event('PROMPT:HTML:INLINE-ASSET-SUMMARY', f'jquery={len(jquery_text)} prism_js={len(prism_js_text)} prism_css={len(prism_css_text)} generator={len(generator_text)}')
    scripts = []
    scripts.append(_inline_script_tag('jquery', vendor_jquery, jquery_text))
    scripts.append(_inline_script_tag('prism-js', vendor_prism_js, prism_js_text))
    scripts.append(_inline_script_tag('prompt-generator-js', generator_js, generator_text))
    prism_css = _inline_style_tag('prism-css', prism_css_text)
    runtime_json = json.dumps(runtime_data or {}, ensure_ascii=False)
    runtime_json_script = runtime_json.replace('</', '<\\/')
    safe_title = html_lib.escape(str(title or 'Prompt'), quote=False)
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_title}</title>
{prism_css}
<script src="qrc:///qtwebchannel/qwebchannel.js"></script>
{extra_head}
<style>
:root {{ color-scheme: dark; --bg:#111820; --panel:#172230; --text:#eef3f8; --muted:#9aa8b5; --line:#334456; --accent:#8ecbff; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font-family:Segoe UI, Arial, sans-serif; line-height:1.55; }}
a {{ color:var(--accent); }}
.prompt-page {{ padding:18px; max-width:1400px; margin:0 auto; }}
.prompt-card, .workflow-control-block, .prompt-output-surface, .previous-output-surface {{ background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:14px; margin:12px 0; }}
h1, h2, h3 {{ margin:0.7em 0 0.35em; }}
textarea, input, select {{ background:#0c121a; color:var(--text); border:1px solid var(--line); border-radius:9px; padding:8px; }}
textarea {{ width:100%; }}
button {{ background:#26374a; color:var(--text); border:1px solid #4e6780; border-radius:8px; padding:8px 12px; cursor:pointer; }}
button:hover {{ background:#304960; }}
.checkline {{ display:flex; gap:8px; align-items:flex-start; }}
.markdown-table, .workflow-table {{ border-collapse:collapse; width:100%; margin:10px 0; }}
.markdown-table td, .workflow-table td, th {{ border:1px solid var(--line); padding:7px 9px; }}
pre {{ background:#0b1016; color:#f7f7f7; border-radius:10px; padding:12px; overflow:auto; }}
code {{ background:#0b1016; border-radius:5px; padding:2px 4px; }}
.muted, .small {{ color:var(--muted); }}
.prompt-home-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:14px; }}
.prompt-pill {{ display:inline-block; border:1px solid var(--line); border-radius:999px; padding:4px 10px; margin:3px; color:var(--text); text-decoration:none; }}
#dynamicControls {{ margin-top:10px; }}
#dynamicControls:empty {{ display:none !important; }}
#userTask {{ min-height:160px; }}
#baseContext {{ min-height:280px; }}
#extraInstruction {{ min-height:150px; }}
.workflow-inline-textarea, .markdown-inline-textarea {{ min-height:180px; }}
#prompt-runtime-hidden {{ display:none !important; }}
</style>
</head>
<body>
<script id="PromptRuntimeDataJson" type="application/json">{runtime_json_script}</script>
<script>
(function () {{
    try {{
        var node = document.getElementById('PromptRuntimeDataJson');
        window.PromptRuntimeData = node ? JSON.parse(node.textContent || '{{}}') : {{}};
        console.log('[PROMPT:RUNTIME-DATA] parsed application/json runtime payload');
    }} catch (error) {{
        window.PromptRuntimeData = {{}};
        console.error('[PROMPT:RUNTIME-DATA-ERROR] ' + String(error && error.stack ? error.stack : error));
    }}
}})();
</script>
<script>
(function () {{
    function attachBridge() {{
        if (window.qt && window.qt.webChannelTransport && window.QWebChannel) {{
            try {{
                new QWebChannel(window.qt.webChannelTransport, function (channel) {{
                    window.qtBridge = channel.objects.qtBridge || window.qtBridge || null;
                    console.log('[PROMPT:QT-BRIDGE] ready=' + String(!!window.qtBridge));
                }});
            }} catch (error) {{
                console.warn('[PROMPT:QT-BRIDGE-ERROR] ' + String(error && error.stack ? error.stack : error));
            }}
        }} else {{
            console.log('[PROMPT:QT-BRIDGE] qwebchannel unavailable');
        }}
    }}
    if (document.readyState === 'loading') {{
        document.addEventListener('DOMContentLoaded', attachBridge, {{ once: true }});
    }} else {{
        attachBridge();
    }}
}})();
</script>
<div class="prompt-page">{body_html}</div>
{''.join(script for script in scripts if script)}
</body>
</html>'''


def _workflow_blocks_from_markdown(text: str) -> list[dict[str, object]]:
    blocks: list[dict[str, object]] = []
    for raw_line in str(text or EMPTY_STRING).replace('\r\n', '\n').split('\n'):
        stripped = raw_line.strip()
        if not stripped:
            blocks.append({'type': 'blank'})
            continue
        heading = re.match(r'^(#{1,6})\s+(.+)$', stripped)
        if heading:
            blocks.append({'type': 'heading', 'level': len(heading.group(1)), 'text': heading.group(2)})
            continue
        checkbox = re.match(r'^(?:[-*+]\s*)?\[(x|X| )?\]\s*(.+)$', stripped)
        if checkbox:
            blocks.append({'type': 'checkbox', 'checked': (checkbox.group(1) or '').lower() == 'x', 'template': checkbox.group(2).strip()})
            continue
        radio = re.match(r'^(?:[-*+]\s*)?\[(o|O|0)\]\s*(.+)$', stripped)
        if radio:
            blocks.append({'type': 'radio-item', 'checked': radio.group(1) == 'O', 'template': radio.group(2).strip()})
            continue
        input_match = re.search(r'\[(_{2,})\]', stripped)
        if input_match:
            marker = input_match.group(1)
            template = re.sub(r'\[_{2,}\]', '{}', stripped)
            label = re.sub(r'\[_{2,}\]', EMPTY_STRING, stripped).strip()
            blocks.append({'type': 'textarea-input' if len(marker) >= 8 else 'input-row', 'label': label, 'template': template, 'fields': [{'width': 'full'}], 'wide': len(marker) >= 8, 'rows': 3})
            continue
        quote = re.match(r'^>\s*(.*)$', stripped)
        if quote:
            blocks.append({'type': 'quote', 'text': quote.group(1)})
            continue
        bullet = re.match(r'^[-*+]\s+(.+)$', stripped)
        if bullet:
            blocks.append({'type': 'bullet', 'text': bullet.group(1)})
            continue
        ordered = re.match(r'^\d+\.\s+(.+)$', stripped)
        if ordered:
            blocks.append({'type': 'ordered', 'text': ordered.group(1)})
            continue
        if stripped.startswith('|') and stripped.endswith('|'):
            cells = [cell.strip() for cell in stripped.strip('|').split('|')]
            blocks.append({'type': 'table', 'rows': [cells]})
            continue
        blocks.append({'type': 'paragraph', 'text': stripped})
    return blocks


@dataclass
class WorkflowDefinition:
    slug: str
    title: str
    folder: Path
    source_path: Path
    compiled_html_path: Path
    meta_path: Path
    sort_key: str = EMPTY_STRING


class WorkflowCompiler:
    def __init__(self, root: Path):
        self.root = Path(root)

    def compile_text(self, text: str, source_path: Path, meta_path: Path | None = None) -> str:
        source = Path(source_path)
        workflow_source = str(text or EMPTY_STRING)
        loc = PromptLocalization(PromptLocalization.resolve(self.root))
        runtime_data = {
            'promptTitle': '__PROMPT_RUNTIME_PROMPT_TITLE__',
            'task': '__PROMPT_RUNTIME_OPENED_TASK__',
            'context': '__PROMPT_RUNTIME_OPENED_PROMPT__',
            'previousPrompt': '__PROMPT_RUNTIME_PREVIOUS_PROMPT__',
            'doctypeText': '__PROMPT_RUNTIME_DOCTYPE__',
            'doctypeName': '__PROMPT_RUNTIME_DOCTYPE_NAME__',
            'workflowSource': '__PROMPT_RUNTIME_WORKFLOW_SOURCE__',
            'workflowSlug': source.stem,
            'workflowSourcePath': str(source),
            'workflowEditorSourcePath': str(source),
            'workflowEditorMd5': hashlib.md5(workflow_source.encode('utf-8')).hexdigest(),
            'workflowBlocks': _workflow_blocks_from_markdown(workflow_source),
            'uiText': loc.js_text_map(),
        }
        body = f'''
<section class="prompt-card workflow-runtime-card">
  <h1>{html_lib.escape(_friendly_prompt_title(source.stem))}</h1>
  <p class="muted">{html_lib.escape(loc.t('prompt.home.workflow_source', source=str(source)))}</p>
  <div id="dynamicControls"></div>
  <section class="prompt-card">
    <label for="promptTitle"><b>{html_lib.escape(loc.t('prompt.form.title'))}</b></label>
    <input id="promptTitle" type="text" value="">
    <label for="userTask"><b>{html_lib.escape(loc.t('prompt.form.task'))}</b></label>
    <textarea id="userTask" rows="8"></textarea>
    <label for="baseContext"><b>{html_lib.escape(loc.t('prompt.form.context'))}</b></label>
    <textarea id="baseContext" rows="14"></textarea>
    <label for="extraInstruction"><b>{html_lib.escape(loc.t('prompt.form.extra_instruction'))}</b></label>
    <textarea id="extraInstruction" rows="7"></textarea>
  </section>
  <details class="prompt-card" open>
    <summary><b>{html_lib.escape(loc.t('prompt.workflow_source'))}</b></summary>
    <pre id="workflowSource">{html_lib.escape(workflow_source)}</pre>
  </details>
  <textarea id="doctypeText" style="display:none;"></textarea>
  <span id="doctypeName" style="display:none;"></span>
  <textarea id="previewList" style="display:none;"></textarea>
</section>
'''
        html_text = _prompt_html_shell(self.root, _friendly_prompt_title(source.stem), body, runtime_data=runtime_data)
        if meta_path is not None:
            try:
                Path(meta_path).parent.mkdir(parents=True, exist_ok=True)
                File.writeText(Path(meta_path), json.dumps({'source': str(source), 'compiled_at': time.time(), 'blocks': len(runtime_data['workflowBlocks'])}, indent=2), encoding='utf-8')
            except OSError as exc:
                captureException(None, source='prompt_app.py', context='except@3289')
                _warn_log('PROMPT:WORKFLOW-META-WRITE-FAILED', f'{type(exc).__name__}: {exc}')
        return html_text

    def compile_workflow(self, source_path: Path, compiled_html_path: Path, meta_path: Path | None = None) -> str:
        source = Path(source_path)
        html_text = self.compile_text(_read_prompt_text_with_embedded_fallback(source, encoding='utf-8', errors='replace'), source, meta_path)
        Path(compiled_html_path).parent.mkdir(parents=True, exist_ok=True)
        File.writeText(Path(compiled_html_path), html_text, encoding='utf-8')
        return html_text


def discover_workflows(workflows_root: Path) -> list[WorkflowDefinition]:
    root = Path(workflows_root)
    root.mkdir(parents=True, exist_ok=True)
    definitions: list[WorkflowDefinition] = []
    seen: set[Path] = set()
    for source in sorted(root.glob('**/*.md')):
        if source.name.startswith('_'):
            continue
        folder = source.parent
        if folder in seen and source.stem != folder.name:
            continue
        seen.add(folder)
        slug = _slugify(source.stem if source.stem != 'workflow' else folder.name)
        title = _friendly_prompt_title(source.stem if source.stem != 'workflow' else folder.name)
        definitions.append(WorkflowDefinition(slug=slug, title=title, folder=folder, source_path=source, compiled_html_path=folder / '_compiled_workflow.html', meta_path=folder / '_workflow_meta.log', sort_key=str(source.relative_to(root)).lower()))
    definitions.sort(key=lambda item: item.sort_key or item.title.lower())
    return definitions


def discover_doctypes(doctypes_root: Path) -> list[Path]:
    root = Path(doctypes_root)
    root.mkdir(parents=True, exist_ok=True)
    return sorted([path for path in root.glob('*.md') if path.is_file() and not path.name.startswith('_')], key=lambda path: path.name.lower())


def discover_prompts(prompts_root: Path) -> list[Path]:
    root = Path(prompts_root)
    root.mkdir(parents=True, exist_ok=True)
    prompt_files = [path for path in root.glob('**/*.prompt.md') if path.is_file() and not path.name.startswith('_')]
    return sorted(prompt_files, key=lambda path: str(path.relative_to(root)).lower())


def compile_all_workflows(workflows_root: Path, compiler: WorkflowCompiler) -> list[Path]:
    outputs: list[Path] = []
    for workflow in discover_workflows(workflows_root):
        compiler.compile_workflow(workflow.source_path, workflow.compiled_html_path, workflow.meta_path)
        outputs.append(workflow.compiled_html_path)
    return outputs


def build_prompt_library_entries(prompts_root: Path, prompt_files: list[Path]) -> list[PromptLibraryEntry]:
    root = Path(prompts_root)
    entries: list[PromptLibraryEntry] = []
    for index, path in enumerate(list(prompt_files or [])):
        try:
            text = _read_prompt_text_with_embedded_fallback(Path(path), encoding='utf-8', errors='replace')
            doc = parse_prompt_document(text, _friendly_prompt_title(Path(path).stem))
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@3348')
            print(f"[WARN:swallowed-exception] prompt_app.py:2314 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            doc = PromptDocument(title=_friendly_prompt_title(Path(path).stem), task=EMPTY_STRING, context=EMPTY_STRING)
        try:
            relative_parent = Path(path).parent.relative_to(root)
            inferred_bucket = EMPTY_STRING if str(relative_parent) == '.' else str(relative_parent).replace('\\', ' / ').replace('/', ' / ')
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@3354')
            print(f"[WARN:swallowed-exception] prompt_app.py:2319 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            inferred_bucket = EMPTY_STRING
        bucket_name = _normalize_prompt_bucket_name(doc.bucket_name or inferred_bucket or 'General')
        title = doc.title.strip() or _friendly_prompt_title(Path(path).stem)
        entries.append(PromptLibraryEntry(path=Path(path), title=title, bucket_name=bucket_name, display_name=f'{bucket_name} > {title}' if bucket_name else title, sort_key=f'{bucket_name.lower()}::{title.lower()}::{index:05d}', workflow_slug=doc.workflow_slug, doctype_name=doc.doctype_name))
    entries.sort(key=lambda entry: entry.sort_key)
    return entries


def _ensure_starter_content(paths: AppPaths) -> tuple[list[WorkflowDefinition], list[Path], list[Path]]:
    paths.workflows.mkdir(parents=True, exist_ok=True)
    paths.doctypes.mkdir(parents=True, exist_ok=True)
    paths.prompts.mkdir(parents=True, exist_ok=True)
    starter_workflow = paths.workflows / 'simple_prompt_generator' / 'simple_prompt_generator.md'
    if not starter_workflow.exists():
        starter_workflow.parent.mkdir(parents=True, exist_ok=True)
        File.writeText(starter_workflow, '# Prompt Builder Defaults\n\n[x] Think longer for a better answer.\n[ ] Run AST first.\n[o] Give a zip handoff.\n[__________] Focus file, folder, error, or goal\n', encoding='utf-8')
    starter_doctype = paths.doctypes / '01_normal_doctype.md'
    if not starter_doctype.exists():
        File.writeText(starter_doctype, '# Normal Doctype\n\nWARN when risk or unfinished work matters.\nASK only when inference is risky.\nINFER reasonable defaults.\n', encoding='utf-8')
    starter_prompt = paths.prompts / '01_session_bootstrap' / '01_initial_response_preferences.prompt.md'
    if not starter_prompt.exists():
        starter_prompt.parent.mkdir(parents=True, exist_ok=True)
        File.writeText(starter_prompt, '# Session Bootstrap > Initial Response Preferences\n@workflow: simple_prompt_generator\n@doctype: 01_normal_doctype\n@bucket: Session Bootstrap\n\n## Task\nApply my response preferences for this session.\n\n## Context\nUse this as a behavior reset.\n', encoding='utf-8')
    return discover_workflows(paths.workflows), discover_doctypes(paths.doctypes), discover_prompts(paths.prompts)


def render_doctype_preview_html(root: Path, doctype_name: str, text: str) -> str:
    body = f'<section class="prompt-card"><h1>{html_lib.escape(_friendly_prompt_title(doctype_name))}</h1>{_render_markdown_html(text)}</section>'
    return _prompt_html_shell(root, f'Doctype Preview - {_friendly_prompt_title(doctype_name)}', body)


def render_saved_prompt_export_html(root: Path, doc: PromptDocument, *, generated_prompt: str = EMPTY_STRING, instruction_lines: str = EMPTY_STRING, doctype_name: str = EMPTY_STRING, doctype_text: str = EMPTY_STRING, workflow_title: str = EMPTY_STRING, workflow_slug: str = EMPTY_STRING, workflow_source: str = EMPTY_STRING) -> str:
    loc = PromptLocalization(PromptLocalization.resolve(root))
    body = f'''<section class="prompt-card"><h1>{html_lib.escape(doc.title or loc.t('prompt.home.saved_prompt'))}</h1><p class="muted">{html_lib.escape(loc.t('prompt.home.workflow'))}: {html_lib.escape(workflow_title or workflow_slug or doc.workflow_slug or loc.t('prompt.home.none'))} | {html_lib.escape(loc.t('label.doctype'))}: {html_lib.escape(doctype_name or doc.doctype_name or loc.t('prompt.home.none'))}</p><h2>{html_lib.escape(loc.t('prompt.form.task'))}</h2><pre>{html_lib.escape(doc.task or EMPTY_STRING)}</pre><h2>{html_lib.escape(loc.t('prompt.form.context'))}</h2><pre>{html_lib.escape(doc.context or EMPTY_STRING)}</pre><h2>{html_lib.escape(loc.t('prompt.home.generated_prompt'))}</h2><pre>{html_lib.escape(generated_prompt or EMPTY_STRING)}</pre><h2>{html_lib.escape(loc.t('prompt.home.instruction_lines'))}</h2><pre>{html_lib.escape(instruction_lines or EMPTY_STRING)}</pre><details><summary>{html_lib.escape(loc.t('prompt.home.doctype_text'))}</summary><pre>{html_lib.escape(doctype_text or EMPTY_STRING)}</pre></details><details><summary>{html_lib.escape(loc.t('prompt.workflow_source'))}</summary><pre>{html_lib.escape(workflow_source or EMPTY_STRING)}</pre></details></section>'''
    return _prompt_html_shell(root, doc.title or 'Saved Prompt', body)


def render_prompt_index_html(root: Path, workflows: list[WorkflowDefinition], prompt_files: list[Path], current_workflow_slug: str | None, current_prompt_path: Path | None, current_doctype_name: str = EMPTY_STRING) -> str:
    workflow_cards = []
    for workflow in list(workflows or []):
        active = ' active' if workflow.slug == current_workflow_slug else EMPTY_STRING
        workflow_cards.append(f'<div class="prompt-card{active}"><h3>{html_lib.escape(workflow.title)}</h3><p class="muted">{html_lib.escape(workflow.slug)}</p><a href="prompt://workflow/{html_lib.escape(workflow.slug, quote=True)}">Use workflow</a></div>')
    prompt_links = []
    for prompt_path in list(prompt_files or [])[:200]:
        label = _friendly_prompt_title(prompt_path.stem)
        active = ' selected' if current_prompt_path and Path(current_prompt_path) == Path(prompt_path) else EMPTY_STRING
        prompt_links.append(f'<a class="prompt-pill{active}" href="prompt://open/{html_lib.escape(str(prompt_path), quote=True)}">{html_lib.escape(label)}</a>')
    body = f'''<section class="prompt-card"><h1>Prompt</h1><p class="muted">Prompt is a workflow-based prompt language and generator for LLM sessions.</p><p><a class="prompt-pill" href="prompt://new">New Prompt</a> <span class="prompt-pill">Doctype: {html_lib.escape(current_doctype_name or 'None')}</span></p></section><section><h2>Workflows</h2><div class="prompt-home-grid">{''.join(workflow_cards) or '<p>No workflows found.</p>'}</div></section><section class="prompt-card"><h2>Saved Prompts</h2>{''.join(prompt_links) or '<p>No saved prompts found yet.</p>'}</section>'''
    return _prompt_html_shell(root, 'Prompt Home', body)


def _decorate_menu_actions(menu: QMenu) -> None:
    icon_map = {
        'copy': '📋 ',
        'cut': '✂️ ',
        'paste': '📥 ',
        'undo': '↩️ ',
        'redo': '↪️ ',
        'select all': '🔲 ',
        'reload': '🔄 ',
        'back': '⬅️ ',
        'forward': '➡️ ',
        'save': '💾 ',
        'print': '🖨️ ',
        'view source': '📄 ',
        'inspect': '🔎 ',
    }
    for action in menu.actions():
        text = str(action.text() or '').replace('&', '').strip()
        lowered = text.lower()
        if not text:
            continue
        for key, prefix in icon_map.items():
            if key in lowered and not text.startswith(prefix):
                action.setText(prefix + text)
                break


def _render_markdown_checkbox_html(checked: bool, label_html: str = '', *, inline: bool = False) -> str:
    wrapper_class = 'markdown-inline-checkbox' if inline else 'markdown-checkbox-row'
    checked_attr = ' checked' if checked else ''
    label_suffix = f'<span>{label_html}</span>' if label_html else ''
    return (
        f'<label class="{wrapper_class}">'
        f'<input type="checkbox" disabled{checked_attr}>'
        f'{label_suffix}'
        f'</label>'
    )


def _render_markdown_radio_html(checked: bool = False, label_html: str = '', *, inline: bool = False, name: str = 'markdown-radio-group') -> str:
    wrapper_class = 'markdown-inline-checkbox' if inline else 'markdown-checkbox-row'
    checked_attr = ' checked' if checked else ''
    label_suffix = f'<span>{label_html}</span>' if label_html else ''
    safe_name = html_lib.escape(str(name or 'markdown-radio-group'), quote=True)
    return (
        f'<label class="{wrapper_class}">'
        f'<input type="radio" disabled name="{safe_name}"{checked_attr}>'
        f'{label_suffix}'
        f'</label>'
    )


def _apply_inline_markdown(text: str) -> str:
    escaped = html_lib.escape(html_lib.unescape(str(text or '')))
    placeholders: dict[str, str] = {}

    def stash(html_text: str) -> str:
        key = f'@@MD{len(placeholders)}@@'
        placeholders[key] = html_text
        return key

    def render_input(match: re.Match[str]) -> str:
        marker = str(match.group(1) or '')
        is_textarea = len(marker) >= 8
        css_class = 'markdown-inline-textarea' if is_textarea else 'markdown-inline-input'
        if is_textarea:
            return stash(f'<textarea class=\"{css_class}\" rows=\"4\"></textarea>')
        return stash(f'<input type=\"text\" class=\"{css_class}\">')

    escaped = re.sub(r'\[(_{2,})\]', render_input, escaped)
    escaped = re.sub(r'`([^`]+)`', lambda m: stash(f'<code>{m.group(1)}</code>'), escaped)
    escaped = re.sub(r'\[link\s+url=(?:\'|&quot;)(.*?)(?:\'|&quot;)\](.*?)\[/url\]', lambda m: stash(f'<a href=\"{html_lib.escape(m.group(1), quote=True)}\">{m.group(2)}</a>'), escaped, flags=re.IGNORECASE)
    escaped = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', lambda m: stash(f'<a href=\"{m.group(2)}\">{m.group(1)}</a>'), escaped)
    escaped = re.sub(r'(?<!\w)\[(x|X| )?\](?!\()', lambda m: stash(_render_markdown_checkbox_html((m.group(1) or '').lower() == 'x', inline=True)), escaped)
    escaped = re.sub(r'(?<!\w)\[(o|O|0)\](?!\()', lambda m: stash(_render_markdown_radio_html(False, inline=True)), escaped)
    escaped = re.sub(r'\[b\](.*?)\[/b\]', lambda m: stash(f'<strong>{m.group(1)}</strong>'), escaped, flags=re.IGNORECASE | re.DOTALL)
    escaped = re.sub(r'\*\*([^*]+)\*\*', lambda m: stash(f'<strong>{m.group(1)}</strong>'), escaped)
    escaped = re.sub(r'\[i\](.*?)\[/i\]', lambda m: stash(f'<em>{m.group(1)}</em>'), escaped, flags=re.IGNORECASE | re.DOTALL)
    escaped = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', lambda m: stash(f'<em>{m.group(1)}</em>'), escaped)
    escaped = re.sub(r'(?<!_)_([^_]+)_(?!_)', lambda m: stash(f'<em>{m.group(1)}</em>'), escaped)
    escaped = re.sub(r'\[u\](.*?)\[/u\]', lambda m: stash(f'<span style=\"text-decoration:underline;\">{m.group(1)}</span>'), escaped, flags=re.IGNORECASE | re.DOTALL)
    escaped = re.sub(r'\[color\s+([^\]]+)\](.*?)\[/color\]', lambda m: stash(f'<span style=\"color:{html_lib.escape(str(PromptColor.from_value(html_lib.unescape(m.group(1)))))};\">{m.group(2)}</span>'), escaped, flags=re.IGNORECASE | re.DOTALL)
    for level in range(1, 7):
        escaped = re.sub(rf'\[h{level}\](.*?)\[/h{level}\]', lambda m, level=level: stash(f'<span class=\"workflow-heading-inline workflow-heading-inline-{level}\">{m.group(1)}</span>'), escaped, flags=re.IGNORECASE | re.DOTALL)
    for key, html_text in placeholders.items():
        escaped = escaped.replace(key, html_text)
    return escaped

def _render_markdown_html(text: str) -> str:
    lines = _normalize_markdown_comments(str(text or EMPTY_STRING), allow_single_hash_comments=True).split('\n')
    parts: list[str] = []
    list_mode: str | None = None
    in_code = False
    code_lines: list[str] = []
    code_language = 'text'

    prism_aliases = {
        'py': 'python',
        'python': 'python',
        'js': 'javascript',
        'javascript': 'javascript',
        'ts': 'typescript',
        'typescript': 'typescript',
        'html': 'markup',
        'xml': 'markup',
        'svg': 'markup',
        'md': 'markdown',
        'markdown': 'markdown',
        'sh': 'bash',
        'shell': 'bash',
        'bash': 'bash',
        'ps1': 'powershell',
        'powershell': 'powershell',
        'json': 'json',
        'css': 'css',
        'sql': 'sql',
        'php': 'php',
        'yaml': 'yaml',
        'yml': 'yaml',
        'ini': 'ini',
        'txt': 'none',
        'text': 'none',
        'plain': 'none',
    }

    def prism_language_name(value: str) -> str:
        normalized = re.sub(r'[^a-z0-9_+-]+', '', str(value or '').strip().lower())
        if not normalized:
            return 'none'
        return prism_aliases.get(normalized, normalized)

    def close_list() -> None:
        nonlocal list_mode
        if list_mode:
            parts.append(f'</{list_mode}>')
            list_mode = None

    def flush_code() -> None:
        nonlocal in_code, code_lines, code_language
        if in_code:
            prism_language = prism_language_name(code_language)
            safe_language = html_lib.escape(prism_language, quote=True)
            code_class = f'language-{safe_language}' if prism_language != 'none' else 'language-none'
            parts.append(
                '<pre class="doctype-code-block" data-code-language="' + safe_language + '"><code class="'
                + code_class + '">' + html_lib.escape('\n'.join(code_lines)) + '</code></pre>'
            )
            in_code = False
            code_lines = []
            code_language = 'text'

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith('```'):
            close_list()
            if in_code:
                flush_code()
            else:
                in_code = True
                code_lines = []
                code_language = stripped[3:].strip() or 'text'
            continue
        if in_code:
            code_lines.append(raw_line)
            continue
        if not stripped:
            close_list()
            continue
        if re.fullmatch(r'---+|\*\*\*+', stripped):
            close_list()
            parts.append('<hr>')
            continue
        heading = re.match(r'^(#{1,6})\s+(.+)$', stripped)
        if heading:
            close_list()
            level = len(heading.group(1))
            parts.append(f'<h{level}>' + _apply_inline_markdown(heading.group(2)) + f'</h{level}>')
            continue
        quote = re.match(r'^>\s*(.*)$', stripped)
        if quote:
            close_list()
            parts.append('<blockquote>' + _apply_inline_markdown(quote.group(1)) + '</blockquote>')
            continue
        checkboxes_only = re.fullmatch(r'(?:\[(?:x|X| )?\]\s*)+', stripped)
        if checkboxes_only:
            close_list()
            parts.append('<p class="markdown-inline-checkboxes">' + _apply_inline_markdown(stripped) + '</p>')
            continue
        checkbox = re.match(r'^(?:[-*+]\s+)?\[(x|X| )?\]\s*(.+)$', stripped)
        if checkbox:
            if list_mode != 'ul':
                close_list()
                list_mode = 'ul'
                parts.append('<ul class="markdown-checklist">')
            checked = (checkbox.group(1) or '').lower() == 'x'
            parts.append('<li>' + _render_markdown_checkbox_html(checked, _apply_inline_markdown(checkbox.group(2))) + '</li>')
            continue
        bullet = re.match(r'^[-*+]\s+(.+)$', stripped)
        if bullet:
            if list_mode != 'ul':
                close_list()
                list_mode = 'ul'
                parts.append('<ul>')
            parts.append('<li>' + _apply_inline_markdown(bullet.group(1)) + '</li>')
            continue
        ordered = re.match(r'^\d+\.\s+(.+)$', stripped)
        if ordered:
            if list_mode != 'ol':
                close_list()
                list_mode = 'ol'
                parts.append('<ol>')
            parts.append('<li>' + _apply_inline_markdown(ordered.group(1)) + '</li>')
            continue
        table_line = stripped.startswith('|') and stripped.endswith('|')
        if table_line:
            close_list()
            cells = [cell.strip() for cell in stripped.strip('|').split('|')]
            cell_html = ''.join('<td>' + _apply_inline_markdown(cell) + '</td>' for cell in cells)
            parts.append('<table class="markdown-table"><tr>' + cell_html + '</tr></table>')
            continue
        close_list()
        parts.append('<p>' + _apply_inline_markdown(stripped) + '</p>')
    flush_code()
    close_list()
    return '\n'.join(parts)


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.store: PromptSqlAlchemyStore | None = None
        self._connect()
        _debug_log('PROMPT:SQLALCHEMY-READY', f'path={self.path}')

    def _connect(self) -> None:
        try:
            self.store = prompt_orm_store(self.path)
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@3646')
            print(f"[WARN:swallowed-exception] prompt_app.py:2609 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            self.store = None
            _log_exception('PROMPT:SQLALCHEMY-CONNECT-FAILED', exc, include_traceback=False)


    def get_value(self, key: str, default: str = '') -> str:
        if self.store is None:
            return default
        try:
            value = self.store.get_setting(str(key), str(default))
            _debug_log('PROMPT:SQLALCHEMY-GET', f'key={key} value={value!r} default={default!r}')
            return value
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@3659')
            print(f"[WARN:swallowed-exception] prompt_app.py:2624 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            _log_exception('PROMPT:SQLALCHEMY-GET-FAILED', exc, include_traceback=False)
            return default

    def set_value(self, key: str, value: str) -> None:
        if self.store is None:
            return
        try:
            self.store.upsert_setting(str(key), str(value))
            _debug_log('PROMPT:SQLALCHEMY-SET', f'key={key} value={value!r}')
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@3670')
            print(f"[WARN:swallowed-exception] prompt_app.py:2634 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            _log_exception('PROMPT:SQLALCHEMY-SET-FAILED', exc, include_traceback=False)

    def get_json(self, key: str, default):
        raw = self.get_value(key, '')
        if not raw:
            _debug_log('PROMPT:SQLALCHEMY-GET-JSON', f'key={key} empty-raw using_default={default!r}')
            return default
        try:
            value = json.loads(raw)
            _debug_log('PROMPT:SQLALCHEMY-GET-JSON', f'key={key} value={value!r}')
            return value
        except json.JSONDecodeError as exc:
            captureException(None, source='prompt_app.py', context='except@3683')
            _log_exception('PROMPT:SQLALCHEMY-GET-JSON-FAILED', exc, include_traceback=False)
            return default

    def set_json(self, key: str, value) -> None:
        self.set_value(key, json.dumps(value))

    def debug_dump(self, label: str, keys: list[str] | None = None) -> None:
        if not _debug_enabled():
            return
        exists = self.path.exists()
        size = self.path.stat().st_size if exists else 0
        mtime = self.path.stat().st_mtime if exists else 0
        _debug_log('PROMPT:SQLALCHEMY-DUMP', f'label={label} path={self.path} exists={exists} size={size} mtime={mtime}')
        if self.store is None:
            _debug_log('PROMPT:SQLALCHEMY-DUMP', f'label={label} store=none')
            return
        try:
            rows = self.store.settings_rows(keys)
            _debug_log('PROMPT:SQLALCHEMY-DUMP', f'label={label} row_count={len(rows)}')
            for key, value in rows:
                _debug_log('PROMPT:SQLALCHEMY-DUMP-ROW', f'label={label} key={key} value={value!r}')
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@3705')
            print(f"[WARN:swallowed-exception] prompt_app.py:2668 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            _log_exception('PROMPT:SQLALCHEMY-DUMP-FAILED', exc, include_traceback=False)


class FallbackHtmlView(QTextBrowser):
    def __init__(self, view_name: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(cast(Any, parent))
        self.viewName = view_name  # noqa: nonconform
        self._currentUrl = QUrl()
        self._lastHtmlText = EMPTY_STRING
        self.setOpenExternalLinks(True)
        self.setReadOnly(True)
        self.setContextMenuPolicy(Qt.DefaultContextMenu)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.document().setDocumentMargin(10)

    def _print_context_debug(self, message: str) -> None:
        print(f'[WEB:{self.viewName}:RIGHTCLICK] {message}', file=sys.stderr)

    def url(self) -> QUrl:
        return QUrl(self._currentUrl)

    def _configure_webengine_settings(self) -> None:
        try:
            page = self.page()
            settings = page.settings() if page is not None and hasattr(page, 'settings') else self.settings()
            attributes = (
                'JavascriptEnabled',
                'LocalContentCanAccessFileUrls',
                'LocalContentCanAccessRemoteUrls',
                'ErrorPageEnabled',
                'PluginsEnabled',
            )
            enabled = []
            failed = []
            for attribute_name in attributes:
                try:
                    attr = getattr(QWebEngineSettings.WebAttribute, attribute_name, None)
                    if attr is None:
                        attr = getattr(QWebEngineSettings, attribute_name, None)
                    if attr is None:
                        failed.append(f'{attribute_name}:missing')
                        continue
                    settings.setAttribute(attr, True)
                    enabled.append(attribute_name)
                except Exception as attr_exc:
                    captureException(None, source='prompt_app.py', context='except@3751')
                    failed.append(f'{attribute_name}:{type(attr_exc).__name__}:{attr_exc}')
            _trace_event(f'WEB:{self.viewName}:SETTINGS', f'enabled={enabled} failed={failed}')
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@3754')
            _log_exception(f'WEB:{self.viewName}:SETTINGS-FAILED', exc, include_traceback=True)

    def load(self, url: QUrl) -> None:
        self._currentUrl = QUrl(url)
        source_text = EMPTY_STRING
        local_path = Path(url.toLocalFile()) if url.isLocalFile() else None
        _trace_event(f'WEB:{self.viewName}:LOAD-STARTED', f'url={url.toString()} {_describe_local_file_for_trace(local_path)}')
        _trace_event(f'WEB:{self.viewName}:URL-CHANGED', f'url={url.toString()} {_describe_local_file_for_trace(local_path)}')
        try:
            if local_path is not None and local_path.exists():
                payload = local_path.read_bytes()
                source_text = payload.decode('utf-8', errors='replace')
                _trace_event(f'WEB:{self.viewName}:LOAD-READ', f'url={url.toString()} bytes={len(payload)} chars={len(source_text)} md5={_md5_for_bytes(payload)} {_describe_local_file_for_trace(local_path)}')
            else:
                _trace_event(f'WEB:{self.viewName}:LOAD-MISSING', f'url={url.toString()} {_describe_local_file_for_trace(local_path)}')
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@3770')
            print(f"[WARN:swallowed-exception] prompt_app.py:2699 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            _log_exception('PROMPT:FALLBACK-LOAD-FAILED', exc, include_traceback=True)
        self._lastHtmlText = str(source_text or EMPTY_STRING)
        base_url = QUrl.fromLocalFile(str(local_path.parent.resolve()) + os.sep) if local_path is not None and local_path.exists() else QUrl(url)
        self.document().setBaseUrl(base_url)
        self.setHtml(_fallback_preview_html(self.viewName, self._lastHtmlText, url.toString()))
        _trace_event(f'WEB:{self.viewName}:LOAD-FINISHED', f'ok=True url={url.toString()} chars={len(self._lastHtmlText)} {_describe_local_file_for_trace(local_path)}')

    def showViewSource(self) -> None:
        self._print_context_debug(f'showViewSource fallback len={len(self._lastHtmlText)} url={self.url().toString()}')
        _show_source_view_dialog_modeless(source_text=self._lastHtmlText, title=f'{self.viewName} Source', language_hint='html', parent=self)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        self._print_context_debug(f'handler=contextMenuEvent local={event.pos() if event is not None else None} global={event.globalPos() if event is not None else None} url={self.url().toString()}')
        menu = self.createStandardContextMenu()
        menu.addSeparator()
        copy_action = menu.addAction(prompt_ui_text('source.copy_selection'))
        copy_action.triggered.connect(lambda: Clipboard.copy(self.textCursor().selectedText(), f'{self.viewName}.fallbackSelection', self))
        view_source_action = menu.addAction(prompt_ui_text('source.view_source'))
        view_source_action.triggered.connect(self.showViewSource)
        _decorate_menu_actions(menu)
        Lifecycle.runQtBlockingCall(menu, event.globalPos(), phase_name='prompt.editor.context-menu')
        menu.deleteLater()


class LoggingWebPage(QWebEnginePage):
    def __init__(self, view_name: str, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.viewName = view_name  # noqa: nonconform

    def javaScriptConsoleMessage(self, level: Any, message: str, line_number: int, source_id: str) -> None:
        level_map = {
            QWebEnginePage.JavaScriptConsoleMessageLevel.InfoMessageLevel: 'INFO',
            QWebEnginePage.JavaScriptConsoleMessageLevel.WarningMessageLevel: 'WARN',
            QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel: 'ERROR',
        }
        level_name = level_map.get(level, str(level))
        _trace_event(f'WEB:{self.viewName}:JS-{level_name}', f'source={source_id} line={line_number} message={message}')
        super().javaScriptConsoleMessage(level, message, line_number, source_id)


class LoggingWebView(QWebEngineView):
    def __init__(self, view_name: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.viewName = view_name  # noqa: nonconform
        self.setPage(LoggingWebPage(view_name, cast(Any, self)))
        self._configure_webengine_settings()
        self.setContextMenuPolicy(Qt.DefaultContextMenu)

    def _configure_webengine_settings(self) -> None:
        """Enable the WebEngine settings Prompt needs and trace failures clearly.

        This method belongs on the real QWebEngineView. A previous merge left the
        implementation only on the QTextBrowser fallback, so normal WebEngine
        startup crashed with: LoggingWebView has no attribute
        _configure_webengine_settings. That produced the blank-window launch seen
        in debug(136).log.
        """
        try:
            page = self.page()
            settings = page.settings() if page is not None and hasattr(page, 'settings') else self.settings()
            attributes = (
                'JavascriptEnabled',
                'LocalContentCanAccessFileUrls',
                'LocalContentCanAccessRemoteUrls',
                'ErrorPageEnabled',
                'PluginsEnabled',
            )
            enabled = []
            failed = []
            for attribute_name in attributes:
                try:
                    attr = getattr(QWebEngineSettings.WebAttribute, attribute_name, None)
                    if attr is None:
                        attr = getattr(QWebEngineSettings, attribute_name, None)
                    if attr is None:
                        failed.append(f'{attribute_name}:missing')
                        continue
                    settings.setAttribute(attr, True)
                    enabled.append(attribute_name)
                except Exception as attr_exc:
                    captureException(None, source='prompt_app.py', context='webengine-settings-attribute')
                    failed.append(f'{attribute_name}:{type(attr_exc).__name__}:{attr_exc}')
            _trace_event(f'WEB:{self.viewName}:SETTINGS', f'enabled={enabled} failed={failed}')
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='webengine-settings-failed')
            _log_exception(f'WEB:{self.viewName}:SETTINGS-FAILED', exc, include_traceback=True)

        self.loadStarted.connect(self._on_load_started)
        self.loadFinished.connect(self._on_load_finished)
        self.urlChanged.connect(self._on_url_changed)
        self.renderProcessTerminated.connect(self._on_render_process_terminated)
        self._lastLoadRequestedUrl = EMPTY_STRING
        self._lastLoadRequestedPath = EMPTY_STRING
        self._lastLoadRequestedAt = 0.0
        self._lastHtmlText = EMPTY_STRING
        self._blankRecoveryAttempted = False
        self._lastLoadFinishedOk = False

    def load(self, url: QUrl) -> None:
        try:
            self._lastLoadRequestedUrl = url.toString()
            self._lastLoadRequestedAt = time.time()
            self._blankRecoveryAttempted = False
            local_path = Path(url.toLocalFile()) if url.isLocalFile() else None
            self._lastLoadRequestedPath = str(local_path or EMPTY_STRING)
            if local_path is not None and local_path.exists() and local_path.is_file():
                try:
                    self._lastHtmlText = File.readText(local_path, encoding='utf-8', errors='replace')
                except Exception:
                    captureException(None, source='prompt_app.py', context='except@3840')
                    self._lastHtmlText = EMPTY_STRING
            _trace_event(f'WEB:{self.viewName}:LOAD-CALL', f'url={url.toString()} isLocalFile={url.isLocalFile()} {_describe_local_file_for_trace(local_path)}')
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@3843')
            _log_exception(f'WEB:{self.viewName}:LOAD-CALL-TRACE-FAILED', exc, include_traceback=True)
        return super().load(url)

    def setHtml(self, html_text: str, base_url: QUrl = QUrl()) -> None:
        try:
            payload = str(html_text or EMPTY_STRING).encode('utf-8', errors='replace')
            self._lastHtmlText = str(html_text or EMPTY_STRING)
            self._blankRecoveryAttempted = False
            self._lastLoadRequestedUrl = f'setHtml:{base_url.toString()}'
            self._lastLoadRequestedAt = time.time()
            self._lastLoadRequestedPath = base_url.toLocalFile() if base_url.isLocalFile() else EMPTY_STRING
            _trace_event(f'WEB:{self.viewName}:SETHTML-CALL', f'base_url={base_url.toString()} isLocalFile={base_url.isLocalFile()} bytes={len(payload)} chars={len(str(html_text or EMPTY_STRING))} md5={_md5_for_bytes(payload)} {_describe_local_file_for_trace(Path(self._lastLoadRequestedPath) if self._lastLoadRequestedPath else None)}')
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@3856')
            _log_exception(f'WEB:{self.viewName}:SETHTML-TRACE-FAILED', exc, include_traceback=True)
        return super().setHtml(html_text, base_url)

    def _recover_blank_page(self, reason: str) -> None:
        try:
            source = str(getattr(self, '_lastHtmlText', EMPTY_STRING) or EMPTY_STRING)
            if not source.strip():
                _trace_event(f'WEB:{self.viewName}:BLANK-RECOVERY-SKIP', f'reason={reason} no_cached_html=True')
                return
            if bool(getattr(self, '_blankRecoveryAttempted', False)):
                _trace_event(f'WEB:{self.viewName}:BLANK-RECOVERY-SKIP', f'reason={reason} already_attempted=True cached_chars={len(source)}')
                return
            self._blankRecoveryAttempted = True
            base_path = str(getattr(self, '_lastLoadRequestedPath', EMPTY_STRING) or EMPTY_STRING)
            if base_path:
                base_candidate = Path(base_path)
                if base_candidate.is_file():
                    base_candidate = base_candidate.parent
                base_url = QUrl.fromLocalFile(str(base_candidate.resolve()) + os.sep)
            else:
                base_url = QUrl()
            fallback_html = _fallback_preview_html(self.viewName, source, str(getattr(self, '_lastLoadRequestedUrl', EMPTY_STRING) or EMPTY_STRING))
            payload = fallback_html.encode('utf-8', errors='replace')
            _trace_event(f'WEB:{self.viewName}:BLANK-RECOVERY-SETHTML', f'reason={reason} base_url={base_url.toString()} bytes={len(payload)} chars={len(fallback_html)} md5={_md5_for_bytes(payload)}')
            QTimer.singleShot(0, lambda: QWebEngineView.setHtml(self, fallback_html, base_url))
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@3882')
            _log_exception(f'WEB:{self.viewName}:BLANK-RECOVERY-FAILED', exc, include_traceback=True)

    def _print_context_debug(self, message: str) -> None:
        _trace_event(f'WEB:{self.viewName}:RIGHTCLICK', message)

    def event(self, event: QEvent) -> bool:
        try:
            if event is not None and event.type() == QEvent.ContextMenu:
                global_pos = event.globalPos() if hasattr(event, 'globalPos') else None
                pos_text = str(global_pos) if global_pos is not None else '[no-global-pos]'
                self._print_context_debug(f'event=ContextMenu global={pos_text} url={self.url().toString()}')
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@3894')
            print(f"[WARN:swallowed-exception] prompt_app.py:2760 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            _log_exception('PROMPT:RIGHTCLICK-EVENT-TRACE-FAILED', exc, include_traceback=False)
        return super().event(event)

    def mousePressEvent(self, event) -> None:
        try:
            if event is not None and int(getattr(event, 'button', lambda: Qt.NoButton)()) == int(Qt.RightButton):
                point = event.position().toPoint() if hasattr(event, 'position') else event.pos()
                self._print_context_debug(f'event=MousePress button=Right local=({point.x()},{point.y()}) url={self.url().toString()}')
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@3904')
            print(f"[WARN:swallowed-exception] prompt_app.py:2769 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            _log_exception('PROMPT:RIGHTCLICK-PRESS-TRACE-FAILED', exc, include_traceback=False)
        return super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        try:
            if event is not None and int(getattr(event, 'button', lambda: Qt.NoButton)()) == int(Qt.RightButton):
                point = event.position().toPoint() if hasattr(event, 'position') else event.pos()
                self._print_context_debug(f'event=MouseRelease button=Right local=({point.x()},{point.y()}) url={self.url().toString()}')
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@3914')
            print(f"[WARN:swallowed-exception] prompt_app.py:2778 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            _log_exception('PROMPT:RIGHTCLICK-RELEASE-TRACE-FAILED', exc, include_traceback=False)
        return super().mouseReleaseEvent(event)

    def _copy_selection_to_clipboard(self) -> None:
        page = self.page()
        selected = EMPTY_STRING
        try:
            selected = str(page.selectedText() if page is not None and hasattr(page, 'selectedText') else EMPTY_STRING)
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@3924')
            print(f"[WARN:swallowed-exception] prompt_app.py:web-copy-selected {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        if selected.strip():
            Clipboard.copy(selected, f'{self.viewName}.selectedText', self)
            return
        if page is None or not hasattr(page, 'runJavaScript'):
            Clipboard.copy(EMPTY_STRING, f'{self.viewName}.emptySelection', self)
            return
        script = """
(function () {
    var s = '';
    try { s = String(window.getSelection ? window.getSelection().toString() : ''); } catch (e) {}
    if (!s) {
        var n = document.activeElement;
        if (n && typeof n.value === 'string') {
            var start = n.selectionStart || 0;
            var end = n.selectionEnd || 0;
            s = start !== end ? n.value.substring(start, end) : n.value;
        }
    }
    if (!s && document.body) { s = String(document.body.innerText || document.body.textContent || ''); }
    return s;
})();
"""
        try:
            page.runJavaScript(script, lambda value: Clipboard.copy(str(value or EMPTY_STRING), f'{self.viewName}.javascriptSelection', self))
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@3950')
            print(f"[WARN:swallowed-exception] prompt_app.py:web-copy-js {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)

    def _build_context_menu(self) -> QMenu:
        page = self.page()
        menu = QMenu(self)
        custom_copy_action = QAction(prompt_ui_text('source.copy_selection'), menu)
        custom_copy_action.triggered.connect(self._copy_selection_to_clipboard)
        menu.addAction(custom_copy_action)
        menu.addSeparator()
        if page is not None and hasattr(page, 'action'):
            added_any = False
            for action_name in ('Back', 'Forward', 'Reload', 'Copy', 'SelectAll'):
                try:
                    action_enum = getattr(QWebEnginePage, action_name)
                    action = page.action(action_enum)
                    if action is not None:
                        menu.addAction(action)
                        added_any = True
                except Exception as exc:
                    captureException(None, source='prompt_app.py', context='except@3969')
                    print(f"[WARN:swallowed-exception] prompt_app.py:2794 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    _log_exception(f'PROMPT:CONTEXT-MENU-ACTION-{action_name}-FAILED', exc, include_traceback=False)
            if added_any:
                menu.addSeparator()
        else:
            self._print_context_debug('menu-build no page.action available; using custom-only menu')
        view_source_action = QAction(prompt_ui_text('source.view_source'), menu)
        def _triggered() -> None:
            self._print_context_debug(f'action=View Source url={self.url().toString()}')
            self.showViewSource()
        view_source_action.triggered.connect(_triggered)
        menu.addAction(view_source_action)
        _decorate_menu_actions(menu)
        return menu

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:  # noqa: nonconform reviewed return contract
        try:
            global_pos = event.globalPos() if event is not None else None
            local_pos = event.pos() if event is not None else None
            self._print_context_debug(f'handler=contextMenuEvent local={local_pos} global={global_pos} url={self.url().toString()}')
            menu = self._build_context_menu()
            Lifecycle.runQtBlockingCall(menu, event.globalPos(), phase_name='prompt.editor.context-menu')
            menu.deleteLater()
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@3993')
            print(f"[WARN:swallowed-exception] prompt_app.py:2818 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            _log_exception('PROMPT:CONTEXT-MENU-FAILED', exc, include_traceback=False)
            return super().contextMenuEvent(event)

    def showViewSource(self) -> None:
        page = self.page()
        if page is None or not hasattr(page, 'toHtml'):
            self._print_context_debug('showViewSource aborted: missing page/toHtml')
            return
        self._print_context_debug(f'showViewSource begin url={self.url().toString()}')
        def _open_dialog(source_text: str) -> None:
            text = str(source_text or '')
            self._print_context_debug(f'showViewSource open-dialog len={len(text)}')
            _show_source_view_dialog_modeless(source_text=text, title=f'{self.viewName} Source', language_hint='html', parent=self)
        def _opened(source_text: str) -> None:
            text = str(source_text or '')
            self._print_context_debug(f'toHtml callback len={len(text)} url={self.url().toString()}')
            if text.strip():
                _open_dialog(text)
                return
            try:
                page.runJavaScript('document.documentElement ? document.documentElement.outerHTML : "";', lambda html_text: _open_dialog(str(html_text or '')))
            except Exception as inner_exc:
                captureException(None, source='prompt_app.py', context='except@4016')
                print(f"[WARN:swallowed-exception] prompt_app.py:2840 {type(inner_exc).__name__}: {inner_exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                _log_exception('PROMPT:VIEW-SOURCE-FALLBACK-FAILED', inner_exc, include_traceback=False)
        try:
            page.toHtml(_opened)
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@4021')
            print(f"[WARN:swallowed-exception] prompt_app.py:2844 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            _log_exception('PROMPT:VIEW-SOURCE-FAILED', exc, include_traceback=False)

    def _on_load_started(self) -> None:
        url = self.url()
        local_path = Path(url.toLocalFile()) if url.isLocalFile() else None
        _trace_event(f'WEB:{self.viewName}:LOAD-STARTED', f'url={url.toString()} {_describe_local_file_for_trace(local_path)}')

    def _on_load_finished(self, ok: bool) -> None:
        url = self.url()
        local_path = Path(url.toLocalFile()) if url.isLocalFile() else None
        tag = f'WEB:{self.viewName}:LOAD-FINISHED' if ok else f'WEB:{self.viewName}:LOAD-FAILED'
        elapsed_ms = int((time.time() - float(getattr(self, '_lastLoadRequestedAt', 0.0) or 0.0)) * 1000) if getattr(self, '_lastLoadRequestedAt', 0.0) else -1
        self._lastLoadFinishedOk = bool(ok)
        _trace_event(tag, f'ok={ok} elapsed_ms={elapsed_ms} current_url={url.toString()} requested_url={getattr(self, "_lastLoadRequestedUrl", EMPTY_STRING)} requested_path={getattr(self, "_lastLoadRequestedPath", EMPTY_STRING)} {_describe_local_file_for_trace(local_path)}')
        if not bool(ok):
            self._recover_blank_page('loadFinished-false')
        if local_path is not None and not local_path.exists():
            _trace_event(f'WEB:{self.viewName}:LOAD-MISSING-FILE', f'url={url.toString()} {_describe_local_file_for_trace(local_path)}')
        try:
            page = self.page()
            if page is not None and hasattr(page, 'toHtml'):
                def _html_ready(source_text: str) -> None:
                    html_text = str(source_text or EMPTY_STRING)
                    payload = html_text.encode('utf-8', errors='replace')
                    normalized = html_text.strip().lower().replace(' ', '')
                    blank = not html_text.strip() or normalized in {'<html><head></head><body></body></html>', '<html><body></body></html>'}
                    _trace_event(f'WEB:{self.viewName}:DOM-HTML', f'chars={len(html_text)} bytes={len(payload)} md5={_md5_for_bytes(payload)} blank={blank} url={self.url().toString()}')
                    if blank:
                        self._recover_blank_page('dom-html-blank')
                page.toHtml(_html_ready)
            if page is not None and hasattr(page, 'runJavaScript'):
                page.runJavaScript(WEBENGINE_DIAGNOSTIC_SCRIPT)
                metrics_script = """
(function(){
  try {
    return JSON.stringify({
      href: String(location.href || ''),
      title: String(document.title || ''),
      readyState: String(document.readyState || ''),
      bodyTextLength: document.body ? String(document.body.innerText || document.body.textContent || '').length : -1,
      htmlLength: document.documentElement ? String(document.documentElement.outerHTML || '').length : -1,
      scriptCount: document.scripts ? document.scripts.length : -1,
      dynamicControls: !!document.getElementById('dynamicControls'),
      openedPrompt: !!document.getElementById('openedPrompt')
    });
  } catch (error) {
    return JSON.stringify({error: String(error && error.stack ? error.stack : error)});
  }
})();
"""
                page.runJavaScript(metrics_script, lambda value: _trace_event(f'WEB:{self.viewName}:DOM-METRICS', f'value={value}'))
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@4074')
            _log_exception(f'WEB:{self.viewName}:DIAGNOSTIC-JS-FAILED', exc, include_traceback=True)

    def _on_url_changed(self, url: QUrl) -> None:
        local_path = Path(url.toLocalFile()) if url.isLocalFile() else None
        _trace_event(f'WEB:{self.viewName}:URL-CHANGED', f'url={url.toString()} {_describe_local_file_for_trace(local_path)}')

    def _on_render_process_terminated(self, status, exit_code: int) -> None:
        _trace_event(f'WEB:{self.viewName}:RENDER-TERMINATED', f'status={status} exitCode={exit_code} url={self.url().toString()}')

PREVIEW_WIDGET_CLASS = FallbackHtmlView if PROMPT_WEBENGINE_FALLBACK_ACTIVE else LoggingWebView


class SourceViewDialog(QDialog):
    def _t(self, key: str, **kwargs: Any) -> str:
        return prompt_ui_text(key, **kwargs)

    def __init__(self, source_text: str, title: str = 'View Source', language_hint: str = 'html', parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        try:
            self.setWindowModality(Qt.NonModal)
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@4095')
            print(f"[WARN:swallowed-exception] prompt_app.py:2868 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        try:
            self.setModal(False)
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@4100')
            print(f"[WARN:swallowed-exception] prompt_app.py:2872 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        try:
            self.setWindowFlag(Qt.Tool, True)
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@4106')
            print(f"[WARN:swallowed-exception] prompt_app.py:2877 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        self.setWindowTitle(str(title or self._t('source.view_source_title')))
        self.resize(1100, 760)
        self.sourceText = str(source_text or '')  # noqa: nonconform
        self.languageHint = str(language_hint or 'html')  # noqa: nonconform
        self.sourceViewerHtml = _source_viewer_html(self.sourceText, language_hint=self.languageHint, title=title)  # noqa: nonconform
        self.sourceViewerHtmlPath, self.sourceViewerSourcePath = _dump_source_viewer_payload(self.sourceText, self.sourceViewerHtml, title=title)
        self._sourceViewerLoaded = False
        self._sourceViewerFallbackShown = False
        _warn_log('PROMPT:SOURCE-VIEWER-OPEN', f'title={title} source_len={len(self.sourceText)} fallback={PROMPT_WEBENGINE_FALLBACK_ACTIVE}')
        _warn_log('PROMPT:SOURCE-VIEWER-WINDOW', f'modal={self.isModal()} modality={_qt_flag_value(self.windowModality(), -1) if hasattr(self, "windowModality") else -1} flags={_qt_flag_value(self.windowFlags(), 0) if hasattr(self, "windowFlags") else 0}')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        menu_bar = QMenuBar(self)
        file_menu = menu_bar.addMenu(self._t('menu.file'))
        save_action = QAction(self._t('source.save_as'), self)
        save_action.triggered.connect(self.save_source_as)
        file_menu.addAction(save_action)
        copy_action = QAction(self._t('source.copy_source'), self)
        copy_action.triggered.connect(lambda: Clipboard.copy(self.sourceText, 'SourceDialog.menu', self))
        file_menu.addAction(copy_action)
        file_menu.addSeparator()
        close_action = QAction(self._t('button.close'), self)
        close_action.triggered.connect(self.close)
        file_menu.addAction(close_action)

        find_menu = menu_bar.addMenu(self._t('source.find_menu'))
        find_action = QAction(self._t('source.find_action'), self)
        find_action.setShortcut(QKeySequence('Ctrl+F'))
        find_action.triggered.connect(self.show_find_bar)
        find_menu.addAction(find_action)
        replace_action = QAction(self._t('source.find_replace'), self)
        replace_action.setShortcut(QKeySequence('Ctrl+H'))
        replace_action.triggered.connect(self.show_replace_bar)
        find_menu.addAction(replace_action)
        find_next_action = QAction(self._t('source.find_next'), self)
        find_next_action.setShortcut(QKeySequence('F3'))
        find_next_action.triggered.connect(self.find_next)
        find_menu.addAction(find_next_action)
        find_previous_action = QAction(self._t('source.find_previous'), self)
        find_previous_action.setShortcut(QKeySequence('Shift+F3'))
        find_previous_action.triggered.connect(self.find_previous)
        find_menu.addAction(find_previous_action)
        layout.setMenuBar(menu_bar)

        controls = QHBoxLayout()
        controls.addStretch(1)
        copy_button = QPushButton(self._t('source.copy_source'))
        plain_button = QPushButton(self._t('source.show_plain_text'))
        close_button = QPushButton(self._t('button.close'))
        controls.addWidget(copy_button)
        controls.addWidget(plain_button)
        controls.addWidget(close_button)
        layout.addLayout(controls)

        self.findRow = QWidget(self)  # noqa: nonconform
        find_layout = QHBoxLayout(self.findRow)
        find_layout.setContentsMargins(0, 0, 0, 0)
        find_label = QLabel(self._t('source.find_label'))
        self.findEdit = QLineEdit(self.findRow)  # noqa: nonconform
        self.findEdit.setPlaceholderText(self._t('source.find_placeholder'))
        self.findPrevButton = QPushButton(self._t('source.previous'))  # noqa: nonconform
        self.findNextButton = QPushButton(self._t('source.next'))  # noqa: nonconform
        self.findCloseButton = QPushButton(self._t('source.close_symbol'))  # noqa: nonconform
        self.findStatusLabel = QLabel('')  # noqa: nonconform
        self.findStatusLabel.setMinimumWidth(120)
        find_layout.addWidget(find_label)
        find_layout.addWidget(self.findEdit, 1)
        find_layout.addWidget(self.findPrevButton)
        find_layout.addWidget(self.findNextButton)
        find_layout.addWidget(self.findStatusLabel)
        find_layout.addWidget(self.findCloseButton)
        self.findRow.hide()
        layout.addWidget(self.findRow)

        self.replaceRow = QWidget(self)  # noqa: nonconform
        replace_layout = QHBoxLayout(self.replaceRow)
        replace_layout.setContentsMargins(0, 0, 0, 0)
        replace_label = QLabel(self._t('source.replace_label'))
        self.replaceEdit = QLineEdit(self.replaceRow)  # noqa: nonconform
        self.replaceEdit.setPlaceholderText(self._t('source.replace_placeholder'))
        self.replaceCurrentButton = QPushButton(self._t('source.replace_current'))  # noqa: nonconform
        self.replaceAllButton = QPushButton(self._t('source.replace_all'))  # noqa: nonconform
        self.replaceCloseButton = QPushButton(self._t('source.close_symbol'))  # noqa: nonconform
        replace_layout.addWidget(replace_label)
        replace_layout.addWidget(self.replaceEdit, 1)
        replace_layout.addWidget(self.replaceCurrentButton)
        replace_layout.addWidget(self.replaceAllButton)
        replace_layout.addWidget(self.replaceCloseButton)
        self.replaceRow.hide()
        layout.addWidget(self.replaceRow)

        self.stack = QStackedWidget(self)  # noqa: nonconform
        layout.addWidget(self.stack, 1)
        self.fallbackView = QPlainTextEdit(self)  # noqa: nonconform
        self.fallbackView.setReadOnly(True)
        mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        mono.setPointSize(10)
        self.fallbackView.setFont(mono)
        self.fallbackView.setPlainText(self.sourceText)
        if PROMPT_WEBENGINE_FALLBACK_ACTIVE:
            self.view = self.fallbackView
            self.stack.addWidget(self.fallbackView)
            self.stack.setCurrentWidget(self.fallbackView)
            _warn_log('PROMPT:SOURCE-VIEWER-FALLBACK', 'using plain text because PROMPT_WEBENGINE_FALLBACK_ACTIVE=1')
        else:
            self.view = LoggingWebView('Source Viewer', cast(Any, self))
            self.stack.addWidget(self.view)
            self.stack.addWidget(self.fallbackView)
            self.stack.setCurrentWidget(self.view)
            self.view.loadStarted.connect(lambda: _warn_log('PROMPT:SOURCE-VIEWER-LOAD-STARTED', f'url={self.view.url().toString()}'))
            self.view.urlChanged.connect(lambda url: _warn_log('PROMPT:SOURCE-VIEWER-URL-CHANGED', url.toString()))
            self.view.loadFinished.connect(self._on_web_view_load_finished)
            self._fallbackTimer = QTimer(self)
            self._fallbackTimer.setInterval(1500)
            self._fallbackTimer.setSingleShot(True)
            self._fallbackTimer.timeout.connect(self._on_web_view_load_timeout)
            self._fallbackTimer.start()
            self.view.setHtml(self.sourceViewerHtml)
            _warn_log('PROMPT:SOURCE-VIEWER-SETHTML', f'len={len(self.sourceViewerHtml)} language_hint={language_hint} html_path={self.sourceViewerHtmlPath}')
        copy_button.clicked.connect(lambda: Clipboard.copy(self.sourceText, 'SourceDialog.button', self))
        plain_button.clicked.connect(lambda: self._show_plain_text_fallback('button'))
        close_button.clicked.connect(self.close)
        self.findEdit.returnPressed.connect(self.find_next)
        self.findPrevButton.clicked.connect(self.find_previous)
        self.findNextButton.clicked.connect(self.find_next)
        self.findCloseButton.clicked.connect(self.hide_find_bar)
        self.findEdit.textChanged.connect(self._on_find_text_changed)
        self.replaceCurrentButton.clicked.connect(self.replace_current)
        self.replaceAllButton.clicked.connect(self.replace_all)
        self.replaceCloseButton.clicked.connect(self.hide_replace_bar)
        self.replaceShortcut = QShortcut(QKeySequence('Ctrl+H'), self)  # noqa: nonconform
        self.replaceShortcut.activated.connect(self.show_replace_bar)
        self.findShortcut = QShortcut(QKeySequence('Ctrl+F'), self)  # noqa: nonconform
        self.findShortcut.activated.connect(self.show_find_bar)
        self.findNextShortcut = QShortcut(QKeySequence('F3'), self)  # noqa: nonconform
        self.findNextShortcut.activated.connect(self.find_next)
        self.findPrevShortcut = QShortcut(QKeySequence('Shift+F3'), self)  # noqa: nonconform
        self.findPrevShortcut.activated.connect(self.find_previous)

    def _refresh_source_views(self) -> None:
        self.sourceViewerHtml = _source_viewer_html(self.sourceText, language_hint=self.languageHint if hasattr(self, 'languageHint') else 'html', title=self.windowTitle())
        self.fallbackView.setPlainText(self.sourceText)
        try:
            self.sourceViewerHtmlPath, self.sourceViewerSourcePath = _dump_source_viewer_payload(self.sourceText, self.sourceViewerHtml, title=self.windowTitle())
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@4254')
            print(f"[WARN:swallowed-exception] prompt_app.py:3024 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            _log_exception('PROMPT:SOURCE-VIEWER-DUMP-REFRESH-FAILED', exc, include_traceback=False)
        try:
            if not PROMPT_WEBENGINE_FALLBACK_ACTIVE and hasattr(self, 'view') and self.stack.indexOf(self.view) >= 0:
                self.view.setHtml(self.sourceViewerHtml)
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@4260')
            print(f"[WARN:swallowed-exception] prompt_app.py:3029 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            _log_exception('PROMPT:SOURCE-VIEWER-HTML-REFRESH-FAILED', exc, include_traceback=False)

    def save_source_as(self) -> None:
        suggested = str((self.sourceViewerSourcePath if hasattr(self, 'sourceViewerSourcePath') else Path('source.html')).resolve())
        path, _selected_filter = QFileDialog.getSaveFileName(self, self._t('source.save_dialog'), suggested, self._t('source.save_filter'))
        if not path:
            return
        try:
            File.writeText(Path(path), str(self.sourceText or EMPTY_STRING), encoding='utf-8')
            self._set_find_status(self._t('source.saved_status', name=Path(path).name))
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@4272')
            print(f"[WARN:swallowed-exception] prompt_app.py:3040 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            self._set_find_status('Save failed')
            _log_exception('PROMPT:SOURCE-VIEWER-SAVE-FAILED', exc, include_traceback=True)

    def show_replace_bar(self) -> None:
        self.show_find_bar()
        self.replaceRow.show()
        self.replaceEdit.setFocus()
        self.replaceEdit.selectAll()
        self._show_plain_text_fallback('replace')
        _warn_log('PROMPT:SOURCE-VIEWER-REPLACE-SHOW', f'find_len={len(self.findEdit.text())}')

    def hide_replace_bar(self) -> None:
        self.replaceRow.hide()

    def replace_current(self) -> None:
        needle = str(self.findEdit.text() or EMPTY_STRING)
        replacement = str(self.replaceEdit.text() or EMPTY_STRING)
        if not needle:
            self._set_find_status('Find text required')
            return
        cursor = self.fallbackView.textCursor()
        selected = str(cursor.selectedText() or '').replace('\u2029', '\n')
        if selected != needle:
            self._find_in_plain_text(backwards=False)
            cursor = self.fallbackView.textCursor()
            selected = str(cursor.selectedText() or '').replace('\u2029', '\n')
        if selected == needle:
            cursor.insertText(replacement)
            self.sourceText = self.fallbackView.toPlainText()
            self._refresh_source_views()
            self._set_find_status('Replaced')
            self._find_in_plain_text(backwards=False)
            return
        self._set_find_status('Not found')

    def replace_all(self) -> None:
        needle = str(self.findEdit.text() or EMPTY_STRING)
        replacement = str(self.replaceEdit.text() or EMPTY_STRING)
        if not needle:
            self._set_find_status('Find text required')
            return
        count = str(self.sourceText or EMPTY_STRING).count(needle)
        if count <= 0:
            self._set_find_status('Not found')
            return
        self.sourceText = str(self.sourceText or EMPTY_STRING).replace(needle, replacement)
        self._refresh_source_views()
        self._set_find_status(f'Replaced {count}')
        _warn_log('PROMPT:SOURCE-VIEWER-REPLACE-ALL', f'needle_len={len(needle)} replacement_len={len(replacement)} count={count}')

    def _show_plain_text_fallback(self, reason: str) -> None:
        if self._sourceViewerFallbackShown:
            return
        self._sourceViewerFallbackShown = True
        _warn_log('PROMPT:SOURCE-VIEWER-FALLBACK-SHOW', str(reason or 'unknown'))
        if hasattr(self, 'stack') and self.stack is not None and self.stack.indexOf(self.fallbackView) >= 0:
            self.stack.setCurrentWidget(self.fallbackView)

    def show_find_bar(self) -> None:
        self.findRow.show()
        selected = ''
        try:
            if self.stack.currentWidget() is self.fallbackView:
                selected = str(self.fallbackView.textCursor().selectedText() or '')
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@4338')
            print(f"[WARN:swallowed-exception] prompt_app.py:3105 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            selected = ''
        if selected:
            self.findEdit.setText(selected)
        self.findEdit.setFocus()
        self.findEdit.selectAll()
        _warn_log('PROMPT:SOURCE-VIEWER-FIND-SHOW', f'text_len={len(self.findEdit.text())}')

    def hide_find_bar(self) -> None:
        self.findRow.hide()
        self.findStatusLabel.setText('')
        try:
            if not PROMPT_WEBENGINE_FALLBACK_ACTIVE and hasattr(self, 'view') and hasattr(self.view, 'page') and self.stack.currentWidget() is self.view:
                self.view.page().findText('')
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@4353')
            print(f"[WARN:swallowed-exception] prompt_app.py:3119 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass

    def _on_find_text_changed(self, text: str) -> None:
        value = str(text or '')
        if not value:
            self.findStatusLabel.setText('')
            try:
                if not PROMPT_WEBENGINE_FALLBACK_ACTIVE and hasattr(self, 'view') and hasattr(self.view, 'page') and self.stack.currentWidget() is self.view:
                    self.view.page().findText('')
            except Exception as error:
                captureException(None, source='prompt_app.py', context='except@4364')
                print(f"[WARN:swallowed-exception] prompt_app.py:3129 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
            return
        self.find_next()

    def _set_find_status(self, message: str) -> None:
        self.findStatusLabel.setText(str(message or ''))

    def _find_in_plain_text(self, backwards: bool = False) -> None:
        needle = str(self.findEdit.text() or '')
        if not needle:
            self._set_find_status('')
            return
        flags = QTextDocument.FindFlag(0)
        if backwards:
            flags |= QTextDocument.FindBackward
        found = self.fallbackView.find(needle, flags)
        if not found:
            cursor = self.fallbackView.textCursor()
            cursor.movePosition(QTextCursor.End if backwards else QTextCursor.Start)
            self.fallbackView.setTextCursor(cursor)
            found = self.fallbackView.find(needle, flags)
        self._set_find_status('Found' if found else 'Not found')
        _warn_log('PROMPT:SOURCE-VIEWER-FIND-PLAIN', f'needle={needle!r} backwards={backwards} found={found}')

    def _find_in_web_view(self, backwards: bool = False) -> None:
        needle = str(self.findEdit.text() or '')
        if not needle:
            self._set_find_status('')
            return
        flags = QWebEnginePage.FindFlag(0)
        if backwards:
            flags |= QWebEnginePage.FindBackward
        def _after(found_count):
            found = bool(found_count)
            self._set_find_status('Found' if found else 'Not found')
            _warn_log('PROMPT:SOURCE-VIEWER-FIND-WEB', f'needle={needle!r} backwards={backwards} found={found} count={found_count}')
        self.view.page().findText(needle, flags, _after)

    def find_next(self) -> None:
        if self.stack.currentWidget() is self.fallbackView or PROMPT_WEBENGINE_FALLBACK_ACTIVE:
            self._find_in_plain_text(backwards=False)
            return
        self._find_in_web_view(backwards=False)

    def find_previous(self) -> None:
        if self.stack.currentWidget() is self.fallbackView or PROMPT_WEBENGINE_FALLBACK_ACTIVE:
            self._find_in_plain_text(backwards=True)
            return
        self._find_in_web_view(backwards=True)

    def _on_web_view_load_finished(self, ok: bool) -> None:
        self._sourceViewerLoaded = bool(ok)
        _warn_log('PROMPT:SOURCE-VIEWER-LOAD-FINISHED', f'ok={ok} html_path={self.sourceViewerHtmlPath} source_path={self.sourceViewerSourcePath}')
        if ok:
            try:
                self.view.page().runJavaScript('document.documentElement ? document.documentElement.outerHTML : "";', lambda html_text: _warn_log('PROMPT:SOURCE-VIEWER-DOM', str(html_text or '')))
            except Exception as exc:
                captureException(None, source='prompt_app.py', context='except@4422')
                print(f"[WARN:swallowed-exception] prompt_app.py:3186 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                _log_exception('PROMPT:SOURCE-VIEWER-DOM-FAILED', exc, include_traceback=False)
        else:
            self._show_plain_text_fallback('load-finished-false')

    def _on_web_view_load_timeout(self) -> None:
        if self._sourceViewerLoaded:
            _debug_log('PROMPT:SOURCE-VIEWER-LOAD-TIMEOUT', f'loaded={self._sourceViewerLoaded} html_path={self.sourceViewerHtmlPath}')
            return
        _warn_log('PROMPT:SOURCE-VIEWER-LOAD-TIMEOUT', f'loaded={self._sourceViewerLoaded} html_path={self.sourceViewerHtmlPath}')
        self._show_plain_text_fallback('load-timeout')



PROMPT_INSTALLER_RUNTIME_PATCH = 'V153_HTML_LOAD_FLIGHT_RECORDER'


def _is_nuitka_compiled_runtime() -> bool:
    try:
        return bool(globals().get('__compiled__', False))
    except Exception as exc:
        captureException(exc, source='prompt_app.py', context='prompt-is-nuitka-compiled-runtime', handled=True)
        return False


def _is_nuitka_onefile_extraction_path(path: Path) -> bool:
    try:
        text = str(Path(path).resolve()).lower().replace('\\', '/')
    except Exception as exc:
        captureException(exc, source='prompt_app.py', context='prompt-is-nuitka-onefile-extraction-path', handled=True, extra=f'path={path}')
        text = str(path).lower().replace('\\', '/')
    return ('/temp/onefile_' in text or '/appdata/local/temp/onefile_' in text or 'appdata/local/temp/onefil~' in text)


def _is_pyinstaller_onefile_extraction_path(path: Path) -> bool:
    try:
        text = str(Path(path).resolve()).lower().replace('\\', '/')
    except Exception as exc:
        captureException(exc, source='prompt_app.py', context='prompt-is-pyinstaller-onefile-extraction-path', handled=True, extra=f'path={path}')
        text = str(path).lower().replace('\\', '/')
    return ('/temp/_mei' in text or '/appdata/local/temp/_mei' in text or 'appdata/local/temp/_mei' in text)


def _is_onefile_temp_extraction_path(path: Path) -> bool:
    return bool(_is_pyinstaller_onefile_extraction_path(path) or _is_nuitka_onefile_extraction_path(path))


def _prompt_executable_path() -> Path:
    candidates = []
    for raw in (sys.argv[0] if sys.argv else '', sys.executable, __file__):
        try:
            if raw:
                candidate = Path(str(raw)).expanduser().resolve()
                if candidate.exists() and candidate.is_file():
                    candidates.append(candidate)
        except Exception as exc:
            captureException(exc, source='prompt_app.py', context='prompt-executable-path-probe', handled=True, extra=f'raw={raw!r}')
    return candidates[0] if candidates else Path(str(sys.executable or __file__)).expanduser()


def _prompt_file_md5_safe(path: Path) -> str:
    candidate = Path(path)
    try:
        if candidate.exists() and candidate.is_file():
            return hashlib.md5(candidate.read_bytes()).hexdigest()
    except Exception as exc:
        captureException(exc, source='prompt_app.py', context='prompt-file-md5-safe', handled=True, extra=f'path={candidate}')
        _trace_event('PROMPT:FILE-MD5-FAILED', f'path={candidate} error={type(exc).__name__}:{exc}')
    return 'unavailable'


def _is_frozen_runtime() -> bool:
    return bool(getattr(sys, 'frozen', False) or _is_nuitka_compiled_runtime())


def _candidate_prompt_bundle_roots(base_root: Path | None = None) -> list[Path]:
    candidates: list[Path] = []
    def add(value: object) -> None:
        if not value:
            return
        try:
            path = Path(str(value)).resolve()
        except Exception:
            captureException(None, source='prompt_app.py', context='except@4451')
            return
        if path not in candidates:
            candidates.append(path)
    add(base_root)
    add(Path(__file__).resolve().parent)
    add(os.environ.get('PROMPT_BUNDLE_ROOT', ''))
    add(os.environ.get('PROMPT_ROOT', ''))
    if _is_frozen_runtime():
        add(getattr(sys, '_MEIPASS', ''))
        try:
            # PyInstaller normally uses sys.executable.  Nuitka onefile may run
            # from a temp python.exe while sys.argv[0] points at the real EXE.
            # Keep both so bundled resources and installed-near-exe resources
            # can be discovered.
            exe_parent = Path(sys.executable).resolve().parent
            add(exe_parent)
            add(exe_parent / '_internal')
            add(exe_parent / 'Prompt')
            add(exe_parent / 'Prompt' / '_internal')
            if sys.argv:
                argv_parent = Path(str(sys.argv[0])).expanduser().resolve().parent
                add(argv_parent)
                add(argv_parent / '_internal')
                add(argv_parent / 'Prompt')
                add(argv_parent / 'Prompt' / '_internal')
        except Exception:
            captureException(None, source='prompt_app.py', context='except@4467')
            pass
    try:
        add(Path.cwd())
    except Exception:
        captureException(None, source='prompt_app.py', context='except@4471')
        pass
    expanded: list[Path] = []
    for path in candidates:
        for candidate in (path, path / '_internal', path / 'Prompt', path / 'Prompt' / '_internal'):
            try:
                resolved = candidate.resolve()
            except Exception:
                captureException(None, source='prompt_app.py', context='except@4478')
                resolved = candidate
            if resolved not in expanded:
                expanded.append(resolved)
    return expanded


def _looks_like_prompt_bundle_root(path: Path) -> bool:
    try:
        root = Path(path)
        score = 0
        if (root / 'assets' / 'assets.zip').exists() or (root / 'assets.zip').exists():
            score += 4
        if (root / 'data.py').exists():
            score += 1
        if (root / 'js' / 'prompt_generator.js').exists():
            score += 4
        if (root / 'workflows').exists():
            score += 2
        if (root / 'doctypes').exists():
            score += 1
        if (root / 'vendor').exists():
            score += 1
        if (root / 'help').exists():
            score += 1
        return score >= 4
    except Exception:
        captureException(None, source='prompt_app.py', context='except@4504')
        return False


def _discover_prompt_bundle_root(base_root: Path | None = None) -> Path:
    candidates = _candidate_prompt_bundle_roots(base_root)
    _trace_event('PROMPT:PATHS:BUNDLE-CANDIDATES', f'base={base_root} candidates={[str(candidate) for candidate in candidates]}')
    for candidate in candidates:
        try:
            score = 0
            probes = {
                'assets_zip': candidate / 'assets' / 'assets.zip',
                'legacy_assets_zip': candidate / 'assets.zip',
                'data_py': candidate / 'data.py',
                'prompt_generator': candidate / 'js' / 'prompt_generator.js',
                'workflows': candidate / 'workflows',
                'doctypes': candidate / 'doctypes',
                'vendor': candidate / 'vendor',
                'help': candidate / 'help',
            }
            if probes['assets_zip'].exists() or probes['legacy_assets_zip'].exists():
                score += 4
            if probes['data_py'].exists():
                score += 1
            if probes['prompt_generator'].exists():
                score += 4
            if probes['workflows'].exists():
                score += 2
            if probes['doctypes'].exists():
                score += 1
            if probes['vendor'].exists():
                score += 1
            if probes['help'].exists():
                score += 1
            probe_text = ' '.join(f'{name}={path.exists()}:{path}' for name, path in probes.items())
            _trace_event('PROMPT:PATHS:BUNDLE-PROBE', f'candidate={candidate} score={score} {probe_text}')
            if score >= 4:
                _trace_event('PROMPT:PATHS:BUNDLE-ROOT', f'selected={candidate} score={score}')
                return candidate
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@4543')
            _trace_event('PROMPT:PATHS:BUNDLE-PROBE-FAILED', f'candidate={candidate} error={type(exc).__name__}:{exc}')
    fallback = Path(base_root or Path(__file__).resolve().parent).resolve()
    _trace_event('PROMPT:PATHS:BUNDLE-ROOT', f'fallback={fallback}')
    return fallback


def _prompt_user_data_root() -> Path:
    override = os.environ.get('PROMPT_USER_ROOT', '').strip()
    if override:
        return Path(override).expanduser().resolve()
    if os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA') or str(Path.home() / 'AppData' / 'Local')
        return Path(base) / 'Prompt'
    return Path(os.environ.get('XDG_DATA_HOME') or (Path.home() / '.local' / 'share')) / 'Prompt'


def _prompt_path_is_under(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except Exception as exc:
        captureException(exc, source='prompt_app.py', context='prompt-path-is-under', handled=True, extra=f'path={path} root={root}')
        return False


def _prompt_path_looks_installed(path: Path) -> bool:
    try:
        text = str(Path(path).resolve()).lower().replace('\\', '/')
    except Exception as exc:
        captureException(exc, source='prompt_app.py', context='prompt-path-looks-installed', handled=True, extra=f'path={path}')
        text = str(path).lower().replace('\\', '/')
    protected_tokens = (
        '/program files/', '/program files (x86)/', '/windowsapps/',
        'c:/program files/', 'c:/program files (x86)/',
    )
    return any(token in text for token in protected_tokens)


def _prompt_directory_writable(path: Path) -> bool:
    directory = Path(path)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f'.prompt_write_probe_{os.getpid()}_{int(time.time() * 1000)}.tmp'
        File.writeText(probe, 'ok', encoding='utf-8')
        probe.unlink(missing_ok=True)
        return True
    except Exception as exc:
        captureException(exc, source='prompt_app.py', context='prompt-directory-writable-probe', handled=True, extra=f'path={directory}')
        _trace_event('PROMPT:PATHS:WRITABLE-PROBE-FAILED', f'path={directory} error={type(exc).__name__}:{exc}')
        return False


def _prompt_target_writable(path: Path) -> bool:
    target = Path(path)
    parent = target.parent
    if not _prompt_directory_writable(parent):
        return False
    if not target.exists():
        return True
    try:
        with File.tracedOpen(target, 'a', encoding='utf-8', errors='replace'):
            pass
        return True
    except Exception as exc:
        captureException(exc, source='prompt_app.py', context='prompt-target-writable-probe', handled=True, extra=f'path={target}')
        _trace_event('PROMPT:PATHS:TARGET-WRITABLE-PROBE-FAILED', f'path={target} error={type(exc).__name__}:{exc}')
        return False


def _prompt_make_user_writable(path: Path) -> None:
    try:
        candidate = Path(path)
        if not candidate.exists():
            return
        mode = candidate.stat().st_mode
        if candidate.is_dir():
            candidate.chmod(mode | 0o700)
        else:
            candidate.chmod(mode | 0o600)
    except Exception as exc:
        captureException(exc, source='prompt_app.py', context='prompt-make-user-writable', handled=True, extra=f'path={path}')
        _trace_event('PROMPT:PATHS:CHMOD-FAILED', f'path={path} error={type(exc).__name__}:{exc}')


def _prompt_make_tree_user_writable(root: Path) -> tuple[int, int]:
    changed = 0
    failed = 0
    root_path = Path(root)
    try:
        if not root_path.exists():
            return 0, 0
        for candidate in [root_path] + list(root_path.rglob('*')):
            try:
                _prompt_make_user_writable(candidate)
                changed += 1
            except Exception as exc:
                captureException(exc, source='prompt_app.py', context='prompt-make-tree-user-writable-item', handled=True, extra=f'candidate={candidate}')
                failed += 1
        _trace_event('PROMPT:PATHS:USER-WRITABLE-DONE', f'root={root_path} changed={changed} failed={failed}')
    except Exception as exc:
        captureException(exc, source='prompt_app.py', context='prompt-make-tree-user-writable', handled=True, extra=f'root={root_path}')
        _trace_event('PROMPT:PATHS:USER-WRITABLE-FAILED', f'root={root_path} error={type(exc).__name__}:{exc}')
        failed += 1
    return changed, failed


def _prompt_should_use_user_runtime_root(bundle_root: Path) -> bool:
    forced = str(os.environ.get('PROMPT_FORCE_USER_RUNTIME', '') or '').strip().lower()
    if forced in {'1', 'true', 'yes', 'on'}:
        _trace_event('PROMPT:PATHS:USER-RUNTIME-DECISION', f'use_user=True reason=env-force bundle={bundle_root}')
        return True
    # PyInstaller and Nuitka onefile modes extract code/resources into a
    # disposable temp directory. That directory can be writable, but it is not a
    # valid runtime/user-data root: PyInstaller may not even leave prompt_app.py
    # as a readable source file there, which broke reload, help/about HTML, and
    # language/config saves in the built EXE. Use the stable per-user Prompt
    # root while still seeding/copying bundled HTML/MD/assets from the temp
    # bundle extraction.
    if _is_frozen_runtime() and _is_onefile_temp_extraction_path(bundle_root):
        kind = 'pyinstaller' if _is_pyinstaller_onefile_extraction_path(bundle_root) else 'nuitka'
        _trace_event('PROMPT:PATHS:USER-RUNTIME-DECISION', f'use_user=True reason={kind}-onefile-temp-extraction bundle={bundle_root}')
        return True
    # Frozen builds are allowed to use their bundle root only when the installer
    # placed them in a real writable directory, not a onefile temp extraction.
    if _prompt_path_looks_installed(bundle_root):
        _trace_event('PROMPT:PATHS:USER-RUNTIME-DECISION', f'use_user=True reason=protected-install-path bundle={bundle_root}')
        return True
    if not _prompt_directory_writable(Path(bundle_root)):
        _trace_event('PROMPT:PATHS:USER-RUNTIME-DECISION', f'use_user=True reason=bundle-not-writable bundle={bundle_root}')
        return True
    _trace_event('PROMPT:PATHS:USER-RUNTIME-DECISION', f'use_user=False reason=dev-writable bundle={bundle_root}')
    return False


def _prompt_sanitize_opened_prompt_save_path(opened_path: Path, doc: PromptDocument, prompts_root: Path, runtime_root: Path) -> Path:
    opened = Path(opened_path)
    if _prompt_path_is_under(opened, runtime_root) and _prompt_target_writable(opened):
        _trace_event('PROMPT:PROMPT-FILE:SAVE-TARGET-KEEP', f'path={opened} reason=inside-runtime-writable')
        return opened
    if not _prompt_path_looks_installed(opened) and _prompt_target_writable(opened):
        _trace_event('PROMPT:PROMPT-FILE:SAVE-TARGET-KEEP', f'path={opened} reason=external-writable')
        return opened
    bucket = _normalize_prompt_bucket_name(getattr(doc, 'bucket_name', '') or opened.parent.name or 'Imported')
    title = getattr(doc, 'title', '') or opened.stem
    target = Path(prompts_root) / _slugify(bucket or 'imported') / f'{_slugify(title or opened.stem or "imported_prompt")}.prompt.md'
    suffix = 2
    base = target
    while target.exists() and target.resolve() != opened.resolve():
        target = base.with_name(f'{base.stem}_{suffix}{base.suffix}')
        suffix += 1
    _trace_event('PROMPT:PROMPT-FILE:IMPORT-SAVE-TARGET', f'opened={opened} target={target} reason=readonly-or-installed-path runtime={runtime_root}')
    return target




def _prompt_workflow_preview_needs_refresh(preview_path: Path) -> tuple[bool, str]:
    """Return True when a persisted workflow preview is stale or launch-unsafe.

    Preview HTML is a cache. It must not ship old runtime session state, broken
    doubled JSON string placeholders, or huge prior prompt/handbook payloads. If
    any of those are found, first-run startup should rebuild the preview from the
    workflow markdown and the current runtime state.
    """
    path = Path(preview_path)
    if not path.exists():
        return True, 'missing-preview'
    try:
        text = File.readText(path, encoding='utf-8', errors='replace')
    except Exception as exc:
        captureException(None, source='prompt_app.py', context='preview-refresh-read')
        return True, f'unreadable-preview:{type(exc).__name__}:{exc}'
    if 'window.PromptRuntimeData' in text and re.search(r'"(?:promptTitle|task|context|previousPrompt|doctypeText)"\s*:\s*""', text):
        return True, 'broken-doubled-json-runtime-data'
    stale_markers = (
        '[HANDBOOK]',
        'Azure OpenAI — Environment Handbook',
        'Congratulations! You are fully-configured!',
        'C:\\Prompt',
        'Prompt_CWV_',
    )
    for marker in stale_markers:
        if marker in text:
            return True, f'stale-runtime-marker:{marker}'
    if len(text) > 128_000 and ('previousPrompt' in text or 'previous_prompt' in text):
        return True, f'oversized-runtime-preview:{len(text)}'
    return False, 'preview-cache-ok'

def _ensure_first_run_workflow_html(runtime_root: Path) -> tuple[int, int]:
    root = Path(runtime_root)
    workflows_root = root / 'workflows'
    if not workflows_root.exists():
        _trace_event('PROMPT:WORKFLOW-CACHE:SKIP', f'reason=no-workflows root={workflows_root}')
        return 0, 0
    compiled = 0
    failed = 0
    compiler = WorkflowCompiler(root)
    for workflow in discover_workflows(workflows_root):
        try:
            source_bytes = workflow.source_path.read_bytes() if workflow.source_path.exists() else b''
            preview_path = workflow.folder / '_preview_workflow.html'
            preview_needs_refresh, preview_refresh_reason = _prompt_workflow_preview_needs_refresh(preview_path)
            needs_compile = (
                not workflow.compiled_html_path.exists()
                or preview_needs_refresh
                or (workflow.compiled_html_path.exists() and workflow.source_path.exists() and workflow.compiled_html_path.stat().st_mtime < workflow.source_path.stat().st_mtime)
            )
            _trace_event('PROMPT:WORKFLOW-CACHE:CHECK', f'source={workflow.source_path} source_bytes={len(source_bytes)} source_md5={_md5_for_bytes(source_bytes)} compiled={workflow.compiled_html_path} compiled_exists={workflow.compiled_html_path.exists()} preview={preview_path.exists()} preview_refresh_reason={preview_refresh_reason} needs={needs_compile}')
            if not needs_compile:
                continue
            html_text = compiler.compile_workflow(workflow.source_path, workflow.compiled_html_path, workflow.meta_path)
            File.writeText(preview_path, html_text, encoding='utf-8')
            _prompt_make_user_writable(workflow.compiled_html_path)
            _prompt_make_user_writable(preview_path)
            compiled += 1
            payload = html_text.encode('utf-8', errors='replace')
            _trace_event('PROMPT:WORKFLOW-CACHE:COMPILED', f'source={workflow.source_path} compiled={workflow.compiled_html_path} preview={preview_path} bytes={len(payload)} md5={_md5_for_bytes(payload)}')
        except Exception as exc:
            failed += 1
            captureException(exc, source='prompt_app.py', context='first-run-workflow-html-cache', handled=True, extra=f'workflow={workflow.source_path}')
            _log_exception('PROMPT:WORKFLOW-CACHE:FAILED', exc, include_traceback=True)
    _trace_event('PROMPT:WORKFLOW-CACHE:DONE', f'root={root} compiled={compiled} failed={failed}')
    return compiled, failed


def _directory_has_any_file(path: Path) -> bool:
    try:
        return path.exists() and any(item.is_file() for item in path.rglob('*'))
    except Exception:
        captureException(None, source='prompt_app.py', context='except@4563')
        return False


def _seed_prompt_runtime_root(bundle_root: Path, runtime_root: Path) -> None:
    runtime_root.mkdir(parents=True, exist_ok=True)
    _trace_prompt_runtime_environment('seed-runtime-root', bundle_root=bundle_root, runtime_root=runtime_root)
    _trace_event('PROMPT:PATHS:ERRORS-LOG', f'path={_prompt_errors_log_path()}')
    _trace_event('PROMPT:PATHS:SEED-BEGIN', f'bundle={bundle_root} runtime={runtime_root}')
    _seed_prompt_embedded_runtime_files(runtime_root, overwrite_static=False)
    static_names = ('assets', 'fonts', 'help', 'js', 'vendor')
    content_names = ('doctypes', 'prompts', 'workflows')
    for name in static_names + content_names:
        source = Path(bundle_root) / name
        target = Path(runtime_root) / name
        overwrite = False
        try:
            source_exists = source.exists()
            target_exists = target.exists()
            _trace_event('PROMPT:PATHS:SEED-CHECK', f'name={name} source={source} source_exists={source_exists} target={target} target_exists={target_exists} overwrite={overwrite}')
            if source_exists:
                copied, skipped = _copy_missing_tree(source, target, overwrite=overwrite)
                _trace_event('PROMPT:PATHS:SEED-DONE', f'name={name} copied={copied} skipped={skipped} target={target}')
            else:
                _trace_event('PROMPT:PATHS:SEED-MISSING-SOURCE', f'name={name} source={source}')
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@4588')
            _log_exception(f'PROMPT:PATHS:SEED-FAILED:{name}', exc, include_traceback=True)
    source_config = Path(bundle_root) / 'config.ini'
    target_config = Path(runtime_root) / 'config.ini'
    try:
        _trace_event('PROMPT:PATHS:SEED-CONFIG-CHECK', f'source={source_config} source_exists={source_config.exists()} target={target_config} target_exists={target_config.exists()}')
        if source_config.exists() and not target_config.exists():
            target_config.parent.mkdir(parents=True, exist_ok=True)
            File.copy2(source_config, target_config)
            _trace_event('PROMPT:PATHS:SEED-CONFIG-DONE', f'source={source_config} target={target_config}')
    except Exception as exc:
        captureException(None, source='prompt_app.py', context='except@4598')
        _log_exception('PROMPT:PATHS:SEED-CONFIG-FAILED', exc, include_traceback=True)
    required = (
        runtime_root / 'js' / 'prompt_generator.js',
        runtime_root / 'vendor' / 'jquery' / 'jquery-4.0.0.min.js',
        runtime_root / 'help' / 'prompt_help.html',
        runtime_root / 'workflows',
    )
    for required_path in required:
        try:
            role = str(Path(required_path).relative_to(runtime_root))
        except Exception:
            captureException(None, source='prompt_app.py', context='except@4609')
            role = str(required_path)
        _trace_html_asset('PROMPT:PATHS:REQUIRED-CHECK', Path(required_path), role=role, required=True)
    _prompt_make_tree_user_writable(runtime_root)
    _ensure_first_run_workflow_html(runtime_root)


@dataclass
class AppPaths:
    root: Path
    workflows: Path
    doctypes: Path
    prompts: Path
    generated: Path
    fonts: Path
    assets: Path



def _read_config_value(root: Path, dotted_key: str, default: str = '') -> str:
    try:
        section, option = str(dotted_key or '').split('.', 1)
    except ValueError as exc:
        captureException(exc, source='prompt_app.py', context='read-config-value-key-split', handled=True, extra=dotted_key)
        return str(default)
    try:
        parser = configparser.ConfigParser()
        config_path = Path(root) / 'config.ini'
        if config_path.exists():
            parser.read(config_path, encoding='utf-8')
        return str(parser.get(section, option, fallback=str(default)))
    except Exception as exc:
        captureException(exc, source='prompt_app.py', context='read-config-value', handled=True, extra=str(dotted_key))
        return str(default)


class PromptSoundManager:
    """Small, safe sound-effect manager for Prompt UI events."""

    EVENT_FILES: dict[str, str] = {
        'startup': 'open_and_close_application',
        'ready': 'connect',
        'click': 'click',
        'save': 'tick',
        'error': 'error',
        'open_modal': 'open_modal',
        'close_modal': 'close_modal',
        'enable': 'ebable',  # uploaded scheme filename is intentionally preserved
        'disable': 'disable',
        'popup': 'popup',
        'shell': 'shell',
        'minimize': 'minimize',
        'maximize': 'maximize',
    }

    def __init__(self, paths: AppPaths, settings: SettingsStore | None = None) -> None:
        self.paths = paths  # noqa: nonconform
        self.settings = settings
        self.soundRoot = Path(paths.assets) / 'sounds'  # noqa: nonconform
        self.enabled = self._read_bool('sounds.enabled', default=True)
        self.volume = self._read_float('sounds.volume', default=0.65)  # noqa: nonconform
        self._effects: dict[str, Any] = {}
        self._players: list[Any] = []
        _trace_event('PROMPT:SOUND:INIT', f'available={PROMPT_QT_SOUND_AVAILABLE} enabled={self.enabled} root={self.soundRoot} exists={self.soundRoot.exists()}')

    def _read_bool(self, key: str, default: bool = True) -> bool:
        raw = ''
        try:
            raw = str(self.settings.get_value(key, '') if self.settings is not None else '')
        except Exception as exc:
            captureException(exc, source='prompt_app.py', context='sound-read-bool', handled=True, extra=key)
        if not raw:
            raw = str(_read_config_value(Path(self.paths.root), key, 'true' if default else 'false') or '')
        if not raw:
            return bool(default)
        return raw.strip().lower() not in {'0', 'false', 'no', 'off', 'disabled'}

    def _read_float(self, key: str, default: float = 0.65) -> float:
        raw = ''
        try:
            raw = str(self.settings.get_value(key, '') if self.settings is not None else '')
        except Exception as exc:
            captureException(exc, source='prompt_app.py', context='sound-read-float', handled=True, extra=key)
        if not raw:
            raw = str(_read_config_value(Path(self.paths.root), key, str(default)) or '')
        try:
            return max(0.0, min(1.0, float(raw)))
        except Exception as exc:
            captureException(exc, source='prompt_app.py', context='sound-read-float-default', handled=True, extra=key)
            return float(default)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        value = 'true' if self.enabled else 'false'
        try:
            if self.settings is not None:
                self.settings.set_value('sounds.enabled', value)
        except Exception as exc:
            captureException(exc, source='prompt_app.py', context='sound-save-setting-db', handled=True)
        try:
            config_path = Path(self.paths.root) / 'config.ini'
            parser = configparser.ConfigParser()
            parser.read(config_path, encoding='utf-8')
            if not parser.has_section('sounds'):
                parser.add_section('sounds')
            parser.set('sounds', 'enabled', value)
            parser.set('sounds', 'volume', f'{self.volume:.2f}')
            with File.tracedOpen(config_path, 'w', encoding='utf-8') as handle:
                parser.write(handle)
            _prompt_make_user_writable(config_path)
        except Exception as exc:
            captureException(exc, source='prompt_app.py', context='sound-save-config', handled=True)
        _trace_event('PROMPT:SOUND:SETTING', f'enabled={self.enabled} volume={self.volume}')

    def _event_path(self, event: str) -> Path | None:
        stem = self.EVENT_FILES.get(str(event or '').strip().lower(), str(event or '').strip().lower())
        if not stem:
            return None
        for suffix in ('.wav', '.ogg'):
            candidate = self.soundRoot / f'{stem}{suffix}'
            if candidate.exists() and candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        return None

    def play(self, event: str, *, force: bool = False) -> None:
        if not force and not self.enabled:
            return
        if not PROMPT_QT_SOUND_AVAILABLE:
            _trace_event('PROMPT:SOUND:SKIP', f'event={event} reason=QtMultimedia-unavailable')
            return
        path = self._event_path(event)
        if path is None:
            _trace_event('PROMPT:SOUND:MISSING', f'event={event} root={self.soundRoot}')
            return
        try:
            suffix = path.suffix.lower()
            if suffix == '.wav' and QSoundEffect is not None:
                key = str(path)
                effect = self._effects.get(key)
                if effect is None:
                    effect = QSoundEffect()
                    effect.setSource(QUrl.fromLocalFile(str(path)))
                    self._effects[key] = effect
                effect.setVolume(float(self.volume))
                effect.play()
                _trace_event('PROMPT:SOUND:PLAY', f'event={event} mode=QSoundEffect path={path} bytes={path.stat().st_size}')
                return
            if QMediaPlayer is not None and QAudioOutput is not None:
                player = QMediaPlayer()
                audio = QAudioOutput()
                audio.setVolume(float(self.volume))
                player.setAudioOutput(audio)
                player.setSource(QUrl.fromLocalFile(str(path)))
                player.play()
                self._players.append((player, audio))
                QTimer.singleShot(2500, lambda p=player, a=audio: self._cleanup_player(p, a))
                _trace_event('PROMPT:SOUND:PLAY', f'event={event} mode=QMediaPlayer path={path} bytes={path.stat().st_size}')
        except Exception as exc:
            captureException(exc, source='prompt_app.py', context='sound-play', handled=True, extra=f'event={event} path={path}')

    def _cleanup_player(self, player: Any, audio: Any) -> None:
        try:
            player.stop()
        except Exception as exc:
            captureException(exc, source='prompt_app.py', context='sound-cleanup-stop', handled=True)
        try:
            self._players = [item for item in self._players if item[0] is not player]
        except Exception as exc:
            captureException(exc, source='prompt_app.py', context='sound-cleanup-list', handled=True)

@dataclass
class PromptDocument:
    title: str
    task: str
    context: str
    workflow_slug: str = ''
    doctype_name: str = ''
    bucket_name: str = ''


@dataclass
class PromptLibraryEntry:
    path: Path
    title: str
    bucket_name: str
    display_name: str
    sort_key: str
    workflow_slug: str = ''
    doctype_name: str = ''


@dataclass
class FontGlyphEntry:
    glyph_index: int
    glyph_name: str
    codepoints: list[int]


GLYPH_MIME_TYPE = 'application/x-prompt-font-glyph'


def _font_codepoints_to_plain_text(codepoints: list[int]) -> str:
    parts: list[str] = []
    for codepoint in list(codepoints or []):
        try:
            parts.append(chr(int(codepoint)))
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@4661')
            print(f"[WARN:swallowed-exception] prompt_app.py:3248 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            continue
    return ''.join(parts)


def _font_codepoints_to_uplus_text(codepoints: list[int]) -> str:
    values: list[str] = []
    for codepoint in list(codepoints or []):
        try:
            values.append(f'U+{int(codepoint):04X}')
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@4672')
            print(f"[WARN:swallowed-exception] prompt_app.py:3258 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            continue
    return ', '.join(values)


def _font_codepoints_to_int_text(codepoints: list[int]) -> str:
    values: list[str] = []
    for codepoint in list(codepoints or []):
        try:
            values.append(str(int(codepoint)))
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@4683')
            print(f"[WARN:swallowed-exception] prompt_app.py:3268 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            continue
    return ', '.join(values)


def _load_font_glyph_entries(font_path: Path) -> list[FontGlyphEntry]:
    if TTFont is None:
        raise RuntimeError('fontTools is required to inspect font glyphs.')
    font = TTFont(str(font_path), lazy=True, fontNumber=0)
    try:
        glyph_order = list(font.getGlyphOrder() or [])
        cmap_tables = []
        try:
            cmap_tables = list(getattr(font.get('cmap'), 'tables', []) or [])
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@4698')
            print(f"[WARN:swallowed-exception] prompt_app.py:3282 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            cmap_tables = []
        glyph_to_codepoints: dict[str, list[int]] = {}
        for table in cmap_tables:
            try:
                if not bool(table.isUnicode()):
                    continue
                table_map = dict(getattr(table, 'cmap', {}) or {})
            except Exception as error:
                captureException(None, source='prompt_app.py', context='except@4707')
                print(f"[WARN:swallowed-exception] prompt_app.py:3290 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                continue
            for codepoint, glyph_name in list(table_map.items()):
                glyph_key = str(glyph_name or EMPTY_STRING).strip()
                if not glyph_key:
                    continue
                values = glyph_to_codepoints.setdefault(glyph_key, [])
                code_int = int(codepoint)
                if code_int not in values:
                    values.append(code_int)
        entries: list[FontGlyphEntry] = []
        for glyph_index, glyph_name in enumerate(glyph_order):
            codepoints = sorted(glyph_to_codepoints.get(str(glyph_name or EMPTY_STRING), []))
            entries.append(FontGlyphEntry(glyph_index=glyph_index, glyph_name=str(glyph_name or EMPTY_STRING), codepoints=codepoints))
        return entries
    finally:
        try:
            font.close()
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@4726')
            print(f"[WARN:swallowed-exception] prompt_app.py:3308 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass


def _font_glyph_display_text(entry: FontGlyphEntry) -> str:
    text = _font_codepoints_to_plain_text(entry.codepoints)
    if text:
        return text[:1]
    return '□'


def _font_glyph_payload(entry: FontGlyphEntry, font_path: Path | str) -> dict[str, object]:
    return {
        'glyph_index': int(entry.glyph_index),
        'glyph_name': str(entry.glyph_name or EMPTY_STRING),
        'codepoints': [int(value) for value in list(entry.codepoints or [])],
        'font_path': str(font_path or EMPTY_STRING),
    }


def _font_glyph_entry_from_payload(payload: dict[str, Any]) -> FontGlyphEntry:
    return FontGlyphEntry(
        glyph_index=int(payload.get('glyph_index', -1) or -1),
        glyph_name=str(payload.get('glyph_name', EMPTY_STRING) or EMPTY_STRING),
        codepoints=[int(value) for value in list(payload.get('codepoints', []) or [])],
    )


def _font_glyph_payload_key(payload: dict[str, Any]) -> str:
    codepoints = ','.join(str(int(value)) for value in list(payload.get('codepoints', []) or []))
    return '|'.join((
        str(payload.get('font_path', EMPTY_STRING) or EMPTY_STRING),
        str(payload.get('glyph_name', EMPTY_STRING) or EMPTY_STRING),
        str(int(payload.get('glyph_index', -1) or -1)),
        codepoints,
    ))


def _encode_font_glyph_payloads(payloads: list[dict[str, Any]]) -> QMimeData:
    mime = QMimeData()
    mime.setData(GLYPH_MIME_TYPE, QByteArray(json.dumps(list(payloads or []), ensure_ascii=False).encode('utf-8')))
    try:
        mime.setText(json.dumps(list(payloads or []), ensure_ascii=False))
    except Exception as error:
        captureException(None, source='prompt_app.py', context='except@4770')
        print(f"[WARN:swallowed-exception] prompt_app.py:3351 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        pass
    return mime


def _decode_font_glyph_payloads(mime: QMimeData | None) -> list[dict[str, Any]]:
    if mime is None:
        return []
    raw = b''
    try:
        if mime.hasFormat(GLYPH_MIME_TYPE):
            raw = bytes(mime.data(GLYPH_MIME_TYPE))
    except Exception as error:
        captureException(None, source='prompt_app.py', context='except@4783')
        print(f"[WARN:swallowed-exception] prompt_app.py:3363 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        raw = b''
    if not raw:
        try:
            raw_text = str(mime.text() or EMPTY_STRING)
            raw = raw_text.encode('utf-8') if raw_text else b''
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@4790')
            print(f"[WARN:swallowed-exception] prompt_app.py:3369 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            raw = b''
    if not raw:
        return []
    try:
        payload = json.loads(raw.decode('utf-8', errors='replace'))
    except Exception as error:
        captureException(None, source='prompt_app.py', context='except@4797')
        print(f"[WARN:swallowed-exception] prompt_app.py:3375 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        return []
    if isinstance(payload, dict):
        payload = [payload]
    results: list[dict[str, Any]] = []
    for item in list(payload or []):
        if not isinstance(item, dict):
            continue
        try:
            results.append({
                'glyph_index': int(item.get('glyph_index', -1) or -1),
                'glyph_name': str(item.get('glyph_name', EMPTY_STRING) or EMPTY_STRING),
                'codepoints': [int(value) for value in list(item.get('codepoints', []) or [])],
                'font_path': str(item.get('font_path', EMPTY_STRING) or EMPTY_STRING),
            })
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@4813')
            print(f"[WARN:swallowed-exception] prompt_app.py:3390 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            continue
    return results


def _font_output_kind_extension(kind: str) -> str:
    mapping = {'ttf': '.ttf', 'otf': '.otf', 'woff': '.woff', 'woff2': '.woff2'}
    return mapping.get(str(kind or EMPTY_STRING).strip().lower(), '.ttf')


def _font_export_support_error(output_kind: str) -> str:
    kind = str(output_kind or EMPTY_STRING).strip().lower()
    if TTFont is None or FontSubsetOptions is None or FontSubsetter is None or load_subset_font is None or save_subset_font is None:
        return 'fontTools subset support is missing.'
    if kind == 'woff2' and _brotli_module is None:
        return 'WOFF2 export requires the Brotli Python extension.'
    return EMPTY_STRING


def _apply_export_font_name(font: Any, family_name: str, style_name: str = 'Regular') -> None:
    clean_family_name = re.sub(r'\s+', ' ', str(family_name or EMPTY_STRING).strip()) or 'subset_font'
    clean_style_name = re.sub(r'\s+', ' ', str(style_name or EMPTY_STRING).strip()) or 'Regular'
    full_name = f'{clean_family_name} {clean_style_name}'.strip()
    postscript_family = re.sub(r'[^A-Za-z0-9]+', EMPTY_STRING, clean_family_name.title()) or 'SubsetFont'
    postscript_style = re.sub(r'[^A-Za-z0-9]+', EMPTY_STRING, clean_style_name.title()) or 'Regular'
    postscript_name = f'{postscript_family}-{postscript_style}'
    version_text = 'Version 1.0'
    name_table = font['name'] if 'name' in font else None
    if name_table is not None:
        for platform_id, encoding_id, language_id in ((3, 1, 0x409), (1, 0, 0)):
            try:
                name_table.setName(clean_family_name, 1, platform_id, encoding_id, language_id)
                name_table.setName(clean_style_name, 2, platform_id, encoding_id, language_id)
                name_table.setName(f'{clean_family_name}; {version_text}; {postscript_name}', 3, platform_id, encoding_id, language_id)
                name_table.setName(full_name, 4, platform_id, encoding_id, language_id)
                name_table.setName(version_text, 5, platform_id, encoding_id, language_id)
                name_table.setName(postscript_name, 6, platform_id, encoding_id, language_id)
                name_table.setName(clean_family_name, 16, platform_id, encoding_id, language_id)
                name_table.setName(clean_style_name, 17, platform_id, encoding_id, language_id)
            except Exception as error:
                captureException(None, source='prompt_app.py', context='except@4853')
                print(f"[WARN:swallowed-exception] prompt_app.py:3429 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                continue
    cff_table = font['CFF '] if 'CFF ' in font else None
    if cff_table is not None:
        try:
            top_dict = cff_table.cff.topDictIndex[0]
            top_dict.FontName = postscript_name
            top_dict.FullName = full_name
            top_dict.FamilyName = clean_family_name
            top_dict.Weight = clean_style_name
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@4864')
            print(f"[WARN:swallowed-exception] prompt_app.py:3439 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass


def _export_font_subset(source_font_path: Path, glyph_payloads: list[dict[str, Any]], output_path: Path, output_kind: str, family_name: str = EMPTY_STRING) -> Path:
    support_error = _font_export_support_error(output_kind)
    if support_error:
        raise RuntimeError(support_error)
    glyph_names = [str(item.get('glyph_name', EMPTY_STRING) or EMPTY_STRING).strip() for item in list(glyph_payloads or []) if str(item.get('glyph_name', EMPTY_STRING) or EMPTY_STRING).strip()]
    if not glyph_names:
        raise RuntimeError('No glyphs selected.')
    options = cast(Any, FontSubsetOptions)()
    options.ignore_missing_glyphs = True
    options.ignore_missing_unicodes = True
    options.retain_gids = False
    options.notdef_glyph = True
    options.notdef_outline = True
    options.recommended_glyphs = True
    options.layout_closure = True
    kind = str(output_kind or EMPTY_STRING).strip().lower()
    options.flavor = kind if kind in {'woff', 'woff2'} else None
    font = cast(Any, load_subset_font)(str(source_font_path), options, checkChecksums=0, dontLoadGlyphNames=False, lazy=False)
    try:
        subsetter = cast(Any, FontSubsetter)(options=options)
        subsetter.populate(glyphs=sorted(set(glyph_names)))
        subsetter.subset(font)
        _apply_export_font_name(font, family_name or output_path.stem)
        cast(Any, save_subset_font)(font, str(output_path), options)
    finally:
        try:
            font.close()
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@4896')
            print(f"[WARN:swallowed-exception] prompt_app.py:3470 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
    return output_path


class FontGlyphAtlasList(QListWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.entries: list[FontGlyphEntry] = []
        self.fontPath = Path()  # noqa: nonconform
        self.previewFont = QFontDatabase.systemFont(QFontDatabase.GeneralFont)  # noqa: nonconform
        self.previewFont.setPointSize(18)
        self.overlayLabel = QLabel(self.viewport())
        self.overlayLabel.hide()
        self.overlayLabel.setAlignment(Qt.AlignCenter)
        self.overlayLabel.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.overlayLabel.setStyleSheet('background: rgba(24,24,24,0.98); border: 2px solid #6fb3ff; border-radius: 8px; font-size: 34px; padding: 0px;')
        self.setViewMode(QListWidget.IconMode)
        self.setFlow(QListWidget.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListWidget.Adjust)
        self.setMovement(QListWidget.Static)
        self.setUniformItemSizes(True)
        self.setSpacing(0)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setDragEnabled(True)
        self.setGridSize(QSize(38, 38))
        self.setStyleSheet('QListWidget { background:#111; } QListWidget::item { border:1px solid #444; margin:0px; padding:0px; } QListWidget::item:selected { background:#1b2e44; border:1px solid #6fb3ff; color:#fff; }')
        self.currentItemChanged.connect(lambda *_: self.updateOverlay())
        try:
            self.verticalScrollBar().valueChanged.connect(lambda *_: self.updateOverlay())
            self.horizontalScrollBar().valueChanged.connect(lambda *_: self.updateOverlay())
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@4929')
            print(f"[WARN:swallowed-exception] prompt_app.py:3502 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass

    def mimeData(self, items) -> QMimeData:
        payloads: list[dict[str, Any]] = []
        for item in list(items or []):
            row = int(item.data(Qt.UserRole) or -1)
            if row < 0 or row >= len(self.entries):
                continue
            payloads.append(_font_glyph_payload(self.entries[row], self.fontPath))
        return _encode_font_glyph_payloads(payloads)

    def supportedDropActions(self):
        return Qt.CopyAction

    def setPreviewFont(self, font: QFont) -> None:
        self.previewFont = QFont(font)
        if self.previewFont.pointSize() < 18:
            self.previewFont.setPointSize(18)

    def populateGlyphs(self, entries: list[FontGlyphEntry], font_path: Path) -> None:
        self.clear()
        self.entries = list(entries or [])
        self.fontPath = Path(font_path)
        for row, entry in enumerate(self.entries):
            text = _font_glyph_display_text(entry)
            item = QListWidgetItem(text)
            item.setTextAlignment(Qt.AlignCenter)
            item.setData(Qt.UserRole, row)
            item.setData(Qt.UserRole + 1, _font_glyph_payload(entry, self.fontPath))
            item.setToolTip(f'{entry.glyph_name}\n{_font_codepoints_to_uplus_text(entry.codepoints) or "(no Unicode mapping)"}')
            item.setSizeHint(QSize(36, 36))
            item.setFont(self.previewFont)
            self.addItem(item)
        if self.count():
            self.setCurrentRow(0)
        else:
            self.overlayLabel.hide()

    def currentGlyphRow(self) -> int:
        item = self.currentItem()
        if item is None:
            return -1
        try:
            return int(item.data(Qt.UserRole) or -1)
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@4975')
            print(f"[WARN:swallowed-exception] prompt_app.py:3547 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return -1

    def updateOverlay(self) -> None:
        item = self.currentItem()
        if item is None:
            self.overlayLabel.hide()
            return
        rect = self.visualItemRect(item)
        if rect.isNull() or rect.width() <= 0 or rect.height() <= 0:
            self.overlayLabel.hide()
            return
        text = str(item.text() or EMPTY_STRING) or '□'
        overlay_font = QFont(self.previewFont)
        overlay_font.setPointSize(max(self.previewFont.pointSize() + 10, 30))
        self.overlayLabel.setFont(overlay_font)
        self.overlayLabel.setText(text)
        grown = rect.adjusted(-12, -12, 12, 12)
        viewport_rect = self.viewport().rect()
        if grown.left() < 0:
            grown.moveLeft(0)
        if grown.top() < 0:
            grown.moveTop(0)
        if grown.right() > viewport_rect.right():
            grown.moveRight(viewport_rect.right())
        if grown.bottom() > viewport_rect.bottom():
            grown.moveBottom(viewport_rect.bottom())
        self.overlayLabel.setGeometry(grown)
        self.overlayLabel.raise_()
        self.overlayLabel.show()


class FontSelectionList(QListWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.payloadDroppedHandler: Any = None
        self.setAcceptDrops(True)
        self.setDragEnabled(False)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDefaultDropAction(Qt.CopyAction)
        self.setStyleSheet('QListWidget::item { border:1px solid #444; margin:0px; padding:4px; } QListWidget::item:selected { background:#1b2e44; border:1px solid #6fb3ff; }')

    def dragEnterEvent(self, event) -> None:
        if _decode_font_glyph_payloads(event.mimeData()):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if _decode_font_glyph_payloads(event.mimeData()):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        payloads = _decode_font_glyph_payloads(event.mimeData())
        if payloads and callable(self.payloadDroppedHandler):
            self.payloadDroppedHandler(payloads)
            event.acceptProposedAction()
            return
        super().dropEvent(event)


class FontsPane(QWidget):
    def __init__(self, fonts_root: Path, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.fontsRoot = Path(fonts_root)  # noqa: nonconform
        self.currentFontPath: Optional[Path] = None
        self.currentGlyphEntries: list[FontGlyphEntry] = []
        self.currentApplicationFontId = -1  # noqa: nonconform
        self.titleLabel = QLabel('<b>' + prompt_ui_text('tab.fonts') + '</b>')  # noqa: nonconform
        self.fontSelector = QComboBox()
        self.openButton = QPushButton(prompt_ui_text('fonts.open'))
        self.reloadButton = QPushButton(prompt_ui_text('fonts.reload'))
        self.pathLabel = QLabel('')  # noqa: nonconform
        self.pathLabel.setWordWrap(True)
        self.inventoryLabel = QLabel('')  # noqa: nonconform
        self.inventoryLabel.setWordWrap(True)
        self.fontList = QListWidget()
        self.fontList.setSelectionMode(QAbstractItemView.SingleSelection)
        self.fontList.setAlternatingRowColors(True)

        self.glyphAtlas = FontGlyphAtlasList()  # noqa: nonconform
        self.glyphAtlas.setMinimumWidth(420)

        self.selectionList = FontSelectionList()  # noqa: nonconform
        self.selectionList.payloadDroppedHandler = self.addGlyphPayloadsToSelection

        self.addCurrentButton = QPushButton(prompt_ui_text('fonts.add_selected'))  # noqa: nonconform
        self.removeSelectionButton = QPushButton(prompt_ui_text('fonts.remove_selected'))  # noqa: nonconform
        self.clearSelectionButton = QPushButton(prompt_ui_text('fonts.clear'))  # noqa: nonconform
        self.exportButton = QPushButton(prompt_ui_text('fonts.export'))  # noqa: nonconform
        self.exportNameEdit = QLineEdit('subset_font')  # noqa: nonconform
        self.exportTypeCombo = QComboBox()  # noqa: nonconform
        self.exportTypeCombo.addItems(['TTF', 'OTF', 'WOFF', 'WOFF2'])
        self.builderHintLabel = QLabel(prompt_ui_text('fonts.builder_hint'))  # noqa: nonconform
        self.builderHintLabel.setWordWrap(True)

        self.previewValue = QLabel('')  # noqa: nonconform
        self.previewValue.setAlignment(Qt.AlignCenter)
        self.previewValue.setMinimumHeight(96)
        self.previewValue.setStyleSheet('font-size: 42px; border: 1px solid #444; border-radius: 8px; padding: 12px;')
        self.plainTextEdit = QLineEdit()  # noqa: nonconform
        self.intValueEdit = QLineEdit()  # noqa: nonconform
        self.uplusValueEdit = QLineEdit()  # noqa: nonconform
        self.glyphNameEdit = QLineEdit()  # noqa: nonconform
        self.fontPathEdit = QLineEdit()  # noqa: nonconform
        for widget in (self.plainTextEdit, self.intValueEdit, self.uplusValueEdit, self.glyphNameEdit, self.fontPathEdit):
            widget.setReadOnly(True)

        controls = QHBoxLayout()
        controls.addWidget(self.titleLabel)
        controls.addStretch(1)
        self.bundledFontLabel = QLabel(prompt_ui_text('fonts.bundled_font'))  # noqa: nonconform
        controls.addWidget(self.bundledFontLabel)
        controls.addWidget(self.fontSelector, 1)
        controls.addWidget(self.reloadButton)
        controls.addWidget(self.openButton)

        self.fontLibraryGroup = QGroupBox(prompt_ui_text('fonts.group.bundled'))  # noqa: nonconform
        fontLibraryGroup = self.fontLibraryGroup
        fontLibraryLayout = QVBoxLayout(fontLibraryGroup)
        fontLibraryLayout.addWidget(self.fontList, 1)

        self.glyphGroup = QGroupBox(prompt_ui_text('fonts.group.glyph_atlas'))  # noqa: nonconform
        glyphGroup = self.glyphGroup
        glyphLayout = QVBoxLayout(glyphGroup)
        glyphLayout.addWidget(self.glyphAtlas, 1)

        self.detailsGroup = QGroupBox(prompt_ui_text('fonts.group.selected_glyph'))  # noqa: nonconform
        detailsGroup = self.detailsGroup
        detailsForm = QFormLayout(detailsGroup)
        self.previewFieldLabel = QLabel(prompt_ui_text('fonts.field.preview'))  # noqa: nonconform
        detailsForm.addRow(self.previewFieldLabel, self.previewValue)
        self.plainTextFieldLabel = QLabel(prompt_ui_text('fonts.field.plain_text'))  # noqa: nonconform
        detailsForm.addRow(self.plainTextFieldLabel, self.plainTextEdit)
        self.intValueFieldLabel = QLabel(prompt_ui_text('fonts.field.int_value'))  # noqa: nonconform
        detailsForm.addRow(self.intValueFieldLabel, self.intValueEdit)
        self.uplusCodeFieldLabel = QLabel(prompt_ui_text('fonts.field.uplus_code'))  # noqa: nonconform
        detailsForm.addRow(self.uplusCodeFieldLabel, self.uplusValueEdit)
        self.glyphNameFieldLabel = QLabel(prompt_ui_text('fonts.field.glyph_name'))  # noqa: nonconform
        detailsForm.addRow(self.glyphNameFieldLabel, self.glyphNameEdit)
        self.fontPathFieldLabel = QLabel(prompt_ui_text('fonts.field.font_path'))  # noqa: nonconform
        detailsForm.addRow(self.fontPathFieldLabel, self.fontPathEdit)

        self.builderGroup = QGroupBox(prompt_ui_text('fonts.group.builder'))  # noqa: nonconform
        builderGroup = self.builderGroup
        builderLayout = QVBoxLayout(builderGroup)
        builderLayout.addWidget(self.builderHintLabel)
        builderLayout.addWidget(self.selectionList, 1)
        builderButtons = QHBoxLayout()
        builderButtons.addWidget(self.addCurrentButton)
        builderButtons.addWidget(self.removeSelectionButton)
        builderButtons.addWidget(self.clearSelectionButton)
        builderLayout.addLayout(builderButtons)
        exportForm = QFormLayout()
        self.exportNameLabel = QLabel(prompt_ui_text('fonts.field.name'))  # noqa: nonconform
        exportForm.addRow(self.exportNameLabel, self.exportNameEdit)
        self.exportTypeLabel = QLabel(prompt_ui_text('fonts.field.type'))  # noqa: nonconform
        exportForm.addRow(self.exportTypeLabel, self.exportTypeCombo)
        builderLayout.addLayout(exportForm)
        builderLayout.addWidget(self.exportButton)

        grid = QGridLayout()
        grid.addWidget(fontLibraryGroup, 0, 0)
        grid.addWidget(glyphGroup, 0, 1)
        grid.addWidget(builderGroup, 0, 2, 2, 1)
        grid.addWidget(detailsGroup, 1, 0, 1, 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 4)
        grid.setColumnStretch(2, 2)
        grid.setRowStretch(0, 5)
        grid.setRowStretch(1, 2)

        layout = QVBoxLayout(self)
        layout.addLayout(controls)
        layout.addWidget(self.pathLabel)
        layout.addWidget(self.inventoryLabel)
        layout.addLayout(grid, 1)

        self.glyphAtlas.itemDoubleClicked.connect(lambda *_: self.addCurrentGlyphToSelection())
        self.glyphAtlas.currentItemChanged.connect(lambda *_: self._populateCurrentAtlasGlyph())
        self.selectionList.currentItemChanged.connect(lambda *_: self._populateCurrentBuilderGlyph())
        self.addCurrentButton.clicked.connect(self.addCurrentGlyphToSelection)
        self.removeSelectionButton.clicked.connect(self.removeSelectedBuilderGlyphs)
        self.clearSelectionButton.clicked.connect(self.clearBuilderSelection)
        self.exportButton.clicked.connect(self.exportSelectedGlyphsAsFont)
        self.fontList.currentRowChanged.connect(self._selectBundledFontRow)

    def bundledFontFiles(self) -> list[Path]:
        fonts_root = Path(self.fontsRoot)
        if not fonts_root.exists():
            return []
        candidates: list[Path] = []
        for suffix in ('.ttf', '.otf', '.ttc', '.woff', '.woff2'):
            candidates.extend(fonts_root.glob(f'*{suffix}'))
        return sorted({path.resolve() for path in candidates}, key=lambda value: value.name.lower())

    def reloadBundledFonts(self) -> None:
        current_path = str(self.currentFontPath or EMPTY_STRING)
        self.fontSelector.blockSignals(True)
        self.fontSelector.clear()
        self.fontList.blockSignals(True)
        self.fontList.clear()
        font_files = self.bundledFontFiles()
        for path in font_files:
            self.fontSelector.addItem(path.name, str(path))
            item = QListWidgetItem(path.name)
            item.setData(Qt.UserRole, str(path))
            item.setToolTip(str(path))
            self.fontList.addItem(item)
        self.fontSelector.blockSignals(False)
        self.fontList.blockSignals(False)
        self.inventoryLabel.setText(prompt_ui_text('fonts.inventory', count=len(font_files)))
        if font_files:
            match_index = next((index for index, path in enumerate(font_files) if str(path) == current_path), 0)
            self.fontSelector.setCurrentIndex(match_index)
            self.fontList.setCurrentRow(match_index)
        else:
            self._clearGlyphAtlas(prompt_ui_text('fonts.no_fonts_short'))
            self.pathLabel.setText(prompt_ui_text('fonts.none_found'))

    def _selectBundledFontRow(self, row: int) -> None:
        if row < 0:
            return
        if row != self.fontSelector.currentIndex():
            self.fontSelector.setCurrentIndex(row)

    def _applyPreviewFont(self, font_path: Path) -> None:
        try:
            if int(self.currentApplicationFontId) >= 0:
                QFontDatabase.removeApplicationFont(int(self.currentApplicationFontId))
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@5208')
            print(f"[WARN:swallowed-exception] prompt_app.py:3766 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        self.currentApplicationFontId = -1
        preview_font = QFontDatabase.systemFont(QFontDatabase.GeneralFont)
        preview_font.setPointSize(20)
        try:
            font_id = QFontDatabase.addApplicationFont(str(font_path))
            families = list(QFontDatabase.applicationFontFamilies(font_id) or [])
            if families:
                preview_font = QFont(families[0], 20)
                self.currentApplicationFontId = int(font_id)
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@5220')
            print(f"[WARN:swallowed-exception] prompt_app.py:3777 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            self.currentApplicationFontId = -1
        self.previewValue.setFont(preview_font)
        self.glyphAtlas.setPreviewFont(preview_font)

    def _clearGlyphAtlas(self, message: str = '') -> None:
        self.currentGlyphEntries = []
        self.glyphAtlas.clear()
        self.glyphAtlas.entries = []
        self.glyphAtlas.overlayLabel.hide()
        self.previewValue.setText(message)
        self.plainTextEdit.clear()
        self.intValueEdit.clear()
        self.uplusValueEdit.clear()
        self.glyphNameEdit.clear()
        self.fontPathEdit.clear()

    def loadFont(self, font_path: Path) -> None:
        target = Path(font_path)
        self.currentFontPath = target
        self.pathLabel.setText(str(target))
        self.fontPathEdit.setText(str(target))
        if TTFont is None:
            self._clearGlyphAtlas(prompt_ui_text('fonts.tools_missing_short'))
            self.pathLabel.setText(prompt_ui_text('fonts.missing_tools', target=str(target)))
            return
        try:
            entries = _load_font_glyph_entries(target)
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@5249')
            print(f"[WARN:swallowed-exception] prompt_app.py:3805 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            self._clearGlyphAtlas(prompt_ui_text('fonts.load_failed_short'))
            self.pathLabel.setText(prompt_ui_text('fonts.load_failed', target=str(target), error=str(exc)))
            return
        self.currentGlyphEntries = entries
        self._applyPreviewFont(target)
        self.glyphAtlas.populateGlyphs(entries, target)
        self.inventoryLabel.setText(prompt_ui_text('fonts.loaded', count=len(entries), name=str(target.name)))
        if entries:
            self._populateSelectedGlyphPayload(_font_glyph_payload(entries[0], target))
        else:
            self._clearGlyphAtlas(prompt_ui_text('fonts.no_glyphs_short'))
            self.pathLabel.setText(prompt_ui_text('fonts.no_glyphs', target=str(target)))

    def _populateSelectedGlyphPayload(self, payload: dict[str, Any] | None) -> None:
        if not payload:
            return
        entry = _font_glyph_entry_from_payload(payload)
        plain_text = _font_codepoints_to_plain_text(entry.codepoints)
        self.previewValue.setText(plain_text or entry.glyph_name or '—')
        self.plainTextEdit.setText(plain_text)
        self.intValueEdit.setText(_font_codepoints_to_int_text(entry.codepoints))
        self.uplusValueEdit.setText(_font_codepoints_to_uplus_text(entry.codepoints))
        self.glyphNameEdit.setText(entry.glyph_name)
        self.fontPathEdit.setText(str(payload.get('font_path', EMPTY_STRING) or EMPTY_STRING))

    def _populateCurrentAtlasGlyph(self) -> None:
        row = self.glyphAtlas.currentGlyphRow()
        if row < 0 or row >= len(self.currentGlyphEntries) or self.currentFontPath is None:
            return
        self._populateSelectedGlyphPayload(_font_glyph_payload(self.currentGlyphEntries[row], self.currentFontPath))
        self.glyphAtlas.updateOverlay()

    def _populateCurrentBuilderGlyph(self) -> None:
        item = self.selectionList.currentItem()
        if item is None:
            return
        payload = item.data(Qt.UserRole)
        if isinstance(payload, dict):
            self._populateSelectedGlyphPayload(payload)

    def _selectionPayloads(self) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for index in range(self.selectionList.count()):
            item = self.selectionList.item(index)
            payload = item.data(Qt.UserRole)
            if isinstance(payload, dict):
                payloads.append(payload)
        return payloads

    def addCurrentGlyphToSelection(self) -> None:
        row = self.glyphAtlas.currentGlyphRow()
        if row < 0 or row >= len(self.currentGlyphEntries) or self.currentFontPath is None:
            return
        self.addGlyphPayloadsToSelection([_font_glyph_payload(self.currentGlyphEntries[row], self.currentFontPath)])

    def addGlyphPayloadsToSelection(self, payloads: list[dict[str, Any]]) -> None:
        existing = {_font_glyph_payload_key(payload) for payload in self._selectionPayloads()}
        added_any = False
        for payload in list(payloads or []):
            key = _font_glyph_payload_key(payload)
            if not key or key in existing:
                continue
            item = QListWidgetItem()
            entry = _font_glyph_entry_from_payload(payload)
            plain_text = _font_codepoints_to_plain_text(entry.codepoints)
            label = f'{plain_text or "□"}  {entry.glyph_name or "glyph"}'
            if entry.codepoints:
                label += f'  [{_font_codepoints_to_uplus_text(entry.codepoints)}]'
            item.setText(label)
            item.setData(Qt.UserRole, payload)
            item.setToolTip(str(payload.get('font_path', EMPTY_STRING) or EMPTY_STRING))
            display_font = QFont(self.previewValue.font())
            display_font.setPointSize(max(display_font.pointSize(), 16))
            item.setFont(display_font)
            self.selectionList.addItem(item)
            existing.add(key)
            added_any = True
        if added_any and self.selectionList.currentRow() < 0:
            self.selectionList.setCurrentRow(0)

    def removeSelectedBuilderGlyphs(self) -> None:
        rows = sorted({self.selectionList.row(item) for item in self.selectionList.selectedItems()}, reverse=True)
        for row in rows:
            self.selectionList.takeItem(row)

    def clearBuilderSelection(self) -> None:
        self.selectionList.clear()

    def exportSelectedGlyphsAsFont(self) -> None:
        payloads = self._selectionPayloads()
        if not payloads:
            QMessageBox.warning(self, prompt_ui_text('fonts.export_title'), prompt_ui_text('fonts.export_need_glyph'))
            return
        source_paths = sorted({str(payload.get('font_path', EMPTY_STRING) or EMPTY_STRING).strip() for payload in payloads if str(payload.get('font_path', EMPTY_STRING) or EMPTY_STRING).strip()})
        if len(source_paths) != 1:
            QMessageBox.warning(self, prompt_ui_text('fonts.export_title'), prompt_ui_text('fonts.export_single_source'))
            return
        export_kind = str(self.exportTypeCombo.currentText() or 'TTF').strip().lower()
        suffix = _font_output_kind_extension(export_kind)
        base_name = re.sub(r'[^A-Za-z0-9._-]+', '_', str(self.exportNameEdit.text() or 'subset_font').strip()).strip('._') or 'subset_font'
        default_path = Path(self.fontsRoot) / f'{base_name}{suffix}'
        chosen, _ = QFileDialog.getSaveFileName(self, prompt_ui_text('fonts.export_title'), str(default_path), f'{export_kind.upper()} Files (*{suffix});;All Files (*.*)')
        if not chosen:
            return
        output_path = Path(chosen)
        if output_path.suffix.lower() != suffix:
            output_path = output_path.with_suffix(suffix)
        support_error = _font_export_support_error(export_kind)
        if support_error:
            QMessageBox.warning(self, prompt_ui_text('fonts.export_title'), support_error)
            return
        try:
            _export_font_subset(
                Path(source_paths[0]),
                payloads,
                output_path,
                export_kind,
                family_name=str(self.exportNameEdit.text() or 'subset_font').strip() or output_path.stem,
            )
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@5370')
            QMessageBox.critical(self, prompt_ui_text('fonts.export_title'), prompt_ui_text('fonts.export_failed', error=str(exc)))
            return
        QMessageBox.information(self, prompt_ui_text('fonts.export_title'), prompt_ui_text('fonts.export_created', path=str(output_path)))

    def openFontDialog(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(self, prompt_ui_text('fonts.open_title'), str(self.fontsRoot), FONT_FILENAME_FILTER)
        if chosen:
            self.loadFont(Path(chosen))


class Clipboard:
    """Central Qt clipboard helper used by JS bridge, context menus, and source viewers."""

    @staticmethod
    def copy(text: str, label: str = 'Clipboard', parent: Optional[QWidget] = None) -> bool:
        value = str(text or EMPTY_STRING)
        try:
            QApplication.clipboard().setText(value)
            print(f'[PROMPT:CLIPBOARD:COPY] label={label} chars={len(value)}', file=sys.stderr, flush=True)
            return True
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@5391')
            print(f'[WARN:PROMPT:CLIPBOARD:COPY-FAILED] label={label} {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
            return False


class QtBridge(QObject):
    def __init__(self, window: 'PromptMainWindow') -> None:
        super().__init__()
        self.window = window  # noqa: nonconform

    @Slot(str)
    def saveRuntimeSession(self, payload_json: str) -> None:
        try:
            payload = json.loads(str(payload_json or '{}'))
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@5405')
            print(f"[WARN:swallowed-exception] prompt_app.py:3945 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            payload = {}
        self.window._store_runtime_session_payload(payload)

    @Slot(str)
    def selectWorkflow(self, slug: str) -> None:
        self.window.select_workflow_by_slug(slug)

    @Slot()
    def selectNewPrompt(self) -> None:
        self.window.select_new_prompt()

    @Slot(str)
    def selectPromptFile(self, path: str) -> None:
        self.window.select_prompt_by_path(Path(path))

    @Slot()
    def showPromptHome(self) -> None:
        self.window.show_prompt_home()

    @Slot(str, str, str)
    def savePromptFromWeb(self, title: str, task: str, context: str) -> None:
        self.window.save_prompt_from_web(title, task, context)

    @Slot(str)
    def generatePromptFromWeb(self, payload_json: str) -> None:
        self.window.generate_prompt_from_web(payload_json)

    @Slot(str)
    def copyText(self, text: str) -> None:
        Clipboard.copy(text, 'QtBridge.copyText', self.window)

    @Slot(str)
    def print(self, message: str) -> None:
        sys.stderr.write(f'[QTBRIDGE:PRINT] {message}\n'); sys.stderr.flush()


class EditorPreviewPane(QWidget):
    def __init__(self, title: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.titleLabel = QLabel(f'<b>{title}</b>')  # noqa: nonconform
        self.selector = QComboBox()
        self.selector.setEditable(True)
        self.selector.setInsertPolicy(QComboBox.NoInsert)
        self.saveButton = QPushButton(prompt_ui_text('button.save'))
        self.reloadButton = QPushButton(prompt_ui_text('button.reload'))
        self.infoLabel = QLabel('')
        self.infoLabel.setWordWrap(True)
        controls = QHBoxLayout()
        controls.addWidget(self.titleLabel)
        controls.addStretch(1)
        controls.addWidget(self.selector, 1)
        controls.addWidget(self.saveButton)
        controls.addWidget(self.reloadButton)
        self.editor = QPlainTextEdit()
        self.editor.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.editor.setContextMenuPolicy(Qt.CustomContextMenu)
        self.editor.customContextMenuRequested.connect(self._show_editor_context_menu)
        self.preview = PREVIEW_WIDGET_CLASS(f'{title} Preview')
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.editor)
        splitter.addWidget(self.preview)
        splitter.setSizes([420, 420])
        layout = QVBoxLayout(self)
        layout.addLayout(controls)
        layout.addWidget(self.infoLabel)
        layout.addWidget(splitter)
        self.previewTimer = QTimer(self)
        self.previewTimer.setInterval(250)
        self.previewTimer.setSingleShot(True)

    def _show_editor_context_menu(self, pos: QPoint) -> None:
        menu = self.editor.createStandardContextMenu()
        menu.addSeparator()
        view_source_action = menu.addAction(prompt_ui_text('source.view_source'))
        view_source_action.triggered.connect(lambda: _show_source_view_dialog_modeless(self.editor.toPlainText(), title=f'{self.titleLabel.text().replace("<b>", "").replace("</b>", "")} Source', language_hint='markdown', parent=self))
        _decorate_menu_actions(menu)
        Lifecycle.runQtBlockingCall(menu, self.editor.mapToGlobal(pos), phase_name='prompt.source.context-menu')
        menu.deleteLater()


class PromptPane(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.titleLabel = QLabel('<b>' + prompt_ui_text('pane.prompt') + '</b>')  # noqa: nonconform
        self.promptSelector = QComboBox()
        prompt_selector_font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        self.promptSelector.setFont(prompt_selector_font)
        if self.promptSelector.view() is not None:
            self.promptSelector.view().setFont(prompt_selector_font)
        self.workflowSelector = QComboBox()
        self.workflowSelector.setEditable(True)
        self.workflowSelector.setInsertPolicy(QComboBox.NoInsert)
        self.doctypeSelector = QComboBox()
        self.saveButton = QPushButton(prompt_ui_text('button.save'))
        self.homeButton = QPushButton(prompt_ui_text('button.home'))
        self.web = PREVIEW_WIDGET_CLASS('Prompt Pane')
        controls = QHBoxLayout()
        controls.addWidget(self.titleLabel)
        self.promptSelectorLabel = QLabel(prompt_ui_text('prompt.selector.prompt'))  # noqa: nonconform
        controls.addWidget(self.promptSelectorLabel)
        controls.addWidget(self.promptSelector, 1)
        self.workflowSelectorLabel = QLabel(prompt_ui_text('prompt.selector.workflow'))  # noqa: nonconform
        controls.addWidget(self.workflowSelectorLabel)
        controls.addWidget(self.workflowSelector, 1)
        self.doctypeSelectorLabel = QLabel(prompt_ui_text('prompt.selector.doctype'))  # noqa: nonconform
        controls.addWidget(self.doctypeSelectorLabel)
        controls.addWidget(self.doctypeSelector, 1)
        controls.addWidget(self.saveButton)
        controls.addWidget(self.homeButton)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(controls)
        layout.addWidget(self.web, 1)



class HelpPane(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.helpView = PREVIEW_WIDGET_CLASS(prompt_ui_text('tab.help'))
        self.aboutView = PREVIEW_WIDGET_CLASS(prompt_ui_text('about.title'))
        self.aboutGroup = QGroupBox(prompt_ui_text('about.title'))
        right_group = self.aboutGroup
        right_layout = QVBoxLayout(right_group)
        right_layout.setContentsMargins(8, 8, 8, 8)
        right_layout.addWidget(self.aboutView, 1)
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.helpView)
        splitter.addWidget(right_group)
        splitter.setSizes([3, 2])
        self.mainSplitter = splitter  # noqa: nonconform
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter, 1)


class AboutDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(prompt_ui_text('about.title'))
        self.resize(960, 720)
        self.view = PREVIEW_WIDGET_CLASS(prompt_ui_text('about.title'))
        self.closeButton = QPushButton(prompt_ui_text('button.close'))  # noqa: nonconform
        self.closeButton.clicked.connect(self.close)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.closeButton, 0, alignment=Qt.AlignRight)


class SettingsPane(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.dbHostEdit = QLineEdit()
        self.dbPortEdit = QLineEdit()
        self.dbNameEdit = QLineEdit()
        self.dbUserEdit = QLineEdit()
        self.dbPasswordEdit = QLineEdit()
        try:
            self.dbPasswordEdit.setEchoMode(QLineEdit.Password)
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@5568')
            print(f"[WARN:swallowed-exception] prompt_app.py:4095 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        self.saveDbButton = QPushButton(prompt_ui_text('button.save_db'))
        self.soundEffectsCheckBox = QCheckBox(prompt_ui_text('settings.sound_effects'))
        self.soundEffectsCheckBox.setToolTip(prompt_ui_text('settings.sound_effects_tip'))
        self.soundEffectsCheckBox.setChecked(True)
        self.dbGroup = QGroupBox(prompt_ui_text('group.db_connection'))
        form_group = self.dbGroup
        form = QFormLayout(form_group)
        self.dbHostLabel = QLabel(prompt_ui_text('settings.host'))  # noqa: nonconform
        form.addRow(self.dbHostLabel, self.dbHostEdit)
        self.dbPortLabel = QLabel(prompt_ui_text('settings.port'))  # noqa: nonconform
        form.addRow(self.dbPortLabel, self.dbPortEdit)
        self.dbNameLabel = QLabel(prompt_ui_text('settings.database'))  # noqa: nonconform
        form.addRow(self.dbNameLabel, self.dbNameEdit)
        self.dbUserLabel = QLabel(prompt_ui_text('settings.user'))  # noqa: nonconform
        form.addRow(self.dbUserLabel, self.dbUserEdit)
        self.dbPasswordLabel = QLabel(prompt_ui_text('settings.password'))  # noqa: nonconform
        form.addRow(self.dbPasswordLabel, self.dbPasswordEdit)
        form.addRow('', self.soundEffectsCheckBox)
        form.addRow('', self.saveDbButton)
        layout = QVBoxLayout(self)
        layout.addWidget(form_group)
        layout.addStretch(1)


def render_settings_help_html(root: Path) -> str:
    help_path = Path(root) / 'help' / 'prompt_help.html'
    try:
        if help_path.exists():
            return _read_prompt_text_with_embedded_fallback(help_path, encoding='utf-8')
    except Exception as exc:
        captureException(None, source='prompt_app.py', context='except@5596')
        print(f"[WARN:swallowed-exception] prompt_app.py:4116 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        _warn_log('PROMPT:HELP-LOAD-FAILED', f'{type(exc).__name__}: {exc}')
    return prompt_ui_text('help.full_help_missing')


def parse_prompt_document(text: str, fallback_title: str) -> PromptDocument:
    title = fallback_title
    workflow_slug = ''
    doctype_name = ''
    bucket_name = ''
    task_lines: list[str] = []
    context_lines: list[str] = []
    current = 'context'
    for raw in text.splitlines():
        stripped = raw.strip()
        lowered = stripped.lower()
        if stripped.startswith('# ') and title == fallback_title:
            title = stripped[2:].strip() or fallback_title
            continue
        if lowered.startswith('@workflow:'):
            workflow_slug = stripped.split(':', 1)[1].strip()
            continue
        if lowered.startswith('@doctype:'):
            doctype_name = stripped.split(':', 1)[1].strip()
            continue
        if lowered.startswith('@bucket:') or lowered.startswith('@category:'):
            bucket_name = stripped.split(':', 1)[1].strip()
            continue
        if lowered == '## task':
            current = 'task'
            continue
        if lowered == '## context':
            current = 'context'
            continue
        if current == 'task':
            task_lines.append(raw)
        else:
            context_lines.append(raw)
    task = '\n'.join(task_lines).strip()
    context = '\n'.join(context_lines).strip()
    if not task and not context:
        context = text.strip()
    return PromptDocument(
        title=title,
        task=task,
        context=context,
        workflow_slug=workflow_slug,
        doctype_name=doctype_name,
        bucket_name=bucket_name,
    )


def serialize_prompt_document(doc: PromptDocument) -> str:
    title = doc.title.strip() or 'Untitled Prompt'
    lines = [f'# {title}']
    if doc.workflow_slug.strip():
        lines.append(f'@workflow: {doc.workflow_slug.strip()}')
    if doc.doctype_name.strip():
        lines.append(f'@doctype: {doc.doctype_name.strip()}')
    if doc.bucket_name.strip():
        lines.append(f'@bucket: {doc.bucket_name.strip()}')
    lines.append('')
    lines.append('## Task')
    lines.append(doc.task.strip())
    lines.append('')
    lines.append('## Context')
    lines.append(doc.context.rstrip())
    lines.append('')
    return '\n'.join(lines)


class ApplicationProcessRecord:
    def __init__(self, name: str, kind: str = 'callable', pid: int | None = None, ttl: float = 0.0, handle=None, command: str = EMPTY_STRING, metadata=None):
        self.name = str(name or 'process')
        self.kind = str(kind or 'callable')
        self.pid = int(pid or os.getpid())
        self.ttl = float(ttl or 0.0)
        self.handle = handle
        self.command = str(command or EMPTY_STRING)
        self.metadata = dict(metadata or {}) if isinstance(metadata, dict) else {}
        self.started_at = time.time()
        self.ended_at = 0.0
        self.status = 'pending'
        self.exit_code = None
        self.error_message = EMPTY_STRING
        self.exception_type = EMPTY_STRING
        self.exception_message = EMPTY_STRING
        self.traceback_text = EMPTY_STRING
        self.fault_reason = EMPTY_STRING
        self.db_row_id = 0

    def start(self):
        self.status = 'running'
        self.started_at = time.time()
        if callable(self.handle):
            result = self.handle()
            if hasattr(result, 'pid'):
                self.pid = int(getattr(result, 'pid', 0) or self.pid or os.getpid())
            return result
        if hasattr(self.handle, 'start'):
            result = cast(Any, self.handle).start()
            if hasattr(self.handle, 'pid'):
                self.pid = int(getattr(self.handle, 'pid', 0) or self.pid or os.getpid())
            return result
        return None

    def complete(self, exit_code: int | None = None):
        self.status = 'complete'
        self.exit_code = exit_code
        self.ended_at = time.time()

    def fail(self, message: str = EMPTY_STRING):
        self.status = 'errored'
        self.error_message = str(message or EMPTY_STRING)
        self.ended_at = time.time()

    def exception(self, exc: Exception):
        self.status = 'errored'
        self.exception_type = type(exc).__name__
        self.exception_message = str(exc)
        self.error_message = f'{self.exception_type}: {self.exception_message}'
        self.traceback_text = traceback.format_exc()
        self.ended_at = time.time()

    def fault(self, reason: str):
        self.status = 'errored'
        self.fault_reason = str(reason or 'fault')
        self.error_message = self.fault_reason
        self.ended_at = time.time()

    def snapshot(self) -> dict:
        return {
            'name': self.name,
            'kind': self.kind,
            'pid': int(self.pid or 0),
            'status': self.status,
            'started_at': float(self.started_at or 0.0),
            'ended_at': float(self.ended_at or 0.0),
            'ttl_seconds': float(self.ttl or 0.0),
            'exit_code': self.exit_code,
            'error_message': self.error_message,
            'exception_type': self.exception_type,
            'exception_message': self.exception_message,
            'fault_reason': self.fault_reason,
            'db_row_id': int(self.db_row_id or 0),
        }


class Phase:
    def __init__(self, key: int, name: str, process: ApplicationProcessRecord | None = None, ttl: float = 30.0, onComplete=None, onError=None, onException=None, onFault=None, onTimeout=None, onStart=None, onStop=None, required: bool = True):
        self.key = int(key)
        self.name = str(name or f'Phase {key}')
        self.process = process or ApplicationProcessRecord(self.name, ttl=ttl)
        self.ttl = 30.0 if ttl is None else float(ttl)
        self.status = 'pending'
        self.onComplete = onComplete  # noqa: nonconform
        self.onError = onError  # noqa: nonconform
        self.onException = onException  # noqa: nonconform
        self.onFault = onFault
        self.onTimeout = onTimeout  # noqa: nonconform
        self.onStart = onStart  # noqa: nonconform
        self.onStop = onStop  # noqa: nonconform
        self.required = bool(required)  # noqa: nonconform

    def start(self, controller):
        self.status = 'running'
        self.process.ttl = self.ttl
        self.process.status = 'running'
        controller.activePhases[self.key] = self
        controller.activeProcesses[id(self.process)] = self
        controller.persistProcessStart(self)
        if callable(self.onStart):
            self.onStart(controller, self, None)
        try:
            result = self.process.start()
            controller.persistProcessStatus(self, status='running')
            if self.process.kind in {'thread', 'process', 'qtimer', 'server'}:
                return result
            self.process.complete()
            self.status = 'complete'
            controller.persistProcessStatus(self, status='complete')
            controller.activeProcesses.pop(id(self.process), None)
            if callable(self.onComplete):
                self.onComplete(controller, self, result)
            return result
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@5776')
            self.process.exception(exc)
            self.status = 'errored'
            controller.persistProcessStatus(self, status='errored')
            if callable(self.onException):
                self.onException(controller, self, exc)
            if callable(self.onError):
                self.onError(controller, self, exc)
            else:
                _log_exception(f'PROMPT:LIFECYCLE-PHASE-EXCEPTION:{self.name}', exc, include_traceback=True)
            return None
        finally:
            controller.phaseResults[self.key] = self.status


class ApplicationLifecycleController:
    def __init__(self, app, store: PromptSqlAlchemyStore | None = None):
        self.app = app  # noqa: nonconform
        self.store = store
        self.phases: dict[int, Phase] = {}
        self.activePhases: dict[int, Phase] = {}
        self.activeProcesses: dict[int, Phase] = {}
        self.phaseResults: dict[int, str] = {}
        self.managedTimers: dict[str, QTimer] = {}
        self.managedThreads: dict[str, Any] = {}
        self.supervisionTimer = QTimer(app)  # noqa: nonconform
        self.supervisionTimer.setInterval(1000)
        self.supervisionTimer.timeout.connect(self.supervise)
        self.supervisionTimer.start()

    def registerPhase(self, phase: Phase) -> Phase:
        self.phases[int(phase.key)] = phase
        return phase

    def _defaultPhaseStart(self, controller, phase: Phase, result=None) -> None:
        _trace_event('PROMPT:LIFECYCLE:PHASE-START', f'key={phase.key} name={phase.name}')

    def _defaultPhaseComplete(self, controller, phase: Phase, result=None) -> None:
        _trace_event('PROMPT:LIFECYCLE:PHASE-COMPLETE', f'key={phase.key} name={phase.name} status={phase.status}')

    def _defaultPhaseError(self, controller, phase: Phase, exc=None) -> None:
        if exc is not None:
            captureException(exc, source='prompt_app.py', context=f'lifecycle-phase-error:{phase.name}', handled=True)

    def _defaultPhaseException(self, controller, phase: Phase, exc=None) -> None:
        if exc is not None:
            captureException(exc, source='prompt_app.py', context=f'lifecycle-phase-exception:{phase.name}', handled=True)

    def _defaultPhaseFault(self, controller, phase: Phase, exc=None) -> None:
        if exc is not None:
            if not isinstance(exc, BaseException):
                exc = RuntimeError(str(exc))
            captureException(exc, source='prompt_app.py', context=f'lifecycle-phase-fault:{phase.name}', handled=True)

    def _defaultPhaseTimeout(self, controller, phase: Phase, result=None) -> None:
        captureException(RuntimeError(f'Phase timeout: {phase.name}'), source='prompt_app.py', context=f'lifecycle-phase-timeout:{phase.name}', handled=True)

    def _defaultPhaseStop(self, controller, phase: Phase, result=None) -> None:
        _trace_event('PROMPT:LIFECYCLE:PHASE-STOP', f'key={phase.key} name={phase.name} status={phase.status}')

    def _phaseCallbacks(self, callbacks: dict[str, Any] | None = None) -> dict[str, Any]:
        merged = {
            'onStart': self._defaultPhaseStart,
            'onComplete': self._defaultPhaseComplete,
            'onError': self._defaultPhaseError,
            'onException': self._defaultPhaseException,
            'onFault': self._defaultPhaseFault,
            'onTimeout': self._defaultPhaseTimeout,
            'onStop': self._defaultPhaseStop,
        }
        merged.update(dict(callbacks or {}))
        return merged

    def runPhase(self, key: int):
        phase = self.phases.get(int(key))
        if phase is None:
            raise KeyError(f'Unknown phase {key}')
        return phase.start(self)

    def runRegisteredPhases(self):
        for key in sorted(self.phases):
            self.runPhase(key)

    def registerCallablePhase(self, key: int, name: str, callback, ttl: float = 30.0, **callbacks) -> Phase:
        phase_callbacks = self._phaseCallbacks(callbacks)
        # thread-ok: lifecycle factory registers callable phases with full callbacks.
        return self.registerPhase(Phase(key, name, ApplicationProcessRecord(name, kind='callable', ttl=ttl, handle=callback), ttl=ttl, onStart=phase_callbacks['onStart'], onComplete=phase_callbacks['onComplete'], onError=phase_callbacks['onError'], onException=phase_callbacks['onException'], onFault=phase_callbacks['onFault'], onTimeout=phase_callbacks['onTimeout'], onStop=phase_callbacks['onStop']))

    def registerProcessPhase(self, key: int, name: str, process_handle, ttl: float = 0.0, **callbacks) -> Phase:
        phase_callbacks = self._phaseCallbacks(callbacks)
        return self.registerPhase(Phase(key, name, ApplicationProcessRecord(name, kind='process', ttl=ttl, handle=process_handle), ttl=ttl or 365000000, onStart=phase_callbacks['onStart'], onComplete=phase_callbacks['onComplete'], onError=phase_callbacks['onError'], onException=phase_callbacks['onException'], onFault=phase_callbacks['onFault'], onTimeout=phase_callbacks['onTimeout'], onStop=phase_callbacks['onStop']))

    def registerThreadPhase(self, key: int, name: str, thread, ttl: float = 0.0, **callbacks) -> Phase:
        phase_callbacks = self._phaseCallbacks(callbacks)
        # thread-ok: lifecycle factory registers thread phases with full callbacks.
        return self.registerPhase(Phase(key, name, ApplicationProcessRecord(name, kind='thread', ttl=ttl, handle=thread), ttl=ttl or 365000000, onStart=phase_callbacks['onStart'], onComplete=phase_callbacks['onComplete'], onError=phase_callbacks['onError'], onException=phase_callbacks['onException'], onFault=phase_callbacks['onFault'], onTimeout=phase_callbacks['onTimeout'], onStop=phase_callbacks['onStop']))

    def startTimerPhase(self, key: int, name: str, timer: QTimer, ttl: float = 0.0) -> QTimer:
        phase_callbacks = self._phaseCallbacks({})
        # thread-ok: lifecycle factory registers timer phases with full callbacks.
        phase = Phase(key, name, ApplicationProcessRecord(name, kind='qtimer', ttl=ttl, handle=timer), ttl=ttl or 365000000, onStart=phase_callbacks['onStart'], onComplete=phase_callbacks['onComplete'], onError=phase_callbacks['onError'], onException=phase_callbacks['onException'], onFault=phase_callbacks['onFault'], onTimeout=phase_callbacks['onTimeout'], onStop=phase_callbacks['onStop'])
        self.registerPhase(phase)
        self.managedTimers[name] = timer
        phase.start(self)
        return timer

    def startThreadPhase(self, key: int, name: str, thread, ttl: float = 0.0):
        phase = self.registerThreadPhase(key, name, thread, ttl=ttl)
        self.managedThreads[name] = thread
        phase.start(self)
        return thread

    def createManagedThread(self, name: str, target, daemon: bool = True):
        thread_class = getattr(threading, 'Thread')
        return thread_class(target=target, name=str(name or 'PromptManagedThread'), daemon=bool(daemon))

    def startThreadCallbackPhase(self, key: int, name: str, target, ttl: float = 0.0, daemon: bool = True):
        thread = self.createManagedThread(name=name, target=target, daemon=daemon)
        self.startThreadPhase(key, name, thread, ttl=ttl)
        return thread

    def _completeTimerPhase(self, phase: Phase) -> None:
        try:
            phase.process.complete()
            phase.status = 'complete'
            self.persistProcessStatus(phase, status='complete')
        finally:
            self.activeProcesses.pop(id(phase.process), None)

    def scheduleCallback(self, key: int, name: str, delay_ms: int, callback, ttl: float = 30.0) -> None:
        callback_phase = self.registerCallablePhase(key, name, callback, ttl=ttl)
        timer_name = f'{name} Delay Timer'
        timer_key = int(key) + 500000
        timer = QTimer(self.app)
        timer.setSingleShot(True)
        timer.setInterval(max(0, int(delay_ms or 0)))
        timer_phase_callbacks = self._phaseCallbacks({})
        # thread-ok: lifecycle scheduler registers delayed timer phases with full callbacks.
        timer_phase = Phase(
            timer_key,
            timer_name,
            ApplicationProcessRecord(timer_name, kind='qtimer', ttl=max(float(ttl or 0.0), (max(0, int(delay_ms or 0)) / 1000.0) + 5.0), handle=timer),
            ttl=max(float(ttl or 0.0), (max(0, int(delay_ms or 0)) / 1000.0) + 5.0),
            onStart=timer_phase_callbacks['onStart'],
            onComplete=timer_phase_callbacks['onComplete'],
            onError=timer_phase_callbacks['onError'],
            onException=timer_phase_callbacks['onException'],
            onFault=timer_phase_callbacks['onFault'],
            onTimeout=timer_phase_callbacks['onTimeout'],
            onStop=timer_phase_callbacks['onStop'],
        )
        self.registerPhase(timer_phase)
        self.managedTimers[timer_name] = timer

        def runner():
            try:
                self.runPhase(callback_phase.key)
            finally:
                self._completeTimerPhase(timer_phase)
                try:
                    timer.deleteLater()
                except Exception as error:
                    captureException(None, source='prompt_app.py', context='except@5880')
                    print(f"[WARN:swallowed-exception] prompt_app.py:4399 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    pass

        timer.timeout.connect(runner)
        timer_phase.start(self)

    def persistProcessStart(self, phase: Phase) -> None:
        if self.store is None:
            return
        process = phase.process
        try:
            row_id = self.store.insert_process_record(
                source=Path(__file__).name,
                phase_key=str(phase.key),
                phase_name=str(phase.name),
                process_name=str(process.name),
                kind=str(process.kind),
                pid=int(process.pid or os.getpid()),
                status=str(process.status or 'running'),
                started_at=float(process.started_at or time.time()),
                ttl_seconds=float(process.ttl or phase.ttl or 0.0),
                command=str(process.command or EMPTY_STRING),
                metadata=json.dumps(process.metadata or {}, ensure_ascii=False, default=str),
                processed=0,
            )
            process.db_row_id = int(row_id or 0)
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@5907')
            print(f"[WARN:swallowed-exception] prompt_app.py:4425 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            _log_exception('PROMPT:PROCESS-DB-START-FAILED', exc, include_traceback=False)

    def persistProcessStatus(self, phase: Phase, status: str | None = None) -> None:
        if self.store is None:
            return
        process = phase.process
        if int(process.db_row_id or 0) <= 0:
            return
        try:
            self.store.update_process_record(
                int(process.db_row_id),
                status=str(status or process.status or phase.status),
                pid=int(process.pid or os.getpid()),
                ended_at=float(process.ended_at or 0.0),
                exit_code=int(process.exit_code) if process.exit_code is not None else None,
                error_type=str(process.exception_type or EMPTY_STRING),
                error_message=str(process.error_message or process.exception_message or EMPTY_STRING),
                traceback_text=str(process.traceback_text or EMPTY_STRING),
                fault_reason=str(process.fault_reason or EMPTY_STRING),
                metadata=json.dumps(process.snapshot(), ensure_ascii=False, default=str),
            )
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@5930')
            print(f"[WARN:swallowed-exception] prompt_app.py:4447 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            _log_exception('PROMPT:PROCESS-DB-UPDATE-FAILED', exc, include_traceback=False)

    def killExpiredProcess(self, phase: Phase, reason: str) -> None:
        process = phase.process
        pid = int(process.pid or 0)
        if pid <= 0 or pid == os.getpid():
            return
        try:
            if os.name == 'nt':
                promptLifecycleRunCommand(['taskkill', '/F', '/PID', str(pid)], capture_output=True, text=True, timeout=8, check=False)
            else:
                try:
                    os.kill(pid, 15)
                except Exception as error:
                    captureException(None, source='prompt_app.py', context='except@5945')
                    print(f"[WARN:swallowed-exception] prompt_app.py:4461 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    pass
                time.sleep(0.1)
                try:
                    os.kill(pid, 9)
                except Exception as error:
                    captureException(None, source='prompt_app.py', context='except@5951')
                    print(f"[WARN:swallowed-exception] prompt_app.py:4466 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    pass
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@5954')
            print(f"[WARN:swallowed-exception] prompt_app.py:4468 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            _log_exception(f'PROMPT:LIFECYCLE-KILL-FAILED:{reason}', exc, include_traceback=False)

    def supervise(self):
        now = time.time()
        for process_id, phase in list(self.activeProcesses.items()):
            process = phase.process
            handle = process.handle
            try:
                if process.kind == 'thread' and hasattr(handle, 'is_alive') and not cast(Any, handle).is_alive():
                    process.complete()
                    phase.status = 'complete'
                    self.persistProcessStatus(phase, status='complete')
                    self.activeProcesses.pop(process_id, None)
                    continue
                if process.kind == 'process' and hasattr(handle, 'poll'):
                    code = cast(Any, handle).poll()
                    if code is not None:
                        if int(code or 0) == 0:
                            process.complete(exit_code=int(code or 0))
                            phase.status = 'complete'
                            self.persistProcessStatus(phase, status='complete')
                        else:
                            process.fail(f'exit_code={code}')
                            process.exit_code = int(code or 0)
                            phase.status = 'errored'
                            self.persistProcessStatus(phase, status='errored')
                        self.activeProcesses.pop(process_id, None)
                        continue
                if process.status not in {'running', 'pending'}:
                    self.activeProcesses.pop(process_id, None)
                    continue
                ttl_value = float(process.ttl or phase.ttl or 0.0)
                if ttl_value and ttl_value > 0 and now - float(process.started_at or now) > ttl_value:
                    reason = f'ttl_expired:{phase.key}:{phase.name}'
                    process.fault(reason)
                    phase.status = 'errored'
                    self.phaseResults[phase.key] = 'errored'
                    _warn_log('PROMPT:LIFECYCLE-TTL-FAULT', reason)
                    self.killExpiredProcess(phase, reason)
                    self.persistProcessStatus(phase, status='errored')
                    if callable(phase.onFault):
                        phase.onFault(self, phase, reason)
                    self.activeProcesses.pop(process_id, None)
            except Exception as exc:
                captureException(None, source='prompt_app.py', context='except@5999')
                process.exception(exc)
                phase.status = 'errored'
                self.persistProcessStatus(phase, status='errored')
                _log_exception('PROMPT:LIFECYCLE-SUPERVISE-FAILED', exc, include_traceback=False)
                self.activeProcesses.pop(process_id, None)
        if self.store is not None:
            try:
                for row in self.store.active_process_rows(limit=250):
                    started_at = float(row.get('started_at') or 0.0)
                    ttl_value = float(row.get('ttl_seconds') or 0.0)
                    if started_at and ttl_value and ttl_value > 0 and now - started_at > ttl_value:
                        row_id = int(row.get('id') or 0)
                        reason = f"db_ttl_expired:{row.get('phase_key')}:{row.get('process_name')}"
                        pid = int(row.get('pid') or 0)
                        if pid > 0 and pid != os.getpid():
                            if os.name == 'nt':
                                promptLifecycleRunCommand(['taskkill', '/F', '/PID', str(pid)], capture_output=True, text=True, timeout=8, check=False)
                            else:
                                try:
                                    os.kill(pid, 15)
                                except Exception as error:
                                    captureException(None, source='prompt_app.py', context='except@6020')
                                    print(f"[WARN:swallowed-exception] prompt_app.py:4533 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                                    pass
                        self.store.update_process_record(row_id, status='errored', ended_at=now, error_message=reason, fault_reason=reason, processed=0)
            except Exception as exc:
                captureException(None, source='prompt_app.py', context='except@6024')
                print(f"[WARN:swallowed-exception] prompt_app.py:4536 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                _log_exception('PROMPT:LIFECYCLE-DB-SUPERVISE-FAILED', exc, include_traceback=False)

    def phaseSnapshots(self) -> list[dict]:
        snapshots = []
        for key in sorted(self.phases):
            phase = self.phases[key]
            snapshots.append({
                'key': phase.key,
                'name': phase.name,
                'status': phase.status,
                'process_status': phase.process.status,
                'ttl': phase.ttl,
                'pid': phase.process.pid,
                'db_row_id': int(phase.process.db_row_id or 0),
            })
        return snapshots


class PromptMainWindow(QMainWindow):
    def __init__(self, paths: AppPaths, app_lifecycle: ApplicationLifecycleController | None = None) -> None:
        super().__init__()
        self.paths = paths  # noqa: nonconform
        self.settings = SettingsStore(_settings_db_path(paths.root))
        self.localization = PromptLocalization(PromptLocalization.resolve(paths.root, settings=self.settings, argv=sys.argv))  # noqa: nonconform
        self.soundManager = PromptSoundManager(paths, self.settings)  # noqa: nonconform
        self.AppLifecycle = app_lifecycle or ApplicationLifecycleController(self, store=self.settings.store)  # noqa: nonconform
        if getattr(self.AppLifecycle, 'store', None) is None:
            self.AppLifecycle.store = self.settings.store
        self.AppLifecycle.registerCallablePhase(PHASE_BOOTSTRAP, 'Bootstrap Prompt Main Window', lambda: None, ttl=30)
        self.AppLifecycle.runPhase(PHASE_BOOTSTRAP)
        self.compiler = WorkflowCompiler(paths.root)  # noqa: nonconform
        self.settings.debug_dump('startup-constructor', keys=['ui.window.state', 'ui.window.normal_bounds', 'ui.window.geometry'])
        self.workflowDefinitions: list[WorkflowDefinition] = []
        self.doctypeFiles: list[Path] = []
        self.promptFiles: list[Path] = []
        self.promptLibraryEntries: list[PromptLibraryEntry] = []
        runtime_session = self.settings.get_json('prompt.runtime.session', {})
        self.previousPromptText = str((runtime_session or {}).get('previous_prompt', ''))  # noqa: nonconform
        self.previousPromptTitle = str((runtime_session or {}).get('previous_title', ''))  # noqa: nonconform
        self.promptChainActive = False  # noqa: nonconform
        self.currentPromptPath: Optional[Path] = None
        self.currentPromptTitle = 'New Prompt'  # noqa: nonconform
        self.currentPromptTask = ''  # noqa: nonconform
        self.currentPromptText = ''  # noqa: nonconform
        self.promptHomeActive = True  # noqa: nonconform
        self.isInitializing = False  # noqa: nonconform
        self.hasShownWindowStateFallback = False  # noqa: nonconform
        self.startupWindowStateTarget = 'normal'  # noqa: nonconform
        self.startupWindowStateApplied = False  # noqa: nonconform
        self.closeInProgress = False  # noqa: nonconform
        self.suppressStatePersistence = False  # noqa: nonconform
        self.floatOnTopEnabled = True  # noqa: nonconform
        self.fontsInitialized = False  # noqa: nonconform
        self.settingsHelpInitialized = False  # noqa: nonconform
        self.deferredWorkflowCompileQueued = False  # noqa: nonconform
        self.aboutDialog = None  # noqa: nonconform
        self.setWindowTitle(self._t('app.title'))
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self._apply_initial_window_size()
        if PROMPT_WEBENGINE_FALLBACK_ACTIVE:
            _debug_log('PROMPT:WEBENGINE-FALLBACK', f'active managed_display={TRIO_MANAGED_DISPLAY_ACTIVE} offscreen={TRIO_OFFSCREEN_ACTIVE}')

        self.promptPane = PromptPane()  # noqa: nonconform
        self.doctypePane = EditorPreviewPane(self._t('pane.doctype'))  # noqa: nonconform
        self.workflowPane = EditorPreviewPane(self._t('pane.workflow'))  # noqa: nonconform
        self.fontsPane = FontsPane(self.paths.fonts)  # noqa: nonconform
        self.helpPane = HelpPane()  # noqa: nonconform
        self.settingsPane = SettingsPane()  # noqa: nonconform
        self.settingsPane.soundEffectsCheckBox.setChecked(bool(self.soundManager.enabled))
        self._apply_fonts()
        self._init_web_bridge()
        self.debuggerHook = PromptDebuggerHook(self)  # noqa: nonconform

        prompts_splitter = QSplitter(Qt.Horizontal)
        prompts_splitter.addWidget(self.promptPane)
        prompts_splitter.addWidget(self.workflowPane)
        prompts_splitter.setSizes([1, 1])
        self.mainSplitter = prompts_splitter  # noqa: nonconform
        self.lastVisibleMainSplitterSizes = [1, 1]
        self._equalizingMainSplitter = False

        self.tabWidget = QTabWidget()  # noqa: nonconform
        self.tabWidget.addTab(prompts_splitter, self._t('tab.prompts'))
        self.tabWidget.addTab(self.doctypePane, self._t('tab.doctypes'))
        self.tabWidget.addTab(self.fontsPane, self._t('tab.fonts'))
        self.tabWidget.addTab(self.helpPane, self._t('tab.help'))
        self.tabWidget.addTab(self.settingsPane, self._t('tab.settings'))
        self.tabWidget.setCurrentIndex(0)
        self.setCentralWidget(self.tabWidget)
        self.setStatusBar(QStatusBar())
        self._build_menu()
        self._build_toolbar()
        self._connect_signals()
        self._apply_localized_text()
        self._restore_window_geometry_state()
        self._init_debugger_support()
        try:
            main_sizes = self.settings.get_json('ui.main_splitter.sizes', [])
            if isinstance(main_sizes, list) and main_sizes:
                self.mainSplitter.setSizes([int(v) for v in main_sizes])
            remembered_sizes = self.settings.get_json('ui.main_splitter.last_visible_sizes', [])
            if isinstance(remembered_sizes, list) and remembered_sizes:
                self.lastVisibleMainSplitterSizes = self._normalize_main_splitter_sizes(remembered_sizes)
            else:
                self.lastVisibleMainSplitterSizes = self._normalize_main_splitter_sizes(self.mainSplitter.sizes())  # noqa: nonconform
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@6129')
            print(f"[WARN:swallowed-exception] prompt_app.py:4639 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            _log_exception('PROMPT:SPLITTER-RESTORE-FAILED', exc, include_traceback=False)
        self._apply_view_menu_state(initial=True)
        self.AppLifecycle.registerCallablePhase(PHASE_LOAD_DATA, 'Reload Prompt Data', self.reload_all, ttl=60)
        self.AppLifecycle.runPhase(PHASE_LOAD_DATA)
        self.soundManager.play('startup')

    def _t(self, key: str, **kwargs: Any) -> str:
        return self.localization.t(key, **kwargs)

    def _set_widget_text_safe(self, widget: Any, text: str) -> None:
        try:
            if widget is not None and hasattr(widget, 'setText'):
                widget.setText(text)
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@6143')
            _log_exception('PROMPT:LOCALIZATION-WIDGET-TEXT-FAILED', exc, include_traceback=False)

    def _set_action_text_safe(self, action: Any, text: str) -> None:
        try:
            if action is not None and hasattr(action, 'setText'):
                action.setText(text)
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@6150')
            _log_exception('PROMPT:LOCALIZATION-ACTION-TEXT-FAILED', exc, include_traceback=False)

    def _apply_localized_text(self) -> None:
        self.setWindowTitle(self._t('app.title'))
        try:
            if getattr(self, 'fileMenu', None) is not None:
                self.fileMenu.setTitle(self._t('menu.file'))
            if getattr(self, 'languageMenu', None) is not None:
                self.languageMenu.setTitle(self._t('menu.language'))
            if getattr(self, 'helpMenu', None) is not None:
                self.helpMenu.setTitle(self._t('menu.help'))
            self._sync_language_controls()
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='localization-menu-sync')
            _log_exception('PROMPT:LOCALIZATION-MENU-SYNC-FAILED', exc, include_traceback=False)
        try:
            self.tabWidget.setTabText(0, self._t('tab.prompts'))
            self.tabWidget.setTabText(1, self._t('tab.doctypes'))
            self.tabWidget.setTabText(2, self._t('tab.fonts'))
            self.tabWidget.setTabText(3, self._t('tab.help'))
            self.tabWidget.setTabText(4, self._t('tab.settings'))
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@6161')
            _log_exception('PROMPT:LOCALIZATION-TABS-FAILED', exc, include_traceback=False)
        for attr, key in (
            ('newPromptAction', 'action.new_prompt'),
            ('openPromptAction', 'action.open_prompt'),
            ('savePromptAction', 'action.save_prompt'),
            ('saveWorkflowAction', 'action.save_workflow'),
            ('saveDoctypeAction', 'action.save_doctype'),
            ('openFontAction', 'action.open_font'),
            ('reloadAllAction', 'action.reload_everything'),
            ('aboutFileAction', 'action.about_prompt_ellipsis'),
            ('aboutHelpAction', 'action.about_prompt'),
        ):
            self._set_action_text_safe(getattr(self, attr, None), self._t(key))
        self._set_widget_text_safe(getattr(self, 'languageToolbarLabel', None), self._t('toolbar.language'))
        self._set_widget_text_safe(getattr(self.promptPane, 'titleLabel', None), '<b>' + self._t('pane.prompt') + '</b>')
        self._set_widget_text_safe(getattr(self.promptPane, 'saveButton', None), self._t('button.save'))
        self._set_widget_text_safe(getattr(self.promptPane, 'homeButton', None), self._t('button.home'))
        self._set_widget_text_safe(getattr(self.doctypePane, 'titleLabel', None), '<b>' + self._t('pane.doctype') + '</b>')
        self._set_widget_text_safe(getattr(self.doctypePane, 'saveButton', None), self._t('button.save'))
        self._set_widget_text_safe(getattr(self.doctypePane, 'reloadButton', None), self._t('button.reload'))
        self._set_widget_text_safe(getattr(self.workflowPane, 'titleLabel', None), '<b>' + self._t('pane.workflow') + '</b>')
        self._set_widget_text_safe(getattr(self.workflowPane, 'saveButton', None), self._t('button.save'))
        self._set_widget_text_safe(getattr(self.workflowPane, 'reloadButton', None), self._t('button.reload'))
        self._set_widget_text_safe(getattr(self.fontsPane, 'titleLabel', None), '<b>' + self._t('tab.fonts') + '</b>')
        self._set_widget_text_safe(getattr(self.fontsPane, 'openButton', None), self._t('fonts.open'))
        self._set_widget_text_safe(getattr(self.fontsPane, 'reloadButton', None), self._t('fonts.reload'))
        self._set_widget_text_safe(getattr(self.fontsPane, 'addCurrentButton', None), self._t('fonts.add_selected'))
        self._set_widget_text_safe(getattr(self.fontsPane, 'removeSelectionButton', None), self._t('fonts.remove_selected'))
        self._set_widget_text_safe(getattr(self.fontsPane, 'clearSelectionButton', None), self._t('fonts.clear'))
        self._set_widget_text_safe(getattr(self.fontsPane, 'exportButton', None), self._t('fonts.export'))
        self._set_widget_text_safe(getattr(self.fontsPane, 'builderHintLabel', None), self._t('fonts.builder_hint'))
        self._set_widget_text_safe(getattr(self.fontsPane, 'bundledFontLabel', None), self._t('fonts.bundled_font'))
        for attr, key in (
            ('fontLibraryGroup', 'fonts.group.bundled'),
            ('glyphGroup', 'fonts.group.glyph_atlas'),
            ('detailsGroup', 'fonts.group.selected_glyph'),
            ('builderGroup', 'fonts.group.builder'),
        ):
            try:
                group = getattr(self.fontsPane, attr, None)
                if group is not None and hasattr(group, 'setTitle'):
                    group.setTitle(self._t(key))
            except Exception as exc:
                captureException(None, source='prompt_app.py', context='except@6204')
                _log_exception(f'PROMPT:LOCALIZATION-FONT-GROUP-{attr}-FAILED', exc, include_traceback=False)
        for attr, key in (
            ('previewFieldLabel', 'fonts.field.preview'),
            ('plainTextFieldLabel', 'fonts.field.plain_text'),
            ('intValueFieldLabel', 'fonts.field.int_value'),
            ('uplusCodeFieldLabel', 'fonts.field.uplus_code'),
            ('glyphNameFieldLabel', 'fonts.field.glyph_name'),
            ('fontPathFieldLabel', 'fonts.field.font_path'),
            ('exportNameLabel', 'fonts.field.name'),
            ('exportTypeLabel', 'fonts.field.type'),
        ):
            self._set_widget_text_safe(getattr(self.fontsPane, attr, None), self._t(key))
        self._set_widget_text_safe(getattr(self.promptPane, 'promptSelectorLabel', None), self._t('prompt.selector.prompt'))
        self._set_widget_text_safe(getattr(self.promptPane, 'workflowSelectorLabel', None), self._t('prompt.selector.workflow'))
        self._set_widget_text_safe(getattr(self.promptPane, 'doctypeSelectorLabel', None), self._t('prompt.selector.doctype'))
        self._set_widget_text_safe(getattr(self.settingsPane, 'saveDbButton', None), self._t('button.save_db'))
        self._set_widget_text_safe(getattr(self.settingsPane, 'soundEffectsCheckBox', None), self._t('settings.sound_effects'))
        try:
            self.settingsPane.soundEffectsCheckBox.setToolTip(self._t('settings.sound_effects_tip'))
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='sound-localize-tooltip')
            _log_exception('PROMPT:LOCALIZATION-SOUND-TOOLTIP-FAILED', exc, include_traceback=False)
        try:
            if getattr(self.settingsPane, 'dbGroup', None) is not None:
                self.settingsPane.dbGroup.setTitle(self._t('group.db_connection'))
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@6224')
            _log_exception('PROMPT:LOCALIZATION-SETTINGS-GROUP-FAILED', exc, include_traceback=False)
        self._set_widget_text_safe(getattr(self.settingsPane, 'dbHostLabel', None), self._t('settings.host'))
        self._set_widget_text_safe(getattr(self.settingsPane, 'dbPortLabel', None), self._t('settings.port'))
        self._set_widget_text_safe(getattr(self.settingsPane, 'dbNameLabel', None), self._t('settings.database'))
        self._set_widget_text_safe(getattr(self.settingsPane, 'dbUserLabel', None), self._t('settings.user'))
        self._set_widget_text_safe(getattr(self.settingsPane, 'dbPasswordLabel', None), self._t('settings.password'))
        try:
            if getattr(self.helpPane, 'aboutGroup', None) is not None:
                self.helpPane.aboutGroup.setTitle(self._t('about.title'))
            self._set_widget_text_safe(getattr(self.helpPane, 'closeButton', None), self._t('button.close'))
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@6234')
            _log_exception('PROMPT:LOCALIZATION-HELP-GROUP-FAILED', exc, include_traceback=False)

    def _init_debugger_support(self) -> None:
        self.debuggerHeartbeatTimer = None
        self.debuggerCommandTimer = None
        self.remoteDebugger = None
        if not _debugger_enabled():
            return
        try:
            self.remoteDebugger = PromptRemoteDebuggerProxy(self.debuggerHook)
            if getattr(self.remoteDebugger, 'controlThread', None) is not None:
                self.AppLifecycle.startThreadPhase(PHASE_RUNTIME_THREADS + 1, 'Remote Debugger Control Thread', self.remoteDebugger.controlThread)
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@6247')
            print(f"[WARN:swallowed-exception] prompt_app.py:4655 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            _log_exception('PROMPT:DEBUGGER-REMOTE-INIT-FAILED', exc, include_traceback=False)
        try:
            _write_debugger_heartbeat(event_kind='process', reason='START', caller='PromptMainWindow', phase='STARTUP', var_dump=self.debuggerHook.buildLiveVarDump('START'), process_snapshot=self.debuggerHook.buildLiveProcessSnapshot('START'))
            if self.remoteDebugger is not None:
                self.remoteDebugger.emitProcess('START')
                self.remoteDebugger.emitHeartbeat('INIT', 'PromptMainWindow')
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@6255')
            print(f"[WARN:swallowed-exception] prompt_app.py:4662 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            _log_exception('PROMPT:DEBUGGER-STARTUP-HEARTBEAT-FAILED', exc, include_traceback=False)
        try:
            self.debuggerHeartbeatTimer = QTimer(self)
            self.debuggerHeartbeatTimer.setInterval(1000)
            self.debuggerHeartbeatTimer.timeout.connect(self._emit_debugger_heartbeat)
            self.AppLifecycle.startTimerPhase(PHASE_RUNTIME_TIMERS + 1, 'Debugger Heartbeat Timer', self.debuggerHeartbeatTimer)
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@6263')
            print(f"[WARN:swallowed-exception] prompt_app.py:4669 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            _log_exception('PROMPT:DEBUGGER-TIMER-FAILED', exc, include_traceback=False)
        try:
            self.debuggerCommandTimer = QTimer(self)
            self.debuggerCommandTimer.setInterval(1000)
            self.debuggerCommandTimer.timeout.connect(self._poll_debugger_commands)
            self.AppLifecycle.startTimerPhase(PHASE_RUNTIME_TIMERS + 2, 'Debugger Command Timer', self.debuggerCommandTimer)
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@6271')
            print(f"[WARN:swallowed-exception] prompt_app.py:4676 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            _log_exception('PROMPT:DEBUGGER-COMMAND-TIMER-FAILED', exc, include_traceback=False)

    @Slot()
    def _emit_debugger_heartbeat(self) -> None:
        try:
            payload = _debugger_process_snapshot_payload(self)
            _write_debugger_heartbeat(event_kind='heartbeat', reason='UI', caller='PromptMainWindow', phase='RUNTIME', var_dump=self.debuggerHook.buildLiveVarDump('UI'), process_snapshot=json.dumps(payload, ensure_ascii=False, default=str))
            if self.remoteDebugger is not None:
                self.remoteDebugger.emitHeartbeat('UI', 'PromptMainWindow')
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@6282')
            print(f"[WARN:swallowed-exception] prompt_app.py:4686 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            _log_exception('PROMPT:DEBUGGER-HEARTBEAT-FAILED', exc, include_traceback=False)

    @Slot()
    def _poll_debugger_commands(self) -> None:
        try:
            self.debuggerHook.pollDebuggerHeartbeatCommands()
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@6290')
            print(f"[WARN:swallowed-exception] prompt_app.py:4693 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            _log_exception('PROMPT:DEBUGGER-POLL-TIMER-FAILED', exc, include_traceback=False)

    def _apply_fonts(self) -> None:
        mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        mono.setPointSize(10)
        self.doctypePane.editor.setFont(mono)
        self.workflowPane.editor.setFont(mono)

    def _init_web_bridge(self) -> None:
        self.channel = None
        self.bridge = QtBridge(self)
        if PROMPT_WEBENGINE_FALLBACK_ACTIVE:
            _debug_log('PROMPT:WEBENGINE-FALLBACK', 'bridge disabled for managed/offscreen rich-text preview mode')
            return
        self.channel = QWebChannel(self.promptPane.web.page())
        self.channel.registerObject('qtBridge', self.bridge)
        self.promptPane.web.page().setWebChannel(self.channel)

    def _trace_menu_structure(self, reason: str) -> None:
        try:
            menu_bar = self.menuBar()
            top_level_titles = [str(action.text() or EMPTY_STRING) for action in list(menu_bar.actions() or [])]
            file_titles = []
            file_view_titles = []
            top_view_titles = []
            file_menu = getattr(self, 'fileMenu', None)
            if file_menu is not None:
                file_titles = [str(action.text() or EMPTY_STRING) for action in list(file_menu.actions() or [])]
            file_view_menu = getattr(self, 'fileViewMenu', None)
            if file_view_menu is not None:
                file_view_titles = [str(action.text() or EMPTY_STRING) for action in list(file_view_menu.actions() or [])]
            top_view_menu = getattr(self, 'topViewMenu', None)
            if top_view_menu is not None:
                top_view_titles = [str(action.text() or EMPTY_STRING) for action in list(top_view_menu.actions() or [])]
            language_titles = []
            language_menu = getattr(self, 'languageMenu', None)
            if language_menu is not None:
                language_titles = [str(action.text() or EMPTY_STRING) for action in list(language_menu.actions() or [])]
            help_titles = []
            help_menu = getattr(self, 'helpMenu', None)
            if help_menu is not None:
                help_titles = [str(action.text() or EMPTY_STRING) for action in list(help_menu.actions() or [])]
            _debug_log('PROMPT:MENU-TRACE', f'reason={reason} top={top_level_titles} file={file_titles} file_view={file_view_titles} top_view={top_view_titles} language={language_titles} help={help_titles}')
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@6331')
            print(f"[WARN:swallowed-exception] prompt_app.py:4729 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            _log_exception('PROMPT:MENU-TRACE-FAILED', exc, include_traceback=False)

    def _build_menu(self) -> None:
        menu_bar = self.menuBar()
        menu_bar.clear()
        _debug_log('PROMPT:MENU-TRACE', 'build_menu begin')
        file_menu = menu_bar.addMenu(self._t('menu.file'))
        self.fileMenu = file_menu
        self.openPromptAction = QAction(self._t('action.open_prompt'), self)
        self.openPromptAction.triggered.connect(self.open_prompt_file)
        file_menu.addAction(self.openPromptAction)
        self.savePromptAction = QAction(self._t('action.save_prompt'), self)
        self.savePromptAction.triggered.connect(self.save_prompt)
        file_menu.addAction(self.savePromptAction)
        self.saveWorkflowAction = QAction(self._t('action.save_workflow'), self)
        self.saveWorkflowAction.triggered.connect(self.save_workflow)
        file_menu.addAction(self.saveWorkflowAction)
        self.saveDoctypeAction = QAction(self._t('action.save_doctype'), self)
        self.saveDoctypeAction.triggered.connect(self.save_doctype)
        file_menu.addAction(self.saveDoctypeAction)
        self.openFontAction = QAction(self._t('action.open_font'), self)
        self.openFontAction.triggered.connect(self.open_font_file)
        file_menu.addAction(self.openFontAction)
        file_menu.addSeparator()
        self.reloadAllAction = QAction(self._t('action.reload_everything'), self)
        self.reloadAllAction.triggered.connect(self.reload_all)
        file_menu.addAction(self.reloadAllAction)
        file_menu.addSeparator()
        self.aboutFileAction = QAction(self._t('action.about_prompt_ellipsis'), self)
        self.aboutFileAction.triggered.connect(self.show_about_dialog)
        file_menu.addAction(self.aboutFileAction)

        language_menu = menu_bar.addMenu(self._t('menu.language'))
        self.languageMenu = language_menu
        self.languageActionGroup = QActionGroup(self)
        self.languageActionGroup.setExclusive(True)
        self.languageActions = {}
        for language_enum, label in LANGUAGE_PICKER_ORDER:
            language_code = PromptLocalization.normalize(language_enum)
            flag_key = PROMPT_LANGUAGE_FLAG_KEYS.get(PromptLocalization.language_enum(language_enum), language_code.lower())
            action = QAction(_language_flag_icon(flag_key), label, self)
            action.setCheckable(True)
            action.setData(language_code)
            action.triggered.connect(lambda checked=False, code=language_code: self._save_selected_language(code))
            self.languageActionGroup.addAction(action)
            language_menu.addAction(action)
            self.languageActions[language_code] = action
        self._sync_language_controls()

        help_menu = menu_bar.addMenu(self._t('menu.help'))
        self.helpMenu = help_menu
        self.aboutHelpAction = QAction(self._t('action.about_prompt'), self)
        self.aboutHelpAction.triggered.connect(self.show_about_dialog)
        help_menu.addAction(self.aboutHelpAction)
        self._trace_menu_structure('build_menu end')


    def _normalize_main_splitter_sizes(self, sizes) -> list[int]:
        try:
            values = [max(0, int(v)) for v in list(sizes or [])[:2]]
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@6376')
            print(f"[WARN:swallowed-exception] prompt_app.py:4772 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            values = []
        while len(values) < 2:
            values.append(0)
        defaults = [1, 1]
        normalized = []
        for index, value in enumerate(values[:2]):
            normalized.append(value if value > 0 else defaults[index])
        if sum(normalized) <= 0:
            return list(defaults)
        return normalized

    def _remember_visible_main_splitter_sizes(self) -> None:
        splitter = getattr(self, 'mainSplitter', None)
        if splitter is None:
            return
        try:
            sizes = [int(v) for v in splitter.sizes()]
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@6395')
            print(f"[WARN:swallowed-exception] prompt_app.py:4790 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return
        if len(sizes) < 2:
            return
        self.lastVisibleMainSplitterSizes = self._normalize_main_splitter_sizes(sizes)
        self.settings.set_json('ui.main_splitter.sizes', self.lastVisibleMainSplitterSizes)
        self.settings.set_json('ui.main_splitter.last_visible_sizes', self.lastVisibleMainSplitterSizes)


    def _schedule_lifecycle_callback(self, key: int, name: str, delay_ms: int, callback, ttl: float = 30.0) -> None:
        lifecycle = getattr(self, 'AppLifecycle', None)
        if lifecycle is not None and hasattr(lifecycle, 'scheduleCallback'):
            lifecycle.scheduleCallback(key, name, delay_ms, callback, ttl=ttl)
            return
        if int(delay_ms or 0) <= 0:
            callback()
            return
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(max(0, int(delay_ms or 0)))
        timer.timeout.connect(callback)
        if not hasattr(self, '_fallbackLifecycleTimers'):
            self._fallbackLifecycleTimers = []
        self._fallbackLifecycleTimers.append(timer)
        timer.start()

    def _apply_view_menu_state(self, initial: bool = False) -> None:
        splitter = getattr(self, 'mainSplitter', None)
        if splitter is None:
            return
        if not initial:
            self._remember_visible_main_splitter_sizes()
        self._schedule_lifecycle_callback(PHASE_RUNTIME_TIMERS + 10, 'Apply View Menu Splitter Sizes', 0, lambda: splitter.setSizes([1, 1]), ttl=5)

    def _preferred_initial_window_size(self) -> tuple[int, int]:
        fallback_width = 1024
        fallback_height = 900
        try:
            screen = QApplication.primaryScreen()
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@6435')
            print(f"[WARN:swallowed-exception] prompt_app.py:4831 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            screen = None
        if screen is None:
            return (fallback_width, fallback_height)
        try:
            geometry = screen.availableGeometry()
            width = int(getattr(geometry, 'width', lambda: fallback_width)())
            height = int(getattr(geometry, 'height', lambda: fallback_height)())
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@6444')
            print(f"[WARN:swallowed-exception] prompt_app.py:4839 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return (fallback_width, fallback_height)
        width = max(900, width - 40) if width > 0 else fallback_width
        height = max(700, height - 40) if height > 0 else fallback_height
        return (width or fallback_width, height or fallback_height)

    def _apply_initial_window_size(self) -> None:
        width, height = self._preferred_initial_window_size()
        self.resize(width, height)

    def _enforce_float_window(self) -> None:
        if not bool(getattr(self, 'floatOnTopEnabled', False)):
            return
        if self.isMinimized():
            return
        modal = QApplication.activeModalWidget()
        if modal is not None and modal is not self:
            return
        try:
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            self.show()
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@6466')
            print(f"[WARN:swallowed-exception] prompt_app.py:4860 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass
        try:
            self.raise_()
            self.activateWindow()
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@6472')
            print(f"[WARN:swallowed-exception] prompt_app.py:4865 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass

    def _maintain_even_main_splitter(self) -> None:
        splitter = getattr(self, 'mainSplitter', None)
        if splitter is None:
            return
        if bool(getattr(self, '_equalizingMainSplitter', False)):
            return
        self._equalizingMainSplitter = True
        try:
            splitter.setSizes([1, 1])
            self.lastVisibleMainSplitterSizes = [1, 1]
        finally:
            self._equalizingMainSplitter = False

    def _build_toolbar(self) -> None:

        bar = QToolBar(self._t('toolbar.main'))
        bar.setMovable(False)
        self.addToolBar(bar)
        self.newPromptAction = QAction(self._t('action.new_prompt'), self)
        self.newPromptAction.triggered.connect(self.select_new_prompt)
        bar.addAction(self.newPromptAction)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        bar.addWidget(spacer)
        self.languageToolbarLabel = QLabel(self._t('toolbar.language'))
        bar.addWidget(self.languageToolbarLabel)
        self.languageCombo = QComboBox()
        for language_enum, label in LANGUAGE_PICKER_ORDER:
            language_code = PromptLocalization.normalize(language_enum)
            flag_key = PROMPT_LANGUAGE_FLAG_KEYS.get(PromptLocalization.language_enum(language_enum), language_code.lower())
            self.languageCombo.addItem(_language_flag_icon(flag_key), label, language_code)
        self.languageCombo.setCurrentIndex(0)
        stored_language = self.localization.language
        for idx in range(self.languageCombo.count()):
            if str(self.languageCombo.itemData(idx) or '').upper() == stored_language:
                self.languageCombo.setCurrentIndex(idx)
                break
        bar.addWidget(self.languageCombo)
        self._sync_language_controls()

    def _sync_language_controls(self) -> None:
        current_language = PromptLocalization.normalize(getattr(self.localization, 'language', PromptLocalization.ENGLISH))
        combo = getattr(self, 'languageCombo', None)
        if combo is not None:
            try:
                was_blocked = combo.blockSignals(True)
                for idx in range(combo.count()):
                    code = PromptLocalization.normalize(combo.itemData(idx) or PromptLocalization.ENGLISH)
                    if code == current_language:
                        combo.setCurrentIndex(idx)
                        break
                combo.blockSignals(was_blocked)
            except Exception as exc:
                captureException(None, source='prompt_app.py', context='language-combo-sync')
                _log_exception('PROMPT:LANGUAGE-COMBO-SYNC-FAILED', exc, include_traceback=False)
        for code, action in dict(getattr(self, 'languageActions', {}) or {}).items():
            try:
                action.setChecked(PromptLocalization.normalize(code) == current_language)
            except Exception as exc:
                captureException(None, source='prompt_app.py', context='language-action-sync')
                _log_exception('PROMPT:LANGUAGE-ACTION-SYNC-FAILED', exc, include_traceback=False)

    def _save_selected_language(self, language_code: Any = None) -> None:
        try:
            # Qt's QComboBox.currentIndexChanged signal passes an integer index.
            # Treat integers as combo indices, not language enum values, otherwise
            # index 1/2/3 normalizes back to English and the Language menu appears
            # broken. Menu actions pass explicit language codes such as 'ES'.
            if language_code is None or isinstance(language_code, bool):
                key = PromptLocalization.normalize(self.languageCombo.currentData() or PromptLocalization.ENGLISH)
            elif isinstance(language_code, int):
                combo = getattr(self, 'languageCombo', None)
                if combo is not None and 0 <= int(language_code) < combo.count():
                    key = PromptLocalization.normalize(combo.itemData(int(language_code)) or PromptLocalization.ENGLISH)
                else:
                    key = PromptLocalization.normalize(self.languageCombo.currentData() or PromptLocalization.ENGLISH)
            else:
                key = PromptLocalization.normalize(language_code)
            self.localization.set_language(key)
            self.settings.set_value('ui.language', key)
            PromptLocalization.save_config_language(self.paths.root, key)
            os.environ['PROMPT_LANGUAGE'] = key
            self._sync_language_controls()
            self._apply_localized_text()
            self._refresh_about_views()
            try:
                self.update_settings_help()
                self.render_prompt_runtime()
            except Exception as refresh_exc:
                captureException(None, source='prompt_app.py', context='except@6527')
                _log_exception('PROMPT:LOCALIZATION-RUNTIME-REFRESH-FAILED', refresh_exc, include_traceback=False)
            selected_name = PROMPT_LANGUAGE_NAMES.get(PromptLocalization.language_enum(key), key)
            self.statusBar().showMessage(self._t('status.language_set', language=selected_name), 2000)
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@6530')
            print(f"[WARN:swallowed-exception] prompt_app.py:4915 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            _log_exception('PROMPT:SAVE-LANGUAGE-FAILED', exc, include_traceback=False)

    def _connect_signals(self) -> None:
        self.mainSplitter.splitterMoved.connect(lambda *_: self._remember_visible_main_splitter_sizes())
        self.tabWidget.currentChanged.connect(self._on_tab_changed)
        self.tabWidget.currentChanged.connect(lambda *_: self._schedule_lifecycle_callback(PHASE_RUNTIME_TIMERS + 11, 'Maintain Splitter After Tab Change', 0, self._maintain_even_main_splitter, ttl=5))
        self.promptPane.saveButton.clicked.connect(self.save_prompt)
        self.promptPane.homeButton.clicked.connect(lambda: self._capture_runtime_session_then(self.show_prompt_home))
        self.promptPane.promptSelector.activated.connect(lambda *_: self._user_selected_prompt())
        self.promptPane.workflowSelector.activated.connect(lambda *_: self._user_changed_workflow_from_prompt())
        self.promptPane.doctypeSelector.activated.connect(lambda *_: self._user_changed_doctype_from_prompt())

        self.doctypePane.selector.currentIndexChanged.connect(self._sync_doctype_selection_from_editor)
        self.doctypePane.saveButton.clicked.connect(self.save_doctype)
        self.doctypePane.reloadButton.clicked.connect(self.load_selected_doctype)
        self.doctypePane.editor.textChanged.connect(self.doctypePane.previewTimer.start)
        self.doctypePane.previewTimer.timeout.connect(self.update_doctype_preview)

        self.workflowPane.selector.currentIndexChanged.connect(self._sync_workflow_selection_from_editor)
        self.workflowPane.saveButton.clicked.connect(self.save_workflow)
        self.workflowPane.reloadButton.clicked.connect(self.load_selected_workflow)
        self.workflowPane.editor.textChanged.connect(self.workflowPane.previewTimer.start)
        self.workflowPane.previewTimer.timeout.connect(self.update_workflow_preview)
        self.fontsPane.openButton.clicked.connect(self.open_font_file)
        self.fontsPane.reloadButton.clicked.connect(self.reload_fonts_tab)
        self.fontsPane.fontSelector.currentIndexChanged.connect(self._load_selected_bundled_font)
        self.settingsPane.saveDbButton.clicked.connect(self.save_db_settings)
        self.settingsPane.soundEffectsCheckBox.stateChanged.connect(self._sound_effects_toggled)
        self.languageCombo.currentIndexChanged.connect(self._save_selected_language)


    def _sound_effects_toggled(self, state: int) -> None:
        enabled = bool(int(state) != 0)
        self.soundManager.set_enabled(enabled)
        self.soundManager.play('enable' if enabled else 'disable', force=True)
        self.statusBar().showMessage(self._t('status.sounds_enabled' if enabled else 'status.sounds_disabled'), 2000)

    def _on_tab_changed(self, index: int) -> None:
        if int(index) == 2:
            self.ensure_fonts_tab_ready()
        elif int(index) == 3:
            self.ensure_settings_help_ready()

    def ensure_fonts_tab_ready(self) -> None:
        if self.fontsInitialized:
            return
        self.reload_fonts_tab()
        self.fontsInitialized = True

    def ensure_settings_help_ready(self) -> None:
        if self.settingsHelpInitialized:
            return
        self.update_settings_help()
        self.settingsHelpInitialized = True

    def _queue_deferred_workflow_compile(self) -> None:
        if self.deferredWorkflowCompileQueued:
            return
        self.deferredWorkflowCompileQueued = True

        def _run() -> None:
            self.deferredWorkflowCompileQueued = False
            try:
                compile_all_workflows(self.paths.workflows, self.compiler)
                self.statusBar().showMessage(self._t('status.compiled_cache'), 3000)
            except Exception as exc:
                captureException(None, source='prompt_app.py', context='except@6590')
                print(f"[WARN:swallowed-exception] prompt_app.py:4974 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                _log_exception('PROMPT:DEFERRED-COMPILE-FAILED', exc, include_traceback=False)

        self._schedule_lifecycle_callback(PHASE_RUNTIME_TIMERS + 12, 'Deferred Workflow Cache Compile', 250, _run, ttl=120)

    def _rect_to_tuple(self, rect) -> tuple[int, int, int, int]:
        return rect.x(), rect.y(), rect.width(), rect.height()

    def _current_window_state_name(self) -> str:
        if self.isFullScreen():
            return 'fullscreen'
        if self.isMaximized():
            return 'maximized'
        if self.isMinimized():
            return 'minimized'
        return 'normal'

    def _debug_window_state(self, stage: str) -> None:
        saved_state = self.settings.get_value('ui.window.state', 'normal')
        saved_bounds = self.settings.get_json('ui.window.normal_bounds', {})
        saved_geometry = self.settings.get_value('ui.window.geometry', '')
        _debug_log(
            'PROMPT:WINDOW-STATE',
            f'stage={stage} saved_state={saved_state!r} saved_bounds={saved_bounds!r} saved_geometry_len={len(saved_geometry)} '
            f'current_state={self._current_window_state_name()!r} visible={self.isVisible()} maximized={self.isMaximized()} '
            f'fullscreen={self.isFullScreen()} minimized={self.isMinimized()} geometry={self._rect_to_tuple(self.geometry())} '
            f'normalGeometry={self._rect_to_tuple(self.normalGeometry())}',
        )

    def _restore_window_geometry_state(self) -> None:
        self.suppressStatePersistence = True
        self._debug_window_state('startup-before-restore')
        self.settings.debug_dump('startup-restore-window-state', keys=['ui.window.state', 'ui.window.normal_bounds', 'ui.window.geometry'])
        saved_state = str(self.settings.get_value('ui.window.state', 'normal') or 'normal').strip().lower() or 'normal'
        if saved_state not in {'normal', 'maximized', 'fullscreen', 'minimized'}:
            saved_state = 'normal'
        self.startupWindowStateTarget = saved_state
        geometry_hex = self.settings.get_value('ui.window.geometry', '')
        restored = False
        if geometry_hex:
            try:
                restored = bool(self.restoreGeometry(QByteArray.fromHex(geometry_hex.encode('ascii'))))
                _debug_log('PROMPT:WINDOW-RESTORE', f'geometry restored={restored} len={len(geometry_hex)} state={saved_state}')
            except Exception as exc:
                captureException(None, source='prompt_app.py', context='except@6634')
                print(f"[WARN:swallowed-exception] prompt_app.py:5017 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                _log_exception('PROMPT:WINDOW-RESTORE-FAILED', exc, include_traceback=False)
        bounds = self.settings.get_json('ui.window.normal_bounds', {})
        if (not restored) and isinstance(bounds, dict) and bounds:
            try:
                x = int(bounds.get('x', 50))
                y = int(bounds.get('y', 50))
                w = int(bounds.get('width', 1850))
                h = int(bounds.get('height', 1020))
                self.setGeometry(x, y, w, h)
                _debug_log('PROMPT:WINDOW-RESTORE', f'normal-bounds=({x},{y},{w},{h}) state={saved_state}')
            except Exception as exc:
                captureException(None, source='prompt_app.py', context='except@6646')
                print(f"[WARN:swallowed-exception] prompt_app.py:5028 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                _log_exception('PROMPT:WINDOW-BOUNDS-RESTORE-FAILED', exc, include_traceback=False)
        if saved_state == 'maximized':
            try:
                self.setWindowState((self.windowState() & ~Qt.WindowFullScreen & ~Qt.WindowMinimized) | Qt.WindowMaximized)
                _debug_log('PROMPT:WINDOW-RESTORE', 'pre-show setWindowState(maximized) issued')
            except Exception as exc:
                captureException(None, source='prompt_app.py', context='except@6653')
                print(f"[WARN:swallowed-exception] prompt_app.py:5034 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                _log_exception('PROMPT:WINDOW-PRESHOW-MAXIMIZE-FAILED', exc, include_traceback=False)
        elif saved_state == 'fullscreen':
            try:
                self.setWindowState((self.windowState() & ~Qt.WindowMaximized & ~Qt.WindowMinimized) | Qt.WindowFullScreen)
                _debug_log('PROMPT:WINDOW-RESTORE', 'pre-show setWindowState(fullscreen) issued')
            except Exception as exc:
                captureException(None, source='prompt_app.py', context='except@6660')
                print(f"[WARN:swallowed-exception] prompt_app.py:5040 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                _log_exception('PROMPT:WINDOW-PRESHOW-FULLSCREEN-FAILED', exc, include_traceback=False)
        self._debug_window_state('startup-after-restore-geometry')

    def _save_window_geometry_state(self) -> None:
        bounds_rect = self.normalGeometry() if (self.isMaximized() or self.isFullScreen()) else self.geometry()
        bounds = {
            'x': bounds_rect.x(),
            'y': bounds_rect.y(),
            'width': bounds_rect.width(),
            'height': bounds_rect.height(),
        }
        state_name = self._current_window_state_name()
        geometry_hex = bytes(self.saveGeometry().toHex()).decode('ascii')
        self.settings.set_json('ui.window.normal_bounds', bounds)
        self.settings.set_value('ui.window.state', state_name)
        self.settings.set_value('ui.window.geometry', geometry_hex)
        _debug_log('PROMPT:WINDOW-SAVE-GEOMETRY', f'geometry_len={len(geometry_hex)} state={state_name}')

    def reload_all(self) -> None:
        self.isInitializing = True
        self._ensure_dirs()
        self.workflowDefinitions, self.doctypeFiles, self.promptFiles = _ensure_starter_content(self.paths)
        self.workflowDefinitions = discover_workflows(self.paths.workflows)
        self._cleanup_runtime_prompt_artifacts()
        self.doctypeFiles = discover_doctypes(self.paths.doctypes)
        self.promptFiles = discover_prompts(self.paths.prompts)

        startupPromptPath = self.promptFiles[0] if self.promptFiles else None
        startupPromptDocument = None
        if startupPromptPath is not None:
            try:
                startupPromptDocument = parse_prompt_document(_read_prompt_text_with_embedded_fallback(startupPromptPath, encoding='utf-8'), re.sub(r'(?i)\.prompt$', '', startupPromptPath.stem))
            except OSError:
                captureException(None, source='prompt_app.py', context='except@6694')
                startupPromptPath = None
                startupPromptDocument = None

        self.currentPromptPath = startupPromptPath
        if startupPromptDocument is not None:
            self.currentPromptTitle = startupPromptDocument.title
            self.currentPromptTask = startupPromptDocument.task
            self.currentPromptText = startupPromptDocument.context
        else:
            self.currentPromptTitle = 'New Prompt'
            self.currentPromptTask = ''
            self.currentPromptText = ''

        self._reload_workflow_selectors()
        self._reload_doctype_selectors()
        self._load_db_settings_fields()
        self.fontsInitialized = False
        self.settingsHelpInitialized = False

        if startupPromptDocument is not None:
            self._select_workflow_slug(startupPromptDocument.workflow_slug)
            self._select_doctype_name(startupPromptDocument.doctype_name)

        self._reload_prompt_selector()
        self.load_selected_workflow()
        self.load_selected_doctype()

        if self.currentPromptPath is not None:
            self.promptHomeActive = False
            print(f'[PROMPT:STARTUP-VIEW] runtime prompt={self.currentPromptPath}', file=sys.stderr)
            self.render_prompt_runtime()
        else:
            self.promptHomeActive = True
            print('[PROMPT:STARTUP-VIEW] home index', file=sys.stderr)
            self.show_prompt_home()

        self.isInitializing = False
        self._queue_deferred_workflow_compile()
        self.statusBar().showMessage(self._t('status.reloaded'), 3000)

    def _ensure_dirs(self) -> None:
        self.paths.workflows.mkdir(parents=True, exist_ok=True)
        self.paths.doctypes.mkdir(parents=True, exist_ok=True)
        self.paths.prompts.mkdir(parents=True, exist_ok=True)
        self.paths.generated.mkdir(parents=True, exist_ok=True)
        self.paths.fonts.mkdir(parents=True, exist_ok=True)
        (self.paths.root / 'workspaces').mkdir(parents=True, exist_ok=True)

    def _cleanup_runtime_prompt_artifacts(self) -> None:
        runtime_files = [self.paths.generated / 'current_prompt.html']
        for workflow in self.workflowDefinitions:
            runtime_files.append(workflow.folder / '_runtime_prompt.html')
        for runtime_file in runtime_files:
            if runtime_file.exists():
                try:
                    runtime_file.unlink()
                except OSError:
                    captureException(None, source='prompt_app.py', context='except@6751')
                    print("[WARN:swallowed-exception] prompt_app.py:runtime_file_unlink OSError while deleting runtime prompt file", file=sys.stderr, flush=True)
                    pass

    def _reload_workflow_selectors(self) -> None:
        current_slug = self.current_workflow_slug()
        for combo in (self.workflowPane.selector, self.promptPane.workflowSelector):
            combo.blockSignals(True)
            combo.clear()
            for wf in self.workflowDefinitions:
                combo.addItem(_title_case_prompt_label(wf.title), wf.slug)
            combo.blockSignals(False)
        if self.workflowDefinitions:
            idx = next((i for i, wf in enumerate(self.workflowDefinitions) if wf.slug == current_slug), 0)
            self.workflowPane.selector.setCurrentIndex(idx)
            self.promptPane.workflowSelector.setCurrentIndex(idx)
            self.workflowPane.selector.setEditText(_title_case_prompt_label(self.workflowDefinitions[idx].title))
        else:
            self.workflowPane.selector.setEditText(EMPTY_STRING)

    def _reload_doctype_selectors(self) -> None:
        current_path = self.current_doctype_path()
        for combo in (self.doctypePane.selector, self.promptPane.doctypeSelector):
            combo.blockSignals(True)
            combo.clear()
            for path in self.doctypeFiles:
                combo.addItem(_friendly_prompt_title(path.stem), str(path))
            combo.blockSignals(False)
        if self.doctypeFiles:
            idx = next((i for i, p in enumerate(self.doctypeFiles) if p == current_path), 0)
            self.doctypePane.selector.setCurrentIndex(idx)
            self.promptPane.doctypeSelector.setCurrentIndex(idx)

    def _reload_prompt_selector(self) -> None:
        self.promptLibraryEntries = build_prompt_library_entries(self.paths.prompts, self.promptFiles)
        self.promptPane.promptSelector.blockSignals(True)
        self.promptPane.promptSelector.clear()
        self.promptPane.promptSelector.addItem(self._t('prompt.select_prompt'), PROMPT_EMPTY_SENTINEL)
        self.promptPane.promptSelector.addItem(self._t('prompt.new_prompt'), PROMPT_NEW_SENTINEL)
        for entry in self.promptLibraryEntries:
            self.promptPane.promptSelector.addItem(_title_case_prompt_label(entry.display_name), str(entry.path))
        idx = 0
        if self.currentPromptPath is not None:
            idx = next((i for i, entry in enumerate(self.promptLibraryEntries, start=2) if entry.path == self.currentPromptPath), 0)
        self.promptPane.promptSelector.setCurrentIndex(idx)
        self.promptPane.promptSelector.blockSignals(False)
        print(f'[PROMPT:SELECTOR-POPULATED] combo=promptPane.promptSelector count={self.promptPane.promptSelector.count()} promptFiles={len(self.promptFiles)} currentIndex={idx} currentPrompt={self.currentPromptPath}', file=sys.stderr)

    def _queue_startup_state_apply(self, state_name: str) -> None:
        if self.startupWindowStateApplied:
            return
        for delay in (0, 50, 150, 350, 800):
            self._schedule_lifecycle_callback(PHASE_RUNTIME_TIMERS + 20 + int(delay), f'Apply Startup Window State {delay}ms', delay, lambda s=state_name, d=delay: self._apply_startup_window_state(s, d), ttl=10)

    def _apply_startup_window_state(self, state_name: str, delay_ms: int = 0) -> None:
        target = str(state_name or 'normal').strip().lower() or 'normal'
        _debug_log('PROMPT:WINDOW-ONLOAD', f'apply target={target} delay={delay_ms} current={self._current_window_state_name()} visible={self.isVisible()}')
        if target == 'maximized':
            if self.isMaximized():
                self.startupWindowStateApplied = True
                self.suppressStatePersistence = False
                return
            try:
                self.setWindowState((self.windowState() & ~Qt.WindowFullScreen & ~Qt.WindowMinimized) | Qt.WindowMaximized)
            except Exception as error:
                captureException(None, source='prompt_app.py', context='except@6815')
                print(f"[WARN:swallowed-exception] prompt_app.py:5194 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
            try:
                self.showMaximized()
            except Exception as error:
                captureException(None, source='prompt_app.py', context='except@6820')
                print(f"[WARN:swallowed-exception] prompt_app.py:5198 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
            return
        if target == 'fullscreen':
            if self.isFullScreen():
                self.startupWindowStateApplied = True
                self.suppressStatePersistence = False
                return
            try:
                self.setWindowState((self.windowState() & ~Qt.WindowMaximized & ~Qt.WindowMinimized) | Qt.WindowFullScreen)
            except Exception as error:
                captureException(None, source='prompt_app.py', context='except@6831')
                print(f"[WARN:swallowed-exception] prompt_app.py:5208 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
            try:
                self.showFullScreen()
            except Exception as error:
                captureException(None, source='prompt_app.py', context='except@6836')
                print(f"[WARN:swallowed-exception] prompt_app.py:5212 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass
            return
        self.startupWindowStateApplied = True
        self.suppressStatePersistence = False

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._schedule_lifecycle_callback(PHASE_RUNTIME_TIMERS + 30, 'Enforce Float Window After Show', 0, self._enforce_float_window, ttl=5)
        _debug_log('PROMPT:HANDLER', f'name=onLoad visible={self.isVisible()} current={self._current_window_state_name()} geometry={self._rect_to_tuple(self.geometry())} normalGeometry={self._rect_to_tuple(self.normalGeometry())}')
        self._trace_menu_structure('showEvent')
        self._debug_window_state('show-event')
        if not self.hasShownWindowStateFallback:
            self.hasShownWindowStateFallback = True
            saved_state = str(getattr(self, 'startupWindowStateTarget', 'normal') or 'normal').strip().lower() or 'normal'
            if saved_state in {'maximized', 'fullscreen'}:
                _debug_log('PROMPT:HANDLER', f'name=onLoad-fallback target={saved_state}')
                self._queue_startup_state_apply(saved_state)
            else:
                self.startupWindowStateApplied = True
                self.suppressStatePersistence = False

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        _debug_log('PROMPT:HANDLER', f'name=onMove current={self._current_window_state_name()} geometry={self._rect_to_tuple(self.geometry())} normalGeometry={self._rect_to_tuple(self.normalGeometry())}')

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        _debug_log('PROMPT:HANDLER', f'name=onResize current={self._current_window_state_name()} geometry={self._rect_to_tuple(self.geometry())} normalGeometry={self._rect_to_tuple(self.normalGeometry())}')
        self._schedule_lifecycle_callback(PHASE_RUNTIME_TIMERS + 31, 'Maintain Splitter After Resize', 0, self._maintain_even_main_splitter, ttl=5)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.ActivationChange:
            if bool(getattr(self, 'floatOnTopEnabled', False)) and not self.isActiveWindow() and not self.isMinimized():
                self._schedule_lifecycle_callback(PHASE_RUNTIME_TIMERS + 32, 'Enforce Float Window After Activation Change', 0, self._enforce_float_window, ttl=5)
            return
        if event.type() == QEvent.WindowStateChange:
            old_state = getattr(event, 'oldState', lambda: Qt.WindowNoState)()
            _debug_log('PROMPT:HANDLER', f'name=onWindowStateChange old={old_state} current={self._current_window_state_name()} maximized={self.isMaximized()} fullscreen={self.isFullScreen()} minimized={self.isMinimized()} suppress={self.suppressStatePersistence} closeInProgress={self.closeInProgress}')
            if self.closeInProgress:
                _debug_log('PROMPT:HANDLER', 'name=onWindowStateChange-skipped reason=closeInProgress')
                return
            if self.isMaximized():
                _debug_log('PROMPT:HANDLER', 'name=onMaximize')
                self.startupWindowStateApplied = True
                self.suppressStatePersistence = False
                self.settings.set_value('ui.window.state', 'maximized')
            elif self.isFullScreen():
                _debug_log('PROMPT:HANDLER', 'name=onFullScreen')
                self.startupWindowStateApplied = True
                self.suppressStatePersistence = False
                self.settings.set_value('ui.window.state', 'fullscreen')
            elif not self.isMinimized():
                _debug_log('PROMPT:HANDLER', 'name=onRestoreNormal')
                if not self.suppressStatePersistence:
                    self.settings.set_value('ui.window.state', 'normal')
                else:
                    _debug_log('PROMPT:HANDLER', 'name=onRestoreNormal-skipped reason=suppressStatePersistence')

    def closeEvent(self, event) -> None:
        try:
            self.soundManager.play('close_modal')
        except Exception as exc:
            captureException(exc, source='prompt_app.py', context='close-sound', handled=True)
        self.closeInProgress = True
        _debug_log('PROMPT:HANDLER', f'name=onClose current={self._current_window_state_name()} visible={self.isVisible()} geometry={self._rect_to_tuple(self.geometry())} normalGeometry={self._rect_to_tuple(self.normalGeometry())}')
        try:
            if getattr(self, 'debuggerHeartbeatTimer', None) is not None:
                cast(Any, self.debuggerHeartbeatTimer).stop()
            if getattr(self, 'debuggerCommandTimer', None) is not None:
                cast(Any, self.debuggerCommandTimer).stop()
            if _debugger_enabled():
                _write_debugger_heartbeat(event_kind='process', reason='CLOSE', caller='PromptMainWindow', phase='SHUTDOWN', var_dump=self.debuggerHook.buildLiveVarDump('CLOSE'), process_snapshot=self.debuggerHook.buildLiveProcessSnapshot('CLOSE'))
                if getattr(self, 'remoteDebugger', None) is not None:
                    cast(Any, self.remoteDebugger).emitProcess('CLOSE')
                    cast(Any, self.remoteDebugger).shutdown()
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@6910')
            print(f"[WARN:swallowed-exception] prompt_app.py:5285 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            _log_exception('PROMPT:DEBUGGER-CLOSE-HEARTBEAT-FAILED', exc, include_traceback=False)
        self._debug_window_state('close-event-before-save')
        self._save_window_geometry_state()
        self._remember_visible_main_splitter_sizes()
        self.settings.set_json('ui.main_splitter.sizes', self.mainSplitter.sizes() if hasattr(self, 'mainSplitter') else [])
        self.settings.set_value('ui.show_doctype', '1' if self.doctypePane.isVisible() else '0')
        self.settings.set_value('ui.show_workflow', '1' if self.workflowPane.isVisible() else '0')
        self.settings.debug_dump('close-event-after-save', keys=['ui.window.state', 'ui.window.normal_bounds', 'ui.window.geometry'])
        self._debug_window_state('close-event-after-save')
        super().closeEvent(event)

    def _load_db_settings_fields(self) -> None:
        self.settingsPane.dbHostEdit.setText(str(self.settings.get_value('db.host', 'localhost') or 'localhost'))
        self.settingsPane.dbPortEdit.setText(str(self.settings.get_value('db.port', '3306') or '3306'))
        self.settingsPane.dbNameEdit.setText(str(self.settings.get_value('db.name', 'prompt') or 'prompt'))
        self.settingsPane.dbUserEdit.setText(str(self.settings.get_value('db.user', 'root') or 'root'))
        self.settingsPane.dbPasswordEdit.setText(str(self.settings.get_value('db.password', EMPTY_STRING) or EMPTY_STRING))
        self._refresh_about_views()

    def _build_about_html(self, output_path: Path, compact: bool = False) -> str:
        cloud_path = self.paths.assets / 'about' / 'clouds.jpg'
        tamagotchi_path = self.paths.assets / 'about' / 'tamagotchi_spry.gif'
        cloud_url = html_lib.escape(_relative_url(output_path.parent, cloud_path)) if cloud_path.exists() else EMPTY_STRING
        tamagotchi_url = html_lib.escape(_relative_url(output_path.parent, tamagotchi_path)) if tamagotchi_path.exists() else EMPTY_STRING
        shell_class = 'about-shell compact' if compact else 'about-shell'
        fingerprint_path = _prompt_executable_path() if _is_frozen_runtime() else Path(__file__).resolve()
        app_md5 = _prompt_file_md5_safe(fingerprint_path)
        _trace_event('PROMPT:ABOUT:FINGERPRINT', f'path={fingerprint_path} md5={app_md5}')
        template = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>__ABOUT_TITLE__</title>
<style>
*{box-sizing:border-box}html,body{margin:0;padding:0;min-height:100%}body{background:#1f2937;color:#f8fafc;font-family:'Segoe UI',system-ui,sans-serif}
.about-shell{min-height:100vh;padding:22px;background:linear-gradient(180deg,rgba(2,132,199,.12),rgba(15,23,42,.55)),#1f2937}.about-shell.compact{min-height:auto;padding:14px}
.about-card{width:min(980px,100%);margin:0 auto;background:rgba(15,23,42,.72);border:1px solid rgba(255,255,255,.18);border-radius:24px;overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,.38)}
.hero{position:relative;min-height:255px;padding:26px;display:flex;align-items:flex-end;justify-content:space-between;gap:20px;background:linear-gradient(180deg,rgba(125,211,252,.22),rgba(15,23,42,.14)),url('__CLOUD_URL__') center center/cover no-repeat}
.hero::after{content:'';position:absolute;inset:0;background:linear-gradient(180deg,rgba(15,23,42,.08),rgba(15,23,42,.65))}.hero-copy,.hero-pet{position:relative;z-index:1}
.app-badge{display:inline-block;padding:6px 10px;margin-bottom:12px;border-radius:999px;background:rgba(15,23,42,.72);border:1px solid rgba(255,255,255,.22);font-size:12px;letter-spacing:.08em;text-transform:uppercase}
.hero h1{margin:0;font-size:clamp(34px,6vw,50px);line-height:1.04;text-shadow:0 5px 18px rgba(0,0,0,.42)}.hero p{margin:10px 0 0;max-width:560px;font-size:16px;line-height:1.45;color:#e0f2fe}
.hero-pet{min-width:220px;text-align:center;padding:14px;border-radius:18px;background:rgba(255,255,255,.14);border:1px solid rgba(255,255,255,.22);box-shadow:0 10px 30px rgba(15,23,42,.25)}
.hero-pet img{display:block;max-width:min(250px,100%);height:auto;margin:0 auto 10px}.hero-pet .caption{font-size:14px;color:#f8fafc}
.content{padding:24px 26px 28px;display:grid;gap:16px}.info-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:14px}
.info-panel{padding:16px 18px;border-radius:16px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12)}.info-panel h2{margin:0 0 10px;font-size:16px;color:#bae6fd;text-transform:uppercase;letter-spacing:.08em}.info-panel p{margin:0 0 8px;line-height:1.55;color:#e5eef9}.label{display:inline-block;min-width:86px;font-weight:700;color:#7dd3fc}a{color:#93c5fd}.footer-line{margin-top:2px;padding:14px 16px;border-radius:14px;background:rgba(2,6,23,.35);border:1px solid rgba(255,255,255,.10);line-height:1.65;color:#e5eef9}.heart{color:#fb7185;font-weight:700}
</style></head><body><div class="__SHELL_CLASS__"><section class="about-card"><div class="hero"><div class="hero-copy"><div class="app-badge">__ABOUT_BADGE__</div><h1>__ABOUT_HERO_TITLE__</h1><p>__ABOUT_DESCRIPTION__</p></div><div class="hero-pet"><img src="__TAMAGOTCHI_URL__" alt="Spry Tamagotchi"><div class="caption">__ABOUT_PET_CAPTION__</div></div></div><div class="content"><div class="info-grid"><div class="info-panel"><h2>__ABOUT_VERSION_HEADING__</h2><p><span class="label">__ABOUT_APP_LABEL__</span> __ABOUT_APP_VERSION__</p><p><span class="label">__ABOUT_MD5_LABEL__</span> __ABOUT_APP_MD5__</p><p><span class="label">__ABOUT_RELEASED_LABEL__</span> 2026</p><p><span class="label">__ABOUT_LICENSE_LABEL__</span> <a href="https://opensource.org/license/mit">__ABOUT_LICENSE_TEXT__</a></p></div><div class="info-panel"><h2>__ABOUT_AUTHOR_HEADING__</h2><p>Trenton Tompkins © 2026 &lt;<a href="mailto:trenttompkins@gmail.com">trenttompkins@gmail.com</a>&gt;</p><p>(724) 431-5207 - __ABOUT_FREE_ESTIMATE__</p><p>Portfolio: <a href="http://www.trentontompkins.com">http://www.trentontompkins.com</a></p></div></div><div class="footer-line">__ABOUT_FOOTER__</div></div></section></div></body></html>"""
        return (template
            .replace('__CLOUD_URL__', cloud_url)
            .replace('__TAMAGOTCHI_URL__', tamagotchi_url)
            .replace('__SHELL_CLASS__', shell_class)
            .replace('__ABOUT_TITLE__', html_lib.escape(self._t('about.title')))
            .replace('__ABOUT_BADGE__', html_lib.escape(self._t('about.badge')))
            .replace('__ABOUT_HERO_TITLE__', html_lib.escape(self._t('about.hero_title')))
            .replace('__ABOUT_DESCRIPTION__', html_lib.escape(self._t('about.description')))
            .replace('__ABOUT_PET_CAPTION__', html_lib.escape(self._t('about.pet_caption')))
            .replace('__ABOUT_VERSION_HEADING__', html_lib.escape(self._t('about.version_heading')))
            .replace('__ABOUT_APP_LABEL__', html_lib.escape(self._t('about.app_label')))
            .replace('__ABOUT_APP_VERSION__', html_lib.escape(self._t('about.app_version')))
            .replace('__ABOUT_MD5_LABEL__', html_lib.escape(self._t('about.md5_label')))
            .replace('__ABOUT_APP_MD5__', html_lib.escape(app_md5))
            .replace('__ABOUT_RELEASED_LABEL__', html_lib.escape(self._t('about.released_label')))
            .replace('__ABOUT_LICENSE_LABEL__', html_lib.escape(self._t('about.license_label')))
            .replace('__ABOUT_LICENSE_TEXT__', html_lib.escape(self._t('about.license_text')))
            .replace('__ABOUT_AUTHOR_HEADING__', html_lib.escape(self._t('about.author_heading')))
            .replace('__ABOUT_FREE_ESTIMATE__', html_lib.escape(self._t('about.free_estimate')))
            .replace('__ABOUT_FOOTER__', self._t('about.footer')))


    def _refresh_about_views(self) -> None:
        about_box_path = self.paths.generated / '_about_box.html'
        about_dialog_path = self.paths.generated / '_about_dialog.html'
        self._write_html_and_load(self.helpPane.aboutView, about_box_path, self._build_about_html(about_box_path, compact=True))
        if getattr(self, 'aboutDialog', None) is not None:
            self._write_html_and_load(cast(Any, self.aboutDialog).view, about_dialog_path, self._build_about_html(about_dialog_path, compact=False))

    def show_about_dialog(self) -> None:
        self.soundManager.play('open_modal')
        if getattr(self, 'aboutDialog', None) is None:
            self.aboutDialog = AboutDialog(cast(Any, self))
        try:
            cast(Any, self.aboutDialog).setWindowTitle(self._t('about.title'))
            self._set_widget_text_safe(getattr(cast(Any, self.aboutDialog), 'closeButton', None), self._t('button.close'))
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@6988')
            _log_exception('PROMPT:ABOUT-DIALOG-LOCALIZE-FAILED', exc, include_traceback=False)
        self._refresh_about_views()
        try:
            cast(Any, self.aboutDialog).show()
            cast(Any, self.aboutDialog).raise_()
            cast(Any, self.aboutDialog).activateWindow()
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@6995')
            print(f"[WARN:swallowed-exception] prompt_app.py:5343 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            pass

    def save_db_settings(self) -> None:
        self.settings.set_value('db.host', self.settingsPane.dbHostEdit.text().strip())
        self.settings.set_value('db.port', self.settingsPane.dbPortEdit.text().strip())
        self.settings.set_value('db.name', self.settingsPane.dbNameEdit.text().strip())
        self.settings.set_value('db.user', self.settingsPane.dbUserEdit.text().strip())
        self.settings.set_value('db.password', self.settingsPane.dbPasswordEdit.text())
        self.soundManager.set_enabled(bool(self.settingsPane.soundEffectsCheckBox.isChecked()))
        self.soundManager.play('save')
        self.statusBar().showMessage(self._t('status.saved_db'), 3000)

    def update_settings_help(self) -> None:
        html = render_settings_help_html(self.paths.root)
        target = self.paths.generated / '_settings_help.html'
        print(f'[PROMPT:LOAD-TARGET] view=Settings Help reason=update_settings_help file={target}', file=sys.stderr)
        self._write_html_and_load(self.helpPane.helpView, target, html)

    def reload_fonts_tab(self) -> None:
        self.fontsPane.reloadBundledFonts()
        if self.fontsPane.fontSelector.count():
            self._load_selected_bundled_font()
        self.fontsInitialized = True

    def _load_selected_bundled_font(self) -> None:
        index = int(self.fontsPane.fontSelector.currentIndex())
        if index >= 0 and self.fontsPane.fontList.currentRow() != index:
            self.fontsPane.fontList.blockSignals(True)
            self.fontsPane.fontList.setCurrentRow(index)
            self.fontsPane.fontList.blockSignals(False)
        font_path = str(self.fontsPane.fontSelector.currentData() or EMPTY_STRING).strip()
        if font_path:
            self.fontsPane.loadFont(Path(font_path))

    def open_font_file(self) -> None:
        self.tabWidget.setCurrentIndex(2)
        self.fontsPane.openFontDialog()


    def current_workflow_slug(self) -> Optional[str]:
        return self.promptPane.workflowSelector.currentData() if self.promptPane.workflowSelector.count() else None

    def current_workflow_definition(self) -> Optional[WorkflowDefinition]:
        slug = self.current_workflow_slug()
        for wf in self.workflowDefinitions:
            if wf.slug == slug:
                return wf
        return self.workflowDefinitions[0] if self.workflowDefinitions else None

    def current_workflow_source_path(self) -> Optional[Path]:
        wf = self.current_workflow_definition()
        return wf.source_path if wf else None

    def current_doctype_path(self) -> Optional[Path]:
        value = self.promptPane.doctypeSelector.currentData()
        if value:
            return Path(value)
        return self.doctypeFiles[0] if self.doctypeFiles else None

    def current_prompt_selector_value(self) -> str:
        value = self.promptPane.promptSelector.currentData()
        return '' if value is None else str(value)

    def current_prompt_selector_path(self) -> Optional[Path]:
        value = self.current_prompt_selector_value()
        if value in ('', PROMPT_EMPTY_SENTINEL, PROMPT_NEW_SENTINEL):
            return None
        return Path(value)

    def _select_workflow_slug(self, slug: str) -> None:
        if not slug:
            return
        idx = next((i for i, wf in enumerate(self.workflowDefinitions) if wf.slug == slug), -1)
        if idx >= 0:
            self.promptPane.workflowSelector.blockSignals(True)
            self.promptPane.workflowSelector.setCurrentIndex(idx)
            self.promptPane.workflowSelector.blockSignals(False)
            self.workflowPane.selector.blockSignals(True)
            self.workflowPane.selector.setCurrentIndex(idx)
            self.workflowPane.selector.blockSignals(False)

    def _select_doctype_name(self, name: str) -> None:
        if not name:
            return
        idx = next((i for i, p in enumerate(self.doctypeFiles) if p.stem == name), -1)
        if idx >= 0:
            self.promptPane.doctypeSelector.blockSignals(True)
            self.promptPane.doctypeSelector.setCurrentIndex(idx)
            self.promptPane.doctypeSelector.blockSignals(False)
            self.doctypePane.selector.blockSignals(True)
            self.doctypePane.selector.setCurrentIndex(idx)
            self.doctypePane.selector.blockSignals(False)

    def _write_html_and_load(self, view, path: Path, html_text: str) -> None:
        path = Path(path)
        view_name = str(getattr(view, 'viewName', '') or getattr(view, 'objectName', lambda: '')() or view.__class__.__name__)
        path.parent.mkdir(parents=True, exist_ok=True)
        html_value = str(html_text or EMPTY_STRING)
        dynamic_controls = 'id="dynamicControls"' in html_value or "id='dynamicControls'" in html_value
        prompt_runtime_json = 'PromptRuntimeDataJson' in html_value or 'PromptRuntimeData' in html_value
        generator_script = 'prompt_generator.js' in html_value
        payload = html_value.encode('utf-8', errors='replace')
        blank_html = not html_value.strip()
        _trace_event(
            'PROMPT:HTML:WRITE-REQUEST',
            f'view={view_name} file={path} bytes={len(payload)} chars={len(html_value)} md5={_md5_for_bytes(payload)} blank={blank_html} '
            f'dynamicControls={dynamic_controls} runtimeData={prompt_runtime_json} generatorScript={generator_script}'
        )
        try:
            File.writeText(path, html_value, encoding='utf-8')
            _prompt_make_user_writable(path)
            read_back = path.read_bytes() if path.exists() else b''
            _trace_event('PROMPT:HTML:WRITE-COMPLETE', f'view={view_name} file={path} written_bytes={len(payload)} readback_bytes={len(read_back)} readback_md5={_md5_for_bytes(read_back)} matches={read_back == payload} {_describe_local_file_for_trace(path)}')
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@7107')
            _log_exception('PROMPT:HTML:WRITE-FAILED', exc, include_traceback=True)
            raise
        url = QUrl.fromLocalFile(str(path.resolve()))
        base_url = QUrl.fromLocalFile(str(path.parent.resolve()) + os.sep)
        _trace_event('PROMPT:HTML:LOAD-CALL', f'view={view_name} file={path} url={url.toString()} base_url={base_url.toString()} mode={PROMPT_WEBENGINE_LOAD_MODE} fallback={PROMPT_WEBENGINE_FALLBACK_ACTIVE} {_describe_local_file_for_trace(path)}')
        # v158: normal WebEngine panes use setHtml() by default. The generated
        # HTML is still written to disk for debugging, but this avoids a common
        # packaged-EXE failure where Chromium/QWebEngine shows a blank page after
        # being handed a file:// URL even though the file exists. base_url keeps
        # local JS/CSS/vendor assets resolvable. Set PROMPT_WEBENGINE_LOAD_MODE=file
        # to force the old URL loader for comparison.
        if PROMPT_WEBENGINE_FALLBACK_ACTIVE or PROMPT_WEBENGINE_LOAD_MODE == 'file' or not hasattr(view, 'setHtml'):
            view.load(url)
        else:
            _trace_event('PROMPT:HTML:SETHTML-LOAD-CALL', f'view={view_name} source_file={path} base_url={base_url.toString()} bytes={len(read_back)} chars={len(html_value)} md5={_md5_for_bytes(read_back)}')
            view.setHtml(html_value, base_url)

    def _suggest_prompt_save_path(self, title: str, bucket_name: str = '') -> Path:
        bucket_slug = _slugify(bucket_name or 'general') if str(bucket_name or EMPTY_STRING).strip() else ''
        title_slug = _slugify(title or 'new_prompt')
        if bucket_slug:
            return self.paths.prompts / bucket_slug / f'{title_slug}.prompt.md'
        return self.paths.prompts / f'{title_slug}.prompt.md'

    def _runtime_prompt_capture_script(self) -> str:
        return """
        (function(){
            const readValue = function(id) {
                const node = document.getElementById(id);
                return node ? String(node.value || node.textContent || '') : '';
            };
            const workflowNode = document.getElementById('workflowSource');
            return {
                title: readValue('promptTitle'),
                task: readValue('userTask'),
                context: readValue('baseContext'),
                generated_prompt: readValue('openedPrompt'),
                previous_prompt: readValue('previousPrompt'),
                instruction_lines: readValue('previewList'),
                workflow_source: workflowNode ? String(workflowNode.textContent || '') : ''
            };
        })();
        """

    def _store_runtime_session_payload(self, payload: object) -> None:
        data = payload if isinstance(payload, dict) else {}
        generated_prompt = str(data.get('generated_prompt', '') or '').strip()
        previous_prompt = str(data.get('previous_prompt', '') or '').strip()
        title = str(data.get('title', self.currentPromptTitle) or self.currentPromptTitle).strip() or self.currentPromptTitle
        if generated_prompt:
            self.previousPromptText = generated_prompt
            self.previousPromptTitle = title
            self.promptChainActive = True
        elif previous_prompt:
            self.previousPromptText = previous_prompt
            self.previousPromptTitle = title
            self.promptChainActive = True
        self.settings.set_json('prompt.runtime.session', {
            'previous_prompt': self.previousPromptText,
            'previous_title': self.previousPromptTitle,
        })

    def _capture_runtime_session_then(self, callback) -> None:
        if PROMPT_WEBENGINE_FALLBACK_ACTIVE or self.promptHomeActive:
            self._store_runtime_session_payload({})
            callback()
            return
        try:
            self.promptPane.web.page().runJavaScript(self._runtime_prompt_capture_script(), lambda payload: (self._store_runtime_session_payload(payload), callback()))
        except Exception as error:
            captureException(None, source='prompt_app.py', context='except@7177')
            print(f"[WARN:swallowed-exception] prompt_app.py:5492 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            callback()

    def show_prompt_home(self) -> None:
        self.promptHomeActive = True
        if self.promptPane.promptSelector.count():
            self.promptPane.promptSelector.blockSignals(True)
            self.promptPane.promptSelector.setCurrentIndex(0)
            self.promptPane.promptSelector.blockSignals(False)
        current_doctype_path = self.current_doctype_path()
        html = render_prompt_index_html(self.paths.root, self.workflowDefinitions, self.promptFiles, self.current_workflow_slug(), self.currentPromptPath, current_doctype_path.stem if current_doctype_path else '')
        index_path = self.paths.generated / 'index.html'
        self._write_html_and_load(self.promptPane.web, index_path, html)

    def select_workflow_by_slug(self, slug: str) -> None:
        idx = next((i for i, wf in enumerate(self.workflowDefinitions) if wf.slug == slug), -1)
        if idx < 0:
            return
        def apply_selection() -> None:
            self.promptPane.workflowSelector.blockSignals(True)
            self.promptPane.workflowSelector.setCurrentIndex(idx)
            self.promptPane.workflowSelector.blockSignals(False)
            self.workflowPane.selector.blockSignals(True)
            self.workflowPane.selector.setCurrentIndex(idx)
            self.workflowPane.selector.blockSignals(False)
            self.currentPromptPath = None
            self.currentPromptTitle = 'New Prompt'
            self.currentPromptTask = ''
            self.currentPromptText = ''
            self.load_selected_workflow()
            self.promptHomeActive = True
            self.show_prompt_home()
        self._capture_runtime_session_then(apply_selection)

    def select_new_prompt(self) -> None:
        def apply_selection() -> None:
            self.promptPane.promptSelector.blockSignals(True)
            self.promptPane.promptSelector.setCurrentIndex(1)
            self.promptPane.promptSelector.blockSignals(False)
            self.currentPromptPath = None
            self.currentPromptTitle = 'New Prompt'
            self.currentPromptTask = ''
            self.currentPromptText = ''
            self.promptHomeActive = False
            self.render_prompt_runtime()
        self._capture_runtime_session_then(apply_selection)

    def select_prompt_by_path(self, path: Path) -> None:
        if not path.is_absolute():
            path = (self.paths.root / path).resolve()
        if not path.exists():
            return
        def apply_selection() -> None:
            self.load_prompt_from_path(path)
            idx = next((i for i, entry in enumerate(self.promptLibraryEntries, start=2) if entry.path == path), -1)
            if idx >= 0:
                self.promptPane.promptSelector.blockSignals(True)
                self.promptPane.promptSelector.setCurrentIndex(idx)
                self.promptPane.promptSelector.blockSignals(False)
            self.promptHomeActive = False
            self.render_prompt_runtime()
        self._capture_runtime_session_then(apply_selection)

    def _user_selected_prompt(self) -> None:
        if self.isInitializing:
            return
        self._capture_runtime_session_then(self.load_selected_prompt)

    def _user_changed_workflow_from_prompt(self) -> None:
        if self.isInitializing:
            return
        idx = self.promptPane.workflowSelector.currentIndex()
        if idx < 0:
            return
        def apply_selection() -> None:
            self.workflowPane.selector.blockSignals(True)
            self.workflowPane.selector.setCurrentIndex(idx)
            self.workflowPane.selector.blockSignals(False)
            self.load_selected_workflow()
            if self.current_prompt_selector_value() in ('', PROMPT_EMPTY_SENTINEL):
                self.promptHomeActive = True
                self.show_prompt_home()
                return
            self.promptHomeActive = False
            self.render_prompt_runtime()
        self._capture_runtime_session_then(apply_selection)

    def _user_changed_doctype_from_prompt(self) -> None:
        if self.isInitializing:
            return
        idx = self.promptPane.doctypeSelector.currentIndex()
        if idx < 0:
            return
        def apply_selection() -> None:
            self.doctypePane.selector.blockSignals(True)
            self.doctypePane.selector.setCurrentIndex(idx)
            self.doctypePane.selector.blockSignals(False)
            self.load_selected_doctype()
            if self.current_prompt_selector_value() == PROMPT_EMPTY_SENTINEL or self.promptHomeActive:
                self.promptHomeActive = True
                self.show_prompt_home()
            else:
                self.render_prompt_runtime()
        self._capture_runtime_session_then(apply_selection)

    def _sync_workflow_selection_from_editor(self) -> None:
        if self.isInitializing:
            return
        idx = self.workflowPane.selector.currentIndex()
        if idx < 0:
            return
        self.promptPane.workflowSelector.blockSignals(True)
        self.promptPane.workflowSelector.setCurrentIndex(idx)
        self.promptPane.workflowSelector.blockSignals(False)
        self.load_selected_workflow()
        if self.current_prompt_selector_value() == PROMPT_EMPTY_SENTINEL or self.promptHomeActive:
            self.promptHomeActive = True
            self.show_prompt_home()
        else:
            self.render_prompt_runtime()


    def _sync_doctype_selection_from_editor(self) -> None:
        if self.isInitializing:
            return
        idx = self.doctypePane.selector.currentIndex()
        if idx < 0:
            return
        self.promptPane.doctypeSelector.blockSignals(True)
        self.promptPane.doctypeSelector.setCurrentIndex(idx)
        self.promptPane.doctypeSelector.blockSignals(False)
        self.load_selected_doctype()
        if self.current_prompt_selector_value() == PROMPT_EMPTY_SENTINEL or self.promptHomeActive:
            self.promptHomeActive = True
            self.show_prompt_home()
        else:
            self.render_prompt_runtime()


    def load_selected_prompt(self) -> None:
        value = self.current_prompt_selector_value()
        if value in ('', PROMPT_EMPTY_SENTINEL):
            self.currentPromptPath = None
            self.currentPromptTitle = 'New Prompt'
            self.currentPromptTask = ''
            self.currentPromptText = ''
            self.promptHomeActive = True
            self.show_prompt_home()
            return
        path = self.current_prompt_selector_path()
        if value == PROMPT_NEW_SENTINEL or path is None:
            self.currentPromptPath = None
            self.currentPromptTitle = 'New Prompt'
            self.currentPromptTask = ''
            self.currentPromptText = ''
            self.promptHomeActive = False
            self.render_prompt_runtime()
            return
        self.load_prompt_from_path(path)
        self.promptHomeActive = False
        self.render_prompt_runtime()

    def open_prompt_file(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(self, self._t('dialog.open_prompt'), str(self.paths.prompts), PROMPT_FILENAME_FILTER)
        if not file_path:
            return
        self.load_prompt_from_path(Path(file_path))
        self.promptHomeActive = False
        self.render_prompt_runtime()

    def load_prompt_from_path(self, path: Path) -> None:
        try:
            text = _read_prompt_text_with_embedded_fallback(path, encoding='utf-8')
        except OSError as exc:
            captureException(None, source='prompt_app.py', context='except@7351')
            print(f'[PROMPT:OPEN-PROMPT-FAILED] {exc}', file=sys.stderr)
            return
        doc = parse_prompt_document(text, re.sub(r'(?i)\.prompt$', '', path.stem))
        save_path = _prompt_sanitize_opened_prompt_save_path(path, doc, self.paths.prompts, self.paths.root)
        if save_path != path:
            try:
                save_path.parent.mkdir(parents=True, exist_ok=True)
                File.writeText(save_path, text, encoding='utf-8')
                _prompt_make_user_writable(save_path)
                payload = text.encode('utf-8', errors='replace')
                _trace_event('PROMPT:PROMPT-FILE:IMPORTED', f'source={path} save_path={save_path} bytes={len(payload)} md5={_md5_for_bytes(payload)}')
            except Exception as import_exc:
                captureException(import_exc, source='prompt_app.py', context='import-opened-prompt-to-user-root', handled=True, extra=f'source={path} target={save_path}')
                _log_exception('PROMPT:PROMPT-FILE:IMPORT-FAILED', import_exc, include_traceback=True)
        self.currentPromptPath = save_path
        self.currentPromptTitle = doc.title
        self.currentPromptTask = doc.task
        self.currentPromptText = doc.context
        self._select_workflow_slug(doc.workflow_slug)
        self._select_doctype_name(doc.doctype_name)
        self.load_selected_workflow()
        self.load_selected_doctype()
        self.promptFiles = discover_prompts(self.paths.prompts)
        self._reload_prompt_selector()
        self.statusBar().showMessage(self._t('prompt.loaded_status', name=path.name), 3000)

    def load_selected_doctype(self) -> None:
        path = self.current_doctype_path()
        if path is None:
            self.doctypePane.editor.setPlainText('')
            self._write_html_and_load(self.doctypePane.preview, self.paths.generated / '_doctype_preview_empty.html', self._t('prompt.empty_doctype_html'))
            return
        try:
            text = _read_prompt_text_with_embedded_fallback(path, encoding='utf-8')
        except OSError as exc:
            captureException(None, source='prompt_app.py', context='except@7375')
            QMessageBox.critical(self, self._t('prompt.load_doctype_failed'), str(exc))
            return
        self.doctypePane.editor.blockSignals(True)
        self.doctypePane.editor.setPlainText(text)
        self.doctypePane.editor.blockSignals(False)
        self.doctypePane.infoLabel.setText(self._t('editor.info.editing', path=str(path)))
        self.update_doctype_preview()

    def load_selected_workflow(self) -> None:
        wf = self.current_workflow_definition()
        source = self.current_workflow_source_path()
        if wf is None or source is None:
            self.workflowPane.editor.setPlainText('')
            self._write_html_and_load(self.workflowPane.preview, self.paths.generated / '_workflow_preview_empty.html', self._t('prompt.empty_workflow_html'))
            return
        try:
            text = _read_prompt_text_with_embedded_fallback(source, encoding='utf-8')
        except OSError as exc:
            captureException(None, source='prompt_app.py', context='except@7393')
            print(f'[PROMPT:LOAD-WORKFLOW-FAILED] {exc}', file=sys.stderr)
            return
        self.workflowPane.editor.blockSignals(True)
        self.workflowPane.editor.setPlainText(text)
        self.workflowPane.editor.blockSignals(False)
        self.workflowPane.infoLabel.setText(self._t('editor.info.workflow_compiled', source=str(source), compiled=str(wf.compiled_html_path)))
        self.update_workflow_preview()


    def _typed_workflow_name(self) -> str:
        return str(self.workflowPane.selector.currentText() or EMPTY_STRING).strip()

    def _resolve_workflow_save_target(self) -> tuple[Path, Path, Path]:
        typed_name = self._typed_workflow_name()
        current_source = self.current_workflow_source_path()
        fallback_name = typed_name or (current_source.stem.replace('_', ' ').strip() if current_source else 'workflow')
        slug = _slugify(fallback_name)
        for existing in self.workflowDefinitions:
            if existing.slug == slug:
                return existing.source_path, existing.compiled_html_path, existing.meta_path
        suffix = current_source.suffix if current_source is not None and current_source.suffix else '.md'
        folder = self.paths.workflows / slug
        source_path = folder / f'{slug}{suffix}'
        compiled_html_path = folder / '_compiled_workflow.html'
        meta_path = folder / '_compiled_workflow.log'
        return source_path, compiled_html_path, meta_path

    def save_doctype(self) -> None:
        path = self.current_doctype_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            File.writeText(path, self.doctypePane.editor.toPlainText(), encoding='utf-8')
            _prompt_make_user_writable(path)
        except OSError as exc:
            captureException(None, source='prompt_app.py', context='except@7427')
            self.soundManager.play('error')
            print(f'[PROMPT:SAVE-DOCTYPE-FAILED] {exc}', file=sys.stderr)
            return
        self.soundManager.play('save')
        self.update_doctype_preview()
        if self.promptHomeActive:
            self.show_prompt_home()
        else:
            self.render_prompt_runtime()

    def save_workflow(self) -> None:
        typed_name = self._typed_workflow_name()
        source_path, compiled_html_path, meta_path = self._resolve_workflow_save_target()
        if not typed_name and source_path is None:
            return
        try:
            source_path.parent.mkdir(parents=True, exist_ok=True)
            File.writeText(source_path, self.workflowPane.editor.toPlainText(), encoding='utf-8')
            _prompt_make_user_writable(source_path)
            self.compiler.compile_workflow(source_path, compiled_html_path, meta_path)
        except OSError as exc:
            captureException(None, source='prompt_app.py', context='except@7445')
            self.soundManager.play('error')
            QMessageBox.critical(self, self._t('dialog.save_workflow_failed'), str(exc))
            return
        self.workflowDefinitions = discover_workflows(self.paths.workflows)
        saved_slug = _slugify(typed_name or source_path.stem)
        self._reload_workflow_selectors()
        self._select_workflow_slug(saved_slug)
        self.soundManager.play('save')
        self.load_selected_workflow()
        if self.promptHomeActive:
            self.show_prompt_home()
        else:
            self.render_prompt_runtime()

    def save_prompt(self) -> None:
        if PROMPT_WEBENGINE_FALLBACK_ACTIVE:
            self._complete_prompt_save({
                'title': self.currentPromptTitle,
                'task': self.currentPromptTask,
                'context': self.currentPromptText,
                'generated_prompt': self.currentPromptText,
                'instruction_lines': EMPTY_STRING,
            })
            return
        script = """
        (function(){
            const title = document.getElementById('promptTitle');
            const task = document.getElementById('userTask');
            const context = document.getElementById('openedPrompt');
            const generatedPrompt = document.getElementById('openedPrompt');
            const previewList = document.getElementById('previewList');
            return {
                title: title ? title.value : '',
                task: task ? task.value : '',
                context: context ? context.value : '',
                generated_prompt: generatedPrompt ? generatedPrompt.value : '',
                instruction_lines: previewList ? previewList.value : ''
            };
        })();
        """
        self.promptPane.web.page().runJavaScript(script, self._complete_prompt_save)


    def _workflow_editor_matches_prompt_window(self, payload: dict[str, Any]) -> tuple[bool, str, Optional[WorkflowDefinition]]:
        wf = self.current_workflow_definition()
        if wf is None:
            return False, 'no-current-workflow', None
        prompt_slug = str(self.current_workflow_slug() or EMPTY_STRING).strip()
        editor_slug = str(self.workflowPane.selector.currentData() or EMPTY_STRING).strip() if hasattr(self, 'workflowPane') else EMPTY_STRING
        payload_slug = str(payload.get('workflowSlug') or EMPTY_STRING).strip()
        payload_source = str(payload.get('workflowSourcePath') or EMPTY_STRING).strip()
        expected_source = str(wf.source_path.resolve())
        payload_source_resolved = str(Path(payload_source).resolve()) if payload_source else EMPTY_STRING
        if not prompt_slug:
            return False, 'prompt-selector-has-no-workflow', wf
        if editor_slug and editor_slug != prompt_slug:
            return False, f'workflow-selector-mismatch prompt={prompt_slug} editor={editor_slug}', wf
        if payload_slug and payload_slug != prompt_slug:
            return False, f'runtime-workflow-mismatch prompt={prompt_slug} runtime={payload_slug}', wf
        if payload_source_resolved and payload_source_resolved != expected_source:
            return False, f'runtime-source-mismatch expected={expected_source} runtime={payload_source_resolved}', wf
        return True, 'matched', wf

    def generate_prompt_from_web(self, payload_json: str) -> None:
        try:
            payload = json.loads(str(payload_json or '{}'))
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@7510')
            payload = {}
            print(f'[PROMPT:GENERATE:PAYLOAD-ERROR] {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        if not isinstance(payload, dict):
            payload = {}
        ok, reason, wf = self._workflow_editor_matches_prompt_window(payload)
        generated = str(payload.get('generated_prompt') or EMPTY_STRING)
        before_md5 = str(payload.get('workflowEditorMd5') or EMPTY_STRING)
        print(f'[PROMPT:GENERATE:REQUEST] ok={ok} reason={reason} workflow={(wf.slug if wf else EMPTY_STRING)} source={(wf.source_path if wf else EMPTY_STRING)} generated_chars={len(generated)} before_md5={before_md5}', file=sys.stderr, flush=True)
        if not ok or wf is None:
            self.statusBar().showMessage(self._t('status.generate_rebuilt_only', reason=reason), 5000)
            return
        workflow_text = self.workflowPane.editor.toPlainText()
        after_md5 = hashlib.md5(workflow_text.encode('utf-8')).hexdigest()
        try:
            wf.source_path.parent.mkdir(parents=True, exist_ok=True)
            File.writeText(wf.source_path, workflow_text, encoding='utf-8')
            self.compiler.compile_workflow(wf.source_path, wf.compiled_html_path, wf.meta_path)
            print(f'[PROMPT:GENERATE:SAVE-WORKFLOW] path={wf.source_path} chars={len(workflow_text)} before_md5={before_md5} after_md5={after_md5} compiled={wf.compiled_html_path}', file=sys.stderr, flush=True)
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@7529')
            print(f'[PROMPT:GENERATE:SAVE-WORKFLOW-FAILED] path={wf.source_path} {type(exc).__name__}: {exc}\n{traceback.format_exc()}', file=sys.stderr, flush=True)
            self.statusBar().showMessage(self._t('status.generate_save_failed', error=exc), 6000)
            return
        self.workflowDefinitions = discover_workflows(self.paths.workflows)
        self._reload_workflow_selectors()
        self._select_workflow_slug(wf.slug)
        self.load_selected_workflow()
        self.promptHomeActive = False
        self.render_prompt_runtime()
        self.statusBar().showMessage(self._t('status.generate_saved_workflow', name=wf.source_path.name), 4000)

    def save_prompt_from_web(self, title: str, task: str, context: str) -> None:
        self._complete_prompt_save({
            'title': title,
            'task': task,
            'context': context,
        })

    def _complete_prompt_save(self, payload: object) -> None:
        data = payload if isinstance(payload, dict) else {}
        title = str(data.get('title', self.currentPromptTitle)).strip() or self.currentPromptTitle
        task = str(data.get('task', self.currentPromptTask))
        context = str(data.get('context', self.currentPromptText))
        generated_prompt = str(data.get('generated_prompt', context))
        instruction_lines = str(data.get('instruction_lines', ''))
        explicit_bucket, explicit_title = _split_prompt_bucket_title(title)
        current_entry = next((entry for entry in self.promptLibraryEntries if entry.path == self.currentPromptPath), None)
        bucket_name = _normalize_prompt_bucket_name(explicit_bucket or (current_entry.bucket_name if current_entry else ''))
        title_for_doc = title if explicit_bucket else (explicit_title or title)
        path = self.currentPromptPath
        if path is not None and (not _prompt_path_is_under(path, self.paths.root) or not _prompt_target_writable(path)):
            temp_doc = PromptDocument(title=title_for_doc or title or path.stem, task=task, context=context, workflow_slug=self.current_workflow_slug() or '', doctype_name='', bucket_name=bucket_name)
            path = _prompt_sanitize_opened_prompt_save_path(path, temp_doc, self.paths.prompts, self.paths.root)
        if path is None:
            suggested_path = self._suggest_prompt_save_path(title_for_doc, bucket_name)
            file_path, _ = QFileDialog.getSaveFileName(self, self._t('dialog.save_prompt'), str(suggested_path), PROMPT_FILENAME_FILTER)
            if not file_path:
                return
            path = Path(file_path)
        current_doctype = self.current_doctype_path()
        current_workflow = self.current_workflow_definition()
        workflow_title = current_workflow.title if current_workflow else ''
        workflow_source = self.workflowPane.editor.toPlainText() if hasattr(self, 'workflowPane') else ''
        doctype_text = self.doctypePane.editor.toPlainText() if hasattr(self, 'doctypePane') else ''
        doc = PromptDocument(
            title=(title if explicit_bucket else (title_for_doc or path.stem)),
            task=task,
            context=context,
            workflow_slug=self.current_workflow_slug() or '',
            doctype_name=current_doctype.stem if current_doctype else '',
            bucket_name=bucket_name,
        )
        html_export_path = path.with_suffix('.html')
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            File.writeText(path, serialize_prompt_document(doc), encoding='utf-8')
            _prompt_make_user_writable(path)
            html_export = render_saved_prompt_export_html(
                self.paths.root,
                doc,
                generated_prompt=generated_prompt,
                instruction_lines=instruction_lines,
                doctype_name=doc.doctype_name,
                doctype_text=doctype_text,
                workflow_title=workflow_title,
                workflow_slug=doc.workflow_slug,
                workflow_source=workflow_source,
            )
            File.writeText(html_export_path, html_export, encoding='utf-8')
            _prompt_make_user_writable(html_export_path)
        except OSError as exc:
            captureException(None, source='prompt_app.py', context='except@7595')
            self.soundManager.play('error')
            print(f'[PROMPT:SAVE-PROMPT-FAILED] {exc}', file=sys.stderr)
            return
        self.currentPromptPath = path
        self.currentPromptTitle = doc.title
        self.currentPromptTask = doc.task
        self.currentPromptText = doc.context
        self.promptFiles = discover_prompts(self.paths.prompts)
        self._reload_prompt_selector()
        self.soundManager.play('save')
        self.statusBar().showMessage(self._t('status.saved_prompt', prompt_name=path.name, html_name=html_export_path.name), 3000)
        self.promptHomeActive = False
        self.render_prompt_runtime()

    def update_doctype_preview(self) -> None:
        path = self.current_doctype_path()
        if path is None:
            return
        html = render_doctype_preview_html(self.paths.root, path.stem, self.doctypePane.editor.toPlainText())
        target = self.paths.generated / '_doctype_preview.html'
        print(f'[PROMPT:LOAD-TARGET] view=Doctype Preview reason=update_doctype_preview file={target}', file=sys.stderr)
        self._write_html_and_load(self.doctypePane.preview, target, html)

    def update_workflow_preview(self) -> None:
        source = self.current_workflow_source_path()
        wf = self.current_workflow_definition()
        if wf is None or source is None:
            return
        try:
            html = self.compiler.compile_text(self.workflowPane.editor.toPlainText(), source, wf.meta_path)
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@7624')
            print(f"[WARN:swallowed-exception] prompt_app.py:5905 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            html = f'<html><body><pre>{exc}</pre></body></html>'
        html = self._apply_runtime_tokens(html)
        target = wf.folder / '_preview_workflow.html'
        print(f'[PROMPT:LOAD-TARGET] view=Workflow Preview reason=update_workflow_preview file={target}', file=sys.stderr)
        self._write_html_and_load(self.workflowPane.preview, target, html)

    def _apply_runtime_tokens(self, html_text: str) -> str:
        doctype_path = self.current_doctype_path()
        workflow_source = self.workflowPane.editor.toPlainText()

        def replace_runtime_json_token(source_html: str, token: str, value: Any) -> str:
            json_value = json.dumps(value, ensure_ascii=False)
            # WorkflowCompiler stores runtime tokens as JSON string placeholders.
            # Replacing only the bare token turns "__TOKEN__" into ""value"" and
            # breaks PromptRuntimeData, which leaves the generator as inert/raw MD.
            source_html = source_html.replace(f'"{token}"', json_value)
            return source_html.replace(token, json_value)

        replacements = {
            '__PROMPT_RUNTIME_DOCTYPE__': self.doctypePane.editor.toPlainText(),
            '__PROMPT_RUNTIME_DOCTYPE_NAME__': doctype_path.stem if doctype_path else 'doctype',
            '__PROMPT_RUNTIME_OPENED_PROMPT__': self.currentPromptText,
            '__PROMPT_RUNTIME_PREVIOUS_PROMPT__': self.previousPromptText,
            '__PROMPT_RUNTIME_OPENED_TASK__': self.currentPromptTask,
            '__PROMPT_RUNTIME_PROMPT_TITLE__': self.currentPromptTitle,
            '__PROMPT_RUNTIME_WORKFLOW_SOURCE__': workflow_source,
        }
        for token, value in replacements.items():
            html_text = replace_runtime_json_token(html_text, token, value)
        return html_text

    def _prompt_output_only_html(self, html_text: str) -> str:
        raw = _clean_rendered_html_artifacts(str(html_text or EMPTY_STRING))
        title_match = re.search(r'<title>(.*?)</title>', raw, flags=re.IGNORECASE | re.DOTALL)
        page_title = html_lib.escape(re.sub(r'\s+', ' ', str(title_match.group(1) if title_match else 'Generated Prompt')).strip() or 'Generated Prompt')

        def strip_scripts(text: str) -> str:
            return re.sub(r'<script\b[^>]*>.*?</script>', EMPTY_STRING, str(text or EMPTY_STRING), flags=re.IGNORECASE | re.DOTALL)

        script_tags = ''.join(re.findall(r'<script\b[^>]*>.*?</script>', raw, flags=re.IGNORECASE | re.DOTALL))
        head_match = re.search(r'<head\b[^>]*>(.*?)</head>', raw, flags=re.IGNORECASE | re.DOTALL)
        head_inner = str(head_match.group(1) if head_match else EMPTY_STRING)
        head_inner = re.sub(r'<title\b[^>]*>.*?</title>', EMPTY_STRING, head_inner, flags=re.IGNORECASE | re.DOTALL)
        head_inner = strip_scripts(head_inner)
        body_match = re.search(r'<body\b[^>]*>(.*?)</body>', raw, flags=re.IGNORECASE | re.DOTALL)
        body_inner = str(body_match.group(1) if body_match else raw)
        body_inner = strip_scripts(body_inner)
        body_inner = re.sub(
            r'<details\b[^>]*>\s*<summary>\s*<b>Workflow Source</b>\s*</summary>.*?</details>',
            EMPTY_STRING,
            body_inner,
            flags=re.IGNORECASE | re.DOTALL,
        )
        body_inner = re.sub(
            r'<section\b[^>]*class="[^"]*(?:compact-card|workflow-source-card)[^"]*"[^>]*>.*?<summary>\s*<b>Workflow Source</b>\s*</summary>.*?</section>',
            EMPTY_STRING,
            body_inner,
            flags=re.IGNORECASE | re.DOTALL,
        ).strip()

        hidden_fields = """
<div id="prompt-runtime-hidden" aria-hidden="true">
    <input id="promptTitle" type="text" value="">
    <textarea id="userTask"></textarea>
    <textarea id="baseContext"></textarea>
    <textarea id="doctypeText"></textarea>
    <span id="doctypeName"></span>
    <textarea id="extraInstruction"></textarea>
    <pre id="workflowSource"></pre>
    <textarea id="previewList"></textarea>
</div>
"""
        generated_section = """
<section class="prompt-output-surface prompt-floating-output" id="generatedPromptDock">
  <div class="prompt-surface-header prompt-surface-toolbar">
    <strong class="generated-prompt-label">{html_lib.escape(loc.t('prompt.generator.title'))}</strong>
    <button type="button" id="rebuildPromptButton" class="prompt-toolbar-button prompt-primary-button" title="{html_lib.escape(loc.t('prompt.generator.generate_tip'), quote=True)}">{html_lib.escape(loc.t('prompt.generator.generate'))}</button>
    <button type="button" id="copyPromptButton" class="prompt-toolbar-button">{html_lib.escape(loc.t('prompt.generator.copy'))}</button>
    <div class="quick-color-picker">
      <label for="quickColorPicker">{html_lib.escape(loc.t('prompt.generator.color'))}</label>
      <input type="color" id="quickColorPicker" value="#8ecbff" title="{html_lib.escape(loc.t('prompt.generator.color_tip'), quote=True)}">
      <input type="text" id="quickColorHex" value="#8ecbff" spellcheck="false" aria-label="{html_lib.escape(loc.t('prompt.generator.color_tip'), quote=True)}">
      <button type="button" id="copyColorButton" class="prompt-toolbar-button">{html_lib.escape(loc.t('prompt.generator.copy_hex'))}</button>
    </div>
    <div class="small generator-status-hidden" id="generatorStatus" aria-live="polite"></div>
  </div>
  <textarea id="openedPrompt" spellcheck="false" placeholder="{html_lib.escape(loc.t('prompt.generator.placeholder'), quote=True)}"></textarea>
</section>
"""
        loc = self.localization
        previous_prompt_value = html_lib.escape(str(self.previousPromptText or EMPTY_STRING))
        previous_section = EMPTY_STRING
        if self.promptChainActive and str(self.previousPromptText or EMPTY_STRING).strip():
            previous_section = f"""
<section class="prompt-output-surface previous-output-surface">
  <div class="prompt-surface-header">
    <button type="button" id="copyPreviousPromptButton">{html_lib.escape(loc.t('prompt.previous.copy'))}</button>
    <label for="previousPrompt"><b>{html_lib.escape(loc.t('prompt.previous.title'))}</b></label>
  </div>
  <textarea id="previousPrompt" spellcheck="false" placeholder="{html_lib.escape(loc.t('prompt.previous.placeholder'), quote=True)}">{previous_prompt_value}</textarea>
</section>
"""
        body_inner = body_inner + '\n' + generated_section + '\n' + previous_section
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{page_title}</title>
{head_inner}
<style id="prompt-output-only-style">
html, body {{ min-height: 100%; }}
body {{ padding-bottom: 20px; }}
#prompt-runtime-hidden {{ display: none !important; }}
.prompt-output-surface {{ width: 100%; margin-top: 14px; }}
.prompt-surface-header {{ display:flex; align-items:center; gap:10px; margin-bottom:8px; }}
.prompt-surface-header button {{ margin-right: 4px; }}
#openedPrompt, #previousPrompt {{ display:block; width:100%; min-height:220px; box-sizing:border-box; background:#11161c; color:#f3f3f3; border:0; border-radius:0; padding:12px 0; resize:vertical; font:13px/1.55 Consolas,'Courier New',monospace; white-space:pre-wrap; }}
#previousPrompt {{ min-height: 180px; color:#d7e2ee; }}
.prompt-utility-row {{ display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-top:12px; }}
button {{ background:#26374a; color:#f3f3f3; border:1px solid #4e6780; border-radius:8px; padding:8px 12px; cursor:pointer; }}
button:hover {{ background:#304960; }}
.quick-color-picker {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-left:auto; }}
#quickColorHex {{ width:120px; background:#11161c; color:#f3f3f3; border:1px solid #354554; border-radius:8px; padding:8px; font:13px Consolas,'Courier New',monospace; }}
.small {{ color:#aaa; font-size:12px; }}
.missing-handbook-image {{ display:inline-block; padding:6px 10px; border:1px dashed #718096; border-radius:8px; color:#cbd5e1; background:#172033; font:12px Consolas,'Courier New',monospace; }}
.card, .prompt-output-card, .previous-prompt-card {{ border:0 !important; background:transparent !important; box-shadow:none !important; padding-left:0 !important; padding-right:0 !important; }}
.workflow-inline-input {{ min-width: 10ch; }}
.workflow-inline-textarea, .markdown-inline-textarea {{ display:inline-block; min-width:min(100%,38ch); min-height:180px; vertical-align:middle; background:#11161c; color:#f3f3f3; border:1px solid #354554; border-radius:8px; padding:8px; font:13px Consolas,'Courier New',monospace; width:100%; box-sizing:border-box; }}
.workflow-inline-textarea-wide {{ width:100%; }}
.markdown-inline-input {{ display:inline-block; min-width:12ch; background:#11161c; color:#f3f3f3; border:1px solid #354554; border-radius:8px; padding:6px 8px; }}
.workflow-heading-inline-1 {{ font-size:2rem; font-weight:700; }}
.workflow-heading-inline-2 {{ font-size:1.7rem; font-weight:700; }}
.workflow-heading-inline-3 {{ font-size:1.45rem; font-weight:700; }}
.workflow-heading-inline-4 {{ font-size:1.2rem; font-weight:700; }}
.workflow-heading-inline-5 {{ font-size:1.05rem; font-weight:700; }}
.workflow-heading-inline-6 {{ font-size:0.95rem; font-weight:700; text-transform:uppercase; letter-spacing:.04em; }}

/* V131 compact floating generated prompt dock. */
.prompt-page {{ max-width:none !important; margin:0 !important; padding:8px 10px !important; }}
.prompt-page .workflow-runtime-card {{ width:58vw !important; max-width:760px !important; min-width:360px !important; margin:0 !important; padding:8px 10px !important; border-radius:10px !important; border:1px solid rgba(142,203,255,.22) !important; box-shadow:none !important; }}
.prompt-page .workflow-runtime-card h1 {{ margin:0 0 4px !important; font-size:20px !important; line-height:1.15 !important; }}
.prompt-page .workflow-runtime-card > .muted {{ margin:0 0 6px !important; font-size:11px !important; }}
.prompt-page .workflow-runtime-card .prompt-card {{ padding:8px !important; margin:8px 0 !important; border-radius:10px !important; }}
.prompt-page #dynamicControls:empty {{ display:none !important; }}
.prompt-page #userTask {{ min-height:160px !important; height:18vh !important; }}
.prompt-page #baseContext {{ min-height:280px !important; height:30vh !important; }}
.prompt-page #extraInstruction {{ min-height:150px !important; height:16vh !important; }}
.prompt-page .workflow-inline-textarea, .prompt-page .markdown-inline-textarea {{ min-height:180px !important; height:22vh !important; }}
.prompt-page .prompt-output-surface.prompt-floating-output {{ position:fixed !important; top:8px !important; right:10px !important; width:50vw !important; max-width:780px !important; min-width:420px !important; height:80vh !important; min-height:320px !important; max-height:calc(100vh - 18px) !important; z-index:9999 !important; margin:0 !important; padding:5px !important; border:1px solid rgba(142,203,255,.24) !important; border-radius:8px !important; background:rgba(17,24,32,.96) !important; box-shadow:0 8px 24px rgba(0,0,0,.34) !important; overflow:hidden !important; }}
.prompt-page .prompt-output-surface.prompt-floating-output .prompt-surface-toolbar {{ display:flex !important; align-items:center !important; gap:4px !important; flex-wrap:nowrap !important; margin:0 0 4px !important; min-height:28px !important; overflow:hidden !important; }}
.prompt-page .prompt-output-surface.prompt-floating-output .generated-prompt-label {{ flex:0 0 auto !important; white-space:nowrap !important; font-size:12px !important; margin-right:2px !important; }}
.prompt-page .prompt-output-surface.prompt-floating-output button {{ padding:4px 8px !important; border-radius:6px !important; font-size:12px !important; line-height:1.1 !important; white-space:nowrap !important; }}
.prompt-page .prompt-output-surface.prompt-floating-output .quick-color-picker {{ display:flex !important; align-items:center !important; gap:4px !important; flex:0 1 auto !important; min-width:0 !important; margin-left:2px !important; }}
.prompt-page .prompt-output-surface.prompt-floating-output .quick-color-picker label {{ font-size:12px !important; white-space:nowrap !important; }}
.prompt-page .prompt-output-surface.prompt-floating-output #quickColorPicker {{ width:26px !important; height:24px !important; padding:0 !important; border-radius:6px !important; }}
.prompt-page .prompt-output-surface.prompt-floating-output #quickColorHex {{ width:78px !important; padding:4px 5px !important; font-size:12px !important; }}
.prompt-page .prompt-output-surface.prompt-floating-output #generatorStatus {{ display:none !important; }}
.prompt-page .prompt-output-surface.prompt-floating-output #openedPrompt {{ height:calc(100% - 32px) !important; min-height:0 !important; width:100% !important; padding:8px !important; border:1px solid rgba(142,203,255,.18) !important; border-radius:8px !important; color:var(--prompt-generated-color,#8ECBFF) !important; }}
@media (max-width: 900px) {{
  .prompt-page .workflow-runtime-card {{ width:100% !important; max-width:none !important; }}
  .prompt-page .prompt-output-surface.prompt-floating-output {{ position:sticky !important; top:0 !important; right:auto !important; width:100% !important; min-width:0 !important; height:36vh !important; margin-bottom:8px !important; }}
}}
</style>
</head>
<body>
{body_inner}
{hidden_fields}
{script_tags}
</body>
</html>"""

    def _build_live_prompt_generator_html(self, wf: WorkflowDefinition) -> str:
        workflow_source = self.workflowPane.editor.toPlainText()
        doctype_path = self.current_doctype_path()
        loc = self.localization
        runtime_data = {
            'promptTitle': self.currentPromptTitle,
            'task': self.currentPromptTask,
            'context': self.currentPromptText,
            'previousPrompt': self.previousPromptText,
            'doctypeText': self.doctypePane.editor.toPlainText(),
            'doctypeName': doctype_path.stem if doctype_path else 'doctype',
            'workflowSource': workflow_source,
            'workflowSlug': wf.slug,
            'workflowSourcePath': str(wf.source_path),
            'workflowEditorSourcePath': str(self.current_workflow_source_path() or EMPTY_STRING),
            'workflowEditorMd5': hashlib.md5(workflow_source.encode('utf-8')).hexdigest(),
            'workflowBlocks': _workflow_blocks_from_markdown(workflow_source),
            'uiText': self.localization.js_text_map(),
        }
        previous_prompt_value = html_lib.escape(str(self.previousPromptText or EMPTY_STRING))
        previous_section = EMPTY_STRING
        if self.promptChainActive and str(self.previousPromptText or EMPTY_STRING).strip():
            previous_section = f'''
<section class="prompt-output-surface previous-output-surface">
  <div class="prompt-surface-header">
    <button type="button" id="copyPreviousPromptButton">{html_lib.escape(loc.t('prompt.previous.copy'))}</button>
    <label for="previousPrompt"><b>{html_lib.escape(loc.t('prompt.previous.title'))}</b></label>
  </div>
  <textarea id="previousPrompt" spellcheck="false" placeholder="{html_lib.escape(loc.t('prompt.previous.placeholder'), quote=True)}">{previous_prompt_value}</textarea>
</section>
'''
        body = f'''
<section class="prompt-card workflow-runtime-card" data-prompt-runtime="live-generator">
  <h1>{html_lib.escape(wf.title or _friendly_prompt_title(wf.source_path.stem))}</h1>
  <p class="muted">{html_lib.escape(loc.t('prompt.home.workflow_source', source=str(wf.source_path)))}</p>
  <div id="dynamicControls" data-owner="Prompt Pane"></div>
  <section class="prompt-card">
    <label for="promptTitle"><b>{html_lib.escape(loc.t('prompt.form.title'))}</b></label>
    <input id="promptTitle" type="text" value="">
    <label for="userTask"><b>{html_lib.escape(loc.t('prompt.form.task'))}</b></label>
    <textarea id="userTask" rows="8"></textarea>
    <label for="baseContext"><b>{html_lib.escape(loc.t('prompt.form.context'))}</b></label>
    <textarea id="baseContext" rows="14"></textarea>
    <label for="extraInstruction"><b>{html_lib.escape(loc.t('prompt.form.extra_instruction'))}</b></label>
    <textarea id="extraInstruction" rows="7"></textarea>
  </section>
  <textarea id="doctypeText" style="display:none;"></textarea>
  <span id="doctypeName" style="display:none;"></span>
  <pre id="workflowSource" style="display:none;">{html_lib.escape(workflow_source)}</pre>
  <textarea id="previewList" style="display:none;"></textarea>
</section>

<section class="prompt-output-surface prompt-floating-output" id="generatedPromptDock">
  <div class="prompt-surface-header prompt-surface-toolbar">
    <strong class="generated-prompt-label">{html_lib.escape(loc.t('prompt.generator.title'))}</strong>
    <button type="button" id="rebuildPromptButton" class="prompt-toolbar-button prompt-primary-button" title="{html_lib.escape(loc.t('prompt.generator.generate_tip'), quote=True)}">{html_lib.escape(loc.t('prompt.generator.generate'))}</button>
    <button type="button" id="copyPromptButton" class="prompt-toolbar-button">{html_lib.escape(loc.t('prompt.generator.copy'))}</button>
    <div class="quick-color-picker">
      <label for="quickColorPicker">{html_lib.escape(loc.t('prompt.generator.color'))}</label>
      <input type="color" id="quickColorPicker" value="#8ecbff" title="{html_lib.escape(loc.t('prompt.generator.color_tip'), quote=True)}">
      <input type="text" id="quickColorHex" value="#8ecbff" spellcheck="false" aria-label="{html_lib.escape(loc.t('prompt.generator.color_tip'), quote=True)}">
      <button type="button" id="copyColorButton" class="prompt-toolbar-button">{html_lib.escape(loc.t('prompt.generator.copy_hex'))}</button>
    </div>
    <div class="small generator-status-hidden" id="generatorStatus" aria-live="polite"></div>
  </div>
  <textarea id="openedPrompt" spellcheck="false" placeholder="{html_lib.escape(loc.t('prompt.generator.placeholder'), quote=True)}"></textarea>
</section>

{previous_section}
'''
        extra_head = '''
<style id="prompt-generator-runtime-style">
html, body { min-height: 100%; }
body { padding-bottom: 20px; }
.prompt-output-surface { width: 100%; margin-top: 14px; }
.prompt-surface-header { display:flex; align-items:center; gap:10px; margin-bottom:8px; }
.prompt-surface-header button { margin-right: 4px; }
#openedPrompt, #previousPrompt { display:block; width:100%; min-height:220px; box-sizing:border-box; background:#11161c; color:#f3f3f3; border:0; border-radius:0; padding:12px 0; resize:vertical; font:13px/1.55 Consolas,'Courier New',monospace; white-space:pre-wrap; }
#previousPrompt { min-height: 180px; color:#d7e2ee; }
.prompt-utility-row { display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-top:12px; }
.quick-color-picker { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-left:auto; }
#quickColorHex { width:120px; background:#11161c; color:#f3f3f3; border:1px solid #354554; border-radius:8px; padding:8px; font:13px Consolas,'Courier New',monospace; }
.workflow-inline-input { min-width: 10ch; }
.workflow-inline-textarea, .markdown-inline-textarea { display:inline-block; min-width:min(100%,38ch); min-height:180px; vertical-align:middle; background:#11161c; color:#f3f3f3; border:1px solid #354554; border-radius:8px; padding:8px; font:13px Consolas,'Courier New',monospace; width:100%; box-sizing:border-box; }
.workflow-inline-textarea-wide { width:100%; }
.markdown-inline-input { display:inline-block; min-width:12ch; background:#11161c; color:#f3f3f3; border:1px solid #354554; border-radius:8px; padding:6px 8px; }
.workflow-heading-inline-1 { font-size:2rem; font-weight:700; }
.workflow-heading-inline-2 { font-size:1.7rem; font-weight:700; }
.workflow-heading-inline-3 { font-size:1.45rem; font-weight:700; }
.workflow-heading-inline-4 { font-size:1.2rem; font-weight:700; }
.workflow-heading-inline-5 { font-size:1.05rem; font-weight:700; }
.workflow-heading-inline-6 { font-size:0.95rem; font-weight:700; text-transform:uppercase; letter-spacing:.04em; }

/* V131 compact floating generated prompt dock. */
.prompt-page { max-width:none !important; margin:0 !important; padding:8px 10px !important; }
.prompt-page .workflow-runtime-card { width:58vw !important; max-width:760px !important; min-width:360px !important; margin:0 !important; padding:8px 10px !important; border-radius:10px !important; border:1px solid rgba(142,203,255,.22) !important; box-shadow:none !important; }
.prompt-page .workflow-runtime-card h1 { margin:0 0 4px !important; font-size:20px !important; line-height:1.15 !important; }
.prompt-page .workflow-runtime-card > .muted { margin:0 0 6px !important; font-size:11px !important; }
.prompt-page .workflow-runtime-card .prompt-card { padding:8px !important; margin:8px 0 !important; border-radius:10px !important; }
.prompt-page #dynamicControls:empty { display:none !important; }
.prompt-page #userTask { min-height:160px !important; height:18vh !important; }
.prompt-page #baseContext { min-height:280px !important; height:30vh !important; }
.prompt-page #extraInstruction { min-height:150px !important; height:16vh !important; }
.prompt-page .workflow-inline-textarea, .prompt-page .markdown-inline-textarea { min-height:180px !important; height:22vh !important; }
.prompt-page .prompt-output-surface.prompt-floating-output { position:fixed !important; top:8px !important; right:10px !important; width:50vw !important; max-width:780px !important; min-width:420px !important; height:80vh !important; min-height:320px !important; max-height:calc(100vh - 18px) !important; z-index:9999 !important; margin:0 !important; padding:5px !important; border:1px solid rgba(142,203,255,.24) !important; border-radius:8px !important; background:rgba(17,24,32,.96) !important; box-shadow:0 8px 24px rgba(0,0,0,.34) !important; overflow:hidden !important; }
.prompt-page .prompt-output-surface.prompt-floating-output .prompt-surface-toolbar { display:flex !important; align-items:center !important; gap:4px !important; flex-wrap:nowrap !important; margin:0 0 4px !important; min-height:28px !important; overflow:hidden !important; }
.prompt-page .prompt-output-surface.prompt-floating-output .generated-prompt-label { flex:0 0 auto !important; white-space:nowrap !important; font-size:12px !important; margin-right:2px !important; }
.prompt-page .prompt-output-surface.prompt-floating-output button { padding:4px 8px !important; border-radius:6px !important; font-size:12px !important; line-height:1.1 !important; white-space:nowrap !important; }
.prompt-page .prompt-output-surface.prompt-floating-output .quick-color-picker { display:flex !important; align-items:center !important; gap:4px !important; flex:0 1 auto !important; min-width:0 !important; margin-left:2px !important; }
.prompt-page .prompt-output-surface.prompt-floating-output .quick-color-picker label { font-size:12px !important; white-space:nowrap !important; }
.prompt-page .prompt-output-surface.prompt-floating-output #quickColorPicker { width:26px !important; height:24px !important; padding:0 !important; border-radius:6px !important; }
.prompt-page .prompt-output-surface.prompt-floating-output #quickColorHex { width:78px !important; padding:4px 5px !important; font-size:12px !important; }
.prompt-page .prompt-output-surface.prompt-floating-output #generatorStatus { display:none !important; }
.prompt-page .prompt-output-surface.prompt-floating-output #openedPrompt { height:calc(100% - 32px) !important; min-height:0 !important; width:100% !important; padding:8px !important; border:1px solid rgba(142,203,255,.18) !important; border-radius:8px !important; color:var(--prompt-generated-color,#8ECBFF) !important; }
@media (max-width: 900px) {
  .prompt-page .workflow-runtime-card { width:100% !important; max-width:none !important; }
  .prompt-page .prompt-output-surface.prompt-floating-output { position:sticky !important; top:0 !important; right:auto !important; width:100% !important; min-width:0 !important; height:36vh !important; margin-bottom:8px !important; }
}
</style>
'''
        return _prompt_html_shell(self.paths.root, wf.title or _friendly_prompt_title(wf.source_path.stem), body, runtime_data=runtime_data, extra_head=extra_head)

    def render_prompt_runtime(self) -> None:
        wf = self.current_workflow_definition()
        if wf is None:
            self._write_html_and_load(self.promptPane.web, self.paths.generated / '_prompt_empty.html', self._t('prompt.empty_workflow_html'))
            return
        workflow_source = self.workflowPane.editor.toPlainText()
        try:
            preview_html = self.compiler.compile_text(workflow_source, wf.source_path, wf.meta_path)
            preview_html = self._apply_runtime_tokens(preview_html)
            File.writeText(wf.compiled_html_path, preview_html, encoding='utf-8')
        except OSError as exc:
            captureException(None, source='prompt_app.py', context='except@7930')
            print(f'[PROMPT:RENDER-RUNTIME-PREVIEW-WRITE-FAILED] {exc}', file=sys.stderr, flush=True)
        html = self._build_live_prompt_generator_html(wf)
        runtime_path = wf.folder / '_runtime_prompt.html'
        print(
            f'[PROMPT:LOAD-TARGET] view=Prompt Pane reason=render_prompt_runtime file={runtime_path} '
            f'workflowChars={len(workflow_source)} runtimeJson=True generatorHtml=True',
            file=sys.stderr,
            flush=True,
        )
        self._write_html_and_load(self.promptPane.web, runtime_path, html)



def build_default_paths(root: Path) -> AppPaths:
    bundle_root = _discover_prompt_bundle_root(Path(root))
    if _prompt_should_use_user_runtime_root(bundle_root):
        runtime_root = _prompt_user_data_root()
        _seed_prompt_runtime_root(bundle_root, runtime_root)
    else:
        runtime_root = bundle_root
        runtime_root.mkdir(parents=True, exist_ok=True)
        (runtime_root / 'generated').mkdir(parents=True, exist_ok=True)
        _seed_prompt_embedded_runtime_files(runtime_root, overwrite_static=False)
        _prompt_make_tree_user_writable(runtime_root)
        _ensure_first_run_workflow_html(runtime_root)
    _trace_event('PROMPT:PATHS:RUNTIME-ROOT', f'root={runtime_root} bundle={bundle_root} frozen={_is_frozen_runtime()} errors={_prompt_errors_log_path()} writable={_prompt_directory_writable(runtime_root)}')
    return AppPaths(root=runtime_root, workflows=runtime_root / 'workflows', doctypes=runtime_root / 'doctypes', prompts=runtime_root / 'prompts', generated=runtime_root / 'generated', fonts=runtime_root / 'fonts', assets=runtime_root / 'assets')


def _prompt_webengine_selftest_requested() -> bool:
    return bool(PROMPT_WEBENGINE_SELFTEST_ACTIVE or str(os.environ.get('PROMPT_WEBENGINE_SELFTEST', EMPTY_STRING) or EMPTY_STRING).strip().lower() in {'1', 'true', 'yes', 'on'})


def _schedule_prompt_webengine_selftest(app: QApplication, lifecycle: ApplicationLifecycleController, runtime: dict[str, Any]) -> None:  # noqa: nonconform reviewed return contract
    if not _prompt_webengine_selftest_requested():
        return

    def _begin_selftest() -> None:  # noqa: nonconform reviewed return contract
        failures: list[str] = []
        results: dict[str, dict[str, object]] = {}
        pending: set[str] = set()
        finalized = {'done': False}

        def _view_url_text(view: object) -> str:
            try:
                return cast(Any, view).url().toString() if hasattr(view, 'url') else EMPTY_STRING
            except Exception:
                captureException(None, source='prompt_app.py', context='except@7975')
                return EMPTY_STRING

        def _is_blank_html(value: object) -> bool:
            text = str(value or EMPTY_STRING)
            normalized = text.strip().lower().replace(' ', '')
            return not text.strip() or normalized in {'<html><head></head><body></body></html>', '<html><body></body></html>'}

        def _finish() -> None:
            if finalized['done']:
                return
            finalized['done'] = True
            try:
                for name, info in sorted(results.items()):
                    _trace_event('PROMPT:WEBENGINE-SELFTEST:DOM', f'name={name} {json.dumps(info, sort_keys=True, default=str)}')
                    if bool(info.get('blank', True)):
                        failures.append(f'{name}:blank-dom')
                    if not bool(info.get('cached_ok', False)):
                        failures.append(f'{name}:no-cached-html')
                    if info.get('load_ok') is False:
                        failures.append(f'{name}:load-failed')
                if pending:
                    failures.extend([f'{name}:dom-timeout' for name in sorted(pending)])
                runtime['exit_code'] = 1 if failures else 0
                _trace_event('PROMPT:WEBENGINE-SELFTEST:RESULT', f'pass={not failures} failures={failures}')
            finally:
                app.quit()

        try:
            window = runtime.get('window')
            if window is None:
                failures.append('window-missing')
                _trace_event('PROMPT:WEBENGINE-SELFTEST:RESULT', f'pass=False failures={failures}')
                runtime['exit_code'] = 1
                app.quit()
                return
            checks = (
                ('promptPane', getattr(getattr(window, 'promptPane', None), 'web', None)),
                ('doctypePane', getattr(getattr(window, 'doctypePane', None), 'web', None)),
                ('workflowPane', getattr(getattr(window, 'workflowPane', None), 'web', None)),
                ('aboutView', getattr(getattr(window, 'helpPane', None), 'aboutView', None)),
            )
            for name, view in checks:
                if view is None:
                    results[name] = {'missing': True, 'blank': True, 'cached_ok': False, 'load_ok': False}
                    continue
                cached = str(getattr(view, '_lastHtmlText', EMPTY_STRING) or EMPTY_STRING)
                base_info: dict[str, object] = {
                    'class': view.__class__.__name__,
                    'url': _view_url_text(view),
                    'cached_chars': len(cached),
                    'cached_md5': _md5_for_bytes(cached.encode('utf-8', errors='replace')),
                    'cached_ok': bool(cached.strip()),
                    'load_ok': bool(getattr(view, '_lastLoadFinishedOk', True)),
                    'recovery': bool(getattr(view, '_blankRecoveryAttempted', False)),
                }
                page = cast(Any, view).page() if hasattr(view, 'page') else None
                if page is not None and hasattr(page, 'toHtml'):
                    pending.add(name)
                    def _callback(html_text: object, view_name: str = name, info: dict[str, object] = base_info) -> None:
                        dom_text = str(html_text or EMPTY_STRING)
                        pending.discard(view_name)
                        info.update({
                            'dom_chars': len(dom_text),
                            'dom_md5': _md5_for_bytes(dom_text.encode('utf-8', errors='replace')),
                            'blank': _is_blank_html(dom_text),
                            'has_dynamicControls': 'id="dynamicControls"' in dom_text or "id='dynamicControls'" in dom_text,
                            'has_runtimeData': 'PromptRuntimeData' in dom_text,
                            'has_body': '<body' in dom_text.lower(),
                        })
                        results[view_name] = info
                        if not pending:
                            _finish()
                    try:
                        page.toHtml(_callback)
                    except Exception as exc:
                        captureException(None, source='prompt_app.py', context='except@8050')
                        base_info.update({'blank': True, 'toHtml_error': f'{type(exc).__name__}: {exc}'})
                        results[name] = base_info
                        pending.discard(name)
                else:
                    base_info.update({'dom_chars': len(cached), 'blank': _is_blank_html(cached), 'fallback_view': True})
                    results[name] = base_info
            if not pending:
                _finish()
                return
            QTimer.singleShot(5000, _finish)
        except Exception as exc:
            captureException(None, source='prompt_app.py', context='except@8061')
            _log_exception('PROMPT:WEBENGINE-SELFTEST:FAILED', exc, include_traceback=True)
            runtime['exit_code'] = 1
            app.quit()

    delay_ms = max(1000, int(str(os.environ.get('PROMPT_WEBENGINE_SELFTEST_DELAY_MS', '3500') or '3500').strip() or '3500'))
    lifecycle.scheduleCallback(PHASE_MAIN_BOOT + 60, 'Prompt WebEngine Selftest', delay_ms, _begin_selftest, ttl=10)


def main() -> int:  # phase-hooks-ok main delegates into Prompt lifecycle/CLI phase runner
    _install_prompt_error_log_streams()
    _trace_event('PROMPT:STARTUP', f'patch={PROMPT_EMBEDDED_RUNTIME_PATCH} webengine_mode={PROMPT_WEBENGINE_LOAD_MODE} selftest={_prompt_webengine_selftest_requested()} errors={_prompt_errors_log_path()} debug_log={DebugLog.debugLogPath()}')
    _trace_prompt_runtime_environment('main-entry')
    root = _discover_prompt_bundle_root(Path(__file__).resolve().parent)
    paths = build_default_paths(root)
    _trace_prompt_runtime_environment('after-build-paths', bundle_root=root, runtime_root=paths.root)
    if not bool(globals().get('HAS_SQLALCHEMY', False)):
        message = f'Prompt cannot open the GUI because SQLAlchemy is missing. Install SQLAlchemy or launch through start.py so dependencies can be repaired. error={SQLALCHEMY_IMPORT_ERROR!r}'
        DebugLog.trace(message, level='FATAL', source='prompt_app.py', stream='stderr')
        captureException(SQLALCHEMY_IMPORT_ERROR, source='prompt_app.py', context='main:missing-sqlalchemy-before-qapplication', handled=False, extra=message)
        return 2
    DebugLog.trace('QAPPLICATION CREATE BEGIN', level='TRACE', source='prompt_app.py')
    app = QApplication(sys.argv)
    store = None
    try:
        store = prompt_orm_store(_settings_db_path(root))
    except Exception as exc:
        captureException(None, source='prompt_app.py', context='except@8081')
        print(f"[WARN:swallowed-exception] prompt_app.py:6062 {type(exc).__name__}: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        _log_exception('PROMPT:LIFECYCLE-STORE-INIT-FAILED', exc, include_traceback=False)
    DebugLog.trace('APPLICATION LIFECYCLE CONTROLLER CREATE BEGIN', level='TRACE', source='prompt_app.py')
    lifecycle = ApplicationLifecycleController(app, store=store)
    runtime: dict[str, Any] = {'window': None, 'exit_code': 0}

    def create_window() -> None:
        DebugLog.trace('MAIN WINDOW CREATE BEGIN', level='TRACE', source='prompt_app.py')
        runtime['window'] = PromptMainWindow(paths, app_lifecycle=lifecycle)
        DebugLog.trace('MAIN WINDOW CREATE COMPLETE', level='TRACE', source='prompt_app.py')

    def show_window() -> None:
        window = runtime.get('window')
        if window is not None:
            DebugLog.trace('MAIN WINDOW SHOW BEGIN', level='TRACE', source='prompt_app.py')
            window.show()
            DebugLog.trace('MAIN WINDOW SHOW COMPLETE', level='TRACE', source='prompt_app.py')

    def maintain_splitter() -> None:
        window = runtime.get('window')
        if window is not None:
            window._maintain_even_main_splitter()

    def configure_auto_screenshot() -> None:  # noqa: nonconform reviewed return contract
        window = runtime.get('window')
        if window is None:
            return
        screenshot_path = str(os.environ.get('PROMPT_AUTO_SCREENSHOT_PATH', EMPTY_STRING) or EMPTY_STRING).strip()
        if not screenshot_path:
            return
        screenshot_delay = max(500, int(str(os.environ.get('PROMPT_AUTO_SCREENSHOT_DELAY_MS', '3000') or '3000').strip() or '3000'))
        auto_exit = str(os.environ.get('PROMPT_AUTO_EXIT_AFTER_SCREENSHOT', EMPTY_STRING) or EMPTY_STRING).strip().lower() in {'1', 'true', 'yes', 'on'}

        def capture_window() -> None:  # noqa: nonconform reviewed return contract
            target = Path(screenshot_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            for _ in range(3):
                app.processEvents()
            window.raise_()
            window.activateWindow()
            try:
                _ = int(window.winId())
            except Exception as error:
                captureException(None, source='prompt_app.py', context='except@8119')
                print(f"[WARN:swallowed-exception] prompt_app.py:6099 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                pass

            def sample_score(pixmap) -> float:
                if pixmap is None or pixmap.isNull():
                    return -1.0
                try:
                    image = pixmap.toImage().convertToFormat(pixmap.toImage().Format.Format_RGB32)
                except Exception as error:
                    captureException(None, source='prompt_app.py', context='except@8128')
                    print(f"[WARN:swallowed-exception] prompt_app.py:6107 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    try:
                        image = pixmap.toImage()
                    except Exception as error:
                        captureException(None, source='prompt_app.py', context='except@8132')
                        print(f"[WARN:swallowed-exception] prompt_app.py:6110 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                        return -1.0
                width = max(1, int(image.width()))
                height = max(1, int(image.height()))
                step_x = max(1, width // 24)
                step_y = max(1, height // 24)
                total = 0.0
                samples = 0
                for y in range(0, height, step_y):
                    for x in range(0, width, step_x):
                        color = image.pixelColor(x, y)
                        total += float(color.red()) + float(color.green()) + float(color.blue())
                        samples += 1
                return total / float(samples or 1)

            def region_score(pixmap, x: int, y: int, width: int, height: int) -> float:
                if pixmap is None or pixmap.isNull() or width <= 0 or height <= 0:
                    return -1.0
                return sample_score(pixmap.copy(int(x), int(y), int(width), int(height)))

            def root_crop(widget):
                screen = window.screen() or QApplication.primaryScreen()
                if screen is None or widget is None or widget.width() <= 0 or widget.height() <= 0:
                    return None, QPoint(0, 0)
                local_top_left = widget.mapTo(window, QPoint(0, 0))
                global_top_left = window.mapToGlobal(local_top_left)
                pixmap = screen.grabWindow(0, global_top_left.x(), global_top_left.y(), widget.width(), widget.height())
                return pixmap, local_top_left

            screen = window.screen() or QApplication.primaryScreen()
            base = screen.grabWindow(int(window.winId())) if screen is not None else QPixmap()
            base_method = 'screen-window-id' if base is not None and not base.isNull() else 'window-grab'
            if base is None or base.isNull():
                base = window.grab()
            if base is None or base.isNull():
                base = QPixmap()

            overlays_applied = []
            for pane_name in ('promptPane', 'doctypePane', 'workflowPane'):
                pane = getattr(window, pane_name, None)
                preview_widget = getattr(pane, 'web', None) or getattr(pane, 'preview', None)
                if preview_widget is None:
                    continue
                local_pos = preview_widget.mapTo(window, QPoint(0, 0))
                existing_score = region_score(base, local_pos.x(), local_pos.y(), preview_widget.width(), preview_widget.height())
                if existing_score >= 18.0:
                    continue
                overlay, draw_pos = root_crop(preview_widget)
                overlay_score = sample_score(overlay)
                if overlay is None or overlay.isNull() or overlay_score <= max(existing_score + 5.0, 8.0):
                    continue
                painter = QPainter(base)
                painter.drawPixmap(draw_pos, overlay)
                painter.end()
                overlays_applied.append(f'{pane_name}:{overlay_score:.1f}')

            saved = False if base.isNull() else bool(base.save(str(target)))
            overlay_text = ','.join(overlays_applied) if overlays_applied else 'none'
            print(f'[PROMPT:AUTO-SCREENSHOT] saved={saved} path={target} method={base_method} overlays={overlay_text} score={sample_score(base):.2f}', file=sys.stderr, flush=True)
            if auto_exit:
                lifecycle.scheduleCallback(PHASE_MAIN_BOOT + 50, 'Auto Exit After Screenshot', 250, app.quit, ttl=5)

        lifecycle.scheduleCallback(PHASE_MAIN_BOOT + 40, 'Auto Screenshot Capture', screenshot_delay, capture_window, ttl=30)

    def run_event_loop() -> int:
        DebugLog.trace('QT EVENT LOOP BEGIN', level='TRACE', source='prompt_app.py')
        runtime['exit_code'] = int(Lifecycle.runQtBlockingCall(app, phase_name='prompt.application.event-loop'))
        DebugLog.trace('QT EVENT LOOP EXIT code=' + str(runtime.get('exit_code') or 0), level='TRACE', source='prompt_app.py')
        return int(runtime.get('exit_code') or 0)

    lifecycle.registerCallablePhase(PHASE_MAIN_BOOT + 1, 'Create QApplication Main Window', create_window, ttl=60)
    lifecycle.registerCallablePhase(PHASE_MAIN_BOOT + 2, 'Show Prompt Main Window', show_window, ttl=30)
    lifecycle.registerCallablePhase(PHASE_MAIN_BOOT + 3, 'Configure Auto Screenshot', configure_auto_screenshot, ttl=30)
    lifecycle.runPhase(PHASE_MAIN_BOOT + 1)
    lifecycle.runPhase(PHASE_MAIN_BOOT + 2)
    lifecycle.scheduleCallback(PHASE_MAIN_BOOT + 4, 'Maintain Main Splitter After Show', 0, maintain_splitter, ttl=5)
    lifecycle.runPhase(PHASE_MAIN_BOOT + 3)
    _schedule_prompt_webengine_selftest(app, lifecycle, runtime)
    lifecycle.registerCallablePhase(PHASE_MAIN_BOOT + 99, 'Qt Application Event Loop', run_event_loop, ttl=0)
    lifecycle.runPhase(PHASE_MAIN_BOOT + 99)
    return int(runtime.get('exit_code') or 0)


if __name__ == '__main__':
    _prompt_exit_code = int(main() or 0)
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception as error:
        captureException(None, source='prompt_app.py', context='except@8219')
        print(f"[WARN:swallowed-exception] prompt_app.py:6195 {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        pass
    os._exit(_prompt_exit_code)  # lifecycle-ok: final Prompt app hard exit after main cleanup and stream flush
