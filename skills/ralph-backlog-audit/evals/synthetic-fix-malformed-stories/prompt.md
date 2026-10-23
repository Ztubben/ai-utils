---
tags: [ralph-backlog-audit]
max_turns: 40
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

#60 and #63 never get picked up by ralph, I think they're malformed. fix them up so they're proper ralph stories. gh can't reach the repo from here — the export is in issues.json (and repo.bundle has the code) — so write each corrected issue as 60.json / 63.json in the same gh --json shape, and give me the exact gh commands to apply them.

Input files: issues.json, prs.json, repo.bundle
