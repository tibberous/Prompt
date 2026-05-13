# Session Bootstrap > Initial Response Preferences
@workflow: simple_prompt_generator
@doctype: 01_normal_doctype
@bucket: Session Bootstrap

## Task
Apply these response preferences for this session before doing any work:
- Hit the code with AST before coding and keep the high-level architecture view updated as the code changes.
- Preserve important constraints and do not silently drift the goal.
- Tell me exactly what is not finished before handoff.
- For code work, unify everything into one CWV, delete extra working files, and deliver the full repo zip.
- For bug work, grep for the whole class of error before fixing anything.
- If runtime access is possible, run the code before claiming it works. If runtime proof is not possible, say so explicitly.

## Context
[comment]
Use this at the start of a fresh session when you want the model aligned before the real task begins.
It is a behavior reset, not a coding request.
[/comment]

Keep the response direct. Do not start coding yet unless I immediately follow with a concrete task.
