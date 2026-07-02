#!/usr/bin/env python3
import re
import subprocess
import sys


VOLATILE_KEYS = {"generated_at", "last_sync"}


def main() -> int:
    diff = subprocess.check_output(
        ["git", "diff", "--cached", "--unified=0", "--", "data/"],
        text=True,
    )
    key_pattern = re.compile(r'"([^"]+)":')
    for line in diff.splitlines():
        if not line.startswith(("+", "-")) or line.startswith(("+++", "---")):
            continue
        body = line[1:].strip()
        match = key_pattern.match(body)
        if match and match.group(1) in VOLATILE_KEYS:
            continue
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
