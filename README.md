# gh-llm

Structured GitHub context for LLMs — read PRs, issues, and repos the way GitHub Web shows them, as LLM-friendly text with actionable commands.

<p align="center">
  <a href="https://python.org/" target="_blank"><img alt="PyPI - Python Version" src="https://img.shields.io/pypi/pyversions/gh-llm?logo=python&style=flat-square"></a>
  <a href="https://pypi.org/project/gh-llm/" target="_blank"><img src="https://img.shields.io/pypi/v/gh-llm?style=flat-square" alt="pypi"></a>
  <a href="https://pypi.org/project/gh-llm/" target="_blank"><img alt="PyPI - Downloads" src="https://img.shields.io/pypi/dm/gh-llm?style=flat-square"></a>
  <a href="LICENSE"><img alt="LICENSE" src="https://img.shields.io/github/license/ShigureLab/gh-llm?style=flat-square"></a>
  <br/>
  <a href="https://github.com/astral-sh/uv"><img alt="uv" src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json&style=flat-square"></a>
  <a href="https://github.com/astral-sh/ruff"><img alt="ruff" src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json&style=flat-square"></a>
  <a href="https://gitmoji.dev"><img alt="Gitmoji" src="https://img.shields.io/badge/gitmoji-%20😜%20😍-FFDD67?style=flat-square"></a>
</p>

## Core Goal

`gh-llm` gives you (or your AI agent) the same context a human reviewer gets on GitHub Web — rendered as structured text with ready-to-run follow-up commands at every decision point.

## Key Ideas

- **Timeline-first rendering** — Comments, reviews, commits, labels, references, force-pushes, and state changes merge into one ordered stream that mirrors the GitHub Web experience.
- **Real cursor pagination** — Uses GitHub GraphQL `first/after` and `last/before` cursors, so page expansion always fetches real server-side data instead of slicing locally.
- **Progressive context loading** — Shows the first + last page first (high-signal summary), then expands hidden pages or events only on request.
- **Action-oriented output** — Places ready-to-run `gh` / `gh-llm` commands at decision points: expand, view detail, reply, resolve, review.
- **Stateless interaction model** — No fragile local session state between commands.

## Requirements

- Python `3.14+`
- `gh` installed and authenticated (`gh auth status`)

## Install

### As CLI (recommended)

```bash
uv tool install gh-llm
gh-llm --help
```

### As gh extension

```bash
gh extension install ShigureLab/gh-llm
gh llm --help
```

The extension entrypoint forwards to local repository path via `uv run --project <extension_repo_path> gh-llm ...`.
`gh llm ...` and `gh-llm ...` are equivalent command surfaces.

## Install Skill

If you want the reusable GitHub conversation skill, install it directly from this repo:

```bash
npx skills add https://github.com/ShigureLab/gh-llm --skill github-conversation
```

## Quick Start

### PR Reading

Read a PR's full timeline — metadata, comments, reviews, checks — with progressive expansion:

```bash
# Initial read: show first + last timeline pages with actionable hints
gh-llm pr view 77900 --repo PaddlePaddle/Paddle
gh llm pr view 77900 --repo PaddlePaddle/Paddle

# Later incremental read: reuse the previous frontmatter `fetched_at`
gh-llm pr view 77900 --repo PaddlePaddle/Paddle --after 2026-04-08T02:41:17Z

# Show selected regions only
gh-llm pr view 77900 --repo PaddlePaddle/Paddle --show timeline,checks

# Expand one hidden timeline page
gh-llm pr timeline-expand 2 --pr 77900 --repo PaddlePaddle/Paddle
gh-llm pr timeline-expand 2 --pr 77900 --repo PaddlePaddle/Paddle --after 2026-04-08T02:41:17Z

# Auto-expand folded content in default/timeline view
gh-llm pr view 77900 --repo PaddlePaddle/Paddle --expand resolved,minimized
gh-llm pr timeline-expand 2 --pr 77900 --repo PaddlePaddle/Paddle --expand all

# Show full content for one comment node id
gh-llm pr comment-expand IC_xxx --pr 77900 --repo PaddlePaddle/Paddle

# Expand resolved review details in batch
gh-llm pr review-expand PRR_xxx,PRR_yyy --pr 77900 --repo PaddlePaddle/Paddle
# Expand only a conversation range (e.g. hidden middle part)
gh-llm pr review-expand PRR_xxx --threads 6-16 --pr 77900 --repo PaddlePaddle/Paddle

# Checks
gh-llm pr checks --pr 77900 --repo PaddlePaddle/Paddle
gh-llm pr checks --pr 77900 --repo PaddlePaddle/Paddle --all

# Detect conflicted files on demand (for conflicted PRs)
gh-llm pr conflict-files --pr 77971 --repo PaddlePaddle/Paddle
```

### PR Body Scaffold

Generate a PR body from the repo's template (or a default scaffold) with required sections pre-filled:

```bash
# Load the repo PR template (when present), append required sections, and write a body file
# The command also prints a ready-to-run `gh pr create --body-file ...` command.
gh-llm pr body-template --repo ShigureLab/watchfs --title 'feat: add watcher summary'

gh-llm pr body-template \
  --repo ShigureLab/watchfs \
  --requirements 'Motivation,Validation,Related Issues' \
  --output /tmp/pr_body.md
```

If the repo has no PR template, `gh-llm` falls back to a simple editable scaffold.
The bundled `skills/github-conversation/SKILL.md` also documents this workflow for skill users.

### Issue Reading

Issue reading works the same way as PR reading — timeline view with progressive expansion:

```bash
gh-llm issue view 77924 --repo PaddlePaddle/Paddle
gh-llm issue view 77924 --repo PaddlePaddle/Paddle --after 2026-04-08T02:41:17Z
gh-llm issue timeline-expand 2 --issue 77924 --repo PaddlePaddle/Paddle
gh-llm issue timeline-expand 2 --issue 77924 --repo PaddlePaddle/Paddle --after 2026-04-08T02:41:17Z
gh-llm issue comment-expand IC_xxx --issue 77924 --repo PaddlePaddle/Paddle
gh-llm issue view 77924 --repo PaddlePaddle/Paddle --expand minimized,details
gh-llm issue view 77924 --repo PaddlePaddle/Paddle --show meta,description
```

For incremental follow-ups, copy the previous output's `fetched_at` value into `--after <fetched_at>`. `--before` is also available when you want to inspect only older timeline slices.

When `--show` does not include `timeline` (for example `--show meta`, `--show summary`, or `--show actions`), both `pr view` and `issue view` stay on the lightweight metadata path and skip timeline bootstrap.

Use `--show` to choose which output sections to render. Use `--expand` to automatically open folded content within those sections.

`--expand` values:

- PR: `resolved`, `minimized`, `details`, `all`
- Issue: `minimized`, `details`, `all`
- Supports comma-separated values and repeated flags.

`--show` values:

- PR: `meta`, `description`, `timeline`, `checks`, `actions`, `mergeability`, `all`
- Issue: `meta`, `description`, `timeline`, `actions`, `all`
- Supports comma-separated values and repeated flags.
- `summary` is supported as an alias for `meta,description`.

### Comment / Thread Actions

Edit comments, reply to review threads, and resolve/unresolve threads directly from the CLI:

```bash
# Edit comment
gh-llm pr comment-edit IC_xxx --body '<new_body>' --pr 77900 --repo PaddlePaddle/Paddle
gh-llm pr comment-edit IC_xxx --body-file edit.md --pr 77900 --repo PaddlePaddle/Paddle
gh-llm issue comment-edit IC_xxx --body '<new_body>' --issue 77924 --repo PaddlePaddle/Paddle
gh-llm issue comment-edit IC_xxx --body-file edit.md --issue 77924 --repo PaddlePaddle/Paddle

# Reply / resolve / unresolve review thread
gh-llm pr thread-reply PRRT_xxx --body '<reply>' --pr 77900 --repo PaddlePaddle/Paddle
gh-llm pr thread-reply PRRT_xxx --body-file reply.md --pr 77900 --repo PaddlePaddle/Paddle
cat reply.md | gh-llm pr thread-reply PRRT_xxx --body-file - --pr 77900 --repo PaddlePaddle/Paddle
gh-llm pr thread-resolve PRRT_xxx --pr 77900 --repo PaddlePaddle/Paddle
gh-llm pr thread-unresolve PRRT_xxx --pr 77900 --repo PaddlePaddle/Paddle
```

### Environment Diagnosis

Verify your setup — `doctor` checks `gh` auth, connectivity, and proxy configuration:

```bash
gh-llm doctor
gh llm doctor
```

`doctor` prints the current entrypoint, resolved executable paths, `gh` / `gh-llm` versions,
active-host `gh auth status`, a REST probe, a minimal GraphQL probe, and proxy-related environment variables.

When `gh-llm` hits transport errors such as GraphQL `EOF` / timeout failures, the CLI now reports the
retry count and suggests concrete follow-up commands such as `gh auth status`,
`gh api graphql -f query='query{viewer{login}}'`, and `gh-llm doctor`.

## PR Review Workflow

A complete code review in four steps — start from diff hunks, add inline comments or suggestions, then submit:

### 1) Start from diff hunks

```bash
gh-llm pr review-start --pr 77938 --repo PaddlePaddle/Paddle

# Large PRs: load the next changed-file page
gh-llm pr review-start --pr 78255 --repo PaddlePaddle/Paddle --page 2 --page-size 5

# Jump to an absolute changed-file range directly
gh-llm pr review-start --pr 78255 --repo PaddlePaddle/Paddle --files 6-12

# Add extra unchanged context around each hunk
gh-llm pr review-start --pr 77938 --repo PaddlePaddle/Paddle --context-lines 3

# Focus one changed file directly
gh-llm pr review-start --pr 78255 --repo PaddlePaddle/Paddle --path 'paddle/phi/api/include/compat/ATen/core/TensorBody.h'

# Show only selected hunks inside that file
gh-llm pr review-start --pr 78255 --repo PaddlePaddle/Paddle --path 'TensorBody.h' --hunks 2-3

# Reuse a pinned head snapshot when loading another page
gh-llm pr review-start --pr 78255 --repo PaddlePaddle/Paddle --page 2 --page-size 5 --head <head_sha>
```

It prints changed-file page summary, existing review-thread summaries with lightweight comment previews inline on matching diff lines when possible, per-hunk commentable LEFT/RIGHT line ranges, numbered diff lines, and ready-to-run comment/suggestion commands.
Generated follow-up commands reuse `--head <head_sha>` automatically so pagination and inline review commands stay on the same PR snapshot; stale snapshots are rejected with a refresh hint.
Use `--context-lines <n>` when the GitHub patch hunk is too tight and you need a small amount of extra unchanged code around it.

### 2) Add inline comment

```bash
gh-llm pr review-comment \
  --path 'paddle/phi/api/include/compat/torch/library.h' \
  --line 106 \
  --side RIGHT \
  --body 'Please add a regression test for duplicate keyword arguments.' \
  --pr 77938 --repo PaddlePaddle/Paddle

gh-llm pr review-comment \
  --path 'paddle/phi/api/include/compat/torch/library.h' \
  --line 106 \
  --side RIGHT \
  --body-file review-comment.md \
  --pr 77938 --repo PaddlePaddle/Paddle
```

### 3) Add inline suggestion

```bash
gh-llm pr review-suggest \
  --path 'path/to/file' \
  --line 123 \
  --side RIGHT \
  --body 'Suggested update' \
  --suggestion 'replacement_code_here' \
  --pr 77938 --repo PaddlePaddle/Paddle

gh-llm pr review-suggest \
  --path 'path/to/file' \
  --line 123 \
  --side RIGHT \
  --body-file suggestion-reason.md \
  --suggestion 'replacement_code_here' \
  --pr 77938 --repo PaddlePaddle/Paddle

gh-llm pr review-suggest \
  --path 'path/to/file' \
  --line 123 \
  --side RIGHT \
  --body-file suggestion-reason.md \
  --suggestion-file replacement.txt \
  --pr 77938 --repo PaddlePaddle/Paddle
```

### 4) Submit review

```bash
gh-llm pr review-submit \
  --event COMMENT \
  --body 'Overall feedback...' \
  --pr 77938 --repo PaddlePaddle/Paddle

gh-llm pr review-submit \
  --event REQUEST_CHANGES \
  --body-file review.md \
  --pr 77938 --repo PaddlePaddle/Paddle
```

Pick the strongest explicit review outcome the evidence supports:

- `APPROVE`: ready to merge from your side
- `REQUEST_CHANGES`: blocking issues remain
- `COMMENT`: non-blocking notes or intermediate status only

`pr comment-edit`, `issue comment-edit`, `thread-reply`, `review-comment`, `review-suggest`, and `review-submit` all support `--body-file -` to read multi-line text from standard input. `review-suggest` also supports `--suggestion-file -` for the suggestion block itself.

> Note: `review-suggest --body-file - --suggestion-file -` is intentionally rejected because standard input can only be consumed once. Use separate files when both the reason text and suggestion block need external input.

## Multiline body safety

GitHub stores body text exactly as sent. If you pass literal escape sequences such as `\n\n` inside `--body`, those backslashes may be stored literally and show up in the final review/comment.

Use `--body` for short one-line text. Use `--body-file` for quotes, multiple paragraphs, bullet lists, and code fences:

```bash
cat <<'EOF' > /tmp/review.md
> Reviewer point

Fixed in `python/demo.py:42`.
Validation: `pytest test/demo_test.py -q`
EOF

gh-llm pr thread-reply PRRT_xxx --body-file /tmp/review.md --pr 77938 --repo PaddlePaddle/Paddle
gh-llm pr review-submit --event COMMENT --body-file /tmp/review.md --pr 77938 --repo PaddlePaddle/Paddle
gh pr comment 77938 --repo PaddlePaddle/Paddle --body-file /tmp/review.md
```

Submit behavior:

- If you already have a pending review on this PR, `review-submit` submits that pending review.
- Otherwise, it creates and submits a new review.

This supports the normal flow where one review contains multiple inline comments.

## Render Conventions

All output follows consistent formatting rules so both humans and LLMs can parse it reliably:

- **Metadata** is rendered as YAML-style frontmatter at the top of PR/issue views.
- Frontmatter includes `fetched_at`, so the next incremental read can use `--after <fetched_at>`.
- When timeline filtering is active, frontmatter also includes `timeline_after` / `timeline_before` and filtered vs unfiltered event counts.
- **Description** is wrapped in `<description>...</description>` tags.
- **Comment bodies** use `<comment>...</comment>` tags to avoid markdown fence ambiguity with code blocks inside comments.
- **Hidden timeline sections** are separated by `---` dividers and include ready-to-run expand commands to load the omitted content.

## Development

```bash
uv run ruff check
uv run pyright
uv run pytest -q
```

## License

MIT
