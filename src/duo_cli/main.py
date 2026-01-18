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

from duo_cli.state import SqliteBackend, SwarmState
from duo_cli.launcher import (
    start_session,
    cleanup_old_processes,
    cleanup_comments,
    cleanup_fix_branches,
    get_pr_info,
    ORCHESTRATOR_PROMPT,
)


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


def is_agent_alive(pid: str | None) -> bool:
    """Check if agent daemon process is alive."""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        result = subprocess.run(["ps", "-p", pid, "-o", "comm="], capture_output=True, text=True)
        return "python" in result.stdout.lower()
    except (OSError, ValueError):
        return False


def ensure_agent_alive(agent: str, state: SwarmState, pr_number: int, repo: str) -> str:
    """Ensure agent is alive, resume if needed. Returns FIFO path."""
    from .launcher import resume_session
    
    if is_agent_alive(state.get(f"{agent}:pid")):
        return state.get(f"{agent}:fifo")
    
    # Not alive, resume
    click.echo(f"{agent} not alive, resuming...")
    session_id = state.get(f"{agent}:session")
    result = resume_session(name=agent, session_id=session_id, pr_number=pr_number, repo=repo)
    
    state.set_agent(
        agent,
        session=session_id,
        fifo=result["fifo"],
        pid=str(result["pid"]),
        log=result["log"],
        model=result.get("model", ""),
    )
    
    return result["fifo"]


def _get_latest_comment(repo: str, pr_number: int) -> tuple[str, str, str]:
    """Get latest PR comment (id, author, body)."""
    owner, repo_name = repo.split("/")
    query = '''
    query($owner:String!,$repo:String!,$pr:Int!){
      repository(owner:$owner,name:$repo){
        pullRequest(number:$pr){
          comments(last:1){
            nodes{databaseId author{login}body}
          }
        }
      }
    }
    '''
    try:
        result = subprocess.run(
            ["gh", "api", "graphql",
             "-f", f"query={query}",
             "-f", f"owner={owner}",
             "-f", f"repo={repo_name}",
             "-F", f"pr={pr_number}"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return ("", "", "")
        
        import json
        data = json.loads(result.stdout)
        nodes = data.get("data", {}).get("repository", {}).get("pullRequest", {}).get("comments", {}).get("nodes", [])
        if not nodes:
            return ("", "", "")
        
        node = nodes[0]
        return (
            str(node.get("databaseId", "")),
            node.get("author", {}).get("login", ""),
            node.get("body", "")
        )
    except Exception:
        return ("", "", "")


def _poll_mention_completion(state: SwarmState, repo: str, pr_number: int, bot_name: str = ""):
    """Poll for mention completion and detect new comments."""
    import time
    
    # Get bot name from env if not provided
    bot_name = bot_name or os.environ.get("BOT_NAME", "")
    
    # Record last seen comment ID
    last_id, _, _ = _get_latest_comment(repo, pr_number)
    if not last_id:
        last_id = "0"
    
    # Poll for completion (max 10 minutes)
    timeout = 600
    elapsed = 0
    
    while elapsed < timeout:
        # Check if mention is done
        status = state.get("mention:status")
        if status == "done":
            click.echo("✅ 完成")
            return
        
        # Detect new comments
        latest_id, latest_author, latest_body = _get_latest_comment(repo, pr_number)
        
        if latest_id and latest_id != last_id:
            # Exclude bot comments
            if latest_author and latest_author != bot_name:
                click.echo(f"📩 检测到新评论 (by {latest_author})，转发给 Orchestrator")
                
                # Send to orchestrator
                fifo_path = state.get("orchestrator:fifo")
                if fifo_path:
                    msg = f'<USER_MENTION repo="{repo}" pr="{pr_number}" author="{latest_author}">\n{latest_body}\n</USER_MENTION>'
                    transport = FIFOTransport.restore(fifo_path=fifo_path, log_path="/dev/null")
                    request = add_user_message_request(msg)
                    transport.send(request)
            last_id = latest_id
        
        # Log every 30 seconds
        if elapsed % 30 == 0 and elapsed > 0:
            click.echo(f"⏳ 处理中...")
        
        time.sleep(3)
        elapsed += 3
    
    click.echo("⚠️ 超时，Orchestrator 仍在后台运行")


HELP_TEXT = """\b
duo-cli - CLI for duoduo multi-agent PR review.

\b
Session Management:
  init                 Initialize PR review (starts Orchestrator daemon)
  status               Show swarm status (agents, stage, progress)
  spawn <agent>        Start a new agent (opus/codex)
  resume <agent>       Resume an existing agent session
  interrupt <agent>    Interrupt agent's current operation
  logs <agent>         Show agent logs
  agents               List all agents
  alive <agent>        Check if agent is alive

\b
Communication:
  send <agent> <msg>   Send message via FIFO
  messages             Show message history

\b
State Management:
  set <key> <value>    Set state value
  get <key>            Get state value

\b
GitHub Integration:
  comment post         Post PR comment
  comment edit         Edit PR comment
  comment list         List PR comments
  comment delete       Delete PR comment
  mention              Handle user @mention

\b
Environment Variables:
  DROID_REPO           Repository (owner/repo)
  DROID_PR_NUMBER      PR number
  DROID_BRANCH         PR branch name
  DROID_BASE           Base branch name
  DROID_PR_NODE_ID     PR GraphQL node ID
  RUNNER               Runner type (droid/workflow)

\b
State Keys:
  stage                Current stage (1-5, done)
  s2:result            Consensus result (both_ok/same_issues/divergent)
  s4:branch            Fix branch name
  mention:status       Mention status (idle/processing/done)
  {agent}:session      Agent session ID
  {agent}:fifo         Agent FIFO path
  {agent}:pid          Agent process ID

\b
Examples:
  # Initialize PR review
  duo-cli init                              # Auto-detect from current branch
  duo-cli init 83                           # Specify PR number
  duo-cli init --watch                      # Watch progress after init

\b
  # Agent management
  duo-cli spawn opus                        # Start Opus agent
  duo-cli resume orchestrator               # Resume Orchestrator
  duo-cli status                            # Check all agents status
  duo-cli logs opus                         # View Opus logs

\b
  # Send messages between agents
  duo-cli send orchestrator "Review done"
  duo-cli send opus --stdin <<< "Hello"
  duo-cli messages --agent opus --limit 10

\b
  # State management
  duo-cli set stage 2
  duo-cli get stage
  duo-cli set s2:result both_ok

\b
  # GitHub comments
  duo-cli comment post "Hello world"
  duo-cli comment post --stdin < review.md
  duo-cli comment edit IC_xxx "Updated content"
  duo-cli comment list
  duo-cli comment delete IC_xxx

\b
  # Handle @mention
  duo-cli mention --author user123 --stdin < comment.txt
"""


class CustomGroup(click.Group):
    """Custom group that hides auto-generated commands and options."""
    
    def format_commands(self, ctx, formatter):
        """Skip the auto-generated commands list."""
        pass
    
    def format_options(self, ctx, formatter):
        """Skip the auto-generated options list."""
        pass


@click.group(cls=CustomGroup)
@click.version_option(version="0.1.0")
def main():
    """duo-cli - CLI for duoduo multi-agent PR review."""
    pass


main.help = HELP_TEXT


@main.command()
@click.argument("pr_number", required=False, type=int)
@click.option("--no-cleanup", is_flag=True, help="Skip cleanup step")
@click.option("--watch", is_flag=True, help="Watch progress after init")
def init(pr_number: int | None, no_cleanup: bool, watch: bool):
    """Initialize duoduo PR review.
    
    Reads from environment variables:
      DROID_REPO, DROID_PR_NUMBER, DROID_BRANCH, DROID_BASE, DROID_PR_NODE_ID, RUNNER
    
    If environment variables are missing, falls back to `gh pr view`.
    
    \b
    Examples:
        duo-cli init                    # Auto-detect from current branch
        duo-cli init 83                 # Specify PR number
        duo-cli init --watch            # Watch progress after init
    """
    # Read from environment variables (droid or actions)
    runner = os.environ.get("RUNNER", "droid")
    repo = os.environ.get("DROID_REPO")
    branch = os.environ.get("DROID_BRANCH")
    base = os.environ.get("DROID_BASE")
    pr_node_id = os.environ.get("DROID_PR_NODE_ID")
    
    # PR number: argument > env var
    if not pr_number:
        pr_number_str = os.environ.get("DROID_PR_NUMBER")
        pr_number = int(pr_number_str) if pr_number_str else None
    
    # If any info missing, get from gh CLI
    if not all([repo, pr_number, branch, base, pr_node_id]):
        info = get_pr_info(pr_number)
        if not info:
            click.echo("Error: Cannot get PR info. Set environment variables or run from a PR branch.", err=True)
            sys.exit(1)
        
        pr_number = info["number"]
        pr_node_id = info["node_id"]
        repo = info["repo"]
        branch = info["branch"]
        base = info["base"]
    
    # Set environment variables for daemon inheritance
    os.environ["DROID_REPO"] = repo
    os.environ["DROID_PR_NUMBER"] = str(pr_number)
    os.environ["DROID_BRANCH"] = branch
    os.environ["DROID_BASE"] = base
    os.environ["DROID_PR_NODE_ID"] = pr_node_id
    os.environ["RUNNER"] = runner
    
    click.echo(f"🚀 Duo Review")
    click.echo(f"   PR: #{pr_number} ({branch} → {base})")
    click.echo(f"   Repo: {repo}")
    click.echo(f"   Runner: {runner}")
    click.echo("")
    
    # Cleanup
    if not no_cleanup:
        click.echo("🧹 Cleaning up...")
        cleanup_old_processes(repo, pr_number)
        cleanup_comments(repo, pr_number)
        cleanup_fix_branches(repo, pr_number)
    
    # Initialize state
    safe_repo = repo.replace("/", "-")
    db_path = f"/tmp/duo-{safe_repo}-{pr_number}.db"
    
    # Remove old database
    if os.path.exists(db_path):
        os.remove(db_path)
    if os.path.exists(f"{db_path}-wal"):
        os.remove(f"{db_path}-wal")
    if os.path.exists(f"{db_path}-shm"):
        os.remove(f"{db_path}-shm")
    
    backend = SqliteBackend(db_path)
    state = SwarmState(backend, repo, pr_number)
    state.init(branch=branch, base=base, runner=runner, pr_node_id=pr_node_id)
    
    # Start Orchestrator
    click.echo("🤖 Starting Orchestrator...")
    result = start_session(
        name="orchestrator",
        model="claude-opus-4-5-20251101",
        pr_number=pr_number,
        repo=repo,
        auto_level="high",
    )
    
    state.set_agent(
        "orchestrator",
        session=result["session_id"],
        fifo=result["fifo"],
        pid=str(result["pid"]),
        log=result["log"],
        model=result["model"],
    )
    
    click.echo(f"   Session: {result['session_id']}")
    click.echo(f"   Log: tail -f {result['log']}")
    click.echo("")
    
    # Send initial prompt
    click.echo("📤 Sending initial prompt...")
    prompt = ORCHESTRATOR_PROMPT.format(
        pr_number=pr_number,
        repo=repo,
        branch=branch,
        base=base,
        runner=runner,
    )
    
    transport = FIFOTransport.restore(fifo_path=result["fifo"], log_path="/dev/null")
    request = add_user_message_request(prompt)
    transport.send(request)
    
    click.echo("✅ Initialized")
    click.echo("")
    
    if watch:
        _watch_progress(state, repo, pr_number)


def _watch_progress(state: SwarmState, repo: str, pr_number: int):
    """Watch and display progress."""
    import time
    
    stage_names = {
        "1": "并行审查",
        "2": "判断共识",
        "3": "交叉确认",
        "4": "修复验证",
        "5": "汇总",
    }
    
    last_stage = ""
    
    click.echo("📊 Watching progress (Ctrl+C to exit)...")
    click.echo("")
    
    try:
        while True:
            stage = state.get("stage") or "1"
            
            if stage != last_stage:
                if stage == "done":
                    result = state.get("s2:result") or ""
                    click.echo(f"✅ 完成: {result}")
                    click.echo(f"   https://github.com/{repo}/pull/{pr_number}")
                    break
                else:
                    name = stage_names.get(stage, stage)
                    click.echo(f"⏳ 阶段 {stage}: {name}中...")
                last_stage = stage
            
            time.sleep(2)
    except KeyboardInterrupt:
        click.echo("")
        click.echo("⚠️  已退出监控，Orchestrator 仍在后台运行")


@main.command()
@click.argument("agent", type=click.Choice(["opus", "codex"]))
@click.option("--prompt-file", "-f", help="File containing initial prompt")
def spawn(agent: str, prompt_file: str | None):
    """Spawn an agent (opus or codex).
    
    \b
    Examples:
        duo-cli spawn opus
        duo-cli spawn codex
        duo-cli spawn opus -f /path/to/prompt.txt
    """
    state = get_state()
    repo = state.repo
    pr_number = state.pr_number
    
    # Model mapping
    models = {
        "opus": "claude-opus-4-5-20251101",
        "codex": "gpt-5.2",
    }
    model = models[agent]
    
    click.echo(f"🚀 Spawning {agent} ({model})...")
    
    result = start_session(
        name=agent,
        model=model,
        pr_number=pr_number,
        repo=repo,
        auto_level="high",
    )
    
    state.set_agent(
        agent,
        session=result["session_id"],
        fifo=result["fifo"],
        pid=str(result["pid"]),
        log=result["log"],
        model=result["model"],
    )
    
    click.echo(f"   Session: {result['session_id']}")
    click.echo(f"   FIFO: {result['fifo']}")
    click.echo(f"   Log: {result['log']}")
    
    # Send initial prompt if provided
    if prompt_file:
        with open(prompt_file, "r") as f:
            prompt = f.read()
        
        transport = FIFOTransport.restore(fifo_path=result["fifo"], log_path="/dev/null")
        request = add_user_message_request(prompt)
        transport.send(request)
        click.echo(f"   Sent prompt from {prompt_file}")
    
    click.echo(f"✅ {agent} spawned")


@main.command()
@click.argument("agent", type=click.Choice(["orchestrator", "opus", "codex"]))
def resume(agent: str):
    """Resume an existing agent session.
    
    Used for @mention handling - restores a previously created session
    using load_session to preserve conversation history.
    
    \b
    Examples:
        duo-cli resume orchestrator
        duo-cli resume opus
    """
    from .launcher import resume_session
    
    state = get_state()
    repo = state.repo
    pr_number = state.pr_number
    
    # Get existing session ID
    session_id = state.get(f"{agent}:session")
    if not session_id:
        click.echo(f"Error: No session found for {agent}", err=True)
        sys.exit(1)
    
    # Check if already alive (must be a Python daemon process)
    old_pid = state.get(f"{agent}:pid")
    if old_pid:
        try:
            os.kill(int(old_pid), 0)
            # Verify it's actually a Python process (our daemon)
            result = subprocess.run(
                ["ps", "-p", old_pid, "-o", "comm="],
                capture_output=True,
                text=True,
            )
            if "python" in result.stdout.lower() or "Python" in result.stdout:
                click.echo(f"{agent} already alive (PID {old_pid})")
                return
        except (OSError, ValueError):
            pass
    
    click.echo(f"🔄 Resuming {agent} (session: {session_id[:8]}...)...")
    
    result = resume_session(
        name=agent,
        session_id=session_id,
        pr_number=pr_number,
        repo=repo,
    )
    
    # Update state with new PID/FIFO
    state.set_agent(
        agent,
        session=session_id,
        fifo=result["fifo"],
        pid=str(result["pid"]),
        log=result["log"],
        model=result.get("model", ""),
    )
    
    click.echo(f"   FIFO: {result['fifo']}")
    click.echo(f"   Log: {result['log']}")
    click.echo(f"✅ {agent} resumed (PID {result['pid']})")


@main.command()
@click.option("--author", required=True, help="Comment author username")
@click.option("--stdin", is_flag=True, required=True, help="Read body from stdin")
def mention(author: str, stdin: bool):
    """Handle user @mention on PR.
    
    If a session exists, sends message to orchestrator.
    If no session exists, starts a new review.
    
    \b
    Examples:
        duo-cli mention --author username --stdin <<EOF
        user comment content
        EOF
    """
    import time
    from .launcher import resume_session, start_session
    
    body = sys.stdin.read().strip()
    if not body:
        click.echo("Error: Empty body", err=True)
        sys.exit(1)
    
    state = get_state()
    repo = state.repo
    pr_number = state.pr_number
    
    # Check for existing session
    session_id = state.get("orchestrator:session")
    
    if session_id:
        # Has session → ensure alive and send message
        click.echo(f"Found session: {session_id}")
        
        # Reset mention status for new mention
        state.set("mention:status", "processing")
        
        # Ensure orchestrator is alive (resume if needed)
        fifo_path = ensure_agent_alive("orchestrator", state, pr_number, repo)
        
        # Format and send message
        msg = f'<USER_MENTION repo="{repo}" pr="{pr_number}" author="{author}">\n{body}\n</USER_MENTION>'
        
        transport = FIFOTransport.restore(fifo_path=fifo_path, log_path="/dev/null")
        request = add_user_message_request(msg)
        transport.send(request)
        
        # Save to database
        timestamp = datetime.now(timezone.utc).isoformat()
        state.add_message(author, "orchestrator", body, timestamp)
        
        click.echo(f"✅ Sent to orchestrator")
        
        # Poll for completion and detect new comments
        bot_name = os.environ.get("BOT_NAME", "")
        _poll_mention_completion(state, repo, pr_number, bot_name)
    else:
        # No session → start new review
        click.echo("No session found, starting new review...")
        
        # Initialize state if needed
        branch = os.environ.get("DROID_BRANCH", "")
        base = os.environ.get("DROID_BASE", "")
        runner = os.environ.get("RUNNER", "droid")
        pr_node_id = os.environ.get("DROID_PR_NODE_ID", "")
        
        state.init(branch=branch, base=base, runner=runner, pr_node_id=pr_node_id)
        
        # Start orchestrator
        result = start_session(
            name="orchestrator",
            pr_number=pr_number,
            repo=repo,
            prompt=ORCHESTRATOR_PROMPT,
        )
        
        state.set_agent(
            "orchestrator",
            session=result["session"],
            fifo=result["fifo"],
            pid=str(result["pid"]),
            log=result["log"],
            model=result.get("model", ""),
        )
        
        click.echo(f"✅ Started orchestrator (session: {result['session'][:8]}...)")


@main.command()
@click.argument("agent")
@click.argument("message", required=False)
@click.option("-f", "--from", "from_agent", default=None, help="Override sender name")
@click.option("--stdin", is_flag=True, help="Read message from stdin")
def send(agent: str, message: str | None, from_agent: str | None, stdin: bool):
    """Send a message to another agent.
    
    \b
    Examples:
        duo-cli send orchestrator "Review complete"
        duo-cli send codex "Please verify the fix"
        cat prompt.txt | duo send opus --stdin
        duo-cli send opus --stdin <<'EOF'
        ... long prompt ...
        EOF
    """
    if stdin:
        message = sys.stdin.read()
    
    if not message:
        click.echo("Error: Message required (use argument or --stdin)", err=True)
        sys.exit(1)
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
        duo-cli set stage 2
        duo-cli set s2:result both_ok
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
        duo-cli get stage
        duo-cli get s2:result
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
        duo-cli status
        duo-cli status --json
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
        duo-cli logs opus
        duo-cli logs orchestrator -f
        duo-cli logs codex -n 100
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
@click.option("--agent", default=None, help="Filter by agent (from or to)")
@click.option("--last", "limit", default=None, type=int, help="Show last N messages")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def messages(agent: str | None, limit: int | None, as_json: bool):
    """Show message history.
    
    \b
    Examples:
        duo-cli messages                  # All messages
        duo-cli messages --agent opus     # Filter by agent
        duo-cli messages --last 10        # Last 10 messages
        duo-cli messages --json           # JSON output
    """
    state = get_state()
    msgs = state.get_messages(agent=agent, limit=limit)
    
    if as_json:
        click.echo(json.dumps(msgs, indent=2, ensure_ascii=False))
        return
    
    if not msgs:
        click.echo("No messages")
        return
    
    for msg in msgs:
        click.echo(f"[{msg['timestamp']}] {msg['from']} → {msg['to']}")
        # Truncate long content
        content = msg['content']
        if len(content) > 200:
            content = content[:200] + "..."
        click.echo(f"  {content}")
        click.echo()


@main.command()
@click.argument("agent")
def alive(agent: str):
    """Check if an agent is alive.
    
    \b
    Examples:
        duo-cli alive opus
        duo-cli alive orchestrator
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
        duo-cli agents
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
        duo-cli messages              # All messages
        duo-cli messages opus         # Messages involving opus
        duo-cli messages --last 10    # Last 10 messages
        duo-cli messages --json       # JSON output
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
        duo-cli interrupt opus
        duo-cli interrupt codex
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
        duo-cli settings opus --auto low
        duo-cli settings codex --model gpt-5.2
        duo-cli settings opus --auto high --model claude-opus-4-5-20251101
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
        duo-cli comment list
        duo-cli comment get DUO-OPUS-R1
        duo-cli comment edit <node_id> "new content"
        duo-cli comment delete <node_id>
    """
    pass


@comment.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def comment_list(as_json: bool):
    """List all DUO comments on the PR.
    
    \b
    Examples:
        duo-cli comment list
        duo-cli comment list --json
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
        duo-cli comment get IC_kwDOxxx
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
        duo-cli comment edit IC_xxx "new content"
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


@comment.command("post")
@click.argument("body", required=False)
@click.option("--stdin", is_flag=True, help="Read body from stdin")
def comment_post(body: str | None, stdin: bool):
    """Post a new comment to the PR. Returns the node ID.
    
    \b
    Examples:
        duo-cli comment post "Hello world"
        echo "Hello world" | duo comment post --stdin
    """
    if stdin:
        body = sys.stdin.read()
    
    if not body:
        click.echo("Error: Body required (use argument or --stdin)", err=True)
        sys.exit(1)
    
    state = get_state()
    pr_node_id = state.get("pr_node_id")
    
    if not pr_node_id:
        click.echo("Error: pr_node_id not found. Run 'duo init' first.", err=True)
        sys.exit(1)
    
    # Post comment via GraphQL (returns node ID directly)
    body_json = json.dumps(body)
    query = f'''mutation {{
        addComment(input: {{subjectId: "{pr_node_id}", body: {body_json}}}) {{
            commentEdge {{
                node {{ id }}
            }}
        }}
    }}'''
    
    result = _run_gh(["api", "graphql", "-f", f"query={query}"])
    if result.returncode != 0:
        click.echo(f"Error: {result.stderr}", err=True)
        sys.exit(1)
    
    # Parse response to get node ID
    data = json.loads(result.stdout)
    node_id = data["data"]["addComment"]["commentEdge"]["node"]["id"]
    click.echo(node_id)


@comment.command("delete")
@click.argument("node_id")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def comment_delete(node_id: str, yes: bool):
    """Delete a comment (silent, no timeline record).
    
    \b
    Examples:
        duo-cli comment delete IC_xxx
        duo-cli comment delete IC_xxx -y
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
