# Runtime and Debugging > Pipe Stdout Stderr and Surface Errors
@workflow: simple_prompt_generator
@doctype: 01_normal_doctype
@bucket: Runtime and Debugging

## Task
Make sure the launcher pipes both stdout and stderr so runtime failures are visible. Then audit the app for swallowed exceptions, silent subprocess failures, and webview/runtime errors that should be surfaced to console or trace output. Fix the visibility path first, then rerun and report the real errors.

## Context
[comment]
Use this when the app is failing quietly, subprocesses disappear, or the UI just “does nothing.”
[/comment]

The goal is observability first, not blind patching.
