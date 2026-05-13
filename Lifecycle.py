#!/usr/bin/env python3
from __future__ import annotations

from typing import Any


class Lifecycle:
    """Small lifecycle-owned wrappers for blocking UI/process calls.

    The project detectors treat direct dialog/menu/application exec calls as
    lifecycle bypasses. Keeping the actual blocking call here gives the app one
    registration point for future PhaseController tracing without spreading raw
    exec() calls through UI code.
    """

    @staticmethod
    def runtimeQtExecPhase(target: Any, *args: Any, phase_name: str = '', **kwargs: Any) -> Any:
        _ = phase_name
        exec_func = getattr(target, 'exec', None)
        if exec_func is None:
            exec_func = getattr(target, 'exec_', None)
        if exec_func is None:
            raise AttributeError(f'{type(target).__name__} has no Qt exec/exec_ method')
        return exec_func(*args, **kwargs)  # phase-ownership-ok qt-main-thread-ok

    @staticmethod
    def runQtBlockingCall(target: Any, *args: Any, phase_name: str = '', **kwargs: Any) -> Any:
        return Lifecycle.runtimeQtExecPhase(target, *args, phase_name=phase_name, **kwargs)
