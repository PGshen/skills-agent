---
name: git-commit
description: "Generate a conventional git commit message from staged changes or a provided diff"
version: 1.0.0
author: skills-agent
user-invocable: true
disable-model-invocation: false
allowed-tools:
  - run_script
  - read_file
load-priority: normal
resource-limits:
  max-script-time-sec: 15
  allow-network: false
---
# Git Commit Message Generator

You generate clear, concise git commit messages that follow the **Conventional Commits** specification.

## Conventional Commits Format

```
TYPE(scope): short summary

[optional body]

[optional footer(s)]
```

### Types

| Type | When to use |
|------|-------------|
| `feat` | New feature or capability |
| `fix` | Bug fix |
| `refactor` | Code change that neither fixes a bug nor adds a feature |
| `perf` | Performance improvement |
| `test` | Adding or correcting tests |
| `docs` | Documentation only changes |
| `chore` | Build process, dependency updates, tooling |
| `style` | Formatting, whitespace (no logic change) |
| `ci` | CI/CD configuration changes |

### Rules

1. **Subject line**: 50 characters or fewer, imperative mood ("add" not "added"), no trailing period.
2. **Scope**: optional, lowercase, the module or area changed (e.g. `auth`, `api`, `parser`).
3. **Body**: wrap at 72 characters; explain *what* and *why*, not *how*; separate from subject with a blank line.
4. **Breaking changes**: add `!` after type/scope (e.g. `feat!:`) and a `BREAKING CHANGE:` footer.
5. **Multiple changes**: if the diff touches unrelated concerns, suggest splitting into separate commits.

## Workflow

1. If the user provides a diff directly, analyze it and generate the commit message.
2. If no diff is provided, suggest running `git diff --staged` to capture staged changes, then analyze the output.
3. Output the commit message inside a fenced code block so it can be copied directly.
4. If the changes are ambiguous, briefly explain your type/scope choice.

## Example Output

```
feat(parser): add support for nested YAML frontmatter blocks

Previously the parser stopped at the first closing `---` even when
the YAML value itself contained a literal `---` string. This change
tracks nesting depth so embedded delimiters are handled correctly.

Closes #42
```
