from __future__ import annotations

import ctypes
import inspect
import json
import linecache
import os
import subprocess
import sys
import threading  # thread-ok
import time
import traceback
from pathlib import Path
from typing import Any, Callable


_RAW_SUBPROCESS_POPEN = subprocess.Popen
_RAW_SUBPROCESS_RUN = subprocess.run


WINDOW_POLICY_HELPER = 'helper-no-window'
WINDOW_POLICY_QT_CHILD = 'qt-child'
WINDOW_POLICY_ALLOW = 'allow-window'
WINDOW_MONITOR_DISABLE_FLAGS = {
    '--allow-new-windows', '--no-window-monitor', '--window-monitor-off',
    'allow-new-windows', 'no-window-monitor', 'window-monitor-off',
}
WINDOW_MONITOR_OFF_VALUES = {'0', 'false', 'no', 'off', 'disabled'}
WINDOW_MONITOR_FAIL_EXIT_CODE = 96


def _process_log_verbose() -> bool:
    try:
        value = str(os.environ.get('PROMPT_VERBOSE_PROCESS_LOGS', '') or os.environ.get('PROMPT_VERBOSE_BUILD_LOG', '') or '').strip().lower()
        return value in {'1', 'true', 'yes', 'on', 'verbose', 'debug'}
    except Exception as error:
        try:
            sys.__stderr__.write(f'[WARN:PhaseProcess] process verbose env parse failed {type(error).__name__}: {error}\n')
            sys.__stderr__.flush()
        except BaseException:
            raise
        return False


def _process_log_quiet_ok() -> bool:
    try:
        value = str(os.environ.get('PROMPT_QUIET_PROCESS_OK', '1') or '1').strip().lower()
        return value not in {'0', 'false', 'no', 'off'}
    except Exception as error:
        try:
            sys.__stderr__.write(f'[WARN:PhaseProcess] process quiet env parse failed {type(error).__name__}: {error}\n')
            sys.__stderr__.flush()
        except BaseException:
            raise
        return True


def recordException(context: str, error: BaseException | None = None, *, handled: bool = True, source: str = 'PhaseProcess.py') -> int:
    """Module-level fallback so early helpers can surface failures before PhaseProcess exists."""
    exc = error if error is not None else sys.exc_info()[1]
    try:
        phase_type = globals().get('PhaseProcess')
        phase_recorder = getattr(phase_type, 'recordException', None) if phase_type is not None else None
        if callable(phase_recorder) and phase_recorder is not recordException:
            return int(phase_recorder(exc, source=source, context=context, handled=handled) or 0)
    except Exception as nested_error:
        try:
            sys.__stderr__.write(f'[WARN:PhaseProcess.recordException] delegate failed {type(nested_error).__name__}: {nested_error}\n')
            sys.__stderr__.flush()
        except BaseException:
            raise
    try:
        sys.__stderr__.write(f'[CAPTURED-EXCEPTION] source={source} context={context} handled={int(bool(handled))} type={type(exc).__name__}: {exc}\n')
        if exc is not None:
            sys.__stderr__.write(''.join(traceback.format_exception(type(exc), exc, getattr(exc, '__traceback__', None))))
        sys.__stderr__.flush()
    except BaseException:
        raise
    return 0


def _command_text(command: Any) -> str:
    try:
        if isinstance(command, (list, tuple)):
            return subprocess.list2cmdline([str(part) for part in command])
        return str(command)
    except Exception as error:
        recordException('format-command', error, handled=True, source='PhaseProcess._command_text')
        return repr(command)


def _window_monitor_enabled() -> bool:
    if os.name != 'nt':
        return False
    try:
        env_value = str(os.environ.get('PROMPT_WINDOW_MONITOR', os.environ.get('FLATLINE_WINDOW_MONITOR', '1')) or '1').strip().lower()
        fail_value = str(os.environ.get('PROMPT_FAIL_ON_DEAD_WINDOW', os.environ.get('FLATLINE_FAIL_ON_DEAD_WINDOW', '1')) or '1').strip().lower()
        if env_value in WINDOW_MONITOR_OFF_VALUES or fail_value in WINDOW_MONITOR_OFF_VALUES:
            return False
        tokens = {str(arg or '').strip().lower() for arg in sys.argv[1:]}
        return not bool(tokens.intersection(WINDOW_MONITOR_DISABLE_FLAGS))
    except Exception as error:
        recordException('env/argv', error, handled=True, source='PhaseProcess.windowMonitorEnabled')
        return True


def _normalize_window_policy(policy: object = None, command: object = None) -> str:
    raw = str(policy or '').strip().lower().replace('_', '-').replace(' ', '-')
    if raw in {'allow', 'allowed', 'allow-window', 'window-ok', 'gui-ok'}:
        return WINDOW_POLICY_ALLOW
    if raw in {'qt', 'qt-child', 'gui', 'gui-child', 'python-gui'}:
        return WINDOW_POLICY_QT_CHILD
    if raw in {'helper', 'console-helper', 'no-window', 'helper-no-window', 'probe'}:
        return WINDOW_POLICY_HELPER
    try:
        parts = [str(part or '').replace('\\', '/').lower() for part in (command if isinstance(command, (list, tuple)) else [command])]
        command_text = ' '.join(parts)
        if any(token in command_text for token in ('prompt_app.py', 'trio.py', 'cutie.py', 'gtp.py', 'operator_client.py', 'operator-client.py')):
            return WINDOW_POLICY_QT_CHILD
    except Exception as error:
        recordException('command auto-detect', error, handled=True, source='PhaseProcess.normalizeWindowPolicy')
    return WINDOW_POLICY_HELPER


class WindowRow:
    __slots__ = ('hwnd', 'pid', 'title', 'className', 'visible')

    def __init__(self, hwnd: int, pid: int, title: str, className: str, visible: bool):
        self.hwnd = int(hwnd or 0)
        self.pid = int(pid or 0)
        self.title = str(title or '')
        self.className = str(className or '')
        self.visible = bool(visible)  # noqa: nonconform

    def toDict(self) -> dict[str, object]:  # noqa: N802
        return {
            'hwnd': int(self.hwnd or 0),
            'pid': int(self.pid or 0),
            'title': self.title,
            'className': self.className,
            'visible': bool(self.visible),
        }

    def describe(self) -> str:
        return json.dumps(self.toDict(), sort_keys=True, ensure_ascii=False)


class DeadWindowMonitor:
    """Fail-fast monitor for unexpected visible Windows helper windows.

    This is not a janitor loop.  It snapshots visible windows before a spawn,
    checks shortly after launch, logs the exact opener and command, kills the
    offending PID once, and exits nonzero so the launch site can be fixed.
    """

    CONSOLE_CLASSES = {'consolewindowclass'}
    PYTHON_TITLE_TOKENS = ('python', 'python.exe', 'pythonw.exe', 'py.exe')
    ALLOWED_QT_CLASS_TOKENS = ('qt', 'qwindow', 'qwidget')

    def __init__(self) -> None:
        self.currentPid = os.getpid()  # noqa: nonconform

    def snapshot(self) -> dict[int, WindowRow]:
        if not _window_monitor_enabled():
            return {}
        rows: dict[int, WindowRow] = {}
        try:
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)  # type: ignore[attr-defined]

            def _callback(hwnd, _lparam):
                try:
                    if not bool(user32.IsWindowVisible(hwnd)):
                        return True
                    pid_value = ctypes.c_ulong(0)
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_value))
                    title_buffer = ctypes.create_unicode_buffer(512)
                    class_buffer = ctypes.create_unicode_buffer(256)
                    user32.GetWindowTextW(hwnd, title_buffer, 512)
                    user32.GetClassNameW(hwnd, class_buffer, 256)
                    row = WindowRow(int(hwnd or 0), int(pid_value.value or 0), title_buffer.value, class_buffer.value, True)
                    if row.hwnd:
                        rows[row.hwnd] = row
                except Exception as error:
                    PhaseProcess.recordException(error, source='DeadWindowMonitor.snapshot', context='enum-callback', handled=True)
                    return True
                return True

            user32.EnumWindows(enum_proc_type(_callback), 0)
        except Exception as error:
            PhaseProcess.recordException(error, source='DeadWindowMonitor.snapshot', context='EnumWindows', handled=True)
        return rows

    def _isConsoleWindow(self, row: WindowRow) -> bool:  # noqa: N802
        return str(row.className or '').strip().lower() in self.CONSOLE_CLASSES

    def _isLikelyPythonWindow(self, row: WindowRow) -> bool:  # noqa: N802
        title = str(row.title or '').strip().lower()
        return self._isConsoleWindow(row) and (not title or any(token in title for token in self.PYTHON_TITLE_TOKENS))

    def _isAllowedQtWindow(self, row: WindowRow, *, launchedPid: int, policy: str) -> bool:  # noqa: N802
        if policy != WINDOW_POLICY_QT_CHILD:
            return False
        if int(launchedPid or 0) <= 0 or int(row.pid or 0) != int(launchedPid or 0):
            return False
        if self._isConsoleWindow(row):
            return False
        class_name = str(row.className or '').lower()
        return bool(class_name) or any(token in class_name for token in self.ALLOWED_QT_CLASS_TOKENS)

    def unexpectedWindows(self, before: dict[int, WindowRow], *, launchedPid: int = 0, policy: str = WINDOW_POLICY_HELPER) -> list[WindowRow]:  # noqa: N802
        if not _window_monitor_enabled():
            return []
        after = self.snapshot()
        unexpected: list[WindowRow] = []
        normalized = _normalize_window_policy(policy)
        for hwnd, row in after.items():
            if hwnd in before or row.pid == self.currentPid or normalized == WINDOW_POLICY_ALLOW:
                continue
            if self._isAllowedQtWindow(row, launchedPid=int(launchedPid or 0), policy=normalized):
                continue
            if normalized == WINDOW_POLICY_QT_CHILD:
                if self._isLikelyPythonWindow(row):
                    unexpected.append(row)
                continue
            unexpected.append(row)
        return unexpected

    def checkAfterLaunch(self, before: dict[int, WindowRow], *, launchedPid: int = 0, command: object = None, policy: str = WINDOW_POLICY_HELPER, sourceLine: str = '', delaySeconds: float = 0.7) -> None:  # noqa: N802
        if not _window_monitor_enabled():
            return
        try:
            time.sleep(max(0.05, float(delaySeconds or 0.7)))
        except Exception as error:
            PhaseProcess.recordException(error, source='DeadWindowMonitor.checkAfterLaunch', context='delay', handled=True)
        unexpected = self.unexpectedWindows(before, launchedPid=int(launchedPid or 0), policy=policy)
        if not unexpected:
            if _process_log_verbose():
                PhaseProcess._debug_line(f'WINDOW-MONITOR:OK policy={_normalize_window_policy(policy, command)} pid={int(launchedPid or 0)} opener={sourceLine or "unknown"}')
            return
        command_text = _command_text(command)
        PhaseProcess._debug_line(f'WINDOW-MONITOR:FAILED unexpected visible window(s) count={len(unexpected)} policy={_normalize_window_policy(policy, command)} launchedPid={int(launchedPid or 0)}', level='ERROR')
        PhaseProcess._debug_line(f'WINDOW-MONITOR:OPENER {sourceLine or "unknown launcher callsite"}', level='ERROR')
        PhaseProcess._debug_line(f'WINDOW-MONITOR:COMMAND {command_text}', level='ERROR')
        killed: set[int] = set()
        for row in unexpected:
            PhaseProcess._debug_line(f'WINDOW-MONITOR:WINDOW {row.describe()}', level='ERROR')
            if int(row.pid or 0) > 0 and int(row.pid or 0) not in killed and int(row.pid or 0) != os.getpid():
                killed.add(int(row.pid or 0))
                self._killPid(int(row.pid or 0), reason=f'unexpected visible window class={row.className!r} title={row.title!r}')
        if int(launchedPid or 0) > 0 and int(launchedPid or 0) not in killed and int(launchedPid or 0) != os.getpid():
            self._killPid(int(launchedPid or 0), reason='launcher command opened unexpected visible window')
        PhaseProcess._debug_line(f'WINDOW-MONITOR:EXIT code={WINDOW_MONITOR_FAIL_EXIT_CODE}; fail-fast so the bad spawn gets fixed', level='ERROR')
        try:
            sys.stdout.flush(); sys.stderr.flush()
        except Exception as error:
            PhaseProcess.recordException(error, source='DeadWindowMonitor.checkAfterLaunch', context='flush-before-exit', handled=True)
        os._exit(WINDOW_MONITOR_FAIL_EXIT_CODE)

    def _killPid(self, pid: int, *, reason: str) -> bool:  # noqa: N802
        pid_value = int(pid or 0)
        if pid_value <= 0 or pid_value == os.getpid():
            return False
        PhaseProcess._debug_line(f'WINDOW-MONITOR:KILL pid={pid_value} reason={reason}', level='WARN')
        try:
            if os.name == 'nt':
                command = ['taskkill', '/PID', str(pid_value), '/T', '/F']
                startupinfo = None
                creationflags = int(getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000) or 0)
                try:
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= int(getattr(subprocess, 'STARTF_USESHOWWINDOW', 1) or 1)
                    startupinfo.wShowWindow = 0
                except Exception as error:
                    recordException('taskkill-startupinfo', error, handled=True, source='DeadWindowMonitor.killPid')
                    startupinfo = None
                completed = _RAW_SUBPROCESS_RUN(command, capture_output=True, text=True, timeout=8, check=False, stdin=subprocess.DEVNULL, startupinfo=startupinfo, creationflags=creationflags)
                output = str(getattr(completed, 'stdout', '') or '') + str(getattr(completed, 'stderr', '') or '')
                for line in output.splitlines():
                    if line.strip():
                        PhaseProcess._debug_line(f'WINDOW-MONITOR:TASKKILL {line.strip()}')
                return int(getattr(completed, 'returncode', 1) or 0) == 0
            os.kill(pid_value, 9)
            return True
        except Exception as error:
            PhaseProcess.recordException(error, source='DeadWindowMonitor.killPid', context=str(pid_value), handled=True)
            return False


DEAD_WINDOW_MONITOR = DeadWindowMonitor()


class PhaseProcess:
    """One lifecycle-visible wrapper for subprocess and thread launch points.

    All Prompt subprocesses should pass through this class.  On Windows the
    default is deliberately boring: no new console window, no ShellExecute, no
    inherited invalid stdio handles from a windowed parent, and a caller file:line
    trace before the process starts.  If a build tool still opens a GUI/dead
    window after this, the trace shows the exact command and the exact code line
    that launched it.
    """

    @staticmethod
    def recordException(exc: BaseException | None = None, *, source: str = 'PhaseProcess', context: str = '', handled: bool = True) -> int:
        error = exc if exc is not None else sys.exc_info()[1]
        for module_name in ('start', 'prompt_app'):
            module = sys.modules.get(module_name)
            capture = getattr(module, 'captureException', None) if module is not None else None
            if callable(capture):
                try:
                    return int(capture(error, source=source, context=context, handled=handled) or 0)
                except Exception as nested_error:
                    print(f"[WARN:swallowed-exception] PhaseProcess.recordException capture-failed {type(nested_error).__name__}: {nested_error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    continue
        try:
            print(f'[CAPTURED-EXCEPTION] source={source} context={context} type={type(error).__name__}: {error}', file=sys.stderr, flush=True)
            print(''.join(traceback.format_exception(type(error), error, getattr(error, '__traceback__', None))), file=sys.stderr, flush=True)
        except Exception as print_error:
            try:
                sys.__stderr__.write(f"[WARN:swallowed-exception] PhaseProcess.recordException print-failed {type(print_error).__name__}: {print_error}\n")
                sys.__stderr__.flush()
            except BaseException:
                raise
        return 0

    @staticmethod
    def _command_text(command: Any) -> str:
        try:
            if isinstance(command, (list, tuple)):
                return subprocess.list2cmdline([str(part) for part in command])
            return str(command)
        except Exception as error:
            PhaseProcess.recordException(error, source='PhaseProcess._command_text', context='format-command', handled=True)
            return repr(command)

    @staticmethod
    def _callsite() -> str:
        this_file = Path(__file__).resolve()
        try:
            for frame in inspect.stack()[2:12]:
                try:
                    filename = Path(frame.filename).resolve()
                except Exception as error:
                    PhaseProcess.recordException(error, source='PhaseProcess._callsite', context='resolve-frame-filename', handled=True)
                    filename = Path(str(frame.filename or ''))
                if filename == this_file:
                    continue
                code = str((frame.code_context or [''])[0]).strip()
                return f'{filename}:{frame.lineno} in {frame.function} :: {code}'
        except Exception as error:
            PhaseProcess.recordException(error, source='PhaseProcess._callsite', context='inspect-stack', handled=True)
        return 'unknown-callsite'

    @staticmethod
    def _debug_line(message: str, *, level: str = 'PROCESS') -> None:
        try:
            from DebugLog import DebugLog
            DebugLog.writeLine(message, level=level, source='PhaseProcess.py', stream='stdout' if level != 'WARN' else 'stderr')
        except Exception:
            try:
                print(f'[{level}:PhaseProcess] {message}', file=sys.stderr if level == 'WARN' else sys.stdout, flush=True)
            except Exception as error:
                PhaseProcess.recordException(error, source='PhaseProcess._debug_line', context='print-fallback', handled=True)
                try:
                    sys.__stderr__.write(f'[WARN:PhaseProcess] debug-line failed {type(error).__name__}: {error}\n')
                    sys.__stderr__.flush()
                except BaseException:
                    raise

    @staticmethod
    def _windows_hidden_startup_kwargs(kwargs: dict[str, Any], *, needs_input: bool = False) -> dict[str, Any]:
        prepared = dict(kwargs)
        if os.name != 'nt':
            return prepared
        if str(os.environ.get('PROMPT_ALLOW_CHILD_WINDOWS', '') or '').strip().lower() in {'1', 'true', 'yes', 'on'}:
            PhaseProcess._debug_line('WINDOW-GUARD:BYPASS env=PROMPT_ALLOW_CHILD_WINDOWS command windows may appear', level='WARN')
            return prepared
        try:
            flags = int(prepared.get('creationflags') or 0)
            create_no_window = int(getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000))
            create_new_console = int(getattr(subprocess, 'CREATE_NEW_CONSOLE', 0x00000010))
            detached_process = int(getattr(subprocess, 'DETACHED_PROCESS', 0x00000008))
            if not (flags & create_new_console) and not (flags & detached_process):
                flags |= create_no_window
            prepared['creationflags'] = flags
        except Exception as error:
            PhaseProcess.recordException(error, source='PhaseProcess', context='windows-creationflags', handled=True)
        try:
            startupinfo = prepared.get('startupinfo')
            if startupinfo is None:
                startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= getattr(subprocess, 'STARTF_USESHOWWINDOW', 1)
            startupinfo.wShowWindow = 0
            prepared['startupinfo'] = startupinfo
        except Exception as error:
            PhaseProcess.recordException(error, source='PhaseProcess', context='windows-startupinfo', handled=True)
        # A windowed parent often has no valid console handles.  If a caller did
        # not explicitly request pipes/inheritance, use DEVNULL so console build
        # tools do not create a new empty console window just to attach stdio.
        try:
            if 'stdin' not in prepared and not needs_input:
                prepared['stdin'] = subprocess.DEVNULL
            if 'stdout' not in prepared and not prepared.get('capture_output'):
                prepared['stdout'] = subprocess.DEVNULL
            if 'stderr' not in prepared and not prepared.get('capture_output'):
                prepared['stderr'] = subprocess.DEVNULL
        except Exception as error:
            PhaseProcess.recordException(error, source='PhaseProcess', context='windows-stdio-defaults', handled=True)
        return prepared

    @staticmethod
    def _warn_dangerous_shell(command: Any, kwargs: dict[str, Any], callsite: str) -> None:
        try:
            shell = bool(kwargs.get('shell', False))
            text = PhaseProcess._command_text(command).lower()
            dangerous = shell or 'cmd /c start' in text or 'start-process' in text or 'shellexecute' in text
            if dangerous:
                PhaseProcess._debug_line(f'WINDOW-GUARD:DANGEROUS-SPAWN shell={shell} callsite={callsite} command={PhaseProcess._command_text(command)}', level='WARN')
        except Exception as error:
            PhaseProcess.recordException(error, source='PhaseProcess', context='warn-dangerous-shell', handled=True)

    @staticmethod
    def run(command: Any, *args: Any, phase_name: str = '', **kwargs: Any) -> subprocess.CompletedProcess:
        callsite = PhaseProcess._callsite()
        context = str(phase_name or '')
        run_kwargs = dict(kwargs)
        timeout = run_kwargs.pop('timeout', None)
        input_data = run_kwargs.pop('input', None)
        check = bool(run_kwargs.pop('check', False))
        capture_output = bool(run_kwargs.pop('capture_output', False))
        window_policy = run_kwargs.pop('windowPolicy', run_kwargs.pop('window_policy', None))
        window_delay = float(run_kwargs.pop('windowDelaySeconds', run_kwargs.pop('window_delay_seconds', 0.7)) or 0.7)
        if capture_output:
            if 'stdout' in run_kwargs or 'stderr' in run_kwargs:
                raise ValueError('stdout and stderr arguments may not be used with capture_output.')
            run_kwargs['stdout'] = subprocess.PIPE
            run_kwargs['stderr'] = subprocess.PIPE
        if input_data is not None:
            if 'stdin' in run_kwargs:
                raise ValueError('stdin and input arguments may not both be used.')
            run_kwargs['stdin'] = subprocess.PIPE
        prepared = PhaseProcess._windows_hidden_startup_kwargs(run_kwargs, needs_input=(input_data is not None))
        PhaseProcess._warn_dangerous_shell(command, prepared, callsite)
        if _process_log_verbose():
            PhaseProcess._debug_line(f'SPAWN:RUN:BEGIN phase={context or "unnamed"} cwd={prepared.get("cwd", "")} callsite={callsite} command={PhaseProcess._command_text(command)}')
        process = None
        try:
            process = PhaseProcess.popen(command, *args, phase_name=context or 'PhaseProcess.run', window_policy=window_policy, windowDelaySeconds=window_delay, **prepared)
            try:
                stdout, stderr = process.communicate(input=input_data, timeout=timeout)
            except subprocess.TimeoutExpired as timeout_error:
                PhaseProcess.recordException(timeout_error, source='PhaseProcess.run', context=f'timeout {context or command} @ {callsite}', handled=True)
                try:
                    PhaseProcess._kill_process_tree(process, reason=f'timeout phase={context or "unnamed"}')
                finally:
                    stdout, stderr = process.communicate(timeout=5)  # lifecycle-bypass-ok: process tree was already killed before draining pipes
                timeout_error.output = stdout
                timeout_error.stderr = stderr
                raise timeout_error
            completed = subprocess.CompletedProcess(command, int(process.returncode or 0), stdout, stderr)
            if _process_log_verbose() or int(completed.returncode or 0) != 0:
                PhaseProcess._debug_line(f'SPAWN:RUN:END phase={context or "unnamed"} rc={completed.returncode} callsite={callsite}')
            if check and int(completed.returncode or 0) != 0:
                raise subprocess.CalledProcessError(int(completed.returncode or 0), command, output=stdout, stderr=stderr)
            return completed
        except BaseException as exc:
            PhaseProcess.recordException(exc, source='PhaseProcess.run', context=f'{context or command} @ {callsite}', handled=True)
            raise

    @staticmethod
    def _kill_process_tree(process: subprocess.Popen, *, reason: str = '') -> bool:
        try:
            pid = int(getattr(process, 'pid', 0) or 0)
            if pid <= 0:
                return False
            PhaseProcess._debug_line(f'SPAWN:KILL pid={pid} reason={reason}', level='WARN')
            if os.name == 'nt':
                startupinfo = None
                creationflags = int(getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000) or 0)
                try:
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= int(getattr(subprocess, 'STARTF_USESHOWWINDOW', 1) or 1)
                    startupinfo.wShowWindow = 0
                except Exception as error:
                    PhaseProcess.recordException(error, source='PhaseProcess.kill_process_tree', context='taskkill-startupinfo', handled=True)
                    startupinfo = None
                _RAW_SUBPROCESS_RUN(['taskkill', '/PID', str(pid), '/T', '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, check=False, timeout=15, startupinfo=startupinfo, creationflags=creationflags)
            else:
                try:
                    process.kill()
                except Exception as error:
                    PhaseProcess.recordException(error, source='PhaseProcess.kill_process_tree', context='process.kill fallback to os.kill', handled=True)
                    os.kill(pid, 9)
            return True
        except Exception as error:
            PhaseProcess.recordException(error, source='PhaseProcess.kill_process_tree', context=reason, handled=True)
            return False

    @staticmethod
    def popen(command: Any, *args: Any, phase_name: str = '', **kwargs: Any) -> subprocess.Popen:
        callsite = PhaseProcess._callsite()
        context = str(phase_name or '')
        window_policy = _normalize_window_policy(kwargs.pop('windowPolicy', kwargs.pop('window_policy', None)), command)
        window_delay = float(kwargs.pop('windowDelaySeconds', kwargs.pop('window_delay_seconds', 0.7)) or 0.7)
        prepared = PhaseProcess._windows_hidden_startup_kwargs(kwargs)
        PhaseProcess._warn_dangerous_shell(command, prepared, callsite)
        if _process_log_verbose():
            PhaseProcess._debug_line(f'SPAWN:POPEN:BEGIN phase={context or "unnamed"} window_policy={window_policy} cwd={prepared.get("cwd", "")} callsite={callsite} command={PhaseProcess._command_text(command)}')
        before_windows = DEAD_WINDOW_MONITOR.snapshot()
        try:
            process = _RAW_SUBPROCESS_POPEN(command, *args, **prepared)  # lifecycle-bypass-ok phase-ownership-ok thread-ok
            if _process_log_verbose():
                PhaseProcess._debug_line(f'SPAWN:POPEN:STARTED phase={context or "unnamed"} pid={getattr(process, "pid", 0)} callsite={callsite}')
            DEAD_WINDOW_MONITOR.checkAfterLaunch(before_windows, launchedPid=int(getattr(process, 'pid', 0) or 0), command=command, policy=window_policy, sourceLine=callsite, delaySeconds=window_delay)
            return process
        except BaseException as exc:
            PhaseProcess.recordException(exc, source='PhaseProcess.popen', context=f'{context or command} @ {callsite}', handled=True)
            raise

    @staticmethod
    def thread(*, target: Callable[..., Any], name: str, daemon: bool = True, ttl: float = 0.0, args: tuple[Any, ...] = (), kwargs: dict[str, Any] | None = None) -> threading.Thread:  # thread-ok
        callsite = PhaseProcess._callsite()
        if _process_log_verbose():
            PhaseProcess._debug_line(f'THREAD:CREATE name={name or "PromptPhaseThread"} daemon={bool(daemon)} ttl={ttl} callsite={callsite}')

        def guarded() -> None:
            try:
                target(*tuple(args or ()), **dict(kwargs or {}))
            except BaseException as exc:
                PhaseProcess.recordException(exc, source='PhaseProcess.thread', context=str(name or 'thread'), handled=True)
                raise

        return threading.Thread(target=guarded, name=str(name or 'PromptPhaseThread'), daemon=bool(daemon))  # lifecycle-bypass-ok phase-ownership-ok thread-ok
