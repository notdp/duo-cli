"""Session launcher for duo agents."""

from __future__ import annotations

import asyncio
import os
import subprocess

from droid_agent_sdk import DroidSession
from droid_agent_sdk.protocol import add_user_message_request


def make_workspace(repo: str = "", pr_number: int = 0) -> str:
    """Create workspace identifier from repo and PR number."""
    if repo and pr_number:
        safe_repo = repo.replace("/", "-")
        return f"{safe_repo}-{pr_number}"
    return "default"


def start_session(
    name: str,
    model: str = "claude-opus-4-5-20251101",
    pr_number: int = 0,
    repo: str = "",
    cwd: str | None = None,
    auto_level: str = "high",
    reasoning_effort: str = "high",
    prompt: str | None = None,
    workspace: str | None = None,
) -> dict:
    """Start a new droid session.

    Returns:
        dict with keys: session_id, fifo, pid, log, model, workspace
    """
    cwd = cwd or os.getcwd()
    workspace = workspace or make_workspace(repo, pr_number)

    # Use SDK DroidSession (high-level API)
    session = DroidSession(
        name=name,
        model=model,
        workspace=workspace,
        cwd=cwd,
        auto_level=auto_level,
        reasoning_effort=reasoning_effort,
        extra_env={"DROID_AGENT_NAME": name},
    )

    # Bridge async to sync with asyncio.run()
    session_id = asyncio.run(session.start())

    # Send prompt if provided
    if prompt and session_id:
        request = add_user_message_request(prompt)
        session.transport.send(request)

    return {
        "session_id": session_id,
        "fifo": str(session.transport.fifo_path),
        "pid": session.transport.pid,
        "log": str(session.transport.log_path),
        "model": model,
        "workspace": workspace,
    }


def resume_session(
    name: str,
    session_id: str,
    pr_number: int = 0,
    repo: str = "",
    cwd: str | None = None,
    workspace: str | None = None,
) -> dict:
    """Resume an existing droid session using load_session.

    Note: No model/auto/reasoning parameters - load_session restores original settings.

    Returns:
        dict with keys: fifo, pid, log, workspace
    """
    cwd = cwd or os.getcwd()
    workspace = workspace or make_workspace(repo, pr_number)

    # Use SDK DroidSession (high-level API)
    session = DroidSession(
        name=name,
        model="",  # Not needed for resume
        workspace=workspace,
        cwd=cwd,
        extra_env={"DROID_AGENT_NAME": name},
    )

    # Bridge async to sync with asyncio.run()
    asyncio.run(session.resume(session_id))

    return {
        "fifo": str(session.transport.fifo_path),
        "pid": session.transport.pid,
        "log": str(session.transport.log_path),
        "workspace": workspace,
    }


def cleanup_old_processes(repo: str, pr_number: int) -> None:
    """Kill old session processes and remove temp files."""
    safe_repo = repo.replace("/", "-")
    pattern = f"duo-{safe_repo}-{pr_number}"

    # Kill processes (both SDK and old CLI daemon patterns)
    subprocess.run(
        ["pkill", "-f", f"droid_agent_sdk.daemon.*{pattern}"],
        capture_output=True,
    )

    # Remove FIFOs and logs
    import glob

    for f in glob.glob(f"/tmp/{pattern}-*"):
        try:
            os.remove(f)
        except Exception:
            pass


def cleanup_comments(repo: str, pr_number: int) -> None:
    """Remove all DUO comments from PR."""
    result = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo,
            "--json",
            "comments",
            "-q",
            '.comments[] | select(.body | test("<!-- duo-")) | .id',
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        return

    for node_id in result.stdout.strip().split("\n"):
        if node_id:
            query = f'mutation {{ deleteIssueComment(input: {{id: "{node_id}"}}) {{ clientMutationId }} }}'
            subprocess.run(
                ["gh", "api", "graphql", "-f", f"query={query}"],
                capture_output=True,
            )


def cleanup_fix_branches(repo: str, pr_number: int) -> None:
    """Delete duo fix branches."""
    result = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{repo}/git/matching-refs/heads/duo/pr{pr_number}-",
            "--jq",
            ".[].ref",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        return

    for ref in result.stdout.strip().split("\n"):
        if ref:
            branch = ref.replace("refs/heads/", "")
            subprocess.run(
                ["gh", "api", f"repos/{repo}/git/refs/heads/{branch}", "-X", "DELETE"],
                capture_output=True,
            )


def get_pr_info(pr_number: int | None = None) -> dict | None:
    """Get PR info from gh CLI.

    Returns:
        dict with keys: number, repo, branch, base, node_id
    """
    cmd = ["gh", "pr", "view"]
    if pr_number:
        cmd.append(str(pr_number))
    cmd.extend(
        [
            "--json",
            "id,number,baseRefName,headRefName,headRepositoryOwner,headRepository",
        ]
    )

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return None

    try:
        data = json.loads(result.stdout)
        return {
            "number": data["number"],
            "node_id": data["id"],
            "repo": f"{data['headRepositoryOwner']['login']}/{data['headRepository']['name']}",
            "branch": data["headRefName"],
            "base": data["baseRefName"],
        }
    except Exception:
        return None


ORCHESTRATOR_PROMPT = """<system-instruction>
你是 Orchestrator，负责编排 duo review 流程。


## 关键变量
PR_NUMBER={pr_number}
REPO={repo}
PR_BRANCH={branch}
BASE_BRANCH={base}
RUNNER={runner}

## ⚠️ 严格禁止
- 禁止读取 PR diff、代码文件、REVIEW.md
- 禁止自己审查代码
- 只能执行 duo-cli 命令、通过 FIFO 与 Agent 通信

## 执行流程
1. You must load skill: duoduo FIRST. Then read ~/.factory/skills/duoduo/SKILL.md
2. Read ~/.factory/skills/duoduo/stages/1-pr-review-orchestrator.md to get detailed instructions for stage 1

## 开始
立即执行阶段 1。
</system-instruction>
"""


MENTION_PROMPT = """<system-instruction>
你是 Orchestrator，负责编排 duo review 流程。
首先 load skill: duoduo

<USER_MENTION repo="{repo}" pr="{pr_number}" author="{author}">
{body}
</USER_MENTION>

读取 ~/.factory/skills/duoduo/stages/0-mention-orchestrator.md 理解并处理用户请求。
</system-instruction>
"""


MENTION_INIT_PROMPT = """<system-instruction>
你是 Orchestrator，负责编排 duo review 流程。
首先 load skill: duoduo

## 关键变量
PR_NUMBER={pr_number}
REPO={repo}
PR_BRANCH={branch}
BASE_BRANCH={base}
RUNNER={runner}

<USER_MENTION repo="{repo}" pr="{pr_number}" author="{author}">
{body}
</USER_MENTION>

读取 ~/.factory/skills/duoduo/stages/0-mention-orchestrator.md 理解并处理用户请求。
</system-instruction>
"""
