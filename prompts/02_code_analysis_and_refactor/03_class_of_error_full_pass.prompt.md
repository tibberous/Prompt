# Code Analysis and Refactor > Class of Error Full Pass
@workflow: simple_prompt_generator
@doctype: 01_normal_doctype
@bucket: Code Analysis and Refactor

## Task
We need to fix this CLASS of error. Grep for every instance of the pattern first, list how many matches there are, then fix them all in one pass. Do not patch one instance and leave the others behind. After the pass, confirm how many you fixed and whether any intentionally remain.

## Context
[comment]
Use this for repeated crashes, bad idioms, inconsistent guards, or copy-pasted bugs.
[/comment]

If the pattern spans multiple files, keep the report grouped by file so the fix scope is obvious.
