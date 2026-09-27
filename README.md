<h1 align="center">Specgate</h1>

<p align="center">
  <em>Coding agents improvise on spec questions. Specgate asks Jev and keeps the answer typed.</em>
</p>

<p align="center">
  <img src="docs/specgate-cover.jpg" alt="Specgate" width="880">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/works%20with-Codex%20%C2%B7%20Claude%20Code%20%C2%B7%20Cursor%20%C2%B7%20Grok%20Build%20%C2%B7%20Grok%20Bot-111111?style=flat-square" alt="Works with Codex, Claude Code, Cursor, Grok Build and Grok Bot">
  <img src="https://img.shields.io/badge/license-MIT-111111?style=flat-square" alt="MIT license">
  <img src="https://img.shields.io/badge/python-3.12%2B-111111?style=flat-square" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/platform-macOS%20%C2%B7%20Linux-111111?style=flat-square" alt="macOS and Linux">
</p>

<p align="center">
  <strong>Grill, spec, tickets, implement &middot; one plugin for every harness on the machine &middot; human review until a real gate exists</strong>
</p>

---

This repository is the MIT client: Python package, skills and harness adapters. It does not contain the MCP server, prompts, calibration or API-key admin.

```
pipx install git+https://github.com/FelipeOFF/specgate.git
specgate install
specgate doctor --project .
```

`pipx install specgate-client==<version>` installs the same `specgate` command once that version is on PyPI. Until then, use the git command above.

Then start `codex`, `claude`, Cursor, `grok` or Grok Bot as usual. That is the whole setup.

macOS or Linux, Python 3.12+, [pipx](https://pipx.pypa.io/). The installer lists the harnesses on this machine with their limits, lets you toggle each one (`a` installs on all of them), asks for the MCP host and API key, and stores the key in an owner-only file. Never pass the key on the command line. `--harness` and `--host` skip those prompts; `--yes` takes every detected harness.

## What it does

A coding agent can write a spec, open tickets and implement a slice without ever deciding a typed question against project evidence. Grill questions become chat. Acceptance criteria become prose. A "looks good" from the same model that wrote the patch is not a verdict.

This package installs skills and an MCP client into the agents you already use. The harness keeps writing. [Jev](https://docs.typesafe.ai/concepts/system-one), TypeSafe's decision model, answers a typed question about the evidence you authorized: a choice, a claim, a screen, a ranking. The MCP call stays attached to the turn. There is no daemon, no callback later, no merge from a model.

- One install for every harness it finds. `--harness` selects; `--yes` takes all of them.
- Skills ship in English and run the same flow everywhere: `triage-jev` → `grill-with-jev` → `to-spec-jev` → `to-tickets-jev` → `implement-spec-jev` → `verify-spec-jev`. `iterate-with-jev` sits beside that flow: a `/iterate` comment is judged before the pull request branch moves.
- Your MCP host, your API key. Model keys stay on the server. This package never logs the credential.
- If the MCP is down, the harness still runs. The decision stays in human review. One warning per session.

Automatic advance stays off until the host operator installs a real calibration gate. Mock, replay and contract fixtures do not open it.

## Why a decision model

Asking the same LLM that is writing the code "is this in scope?" returns more prose. You then have to parse it, and you cannot treat the answer as a verdict.

Jev answers a typed question with a structured result. `jev_decide` returns a choice. `jev_verify` returns verified, contradicted or unsupported. `jev_screen` returns injection, substance and relevance. `jev_find` ranks candidates. There is nothing to quote back into the prompt as if it were evidence.

That is what makes it reasonable to put a decision on every grill question, every acceptance criterion and every implement gate, without turning the session into another chat with the author of the patch.

## Three minutes to the first routed skill

1. You need a Specgate MCP host (`https://…/mcp`) and an API key for it. The key is not a TypeSafe or OpenRouter key; those live on the server.
2. Run `pipx install git+https://github.com/FelipeOFF/specgate.git`, then `specgate install --host https://YOUR_MCP/mcp`. Pick the harnesses it found (`a` for all), or pass `--yes`. Paste the API key when asked; it is written to `~/.config/specgate/credentials/default.key` with mode `0600`.
3. Run `specgate doctor --project .` in the repo you will work in. It prints one line per harness. Add `--json` for the full report. Doctor checks runtime, handshake, auth and the tool catalog. It does not start a model turn.
4. Open the harness. Use `grill-with-jev` on a real scope question.

A question Jev can close from `CONTEXT.md` and the authorized roots comes back as a typed result. A business choice, an authorization, or a gap after three evidence retries stays with you.

## Agents

| Agent | Install | Where it lands |
| --- | --- | --- |
| Codex | `specgate install --harness codex` | `~/.agents/skills`, `~/.local/bin/specgate-codex`, Codex MCP `specgate`, `UserPromptSubmit` in `$CODEX_HOME/hooks.json` (default `~/.codex`) |
| Claude Code | `specgate install --harness claude-code` | `~/.claude.json` (`mcpServers.specgate`, user scope), hooks in `~/.claude/settings.json`, `~/.claude/skills` |
| Cursor | `specgate install --harness cursor` | `~/.cursor/mcp.json`, `~/.cursor/skills`, `~/.local/bin/specgate-cursor` |
| Grok Build | `specgate install --harness grok-build` | `~/.grok/config.toml`, `~/.grok/skills`, `~/.local/bin/specgate-grok-build` |
| Grok Bot | `specgate install --harness grok-bot` | `~/.specgate/grok-bot` |

`install` with no `--harness` detects what is on this machine and asks once per agent. Repeat installs keep foreign skills and config they do not own.

Codex installs a native `UserPromptSubmit` hook and retains the controlled app-server client for structured questions. After installation or a changed hook command, review the hook in Codex `/hooks`; installation does not grant native trust. The prompt hook sends only the current prompt and the public Specgate bundle to the configured MCP. It consults Codex's enabled skills locally, checks installed revisions, and excludes disabled or modified skills. Personal catalogs, project files and native session metadata are not sent by this adapter. A remote review gate never becomes an automatic selection: the hook supplies its status instead. Doctor inspects installation without proving runtime execution or calibration. Install/update/uninstall preserve other hooks and refuse to overwrite a modified managed entry.

Claude Code uses native `UserPromptSubmit` and `PreToolUse` hooks plus user-scoped `mcpServers.specgate` in `~/.claude.json`. Cursor writes `~/.cursor/mcp.json`. Grok Build merges `[mcp_servers.specgate]` into `~/.grok/config.toml` (and `~/.grok/mcp.json` if that file already exists); the wrapper still injects the key for the ACP client. Grok Bot has no public interception contract: after install, copy `~/.specgate/grok-bot/specgate/GROK_BOT.md` as a private skill and attach the Custom MCP in the product. Doctor reports that limit instead of pretending the roundtrip was tested.

Original binaries stay on `PATH`. Wrappers inject `SPECGATE_MCP_API_KEY` (and `DEV_DECISION_MCP_API_KEY` for old clients) into that process. Install also publishes those variables for every new shell and the graphical login session, reading the credential file each time. The key is not copied into shell rc files or the login agent. Restart the harness after install.

## Skills

| Skill | What it does |
| --- | --- |
| `triage-jev` | Classify a tracker issue. Labels change only after the maintainer confirms. |
| `grill-with-jev` | Close the scope. Up to three evaluations per question, each retry with new evidence. |
| `to-spec-jev` | Authorized research and a versioned spec. `jev_verify` covers the claims. |
| `to-tickets-jev` | Vertical ticket graph. One repository per ticket. |
| `implement-spec-jev` | Walk the unblocked frontier in a worktree. |
| `verify-spec-jev` | Last gate: every acceptance criterion against worktree evidence, before a draft PR. |
| `iterate-with-jev` | Judge a `/iterate` comment with `jev_verify` before any push. Does not merge. |

`verify-spec-jev` does not close issues, merge pull requests or publish packages. Those stay human. `iterate-with-jev` does not merge either.

## Commands

| Command | What it does |
| --- | --- |
| `specgate install` | Detect harnesses, store the MCP key, install skills, wrappers and native MCP entries |
| `specgate doctor` | One status line per harness. `--json` for the full report |
| `specgate smoke` | Attached handshake + route + one mock tool. No paid inference |
| `specgate update` | Explicit. Refresh managed skills and wrappers from this package |
| `specgate uninstall` | Remove plugin-managed files only |

There is no auto-update. A failed install rolls back every destination in that transaction, including a harness that succeeded before a later one failed.

From a clone of this repository:

```
python3 -m venv .venv
.venv/bin/pip install .
.venv/bin/specgate install --host https://YOUR_MCP/mcp
.venv/bin/specgate doctor --project .
```

## This repository

| Path | What |
| --- | --- |
| `src/specgate/` | Public Python client and harness adapters |
| `src/specgate/skills/` | `triage-jev`, `grill-with-jev`, `to-spec-jev`, `to-tickets-jev`, `implement-spec-jev`, `verify-spec-jev`, `iterate-with-jev` |
| `scripts/verify_release.py` | Version lock and SHA-256 of the exported tree |

## Privacy and safety

- The API key is never a flag, never in harness config, never logged. Lookups: the credential file, or `SPECGATE_MCP_API_KEY` (fallback `DEV_DECISION_MCP_API_KEY`) for local automation (still written only to that file).
- Wrappers live in `~/.local/bin/specgate-*`. Profile: `~/.config/specgate/profiles/default.json`. The Python runtime is the pipx environment for `specgate-client`.
- Context sent to the MCP is the evidence you authorized for that call. Model provider keys never leave the server process.
- The client talks to the host over HTTPS (HTTP only on loopback) and uses the system CAs.
- No MCP, auth failure, or a protocol major mismatch: the harness continues and the decision stays in review.
- `action=auto` is not permission to merge, deploy, or publish. Without a calibration manifest, `jev_decide` still returns `action=auto` for one real option strictly above 0.80. Verify, screen and find stay at `calibrated=false` and `auto_advance=false`.

## Uninstall

```
specgate uninstall
pipx uninstall specgate-client
```

Removes the managed profile, credential, wrappers, Specgate MCP catalog entries and skills this plugin wrote. Harness config it did not create stays. `pipx uninstall` removes the CLI itself.

## License

[MIT](LICENSE)
