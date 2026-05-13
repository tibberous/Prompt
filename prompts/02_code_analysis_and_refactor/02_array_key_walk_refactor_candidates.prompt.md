# Code Analysis and Refactor > Array Key Walk Refactor Candidates
@workflow: simple_prompt_generator
@doctype: 01_normal_doctype
@bucket: Code Analysis and Refactor

## Task
Look through [file] for any place where we are dispatching by repeated if/elif blocks, duplicated handler tables, or string-key dicts that map to behavior. List where a registered-array + key-walk pattern would reduce code, but do not refactor yet. Then find 10 other methods that would use less code with the same pattern.

## Context
[comment]
Use this when you want architecture leverage instead of random cleanup.
The goal is to identify repetition that can be collapsed safely.
[/comment]

Prefer ENUM-backed dispatch where possible instead of raw string keys.
