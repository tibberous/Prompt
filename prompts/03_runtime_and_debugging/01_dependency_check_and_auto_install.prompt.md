# Runtime and Debugging > Dependency Check and Auto Install
@workflow: simple_prompt_generator
@doctype: 01_normal_doctype
@bucket: Runtime and Debugging

## Task
Audit the project dependencies before runtime:
- inventory imports, binaries, and helper tools,
- declare what is required versus optional,
- wire missing dependency checks into the launcher/bootstrap path, and
- auto-install missing dependencies when the project is designed to do so, including elevated shells where appropriate.
Do not silently assume packages are present.

## Context
[comment]
Use this when the real blocker is startup friction, missing wheels, or machine-to-machine inconsistency.
[/comment]

At the top of the response, say exactly what packages or system tools are still missing if any remain unresolved.
