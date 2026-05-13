# Session Bootstrap > Stage New Zip and Start New Project
@workflow: simple_prompt_generator
@doctype: 01_normal_doctype
@bucket: Session Bootstrap

## Task
Stage the uploaded zip or repo first, inspect the real current files, and start the project the right way:
- Unify everything into one canonical working version before edits.
- Run AST on the main entry files, list functions over 40 lines, and propose class boundaries before coding.
- Dependency-check the launcher/bootstrap flow and install missing imports automatically when possible, including elevated shells when needed.
- Create one root application class named [RootAppClassName] and keep other top-level classes limited to small data objects or clear helpers.
- Prefer one file per class for new project structure, and add a top comment when a file intentionally owns a single main class.
- Do not leave scratch branches, duplicate files, or half-merged drafts in the workspace.

## Context
[comment]
Use this when you are handing the model a fresh zip or repo and want a disciplined first pass instead of random edits.
[/comment]

Before coding, tell me what the CWV is, what the main entry files are, and what the first safe implementation step should be.
