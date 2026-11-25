"""Call tools of MCP servers and claude.ai connectors through Claude Code, with no model call.

Claude Code runs each tool in an `mcp_tool` hook on UserPromptSubmit. A command hook in the same group
always exits 2, so Claude Code blocks the prompt and the model never runs. The answers come back as
`hook_response` events. Two more checks make sure that no tokens are used:

1. The model API of each session points at a closed local port (ANTHROPIC_BASE_URL). A model call cannot
   reach Anthropic. MCP servers and claude.ai connectors do not use this URL, so they keep working.
2. After each session, `num_turns` must be 0 and `total_cost_usd` must be 0. Otherwise it raises ModelWasCalled.

Limit: each session takes 2 to 10 seconds (Claude Code start and the MCP connection).
Revisit when Claude Code ships a direct tool call.
"""

import argparse
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from typing import NamedTuple

GUARD = "claude-mcp-call guard"
# Claude Code writes this to stderr when a tool answered and the answer blocked the prompt.
ANSWERED = "The MCP tool's answer is not a hook's JSON output"
NO_MODEL_API = "http://127.0.0.1:9"
MODEL_PATH = "/v1/messages"

# (num_turns, total_cost_usd) of each session in this process. Log it to show that no model ran.
SESSIONS: list[tuple[int, float]] = []


class Call(NamedTuple):
    server: str  # the name that `claude mcp list` shows, for example "claude.ai Microsoft 365"
    tool: str
    input: dict


class ModelWasCalled(RuntimeError):
    pass


class ToolFailed(RuntimeError):
    pass


def settings_for(calls: list[Call], timeout: int, deny: list[str], model_api: str) -> dict:
    guard = {"type": "command", "command": f"echo '{GUARD}' >&2; exit 2"}
    tools = [
        {"type": "mcp_tool", "server": c.server, "tool": c.tool, "input": c.input, "onFailure": "block", "timeout": timeout}
        for c in calls
    ]
    return {
        # The env of a settings file beats the process env, and --settings beats user settings.
        # Only managed (enterprise) settings rank higher: `--verify` shows when they win.
        "env": {"ANTHROPIC_BASE_URL": model_api},
        "deniedMcpServers": [{"serverName": name} for name in deny],
        "hooks": {"UserPromptSubmit": [{"hooks": [guard, *tools]}]},
    }


def session(calls: list[Call], timeout: int = 120, deny: list[str] | None = None, model_api: str = NO_MODEL_API,
            config_dir: str | None = None) -> list[str | ToolFailed]:
    """One Claude Code session that runs `calls` in parallel. Return the answers in the order of `calls`.
    A call that failed gives a ToolFailed in its place. `config_dir` picks another Claude Code login (CLAUDE_CONFIG_DIR)."""
    with tempfile.NamedTemporaryFile("w", suffix=".json") as file:
        json.dump(settings_for(calls, timeout, deny or [], model_api), file)
        file.flush()
        run = subprocess.run(
            ["claude", "-p", "x", "--model", "haiku", "--settings", file.name, "--output-format", "stream-json", "--verbose", "--include-hook-events"],
            check=False, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout + 60,
            env=os.environ | {"ANTHROPIC_BASE_URL": model_api} | ({"CLAUDE_CONFIG_DIR": config_dir} if config_dir else {}),
        )
    started: list[str] = []
    responses: dict[str, dict] = {}
    result = None
    for line in run.stdout.splitlines():
        event = json.loads(line)
        if event.get("type") == "result":
            result = event
        if event.get("hook_event") != "UserPromptSubmit":
            continue
        if event.get("subtype") == "hook_started":
            started.append(event["hook_id"])
        if event.get("subtype") == "hook_response":
            responses[event["hook_id"]] = event
    assert result is not None, f"no result from claude: {run.stderr[-500:]}"
    SESSIONS.append((result.get("num_turns"), result.get("total_cost_usd")))
    if result.get("num_turns") != 0 or result.get("total_cost_usd"):
        raise ModelWasCalled(f"num_turns={result.get('num_turns')} cost={result.get('total_cost_usd')}")
    # Claude Code starts command hooks first, then mcp_tool hooks in the order of the settings.
    # Limit: hook_started events carry no input, so this order is the only link from answer to call.
    # Revisit when the events name the tool or its input.
    assert len(started) == len(calls) + 1, f"expected {len(calls) + 1} hooks, started {len(started)}"
    assert GUARD in responses[started[0]].get("stderr", ""), "the guard hook did not start first"
    answers = []
    for c, hook_id in zip(calls, started[1:], strict=True):
        response = responses[hook_id]
        if response["exit_code"] != 0 and not response.get("stderr", "").startswith(ANSWERED):
            answers.append(ToolFailed(f"{c.server}/{c.tool} {json.dumps(c.input)[:300]}: {response.get('stderr', '')[:500]}"))
        else:
            answers.append(response.get("stdout", ""))
    return answers


def call_many(calls: list[Call], timeout: int = 120, deny: list[str] | None = None, config_dir: str | None = None) -> list[str]:
    """Run all calls in one Claude Code session, in parallel. Return the answers in the order of `calls`.

    `deny` names MCP servers to turn off. Use it when a plugin and a claude.ai connector point at the same
    server: Claude Code then suppresses the plugin, and the hook finds it "not connected".
    `config_dir` picks another Claude Code login (CLAUDE_CONFIG_DIR), with its own connectors and MCP servers.
    """
    answers = call_each(calls, timeout, deny, config_dir)
    for answer in answers:
        if isinstance(answer, ToolFailed):
            raise answer
    return [str(answer) for answer in answers]


def call_each(calls: list[Call], timeout: int = 120, deny: list[str] | None = None, config_dir: str | None = None) -> list[str | ToolFailed]:
    """Like call_many, but a call that fails gives a ToolFailed in its place and the other answers still come back.
    Use it when one server can be down, for example before its login is done."""
    return session(calls, timeout, deny, config_dir=config_dir) if calls else []


def call(server: str, tool: str, input: dict | None = None, timeout: int = 120, deny: list[str] | None = None,
         config_dir: str | None = None) -> str:
    return call_many([Call(server, tool, input or {})], timeout, deny, config_dir)[0]


def verify(probe: Call | None = None, deny: list[str] | None = None) -> None:
    """Prove that the block works with this Claude Code version. A local trap server takes the model API:
    1. A normal prompt must send a model request to the trap. Otherwise Claude Code ignores ANTHROPIC_BASE_URL.
    2. A session of this tool must send none. With `probe`, that tool must also answer."""
    hits: list[str] = []

    class Trap(http.server.BaseHTTPRequestHandler):
        def refuse(self) -> None:
            hits.append(f"{self.command} {self.path}")
            self.send_response(503)
            self.end_headers()

        do_GET = do_POST = do_HEAD = refuse

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Trap)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        control = json.dumps({"env": {"ANTHROPIC_BASE_URL": url}})
        prompt = subprocess.Popen(["claude", "-p", "say hi", "--settings", control], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=os.environ | {"ANTHROPIC_BASE_URL": url})
        deadline = time.time() + 60
        while not any(MODEL_PATH in h for h in hits) and prompt.poll() is None and time.time() < deadline:
            time.sleep(0.2)
        prompt.kill()
        prompt.wait()
        assert any(MODEL_PATH in h for h in hits), f"a normal prompt sent no model request to the trap: Claude Code ignores ANTHROPIC_BASE_URL. Requests: {hits}"
        print(f"1. ok: a normal prompt sent its model request to the trap ({hits[-1]})")
        hits.clear()
        answers = session([probe] if probe else [], deny=deny, model_api=url)
        model = [h for h in hits if MODEL_PATH in h]
        assert not model, f"a blocked session sent model requests: {model}"
        print(f"2. ok: a blocked session sent no model request. Other requests: {hits or 'none'}")
        if probe:
            if isinstance(answers[0], ToolFailed):
                raise answers[0]
            print(f"   {probe.server}/{probe.tool} answered {len(answers[0])} characters")
    finally:
        server.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(prog="claude-mcp-call", description=__doc__.split("\n\n")[0])
    parser.add_argument("server", nargs="?", help='MCP server name, as `claude mcp list` shows it')
    parser.add_argument("tool", nargs="?", help="tool name")
    parser.add_argument("input", nargs="?", default="{}", help="tool input as JSON (default: {})")
    parser.add_argument("--deny", action="append", default=[], metavar="SERVER", help="turn off this MCP server (repeat for more)")
    parser.add_argument("--timeout", type=int, default=120, help="seconds per tool (default: 120)")
    parser.add_argument("--verify", action="store_true", help="prove that no model call can happen; with a server and tool, call it too")
    args = parser.parse_args()
    if bool(args.server) != bool(args.tool):
        parser.error("give both a server and a tool")
    probe = Call(args.server, args.tool, json.loads(args.input)) if args.server else None
    if args.verify:
        verify(probe, args.deny)
        return
    if probe is None:
        parser.error("give a server and a tool, or --verify")
    print(call(probe.server, probe.tool, probe.input, args.timeout, args.deny))
    turns, cost = SESSIONS[-1]
    print(f"num_turns {turns}, total_cost_usd {cost}", file=sys.stderr)
