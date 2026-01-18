"""Session launcher for duoduo agents."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


DROID = Path.home() / ".local" / "bin" / "droid"


def start_session(
    name: str,
    model: str,
    pr_number: int,
    repo: str,
    cwd: str | None = None,
    auto_level: str = "high",
) -> dict:
    """Start a new droid session.
    
    Returns:
        dict with keys: session_id, fifo, pid, log
    """
    cwd = cwd or os.getcwd()
    pr = str(pr_number)
    safe_repo = repo.replace("/", "-")
    
    fifo = f"/tmp/duo-{safe_repo}-{pr}-{name}"
    log = f"/tmp/duo-{safe_repo}-{pr}-{name}.log"
    
    # Clean up old FIFO
    if os.path.exists(fifo):
        os.remove(fifo)
    os.mkfifo(fifo)
    
    # Clear log
    open(log, "w").close()
    
    # Build environment with agent identity
    env = os.environ.copy()
    env["DROID_AGENT_NAME"] = name
    
    # Start daemon
    daemon_proc = subprocess.Popen(
        [
            "nohup", sys.executable, "-m", "duo_cli.daemon",
            name, model, pr, repo, cwd, auto_level,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )
    
    # Wait for session ID (max 15 seconds)
    session_id = None
    for _ in range(30):
        time.sleep(0.5)
        try:
            with open(log, "r") as f:
                for line in f:
                    if '"sessionId"' in line:
                        data = json.loads(line)
                        if "result" in data and "sessionId" in data["result"]:
                            session_id = data["result"]["sessionId"]
                            break
            if session_id:
                break
        except Exception:
            pass
    
    return {
        "session_id": session_id or "",
        "fifo": fifo,
        "pid": daemon_proc.pid,
        "log": log,
        "model": model,
    }


def resume_session(
    name: str,
    session_id: str,
    pr_number: int,
    repo: str,
    cwd: str | None = None,
    auto_level: str = "high",
) -> dict:
    """Resume an existing droid session using load_session.
    
    Used for @mention handling to restore conversation history.
    
    Returns:
        dict with keys: fifo, pid, log
    """
    cwd = cwd or os.getcwd()
    pr = str(pr_number)
    safe_repo = repo.replace("/", "-")
    
    fifo = f"/tmp/duo-{safe_repo}-{pr}-{name}"
    log = f"/tmp/duo-{safe_repo}-{pr}-{name}.log"
    
    # Clean up old FIFO
    if os.path.exists(fifo):
        os.remove(fifo)
    os.mkfifo(fifo)
    
    # Start daemon in resume mode
    daemon_proc = subprocess.Popen(
        [
            "nohup", sys.executable, "-m", "duo_cli.daemon",
            name, "", pr, repo, cwd, auto_level, "--resume", session_id,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=os.environ.copy(),
    )
    
    # Wait for load_session to complete (max 15 seconds)
    # Response format: {"id":"load","result":{"session":...}}
    for _ in range(30):
        time.sleep(0.5)
        try:
            with open(log, "r") as f:
                content = f.read()
                if '"id":"load"' in content and '"result":{"session"' in content:
                    break
        except Exception:
            pass
    
    return {
        "fifo": fifo,
        "pid": daemon_proc.pid,
        "log": log,
    }


def cleanup_old_processes(repo: str, pr_number: int) -> None:
    """Kill old session processes and remove temp files."""
    safe_repo = repo.replace("/", "-")
    pattern = f"duo-{safe_repo}-{pr_number}"
    
    # Kill processes
    subprocess.run(
        ["pkill", "-f", f"duoduo.daemon.*{repo}.*{pr_number}"],
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
            "gh", "pr", "view", str(pr_number), "--repo", repo,
            "--json", "comments",
            "-q", '.comments[] | select(.body | test("<!-- duo-")) | .id',
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
            "gh", "api", f"repos/{repo}/git/matching-refs/heads/duo/pr{pr_number}-",
            "--jq", ".[].ref",
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
    cmd.extend([
        "--json", "id,number,baseRefName,headRefName,headRepositoryOwner,headRepository",
    ])
    
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
你是 Orchestrator，负责编排 duoduo review 流程。
首先 load skill: duoduo

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
1. 读取 ~/.factory/skills/duoduo/stages/1-pr-review-orchestrator.md 获取阶段 1 详细指令
2. 按指令执行：并行启动 Codex/Opus（它们自己创建占位评论）
3. 等待 Agent 通过 FIFO 发回结果
4. 依次执行后续阶段
5. 每个阶段执行前必须先读取对应的 ~/.factory/skills/duoduo/stages/*-orchestrator.md 文件

## 开始
立即执行阶段 1。
</system-instruction>
"""
