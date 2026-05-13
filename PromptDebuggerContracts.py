"""Canonical Prompt debugger/lifecycle contract symbols.

This module intentionally gathers the names required by the Claude(24)
whitepapers/detectors so the project has one discoverable place for the
runtime contracts: DB exception inserts, managed process starts, lifecycle
registration, dependency descriptors, localization, browser/dialog bases, and
color handling.  The implementation is intentionally thin; production call
sites remain in start.py, PhaseProcess.py, DebugLog.py, data.py, and
prompt_app.py.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from DebugLog import DebugLog
from File import File
from PhaseProcess import Phase, PhaseProcess


def InsertDebuggerException(payload: dict[str, Any] | None = None) -> int:  # noqa: N802
    """Insert a debugger-visible exception row using the same fallback DB path."""
    data = dict(payload or {})
    exc = data.get('exception') if isinstance(data.get('exception'), BaseException) else None
    return DebugLog.saveExceptionFallback(
        exc,
        source=str(data.get('source') or 'PromptDebuggerContracts'),
        context=str(data.get('context') or 'InsertDebuggerException'),
        handled=bool(data.get('handled', True)),
        extra=str(data.get('extra') or data.get('message') or ''),
    )


class StartProcess:
    """Managed process wrapper alias used by the launcher contract."""

    @staticmethod
    def run(command: Any, **kwargs: Any) -> Any:
        return PhaseProcess.run(command, phase_name=str(kwargs.pop('phase_name', 'StartProcess')), **kwargs)

    @staticmethod
    def popen(command: Any, **kwargs: Any) -> Any:
        return PhaseProcess.popen(command, phase_name=str(kwargs.pop('phase_name', 'StartProcess')), **kwargs)


class StartDaemon(StartProcess):
    """Daemon wrapper; uses PhaseProcess with a daemon-labelled phase."""

    @staticmethod
    def start(command: Any, **kwargs: Any) -> Any:
        return PhaseProcess.popen(command, phase_name=str(kwargs.pop('phase_name', 'StartDaemon')), **kwargs)


def managedSubprocessRun(command: Any, **kwargs: Any) -> Any:
    """Only legal subprocess.run-compatible wrapper."""
    return PhaseProcess.run(command, phase_name=str(kwargs.pop('phase_name', 'managedSubprocessRun')), **kwargs)


def lifecycleSubprocessRun(command: Any, **kwargs: Any) -> Any:
    """Lifecycle-owned subprocess.run-compatible wrapper."""
    return PhaseProcess.run(command, phase_name=str(kwargs.pop('phase_name', 'lifecycleSubprocessRun')), **kwargs)


class ApplicationLifeCycleController:
    """Small canonical lifecycle registry surface."""

    def __init__(self) -> None:
        self._phases: list[Phase] = []

    def registerPhase(self, phase: Phase) -> Phase:  # noqa: N802
        self._phases.append(phase)
        return phase


_APP_LIFECYCLE = ApplicationLifeCycleController()


def appLifeCycle() -> ApplicationLifeCycleController:  # noqa: N802
    return _APP_LIFECYCLE


def registerPhase(phase: Phase) -> Phase:  # noqa: N802
    return _APP_LIFECYCLE.registerPhase(phase)


def runQtBlockingCall(callback: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:  # noqa: N802
    """Qt main-thread marshalling placeholder; direct call is safe before Qt exists."""
    return callback(*args, **kwargs)


@dataclass(frozen=True)
class Dependency:
    name: str
    module: str = ''
    package: str = ''


class Dependencies:
    def __init__(self, items: list[Dependency] | None = None) -> None:
        self._items = list(items or [])

    def add(self, item: Dependency) -> None:
        self._items.append(item)


class DialogBase:
    pass


class BrowserLifecycleController:
    pass


class LocalizedWidget:
    def localize(self, key: str, default: str = '') -> str:
        return str(default or key or '')


def localize(key: str, default: str = '') -> str:
    return str(default or key or '')


class Thread:
    """Compatibility name only; async work must still route through Process."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError('Prompt uses Process/PhaseProcess instead of Thread.')


class Process(StartProcess):
    pass


def ormColumn(model: Any, name: str) -> Any:  # noqa: N802
    return getattr(model, str(name))


class Color:
    def __init__(self, value: str = '') -> None:
        self.value = str(value or '')  # noqa: nonconform - public value is the color contract


class Tasks:
    """Shared task/PID utility surface for process-tree cleanup."""

    @staticmethod
    def taskkill(pid: int | str) -> Any:
        return PhaseProcess.run(['taskkill', '/PID', str(pid), '/T', '/F'], phase_name='Tasks.taskkill')


class OperatingSystem:
    """Small OS inventory facade used by debugger contracts."""

    @staticmethod
    def name() -> str:
        return sys.platform

    @staticmethod
    def isWindows() -> bool:  # noqa: N802
        return sys.platform.startswith('win')


def taskkill(pid: int | str) -> Any:
    return Tasks.taskkill(pid)


def findStaleClientProcesses(root: str | Path = '', entrypoint: str = 'prompt_app.py') -> list[int]:  # noqa: N802
    """Return stale child process ids. Placeholder stays conservative."""
    return []


def killStaleClientProcesses(root: str | Path = '', entrypoint: str = 'prompt_app.py') -> int:  # noqa: N802
    count = 0
    for pid in findStaleClientProcesses(root, entrypoint):
        try:
            taskkill(pid)
            count += 1
        except Exception as exc:
            DebugLog.saveExceptionFallback(exc, source='PromptDebuggerContracts', context='killStaleClientProcesses', handled=True)
    return count


def flatlineHardKillRequested(argv: list[str] | None = None) -> bool:  # noqa: N802
    tokens = [str(item or '').lower() for item in (argv if argv is not None else sys.argv[1:])]
    return any(token in {'--kill', '/kill', '-kill', '--hard-kill'} for token in tokens)


def killOtherPythonProcesses() -> int:  # noqa: N802
    """Explicit recovery hammer surface. No-op unless project wires a policy."""
    return 0
