"""CLI commands for duoduo multi-agent PR review.

Usage:
    duo send <agent> <message>   Send message to another agent
    duo set <key> <value>        Set state value
    duo get <key>                Get state value
    duo status                   Show swarm status

Environment variables (auto-detected):
    DROID_PR_NUMBER    PR number (required)
    DROID_AGENT_NAME   Current agent name (for send)
    DROID_REPO         Repository name
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import click

from droid_agent_sdk import AgentMessage, FIFOTransport
from droid_agent_sdk.protocol import (
    add_user_message_request,
    interrupt_session_request,
    update_session_settings_request,
)

from duoduo.state import SqliteBackend, SwarmState


def get_env(name: str, required: bool = True) -> str | None:
    """Get environment variable."""
    value = os.environ.get(name)
    if required and not value:
        click.echo(f"Error: {name} not set. Export it or check your session.", err=True)
        sys.exit(1)
    return value


def get_state() -> SwarmState:
    """Get state backend from environment."""
    repo = get_env("DROID_REPO")
    pr_number = int(get_env("DROID_PR_NUMBER"))
    db_path = f"/tmp/duo-{repo.replace('/', '-')}-{pr_number}.db"
    backend = SqliteBackend(db_path)
    return SwarmState(backend, repo, pr_number)


def _is_alive(pid: str | None) -> bool:
    """Check if a process is alive."""
    if not pid or pid == "?":
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


@click.group()
@click.version_option(version="0.1.0")
def main():
    """duo - CLI tools for duoduo multi-agent PR review.
    
    \b
    Examples:
        duo send orchestrator "Review complete, no issues"
        duo set stage 2
        duo get stage
        duo status
    """
    pass


@main.command()
@click.argument("agent")
@click.argument("message")
@click.option("-f", "--from", "from_agent", default=None, help="Override sender name")
def send(agent: str, message: str, from_agent: str | None):
    """Send a message to another agent.
    
    \b
    Examples:
        duo send orchestrator "Review complete"
        duo send codex "Please verify the fix"
    """
    pr_number = get_env("DROID_PR_NUMBER")
    from_agent = from_agent or get_env("DROID_AGENT_NAME", required=False) or "unknown"
    
    state = get_state()
    fifo_path = state.get(f"{agent}:fifo")
    
    if not fifo_path:
        click.echo(f"Error: Agent '{agent}' not found. Check 'duo status'", err=True)
        sys.exit(1)
    
    # Format and send
    timestamp = datetime.now(timezone.utc).isoformat()
    agent_msg = AgentMessage(
        from_agent=from_agent,
        to_agent=agent,
        content=message,
        timestamp=timestamp,
    )
    
    transport = FIFOTransport.restore(fifo_path=fifo_path, log_path="/dev/null")
    request = add_user_message_request(agent_msg.format())
    transport.send(request)
    
    # Save to database
    state.add_message(from_agent, agent, message, timestamp)
    
    click.echo(f"Sent to {agent}")


@main.command("set")
@click.argument("key")
@click.argument("value")
def set_state(key: str, value: str):
    """Set a state value.
    
    \b
    Examples:
        duo set stage 2
        duo set s2:result both_ok
    """
    state = get_state()
    state.set(key, value)
    click.echo(f"{key}={value}")


@main.command("get")
@click.argument("key")
def get_state_value(key: str):
    """Get a state value.
    
    \b
    Examples:
        duo get stage
        duo get s2:result
    """
    state = get_state()
    value = state.get(key)
    if value is None:
        click.echo(f"(not set)", err=True)
        sys.exit(1)
    click.echo(value)


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def status(as_json: bool):
    """Show swarm status.
    
    \b
    Examples:
        duo status
        duo status --json
    """
    state = get_state()
    all_state = state.get_all()
    
    if as_json:
        click.echo(json.dumps(all_state, indent=2))
        return
    
    click.echo(f"PR #{state.pr_number}")
    click.echo("-" * 40)
    
    # Metadata
    meta = ["repo", "branch", "base", "runner", "stage"]
    for key in meta:
        if key in all_state:
            click.echo(f"{key}: {all_state[key]}")
    
    # Find agents
    agents = set()
    for key in all_state:
        if ":session" in key:
            agents.add(key.split(":")[0])
    
    if agents:
        click.echo("\nAgents:")
        for name in sorted(agents):
            pid = all_state.get(f"{name}:pid", "?")
            model = all_state.get(f"{name}:model", "?")
            alive = "●" if _is_alive(pid) else "○"
            click.echo(f"  {alive} {name}: {model} (pid={pid})")


@main.command()
@click.argument("agent")
@click.option("-f", "--follow", is_flag=True, help="Follow log output")
@click.option("-n", "--lines", default=50, help="Number of lines to show")
def logs(agent: str, follow: bool, lines: int):
    """Show agent logs.
    
    \b
    Examples:
        duo logs opus
        duo logs orchestrator -f
        duo logs codex -n 100
    """
    state = get_state()
    log_path = state.get(f"{agent}:log")
    
    if not log_path:
        click.echo(f"Error: Agent '{agent}' not found", err=True)
        sys.exit(1)
    
    if follow:
        subprocess.run(["tail", "-f", log_path])
    else:
        subprocess.run(["tail", "-n", str(lines), log_path])


@main.command()
@click.argument("agent")
def alive(agent: str):
    """Check if an agent is alive.
    
    \b
    Examples:
        duo alive opus
        duo alive orchestrator
    """
    state = get_state()
    pid = state.get(f"{agent}:pid")
    
    if not pid:
        click.echo(f"not found")
        sys.exit(1)
    
    if _is_alive(pid):
        click.echo(f"alive (pid={pid})")
    else:
        click.echo(f"dead (was pid={pid})")
        sys.exit(1)


@main.command()
def agents():
    """List all agents.
    
    \b
    Examples:
        duo agents
    """
    state = get_state()
    all_state = state.get_all()
    
    found = set()
    for key in all_state:
        if ":session" in key:
            found.add(key.split(":")[0])
    
    for name in sorted(found):
        click.echo(name)


@main.command()
@click.argument("agent", required=False)
@click.option("-n", "--last", "limit", type=int, help="Show last N messages")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def messages(agent: str | None, limit: int | None, as_json: bool):
    """Show message history between agents.
    
    \b
    Examples:
        duo messages              # All messages
        duo messages opus         # Messages involving opus
        duo messages --last 10    # Last 10 messages
        duo messages --json       # JSON output
    """
    state = get_state()
    msgs = state.get_messages(agent=agent, limit=limit)
    
    if as_json:
        click.echo(json.dumps(msgs, indent=2))
        return
    
    if not msgs:
        click.echo("No messages found")
        return
    
    for msg in msgs:
        ts = msg["timestamp"][:19].replace("T", " ")  # Trim to seconds
        click.echo(f"[{ts}] {msg['from']} -> {msg['to']}: {msg['content'][:80]}")


@main.command()
@click.argument("agent")
def interrupt(agent: str):
    """Interrupt an agent's current operation.
    
    \b
    Examples:
        duo interrupt opus
        duo interrupt codex
    """
    state = get_state()
    fifo_path = state.get(f"{agent}:fifo")
    
    if not fifo_path:
        click.echo(f"Error: Agent '{agent}' not found", err=True)
        sys.exit(1)
    
    transport = FIFOTransport.restore(fifo_path=fifo_path, log_path="/dev/null")
    request = interrupt_session_request()
    transport.send(request)
    
    click.echo(f"Interrupted {agent}")


@main.command()
@click.argument("agent")
@click.option("--auto", "auto_mode", type=click.Choice(["off", "low", "high"]), help="Auto mode")
@click.option("--model", help="Model to use")
def settings(agent: str, auto_mode: str | None, model: str | None):
    """Update agent session settings.
    
    \b
    Examples:
        duo settings opus --auto low
        duo settings codex --model gpt-5.2
        duo settings opus --auto high --model claude-opus-4-5-20251101
    """
    if not auto_mode and not model:
        click.echo("Error: Specify --auto or --model", err=True)
        sys.exit(1)
    
    state = get_state()
    fifo_path = state.get(f"{agent}:fifo")
    
    if not fifo_path:
        click.echo(f"Error: Agent '{agent}' not found", err=True)
        sys.exit(1)
    
    transport = FIFOTransport.restore(fifo_path=fifo_path, log_path="/dev/null")
    request = update_session_settings_request(auto_mode=auto_mode, model=model)
    transport.send(request)
    
    parts = []
    if auto_mode:
        parts.append(f"auto={auto_mode}")
    if model:
        parts.append(f"model={model}")
    click.echo(f"Updated {agent}: {', '.join(parts)}")


# =============================================================================
# Comment commands
# =============================================================================

def _get_gh_env() -> dict:
    """Get environment with GH_TOKEN if available."""
    env = os.environ.copy()
    # GH_TOKEN is set by workflow (App token or Actions bot)
    # If not set, gh CLI will use local auth
    return env


def _run_gh(args: list[str]) -> subprocess.CompletedProcess:
    """Run gh command with proper environment."""
    return subprocess.run(
        ["gh"] + args,
        capture_output=True,
        text=True,
        env=_get_gh_env(),
    )


@main.group()
def comment():
    """Manage GitHub PR comments.
    
    \b
    Examples:
        duo comment list
        duo comment get DUO-OPUS-R1
        duo comment edit <node_id> "new content"
        duo comment delete <node_id>
    """
    pass


@comment.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def comment_list(as_json: bool):
    """List all DUO comments on the PR.
    
    \b
    Examples:
        duo comment list
        duo comment list --json
    """
    repo = get_env("DROID_REPO")
    pr = get_env("DROID_PR_NUMBER")
    
    result = _run_gh([
        "pr", "view", pr, "--repo", repo,
        "--json", "comments",
        "-q", '.comments[] | select(.body | test("<!-- duo-")) | {id: .id, marker: (.body | capture("<!-- (?<m>duo-[a-z0-9-]+) -->") | .m), createdAt: .createdAt}'
    ])
    
    if result.returncode != 0:
        click.echo(f"Error: {result.stderr}", err=True)
        sys.exit(1)
    
    comments = []
    for line in result.stdout.strip().split("\n"):
        if line:
            try:
                comments.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    
    if as_json:
        click.echo(json.dumps(comments, indent=2))
        return
    
    if not comments:
        click.echo("No DUO comments found")
        return
    
    for cmt in comments:
        click.echo(f"{cmt.get('marker', '?'):20} {cmt['id']}")


@comment.command("get")
@click.argument("node_id")
def comment_get(node_id: str):
    """Get a comment by node ID.
    
    \b
    Examples:
        duo comment get IC_kwDOxxx
    """
    repo = get_env("DROID_REPO")
    pr = get_env("DROID_PR_NUMBER")
    
    result = _run_gh([
        "pr", "view", pr, "--repo", repo,
        "--json", "comments",
        "-q", f'.comments[] | select(.id == "{node_id}") | .body'
    ])
    
    if result.returncode != 0:
        click.echo(f"Error: {result.stderr}", err=True)
        sys.exit(1)
    
    if not result.stdout.strip():
        click.echo(f"Comment '{node_id}' not found", err=True)
        sys.exit(1)
    
    click.echo(result.stdout.strip())


@comment.command("edit")
@click.argument("node_id")
@click.argument("body", required=False)
@click.option("--stdin", is_flag=True, help="Read body from stdin")
def comment_edit(node_id: str, body: str | None, stdin: bool):
    """Edit a comment.
    
    \b
    Examples:
        duo comment edit IC_xxx "new content"
        echo "new content" | duo comment edit IC_xxx --stdin
    """
    if stdin:
        body = sys.stdin.read()
    
    if not body:
        click.echo("Error: Body required (use argument or --stdin)", err=True)
        sys.exit(1)
    
    body_json = json.dumps(body)
    query = f'''mutation {{
        updateIssueComment(input: {{id: "{node_id}", body: {body_json}}}) {{
            issueComment {{ id }}
        }}
    }}'''
    
    result = _run_gh(["api", "graphql", "-f", f"query={query}"])
    
    if result.returncode != 0:
        click.echo(f"Error: {result.stderr}", err=True)
        sys.exit(1)
    
    click.echo(f"Updated {node_id}")


@comment.command("delete")
@click.argument("node_id")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def comment_delete(node_id: str, yes: bool):
    """Delete a comment (silent, no timeline record).
    
    \b
    Examples:
        duo comment delete IC_xxx
        duo comment delete IC_xxx -y
    """
    if not yes:
        click.confirm(f"Delete comment {node_id}?", abort=True)
    
    query = f'''mutation {{
        deleteIssueComment(input: {{id: "{node_id}"}}) {{
            clientMutationId
        }}
    }}'''
    
    result = _run_gh(["api", "graphql", "-f", f"query={query}"])
    
    if result.returncode != 0:
        click.echo(f"Error: {result.stderr}", err=True)
        sys.exit(1)
    
    click.echo(f"Deleted {node_id}")


if __name__ == "__main__":
    main()
