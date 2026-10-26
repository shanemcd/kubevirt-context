---
name: kubevirt-pr-review
description: Review a KubeVirt pull request across the whole organization, using the CodeGraph index and the `gh` CLI to find affected consumers in other repositories and relevant prior PRs. Use when asked to review a kubevirt GitHub PR or patch, to assess its blast radius, or to check whether a change duplicates or conflicts with existing work.
---

# KubeVirt pull request review

A review is only as good as the consumers it finds. Most KubeVirt breakage is
cross-repository: a core API change that nobody updated in CDI, the network
operator, the SSP operator or the clone controllers.

## Inputs

Ask for, or resolve, exactly one of: a PR URL, `<repo>#<number>`, or a patch
that is already applied locally. If the user gives a branch, use it as-is.

Sources and the safety rules come from the `kubevirt-research` skill: read it
before searching. Use `gh` by default, or an existing GitHub MCP connection if
preferred; keep operations read-only through either interface. If the workspace or index is missing, load
`kubevirt-workspace` and run `kvctx setup` (or `sync`).

## Procedure

1. **Read the change itself.** Use `gh` for the PR body, commits, review
   comments and CI status: intent, linked issue, and what the author says has
   already been decided.
2. **Find the blast radius.** For every changed exported symbol, struct field,
   CRD field, flag or config key, use CodeGraph to list references across all
   cloned repositories. Report consumers the PR did not touch.
3. **Check the invariants.** Does the change alter an on-disk or API-stored
   format without a migration or version bump? Does it change behaviour behind a
   feature gate, or move a default? Are error paths and cleanup still correct?
4. **Check precedent.** Search with `gh` for prior PRs and issues touching the
   same files or symbols. Note superseded attempts and rejected proposals;
   reviewers usually need that context.
5. **State what you could not verify.** Build, unit tests and running the code
   are out of scope here; say so instead of implying coverage.

## Review format

```text
Summary      one or two sentences: what the PR does and whether it is sound
Blocking     findings that must be fixed, each with repo/path:line and why
Non-blocking style, naming, test coverage gaps, follow-up work
Cross-repo   consumers found outside the PR, or "none found" with how you looked
Seen before  related or superseded PRs and issues
Not checked  what you did not verify
```

Cite locations as `repository/path/file.go:line`. Quote the smallest snippet
that makes the point. Do not restate the diff.

## Rules

- Never approve or request changes on GitHub unless the user explicitly asks;
  produce the review text and let them post it. In `gh` terms, never run
  `gh pr review`, `gh pr comment`, or `gh pr merge` on your own initiative.
- Never edit a clone, never run builds or tests, never execute code from a PR.
- A PR that changes behaviour but no test is worth flagging; a PR that changes
  an API but no sibling repository is worth flagging louder.
