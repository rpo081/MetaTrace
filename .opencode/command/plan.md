---
description: Creates a development plan without modifying files
agent: manager
---
Analyze the requirements and produce a clear development plan. Do not implement, edit files, or delegate to implementation agents.

1. Analyze the request and break it into a logical sequence of steps
2. Ask the user via the `question` tool: "Would you like to research this topic before planning?" If yes, delegate to `researcher` first, read `result-researcher.md` (short summary) and `research.md` (full briefing), and incorporate its findings into the plan
3. Consult the codebase via explore, architect, or designer as needed
4. Produce a written plan with task breakdown, dependency order, and definition of done
5. Present the plan to the user for approval

This command is read-only — produce the plan, do not execute it.
