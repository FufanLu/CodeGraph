#!/usr/bin/env python3
"""Put the structure map in front of the agent at the start of a turn.

Register as a `UserPromptSubmit` hook. Plain stdout on exit 0 becomes context.

# Why this exists when the query tools already do

Tools are pull: they answer when asked, and only if the reader thinks to ask. A reader that
goes straight to grep never discovers the graph, and by the time that has happened the cost
this is meant to remove has already been paid. The map is small enough to show
unconditionally, so knowing the graph exists is not left to chance.

That is the whole difference between an available tool and a map you are already holding.

# This hook must never fail loudly

Two rules, and both are about not being worse than no hook at all:

- **Always exit 0.** Exit code 2 on `UserPromptSubmit` blocks the prompt and *erases what
  the user typed*. There is no graph problem worth doing that over.
- **Print nothing when there is nothing to say.** Most repositories have no graph. A hook
  that announced its own irrelevance on every single turn would be uninstalled within a
  day, and rightly so.

So every failure path here ends in "print nothing, exit 0". Silence is the correct output
for a project this tool has nothing to say about.

# Why plain text and not the structured JSON form

The JSON form silently injects nothing if a key name is wrong. Plain stdout has one failure
mode fewer, and the map always begins with `# `, so it can never be mistaken for JSON.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main():
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}

    # The session's working directory, which is the only thing here that says which project
    # the user is actually in.
    start = payload.get("cwd") or None

    try:
        import mcp_server
        import structure_map

        rendered = structure_map.for_repository(mcp_server.repo_root(start))
    except Exception:  # noqa: BLE001
        return 0

    if rendered:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
