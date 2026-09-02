# AGENTS.md — OpenCode Agent Definitions

This directory defines the subagents. Each `.md` file is a self-contained agent spec with YAML frontmatter + markdown body — all permission and skill configuration lives in the agent file itself. (The global `~/.config/opencode/opencode.json` only defines the `explore` agent and `continue_loop_on_deny`.)

## File format

```yaml
---
description: <short role>
mode: subagent
color: "<hex>"
temperature: <0.0-1.0>
permission:
  read: allow|deny
  edit: allow|deny
  bash:
    "*": deny
    "<specific>*": allow
  glob: allow|deny
  grep: allow|deny
  webfetch: allow|deny
  websearch: allow|deny
  lsp: allow|deny
  skill:
    "*": deny
    "<specific-skill>": allow
---
```

## Permission patterns

| Agent | edit | bash | task | question | skills allowed |
|---|---|---|---|---|---|
| **manager** | deny | `ls`/`find`/`rg`/`cat`, read-only `git`; fs mutations (`mkdir`/`rm`/`mv`/…) **only after user approval** | **yes** | **allow** (escalates to user) | `architecture-guide` |
| **developer** | **allow** | all except destructive git (`git clean`, `git reset --hard`); never chains commands | no | deny (escalates to manager) | all 4 project skills |
| **architect** | deny | `ls`/`find`, read-only `git` | no | deny (escalates to manager) | `architecture-guide` |
| **designer** | deny | `ls`/`find` | no | deny (escalates to manager) | `design-system` |
| **tester** | deny | test/lint commands, `ls`/`find`/`cat` | no | deny (escalates to manager) | `testing-standards` |
| **security** | deny | `ls`/`find`/`cat`/`head`, `npm audit`, read-only `git` | no | deny (escalates to manager) | `security-practices` |
| **researcher** | deny | `ls`/`find`/`cat`/`rg`, read-only `git`, `cat *`/`touch*` (writes result file only) | no | **allow** (asks the user directly) | `architecture-guide` |

- Only `manager` spawns subagents (`task: allow`). **`researcher` is the standalone exception** — it is NOT a craft agent: it is invoked directly by the user (`/research` command) or the main session, never by the manager, and it does not take part in the orchestrated workflow.
- `developer` is the only agent that edits/writes files — it may also create folders and remove/rename files during implementation. (`researcher` writes only `research.md` and `result-researcher.md` via `cat >` heredoc, like the manager.)
- `researcher` has `question: allow` because it runs at depth 0/1 (via `/research` or direct delegation), so interactive prompts surface to the user — it has no escalation protocol.
- Feature implementation workflows must include `developer` → `tester` → `security` before final synthesis.
- Agents missing a skill they need get denied cleanly and report back to the manager.
- `experimental.continue_loop_on_deny` is enabled in the global config — denials never stall the loop.

## File-system mutation policy (folders & files)

Folder creation and file removal are allowed where they serve the workflow — never for the read-only
craft agents:

- **Denied for read-only craft agents (`architect`, `designer`, `tester`, `security`, `explore`)**: `mkdir`,
  `rmdir`, `rm`, `unlink`, `mv`, `cp`, `touch`, `ln`, `install`, `truncate`, `mkfile`, `git rm`, `git clean`
  — and any compound command containing them (`* && *`, `* || *`, `* ; *` are denied for everyone).
- **`developer` is exempt**: it is the implementation agent. It creates folders/files and removes,
  renames, and copies files freely during feature work. Only destructive git commands (`git clean`,
  `git reset --hard`) are denied, plus command chaining. The write tool also creates missing parent
  folders automatically, so writing a new file into a new folder never needs permission.
- **The manager is the approval gate**: a read-only craft agent that hits a file-system need must NOT
  attempt a denied command (attempting a denied command stalls the workflow). It stops and returns an
  `## ESCALATION QUESTION` with the exact path(s) and reason. The manager reviews, asks the user via
  the `question` tool, performs the operation itself, and resumes delegation.
- `doom_loop` is set to `deny` on all craft agents so a repeated blocked call resolves as a clean
  denial instead of a non-surfacing "ask" that stalls the workflow at sub-sub-agent depth.

## Question escalation (why `question` is allowed only on manager)

Craft agents run at sub-sub-agent depth (via `subagent_depth: 2`), where interactive prompts
and `ask` permissions do not surface to the user and stall the session (upstream bug, see
anomalyco/opencode#39112). Therefore:

- Craft agents have `question: deny` and an **Escalation Protocol**: on ambiguity they stop and
  return a `## ESCALATION QUESTION` block instead of guessing or stalling.
- The `manager` (`question: allow`) is the escalation router: it answers the question itself if it
  can, otherwise it asks the user via the `question` tool and resumes delegation with the answer.

## Commands

Commands in `~/.config/opencode/commands/` route to agents via `agent:` frontmatter. They define a prompt template only — all permission and skill configuration lives in the agent file itself.

## Command execution anti-stall rules

Every agent that runs shell commands (`developer`, `tester`, `security`) carries the same rules in its body. Enforce them when writing or reviewing agent prompts:

- **Never bare `npx <pkg>`** — use `./node_modules/.bin/<bin>` or `npx --no-install` so a registry/network check can't hang.
- **Prefer project npm scripts** (`npm run lint`/`test`/`build`) over raw `npx`.
- **Never pipe into `head`/`tail`/`grep` to trim output** — early pipe close sends SIGPIPE and the tool appears stuck. Run plain or redirect to a temp file.
- **Always pass an explicit `timeout`** on bash tool calls; never rely on the default.

## Important constraints

- `mode` must always be `subagent`.
- Permission entries are case-sensitive: `allow`, `deny`.
- Within a `skill:` or `bash:` object, insertion order matters — put the broad `"*": deny` rule first, then specific allows last.
- Config changes require an OpenCode restart.

## Result files (`result-*.md`)

Every workflow run produces durable `result-<agent>.md` files in the project root directory. These serve as:

- **Traceability** — see what each agent did and decided
- **Restartability** — if a workflow stalls, inspect the last result file and resume from there
- **Debugging** — review an agent's full output without scrolling back through the session

### Who writes what

| File | Writer | Method |
|---|---|---|
| `result-manager.md` | Manager | `cat > result-manager.md << 'EOF'` (bash, after synthesis) |
| `result-developer.md` | Developer | `write` tool (edit: allow, as last action) |
| `result-architect.md` | Manager | `cat >` (bash, after delegation returns) |
| `result-designer.md` | Manager | `cat >` (bash, after delegation returns) |
| `result-tester.md` | Manager | `cat >` (bash, after delegation returns) |
| `result-security.md` | Manager | `cat >` (bash, after delegation returns) |
| `research.md` | Researcher | `cat >` (bash, after research completes, full briefing) |
| `result-researcher.md` | Researcher | `cat >` (bash, after research completes, short summary, as last action) |

Result file writing is considered workflow artifact management, **not** a code file-system mutation — the Manager does it without user approval. The Researcher writes its own `research.md` (full) and `result-researcher.md` (short summary) the same way; these are the only files it ever writes. The summary is written last so the full `research.md` is never overwritten.

### Additional agents

You can add custom agent specs as `.md` files in this directory; they will be automatically picked up by OpenCode. Example: `ethical-hacker.md`.

### Restart flow
1. Check which `result-*.md` files exist → the last one tells you where the workflow stopped.
2. Read the last file's `## Next Step` field to know what agent to delegate to next.
3. Start the `/feature` command (or `/plan`) and tell the Manager to resume from the last completed step, referencing the result files for context.
