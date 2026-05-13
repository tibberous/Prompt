# Quality Localization and Data > Verify DB Mutations and Warnings
@workflow: simple_prompt_generator
@doctype: 01_normal_doctype
@bucket: Quality Localization and Data

## Task
Audit every INSERT, UPDATE, and DELETE in [file]. Find the mutation calls that do not verify rows affected, then add the verification and warning path. Queries intentionally designed to sometimes no-op can stay as-is, but they need a comment explaining why.

## Context
[comment]
Use this when data writes are silently failing, admin panels look flaky, or schema code is hard to trust.
[/comment]

Report which mutations were upgraded and which were intentionally left alone.
