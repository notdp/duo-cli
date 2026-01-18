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
from droid_agent_sdk.protocol import add_user_message_request

from .state import SqliteBackend, SwarmState


def get_env(name: str, required: bool = True) -> str | None:
    """Get environment variable."""
    value = os.environ.get(name)
    if required and not value:
        click.echo(f"Error: {name} not set. Export it or check your session.", err=True)
        sys.exit(1)
    return value


def get_state() -> SwarmState:
    """Get state backend from environment."""
    pr_number = get_env("DROID_PR_NUMBER")
    db_path = f"/tmp/duo-{pr_number}.db"
    backend = SqliteBackend(db_path)
    return SwarmState(backend, pr_number)


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


if __name__ == "__main__":
    main()
