# KubeVirt Context

Organization-wide context for AI coding agents working on [KubeVirt](https://github.com/kubevirt). `kvctx` clones every active, non-fork repository and builds a cross-repository [CodeGraph](https://github.com/colbymchenry/codegraph) index. Agent skills guide setup, research, PR reviews, and workspace maintenance.

Works with [Pi](https://github.com/earendil-works/pi), [Claude Code](https://claude.com/claude-code), [Codex](https://developers.openai.com/codex/cli/), [Cursor Agent](https://cursor.com/), and [OpenCode](https://opencode.ai/).

## Get started

### 1. Install kvctx

With [uv](https://docs.astral.sh/uv/) installed:

```sh
uv tool install --from git+https://github.com/codingben/kubevirt-context kubevirt-context
```

### 2. Install the skills

```sh
kvctx skills install
```

Skills go into `~/.agents/skills` by default. Pass a directory if your agent uses another location. Reload or restart your agent to discover them.

### 3. Invoke the setup skill

Invoke the `kubevirt-setup` skill using your agent's skill mechanism, or ask it to **"use kubevirt-setup to set up the KubeVirt workspace"**.

The skill checks Git, authenticated `gh`, CodeGraph, and the agent's live CodeGraph MCP access. It explains missing prerequisites, confirms the repository scope, then clones and indexes the workspace and verifies that queries work.

Data lives in `~/.local/share/kubevirt-context` by default; set `KUBEVIRT_CONTEXT_HOME` to choose another location. Full setup can take minutes and substantial disk space. Ask for a smaller trial if you prefer.

## Skills

| Skill | Purpose |
|---|---|
| `kubevirt-context` | Orientation and routing to the right task skill |
| `kubevirt-setup` | First-time setup and verification |
| `kubevirt-doctor` | Diagnose tooling and connection problems |
| `kubevirt-workspace` | Sync repositories, refresh the index, inspect status, and clean up |
| `kubevirt-research` | Trace architecture, implementations, and consumers across repositories |
| `kubevirt-pr-review` | Review a PR, including its cross-repository impact |

After setup, ask questions such as **"how does VM cloning work across repositories?"** or **"review this KubeVirt PR for affected consumers."** GitHub history uses `gh` by default; an existing GitHub MCP connection can also be used.

Research clones are read-only snapshots: the skills do not build, test, edit, or execute repository code. Workspace operations leave your agent configuration alone.

See [docs/skills.md](docs/skills.md) for details, or run `kvctx --help` for CLI commands.

## Uninstall

Remove the installed skills before uninstalling the tool:

```sh
kvctx skills uninstall
uv tool uninstall kubevirt-context
```

Pass the same directory used during installation if it was not the default. Only manifest-owned skills are removed; locally modified skills require `--force`. Other skills, agent configuration, and workspace data are left alone. Reload or restart your agent afterward.

## Development

Install from source:

```sh
git clone https://github.com/codingben/kubevirt-context.git
cd kubevirt-context
uv tool install --from . kubevirt-context
kvctx skills install
```

Reload or restart your agent, then invoke `kubevirt-setup` as above.

## License

[Apache License 2.0](LICENSE)
