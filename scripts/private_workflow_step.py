#!/usr/bin/env python3
"""Run a public Actions shell step without publishing its private output.

This is output containment, not a sandbox for untrusted code. Trusted scripts
retain normal files, exit status, GITHUB_ENV and GITHUB_OUTPUT behavior. Stdout,
stderr (including child processes and tracebacks) and step summaries are never
forwarded to GitHub. No plaintext diagnostic artifact is created.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def run_step(script: str) -> int:
    if not Path(script).is_file():
        print("Private step could not start: script unavailable.", flush=True)
        return 1
    env = os.environ.copy()
    # GitHub displays summaries separately from stdout; discarding only the
    # console output would leave this second disclosure channel open.
    env["GITHUB_STEP_SUMMARY"] = os.devnull
    # Never inherit shell tracing from a diagnostic session.
    env.pop("BASH_ENV", None)
    env.pop("ENV", None)
    print("Private step started; detailed output and summaries are withheld.", flush=True)
    try:
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", script],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
            check=False,
        )
    except (OSError, KeyboardInterrupt):
        print("Private step could not complete; no diagnostic content was published.", flush=True)
        return 1
    code = result.returncode if result.returncode >= 0 else 128 - result.returncode
    if code:
        print(f"Private step failed (exit {code}); detailed output remains private.", flush=True)
    else:
        print("Private step passed.", flush=True)
    return code


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Private step could not start: one script is required.")
        raise SystemExit(1)
    raise SystemExit(run_step(sys.argv[1]))
