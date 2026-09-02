---
description: Full feature implementation workflow via the manager
agent: manager
---
Implement the requested feature by orchestrating the full pipeline.

1. **Research gate** — Ask the user via the `question` tool: "Would you like to research this topic before implementing?" If yes, delegate to `researcher` first, read `result-researcher.md` (short summary) and `research.md` (full briefing), and incorporate findings into subsequent steps
2. Plan the work and decide which craft agents are needed (architect/designer as appropriate, developer, tester, and security)
3. Delegate to architect/designer first when architecture or UI changes are needed
4. Delegate implementation to `developer` with full requirements and context
5. Delegate QA to `tester` to run the test suite and review coverage
6. Delegate security review to `security` after QA for every feature implementation or behavior-changing code task
7. If tests fail or reviewers find issues, delegate fixes back to `developer` and re-run the relevant verification (keep iterations low; escalate at 3)
8. If a craft agent returns a `## ESCALATION QUESTION`, answer it yourself when possible; otherwise ask the user via the `question` tool, then resume delegation with the answer
9. Synthesize final results with changed files, test status, security review status, and remaining risks
