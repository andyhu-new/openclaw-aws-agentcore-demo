---
name: clawhub-manage
description: Install, uninstall, and list ClawHub community skills. Use when the user asks to install a new skill, remove an existing skill, or see what skills are available. Skills are installed globally in the container and available after OpenClaw restarts or on new sessions.
allowed-tools: Bash(node:*)
---

# ClawHub Skill Manager

Install, uninstall, and list ClawHub community skills from the marketplace.

## Usage

### install_skill

Install a community skill from the ClawHub marketplace.

```bash
node {baseDir}/install.js <skill_name>
```

- `skill_name` (required): The skill name from ClawHub (e.g., `baidu-search`, `reddit-readonly`)

### uninstall_skill

Remove a previously installed skill.

```bash
node {baseDir}/uninstall.js <skill_name>
```

- `skill_name` (required): The skill name to remove

### list_skills

List all installed ClawHub skills.

```bash
node {baseDir}/list.js
```

## From Agent Chat

- "Install baidu-search skill" -> install_skill with `baidu-search`
- "Add the reddit-readonly skill" -> install_skill with `reddit-readonly`
- "Remove the transcript skill" -> uninstall_skill with `transcript`
- "What skills are installed?" -> list_skills
- "Show me available skills" -> list_skills

## Notes

- After install/uninstall, the skill will be loaded/unloaded on the next session start (after idle timeout or new conversation) (web search, file storage, scheduling)
- Only valid ClawHub skill names are accepted (letters, numbers, hyphens)
- Pre-installed skills: jina-reader, deep-research-pro, telegram-compose, transcript, task-decomposer
