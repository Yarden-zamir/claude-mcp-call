# claude-mcp-call

Call tools of MCP servers and claude.ai connectors from scripts, through Claude Code, with no model call and no tokens.

It uses the connections that Claude Code already has: plugins, `claude mcp add` servers and claude.ai connectors with their logins.

## Install

```sh
uv tool install git+https://github.com/Yarden-zamir/claude-mcp-call
```

It needs [Claude Code](https://code.claude.com) on `PATH`, logged in. It has no other dependencies.

## Use

From the shell:

```sh
claude-mcp-call "claude.ai Microsoft 365" teams_list_channels '{"teamId": "<team id>"}'
claude-mcp-call plugin:slack:slack slack_read_channel '{"channel_id": "C0123456789", "limit": 10}'
```

The server name is the name that `claude mcp list` shows.

From Python, as a dependency (`uv add git+https://github.com/Yarden-zamir/claude-mcp-call`):

```python
from claude_mcp_call import Call, call, call_each, call_many

channels = call("claude.ai Microsoft 365", "teams_list_channels", {"teamId": "<team id>"})

# Calls in one call_many run in one Claude Code session, in parallel.
teams, slack = call_many([
    Call("claude.ai Microsoft 365", "teams_list_channels", {"teamId": "<team id>"}),
    Call("plugin:slack:slack", "slack_list_user_channels", {"limit": 200}),
])
```

Each answer is the text that the tool returned. A tool error raises `ToolFailed`.
`call_each` returns a `ToolFailed` in place of each failed answer and keeps the other answers. Use it when one server can be down.

## Prove that no model runs

```sh
claude-mcp-call --verify
claude-mcp-call --verify "<server>" <tool> '<json input>'
```

1. A local trap server takes the model API. A normal prompt must send its model request there. Otherwise Claude Code ignores the block.
2. A session of this tool must send no model request. With a server and a tool, that tool must also answer.

Run it again after each Claude Code update.

## How it works

1. Claude Code runs each tool in an `mcp_tool` hook on `UserPromptSubmit`.
2. A command hook in the same group always exits 2. Claude Code then blocks the prompt, so the model never runs.
3. The answers come back as `hook_response` events in `--output-format stream-json`.

Three checks make sure that no tokens are used:

- The model API of each session points at a closed local port (`ANTHROPIC_BASE_URL=http://127.0.0.1:9`).
  The tool sets it in the process environment and in its `--settings` file, which beats user settings.
  MCP servers and claude.ai connectors do not use this URL, so they keep working.
- After each session, `num_turns` must be 0 and `total_cost_usd` must be 0. Otherwise it raises `ModelWasCalled`.
  `SESSIONS` holds `(num_turns, total_cost_usd)` of each session, for your logs.
- Each session asks for the smallest model (`--model haiku`), in case all of the above fail.

## Limits

- Each session takes 2 to 10 seconds: Claude Code starts and connects to the MCP servers.
- A hook gets only text. A tool that answers with binary content (an image, a file) gives `[resource]`.
- Managed (enterprise) settings can override the model API URL. `--verify` shows it when that happens.
- When a plugin and a claude.ai connector point at the same server, Claude Code suppresses the plugin.
  Turn off the connector with `--deny "<connector name>"` or `deny=[...]`.
