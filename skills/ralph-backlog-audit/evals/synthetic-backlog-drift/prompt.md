---
tags: [ralph-backlog-audit]
max_turns: 40
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

can you go through our firmware backlog and tell me what's out of date, blocked for no reason, or malformed? gh can't reach the repo from this box so I exported everything: issues.json is `gh issue list --state all --json number,title,labels,body,state,stateReason,updatedAt,closedAt`, prs.json is the PRs, and repo.bundle is the git history (main). don't change anything yet — write the report to backlog-audit.md

Input files: issues.json, prs.json, repo.bundle
