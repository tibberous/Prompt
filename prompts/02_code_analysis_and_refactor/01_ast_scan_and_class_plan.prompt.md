# Code Analysis and Refactor > AST Scan and Class Plan
@workflow: simple_prompt_generator
@doctype: 01_normal_doctype
@bucket: Code Analysis and Refactor

## Task
Run AST on [file]. List every function, its line count, and which ones are over 40 lines. Then propose which functions should be grouped into classes — name the class, list the methods that belong in it, and explain the one-line responsibility. Do not write code yet. Analysis first.

## Context
[comment]
This is the best “start smart” coding prompt in the library.
Use it before refactors, architecture changes, or when inheriting a messy file.
[/comment]

Also point out at least one copy-paste cluster that could become a registered-array or key-walk pattern.
