---
description: Review this session for mistakes and good calls, then update persistent memory so the same mistake isn't repeated next time.
model: claude-sonnet-5
reasoning_effort: medium
---

Run a retrospective on the current session and update your persistent memory files accordingly.

Think carefully here rather than pattern-matching on surface keywords. A real lesson is specific and non-obvious enough that a future session would actually act differently because of it. "The user asked a question and I answered it" is not a lesson. Skim-level scanning for words like "no" or "don't" will produce noise — read for what actually happened and why it mattered.

## 1. Review the session

Look back over everything that happened in this conversation (not just the last message) and identify:

- **Mistakes you made** that the user had to correct — wrong assumptions, wrong files touched, wrong approach, broken tests, things you had to redo. For each, figure out the *root cause*, not just the symptom (e.g. not "forgot a flag" but "assumed X was true without checking, and it wasn't").
- **Corrections the user gave you** — explicit "no, don't do that", "stop doing X", "use Y instead", even mild ones.
- **Approaches that worked and were confirmed** — the user accepted or praised a non-obvious choice you made (a way of testing, a scope decision, a tool choice). These are just as important to keep as corrections are to avoid repeating.
- **New project context surfaced** — decisions, constraints, deadlines, or "why we're doing this" that came from the user rather than from the code, and that would help a future session make better calls.
- **New reference pointers** — external systems the user pointed you to (issue trackers, dashboards, docs) that aren't already recorded.

Ignore anything that's just derivable from reading the code, git log, or CLAUDE.md — that doesn't belong in memory (see "What NOT to save in memory" in your instructions).

If nothing in the session rises above "normal work with no corrections and nothing surprising," say so plainly and stop — don't force a memory write for the sake of it.

## 2. Check against existing memory

Before writing anything new, read `MEMORY.md` in your memory directory and open any memory files that look related to what you just found. Prefer **updating** an existing memory over creating a near-duplicate one — e.g. if `feedback-generation-integrity.md` already covers a mistake in the same family, extend it instead of writing `feedback-generation-integrity-2.md`.

## 3. Write memory updates

For each genuinely new or updated lesson, follow the memory system's own rules exactly (type selection, frontmatter format, `MEMORY.md` index format, the `**Why:**` / `**How to apply:**` structure for feedback and project memories, linking related memories with `[[name]]`). Don't invent a different format for this command.

The memory directory is organized as an Obsidian vault with one subfolder per type: `feedback/`, `project/`, `reference/` (create a type subfolder the first time it's needed — e.g. `user/` for a `user`-type memory). `MEMORY.md` itself stays at the directory root, grouped by the same type headings, with links pointing into the subfolders (e.g. `feedback/feedback-foo.md`). Never write a new memory file flat at the root — file it under its type folder so the vault stays sorted.

Before writing, sanity-check the lesson itself:
- If it names a specific file, function, flag, or command, confirm it still exists/works rather than trusting your recollection from mid-session.
- State the rule at the right altitude — general enough to apply next time, specific enough to not be vague filler ("be careful" is not a lesson; "pin the asset by id before running save-mutating tests, because 'first match' silently hit the wrong record" is).
- If a mistake was a one-off slip with an obvious cause (typo, transient tool error) and no pattern behind it, it's not worth memory — only save things likely to recur.

## 4. Sync the tooling catalog

This session may have added, edited, or removed custom tooling — slash commands (`.claude/commands/*.md`), agents (`.claude/agents/*.md`), or similar project-specific config. Check for a memory file that catalogs this project's custom commands/agents (look for something like `project-custom-commands.md` in `MEMORY.md`'s index; if none exists yet, this session is the seed for it).

- **Added:** a new command/agent was created this session but isn't in the catalog yet → add an entry (name, one-line purpose, key config like model/reasoning effort).
- **Changed:** an existing command/agent's purpose or config changed → update its entry in place, don't append a duplicate.
- **Removed:** a command/agent file was deleted this session but is still listed → remove its entry.
- If the catalog file doesn't exist and there's nothing to seed it with, don't create an empty file.

This is what keeps future sessions aware of what tooling already exists instead of rediscovering or re-explaining it each time.

## 5. Check project docs

If something you learned is a durable, project-wide convention (not session-specific, not user-preference-specific) that belongs in `CLAUDE.md` rather than memory — e.g. a command that turned out to be wrong, a workflow step that was missing — propose the edit and apply it after a quick confirmation. Most retros will NOT touch `CLAUDE.md`; memory is the default target.

## 6. Report back

Give a short summary: what mistake(s) or confirmed approach(es) you found, which memory files you created or updated (including any tooling-catalog change), and whether you touched `CLAUDE.md`. If nothing was worth saving, say that instead of padding the report.
