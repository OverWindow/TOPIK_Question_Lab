# Agent Instructions

## Technology Stack

- Python, Streamlit and Pydantic; SQLite for generation records and PostgreSQL for the question bank.
- Model API calls use the existing OpenAI-compatible client; S3 media uses boto3/httpx.

## Project Structure

- `topik_question_lab/main.py` starts the UI in `topik_question_lab/ui/`.
- `step_01_source/` → `step_02_planning/` → `step_03_generation/` → `step_04_review/` → `step_05_export/` → `step_06_publication/` follow the question workflow.
- `common/` holds shared models/storage; `maintenance/` holds backup/server tools. These folders are inside `topik_question_lab/`; tests are in `tests/` and persistent records are in `data/`.

## Working Scope

- Scope all work to the local TOPIK question generator in this repository (`Unigate-PBT-Hub`).
- Exclude `Unigate-PBT-Web` and other Web application projects from investigation, implementation, and verification unless the user explicitly includes them in the task.

## Working Guidelines

- Keep code as simple as possible. Implement only what is needed for existing functionality and the current request to work. Do not design for unrequested features, hypothetical future requirements, or speculative extension points. Avoid unnecessary abstractions and complexity.
- Keep this document limited to the technology stack, a brief project structure, and working guidelines. Do not append feature descriptions, operational procedures, implementation histories, or per-task summaries. Update these three sections only when necessary.
- Before making changes, explain the proposed steps, affected files or systems, expected API or database changes, and relevant verification. Ask for explicit approval and wait, unless the user explicitly waives this step. The initial task request alone is not approval of an unpresented plan.
- Read-only inspection is allowed before approval. Do not edit files, install dependencies, or change database or service state until approved. Proceed within the approved scope without repeated approval requests; obtain approval for material scope changes.
- Keep changes within the requested scope. Do not perform unrelated refactoring or modify unrelated files.
- If instructions are unclear or open to multiple interpretations, ask for clarification and wait before proceeding with dependent work.
- Evaluate existing functionality before adding a dependency. Reuse existing tables and relationships where possible; add tables only for a clear, current requirement.
- When proposing a new table, clearly specify its purpose, column names, data types, nullability, default values, and primary key, foreign key, and unique constraints.
- Before adding or configuring a background worker, explain its purpose and obtain approval.
- Obtain approval before deleting data or making database changes that break schema compatibility.
- Do not expose personal information, student information, API keys, database connection strings, credentials, or secrets in logs or the repository. Never print environment file values or commit secrets.
- After every modification, state whether APIs or database tables were added. For new APIs, list the HTTP method, path, and purpose; for new tables, list the name and purpose. Explicitly state when none were added.
- Keep comments short and explain intent, constraints, or non-obvious behavior. Avoid line-by-line narration and duplicate explanations. Concise English/Korean comments are welcome where useful; keep lengthy setup instructions in README.

## Feature Scope

- Do not add unnecessary features or functionality beyond the user's request.
- If additional functionality is needed, explain the need to the user before implementing it.
