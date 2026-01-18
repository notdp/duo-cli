#!/usr/bin/env python3
"""Daemon for running droid session.

Usage:
    python -m duo_cli.daemon <name> <model> <pr> <repo> <cwd> <auto>
    python -m duo_cli.daemon <name> "" <pr> <repo> <cwd> <auto> --resume <session_id>
"""

import json
import os
import subprocess
import sys
import time
import threading
from pathlib import Path

DROID = Path.home() / ".local" / "bin" / "droid"


def main():
    if len(sys.argv) < 7:
        print("Usage: python -m duo_cli.daemon <name> <model> <pr> <repo> <cwd> <auto> [--resume <session_id>]")
        sys.exit(1)
    
    name = sys.argv[1]
    model = sys.argv[2]
    pr = sys.argv[3]
    repo = sys.argv[4]
    cwd = sys.argv[5]
    auto_level = sys.argv[6]
    
    # Check for resume mode
    resume_mode = "--resume" in sys.argv
    session_id = None
    if resume_mode:
        idx = sys.argv.index("--resume")
        if idx + 1 < len(sys.argv):
            session_id = sys.argv[idx + 1]
    
    safe_repo = repo.replace("/", "-")
    fifo = f"/tmp/duo-{safe_repo}-{pr}-{name}"
    log = f"/tmp/duo-{safe_repo}-{pr}-{name}.log"
    
    log_file = open(log, "a", buffering=1)
    
    # Build droid command
    droid_args = [
        str(DROID), "exec",
        "--input-format", "stream-jsonrpc",
        "--output-format", "stream-jsonrpc",
        "--auto", auto_level,
        "--allow-background-processes",
    ]
    
    # Only add model for new sessions (resume uses existing model)
    if model and not resume_mode:
        droid_args.extend(["-m", model])
    
    proc = subprocess.Popen(
        droid_args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,  # Use PIPE to read and detect session ready
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=cwd,
        env=os.environ.copy(),
    )
    
    if resume_mode and session_id:
        # Send load_session (restores conversation history)
        req = {
            "jsonrpc": "2.0",
            "type": "request",
            "factoryApiVersion": "1.0.0",
            "method": "droid.load_session",
            "params": {"sessionId": session_id},
            "id": "load",
        }
    else:
        # Send initialize_session (new session)
        req = {
            "jsonrpc": "2.0",
            "type": "request",
            "factoryApiVersion": "1.0.0",
            "method": "droid.initialize_session",
            "params": {"machineId": os.uname().nodename, "cwd": cwd},
            "id": "init",
        }
    
    proc.stdin.write(json.dumps(req) + "\n")
    proc.stdin.flush()
    
    # Read stdout, write to log, detect session ready
    session_ready = False
    
    def is_session_ready(line: str) -> bool:
        if resume_mode:
            return '"id":"load"' in line and '"result":{"session"' in line
        else:
            return '"sessionId"' in line
    
    # Read until session is ready
    for line in proc.stdout:
        log_file.write(line)
        log_file.flush()
        
        if not session_ready and is_session_ready(line):
            session_ready = True
            break
    
    # Start background thread to continue reading stdout -> log
    def stdout_to_log():
        try:
            for line in proc.stdout:
                log_file.write(line)
                log_file.flush()
        except Exception:
            pass
    
    log_thread = threading.Thread(target=stdout_to_log, daemon=True)
    log_thread.start()
    
    # Now open FIFO (this unblocks launcher's write)
    # Main loop: FIFO -> droid stdin
    try:
        while True:
            if proc.poll() is not None:
                break
            
            try:
                with open(fifo, "r") as f:
                    for line in f:
                        if line.strip():
                            proc.stdin.write(line)
                            proc.stdin.flush()
            except Exception:
                time.sleep(0.1)
                continue
    finally:
        log_file.close()
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        try:
            os.remove(fifo)
        except Exception:
            pass


if __name__ == "__main__":
    main()
