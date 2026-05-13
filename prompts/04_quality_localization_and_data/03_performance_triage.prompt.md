# Quality Localization and Data > Performance Triage
@workflow: simple_prompt_generator
@doctype: 01_normal_doctype
@bucket: Quality Localization and Data

## Task
What is on the main thread that should not be? Inspect [file] and list every database call, file I/O, and network call that happens during startup or in response to UI events. For each one, say whether it should move to a thread, a deferred timer, or stay on the main thread and why.

## Context
[comment]
Use this when the GUI is sluggish, the first paint is late, or the app feels blocked by startup work.
[/comment]

Separate must-run-before-paint work from everything that can safely defer.
