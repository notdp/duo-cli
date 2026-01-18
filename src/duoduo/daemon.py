#!/usr/bin/env python3
"""Daemon for running droid session.

Usage:
    python -m duoduo.daemon <name> <model> <pr> <repo> <cwd> <auto>
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

DROID = Path.home() / ".local" / "bin" / "droid"


def main():
    if len(sys.argv) < 7:
        print("Usage: python -m duoduo.daemon <name> <model> <pr> <repo> <cwd> <auto>")
        sys.exit(1)
    
    name = sys.argv[1]
    model = sys.argv[2]
    pr = sys.argv[3]
    repo = sys.argv[4]
    cwd = sys.argv[5]
    auto_level = sys.argv[6]
    
    safe_repo = repo.replace("/", "-")
    fifo = f"/tmp/duo-{safe_repo}-{pr}-{name}"
    log = f"/tmp/duo-{safe_repo}-{pr}-{name}.log"
    
    log_file = open(log, "a", buffering=1)
    proc = subprocess.Popen(
        [
            str(DROID), "exec",
            "--input-format", "stream-jsonrpc",
            "--output-format", "stream-jsonrpc",
            "-m", model,
            "--auto", auto_level,
            "--allow-background-processes",
        ],
        stdin=subprocess.PIPE,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=cwd,
        env=os.environ.copy(),
    )
    
    # Send initialize_session
    init_req = {
        "jsonrpc": "2.0",
        "type": "request",
        "factoryApiVersion": "1.0.0",
        "method": "droid.initialize_session",
        "params": {"machineId": os.uname().nodename, "cwd": cwd},
        "id": "init",
    }
    proc.stdin.write(json.dumps(init_req) + "\n")
    proc.stdin.flush()
    
    # Main loop: FIFO -> droid stdin
    while True:
        try:
            with open(fifo, "r") as f:
                for line in f:
                    if line.strip():
                        proc.stdin.write(line)
                        proc.stdin.flush()
        except Exception:
            time.sleep(0.1)
            continue


if __name__ == "__main__":
    main()
