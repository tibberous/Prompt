# Quality Localization and Data > Localization One File Pass
@workflow: simple_prompt_generator
@doctype: 01_normal_doctype
@bucket: Quality Localization and Data

## Task
Localization pass for [view name]. Find every user-facing string in this file. For each one: add it to the proper ENUM/constants location, replace the raw string with the localization lookup, and mark the touched lines so the pass is auditable. Do one file at a time and show the before/after diff.

## Context
[comment]
Use this when you want a disciplined translation or localization pass instead of a vague “add i18n” request.
[/comment]

Do not stop halfway without saying so. If it needs multiple passes, say which pass you are on.
