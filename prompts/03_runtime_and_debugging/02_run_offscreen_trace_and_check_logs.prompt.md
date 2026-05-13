# Runtime and Debugging > Run Offscreen Trace and Check Logs
@workflow: simple_prompt_generator
@doctype: 01_normal_doctype
@bucket: Runtime and Debugging

## Task
Run the app with the project's offscreen/headless and verbose trace flags. Then inspect the console, trace log, and any generated screenshots or runtime artifacts. Tell me whether it executed a real amount of code, whether it surfaced errors to console, and what the first runtime failure actually is.

## Context
[comment]
Use this when syntax is clean but you still do not trust the runtime.
[/comment]

If the trace is suspiciously shallow, treat that as a problem and investigate instead of calling the run successful.
