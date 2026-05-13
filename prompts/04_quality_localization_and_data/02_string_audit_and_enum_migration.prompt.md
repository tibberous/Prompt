# Quality Localization and Data > String Audit and ENUM Migration
@workflow: simple_prompt_generator
@doctype: 01_normal_doctype
@bucket: Quality Localization and Data

## Task
Run a string audit on [file]. Find repeated strings and any dicts that use string keys where an ENUM or typed constant would be better. For each candidate, write the proposed ENUM name and its key list. Do not replace anything yet — produce the migration plan first.

## Context
[comment]
Use this before a cleanup pass when you want raw strings and fragile keyed structures under control.
[/comment]

Call out which strings are user-facing versus internal so the migration can be prioritized correctly.
