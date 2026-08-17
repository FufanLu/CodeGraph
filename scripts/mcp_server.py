#!/usr/bin/env python3
"""An MCP server over a project's design graph: find / show / users / bootstrap / apply_delta.

Speaks newline-delimited JSON-RPC on stdin and stdout, which is the MCP stdio transport.
Standard library only — there is nothing to install.

# stdout is the protocol channel

Every byte written to stdout here is a JSON-RPC frame. Nothing else in this process may
print: one stray `print()` corrupts the stream, and the client's failure then looks nothing
like its cause. Diagnostics go to stderr, which the client collects as server logs.

That is why the query functions return strings instead of printing them.
"""

import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import delta as delta_rules  # noqa: E402
import graph as graph_lib  # noqa: E402
import structure_map  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "code-graph"
SERVER_VERSION = "1.0.0"


# ------------------------------------------------------------------ bootstrap guidance

# Two things in this text are deliberate and easy to undo by accident:
#
#   - The words CLASS and INTERFACE do not appear. Options offered in a list get used, and
#     naming all three kinds here produces one node per file — the directory tree wearing a
#     graph's clothes. Saying "do not enumerate classes" has to be done in prose.
#   - `file` is introduced *after* the grouping rules, and overlap is explicitly allowed.
#     Asking for a location at all pulls the output towards one-module-per-directory, which
#     is the shape this map most needs to avoid.
BOOTSTRAP_GUIDE = """\
You are drawing this project's **design graph**: the map every later task reads before
touching the code. Draw the **module skeleton only**. Do not enumerate the types inside
them — later work declares those at the moment it touches that code, so detail grows where
the work is, which is where it is worth reading anyway. What you draw now is the frame the
rest hangs on, not a half-finished map.

Read the repository, then call `apply_delta` with this shape:

```json
{
  "schema_version": 1,
  "adds": {
    "nodes": [
      {"path": "backend", "kind": "MODULE", "title": "Backend", "summary": "one sentence", "file": "src/server/"},
      {"path": "backend/storage", "kind": "MODULE", "title": "Storage", "summary": "one sentence", "file": "src/server/db/"},
      {"path": "backend/config", "kind": "MODULE", "title": "Config", "summary": "one sentence", "file": "src/server/config.mjs"}
    ],
    "edges": [
      {"from": "backend/storage", "to": "backend/config", "kind": "USES"}
    ]
  }
}
```

What to draw:

- **Modules only**: every node's `kind` is `MODULE`. There is no file-level node, and a
  module is not a file — it is the group of files that does one job.
- **At most two levels**: a few top-level areas, and the modules inside them. Nothing
  deeper than `area/module`.
- Group by what the code **is for**, not by directory depth. A module is something you can
  describe in one sentence: three directories doing one job are one module, and one
  directory holding three unrelated things is three.
- If your node count comes out near your file count, you drew the directory tree instead of
  the design. Start again from what the parts are for.
- Every edge is `USES`: this module depends on or calls that one. Leave out what is true of
  everything — a map where everything points at everything says nothing.
- `summary` is one sentence saying what the thing is **for**. Do not restate its name.
- `file` says **where in the repository that module lives**: the directory it mostly lives
  in, written with a trailing slash (`src/server/db/`), or the one file when the module
  really is one file. Without it, nothing can check this map against the repository.
- `file` is **where to start reading, not a carve-up of the repository**. Two modules may
  name the same directory, and a module may name a directory that also holds another
  module's code. Group by purpose first, say where it lives second.

Rules that are checked mechanically. One broken rule rejects the whole delta, so a single
bad edge costs you every node you got right:

- `path` is a slash-separated chain of slugs (`[a-z0-9-]+`). A node's parent must appear
  earlier in the list.
- **Every node needs a `file`** in this first delta, and it is a location inside this
  repository: relative, forward slashes, no leading slash and no `.` or `..` segment.
- **Both ends of every edge must be a node that exists once this delta is applied.** An
  edge pointing at an external library, or at a module you left out, rejects everything.
- No extra fields anywhere.
"""


# ------------------------------------------------------------------ tools

TOOLS = [
    {
        "name": "find",
        "description": (
            "Locate nodes in the design graph by keyword, across title, summary, path and "
            "file. Multiple keywords are ANDed. Start here when you do not know a node's "
            "path."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "One or more keywords, ANDed together.",
                }
            },
            "required": ["keywords"],
        },
    },
    {
        "name": "show",
        "description": (
            "One node's kind, title, summary, file, direct children, what it depends on and "
            "what uses it. `path` is the slash-separated slug path (for example "
            "server/store), not a file path."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "users",
        "description": (
            "Everything that depends on a node, directly and indirectly. Read this before "
            "changing a node — it is the blast radius."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "map",
        "description": (
            "The structure map: the whole design at its top level, plus what changed "
            "recently. This is the same text the session hook shows at the start of a turn "
            "— call it directly when that hook is not installed."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "bootstrap",
        "description": (
            "Explains how to draw this project's design graph when it has none yet. Read "
            "the repository, then call apply_delta. Refuses when a graph already exists."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "apply_delta",
        "description": (
            "Validate a change to the design graph and record it: what your work added, "
            "removed or rewired. Any broken rule rejects the whole delta and writes nothing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "delta": {
                    "type": "object",
                    "description": (
                        'Shape: {"schema_version": 1, "adds": {"nodes": [...], "edges": '
                        '[...]}, "removes": {"nodes": [...], "edges": [...]}}'
                    ),
                }
            },
            "required": ["delta"],
        },
    },
]


def content(text, is_error=False):
    result = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def _with_notes(graph, body):
    if not graph.skipped:
        return body
    # Say it out loud. Nothing in the output would let a reader infer that entries are
    # missing, and a partial graph read as a complete one is how you conclude "nothing
    # depends on this" and break something.
    return f"{body}\n\n⚠ {graph.skipped} node(s) were skipped: no path, or the wrong shape."


def _tool_find(options, root):
    keywords = options.get("keywords")
    if not isinstance(keywords, list):
        keywords = [keywords] if isinstance(keywords, str) else []
    graph = graph_lib.load(root)
    return content(_with_notes(graph, graph_lib.find(root, graph, [str(w) for w in keywords])))


def _tool_show(options, root):
    path = str(options.get("path", "")).strip()
    if not path:
        return content("show needs a node path, for example: show server/store")
    graph = graph_lib.load(root)
    return content(_with_notes(graph, graph_lib.show(root, graph, path)))


def _tool_users(options, root):
    path = str(options.get("path", "")).strip()
    if not path:
        return content("users needs a node path, for example: users server/store")
    graph = graph_lib.load(root)
    return content(_with_notes(graph, graph_lib.users(root, graph, path)))


def _tool_map(options, root):
    rendered = structure_map.for_repository(root)
    if rendered is None:
        raise graph_lib.GraphUnavailable(
            f"this project has no design graph yet ({root}).\n"
            "Call `bootstrap` to draw one."
        )
    return content(rendered)


def _tool_bootstrap(options, root):
    payload = graph_lib.read_store(root)
    if payload and payload.get("nodes"):
        # Refusing to redraw is the point, not a limitation. The graph is maintained by the
        # work that touches the code, one delta at a time; redrawing would discard
        # everything every later change declared.
        return content(
            f"This project already has a design graph ({len(payload['nodes'])} nodes). "
            "Change it with `apply_delta` instead of redrawing it: declare only what your "
            "work adds, removes or rewires.\n\nUse `find` to see what is already there."
        )
    return content(
        f"{BOOTSTRAP_GUIDE}\n"
        f"Your delta will be written to `{graph_lib.STORE_PATH}`. Commit that file if you "
        f"want the graph to travel with the repository.\nRepository root: {root}"
    )


def _tool_apply_delta(options, root):
    payload = options.get("delta")
    if isinstance(payload, str):
        # Models sometimes pass the JSON as a string. Accepting it costs three lines and
        # saves a round trip that would teach nobody anything.
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as error:
            return content(f"delta is not valid JSON: {error}", is_error=True)

    store = graph_lib.read_store(root) or {
        "schema_version": delta_rules.SCHEMA_VERSION,
        "project": root.name,
        "nodes": [],
        "edges": [],
    }
    current = {node["path"] for node in store["nodes"]}

    try:
        plan = delta_rules.validate(current, payload, require_file=not current)
    except delta_rules.DeltaRejected as rejection:
        return content(
            f"Delta rejected, nothing was written: {rejection}\n\n"
            "The whole delta is rejected rather than partly applied, because half an intent "
            "is a state nobody declared. Fix it and call `apply_delta` again."
        )

    updated = delta_rules.apply(store, plan, stamp=_stamp(root))
    written = graph_lib.write_store(root, updated)
    return content(
        f"Delta applied to `{written.relative_to(root)}`: "
        f"{len(plan['add_nodes'])} node(s) added, {len(plan['add_edges'])} edge(s) added, "
        f"{len(plan['remove_nodes'])} node(s) removed, {len(plan['remove_edges'])} edge(s) "
        f"removed. The graph now has {len(updated['nodes'])} node(s)."
    )


HANDLERS = {
    "find": _tool_find,
    "show": _tool_show,
    "users": _tool_users,
    "map": _tool_map,
    "bootstrap": _tool_bootstrap,
    "apply_delta": _tool_apply_delta,
}


def _stamp(root):
    """When a change was recorded, and the commit it sat on if there is one.

    The commit is the one HEAD pointed at while the work was being done — not a commit
    containing the change, which does not exist yet. It is a coarse anchor for "roughly
    when", which is all the recent-changes list claims to be.
    """
    stamp = {"at": datetime.datetime.now().astimezone().strftime("%Y-%m-%d")}
    try:
        found = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if found.returncode == 0 and found.stdout.strip():
            stamp["commit"] = found.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return stamp


def call_tool(params, root):
    name = params.get("name")
    handler = HANDLERS.get(name)
    if handler is None:
        return content(f"unknown tool: {name}", is_error=True)
    try:
        return handler(params.get("arguments") or {}, root)
    except graph_lib.GraphUnavailable as error:
        return content(str(error), is_error=True)
    except Exception as error:  # noqa: BLE001
        # Never let a traceback reach the client. It reads as "this tool is broken", and the
        # reader stops using it for the rest of the session.
        return content(f"{name} could not run: {error}", is_error=True)


# ------------------------------------------------------------------ repository root


def repo_root(start=None):
    """The repository root, or the starting directory when this is not a git checkout.

    This value does double duty: it is where the graph is stored, and it is the base every
    `file` field is resolved against for the missing-file check. Getting it wrong makes
    every node report a missing file, which reads as a broken graph rather than a bad cwd.
    """
    start = Path(start or os.getcwd()).resolve()
    try:
        found = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if found.returncode == 0 and found.stdout.strip():
            return Path(found.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return start


# ------------------------------------------------------------------ protocol


def dispatch(message, root):
    """One request in, one result-or-error out. Returns `None` for notifications."""
    if "id" not in message:
        return None
    method = message.get("method", "")
    if method == "initialize":
        return "result", {
            # Echo the client's version rather than asserting ours: a client speaking a
            # newer revision should not be told it reached the wrong kind of server.
            "protocolVersion": (message.get("params") or {}).get(
                "protocolVersion", PROTOCOL_VERSION
            ),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    if method == "ping":
        return "result", {}
    if method == "tools/list":
        return "result", {"tools": TOOLS}
    if method == "tools/call":
        return "result", call_tool(message.get("params") or {}, root)
    return "error", {"code": -32601, "message": f"method not found: {method}"}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    start = None
    while argv:
        flag = argv.pop(0)
        value = argv.pop(0) if argv else None
        if value is None:
            print(f"{flag} needs a value", file=sys.stderr)
            return 2
        if flag == "--repo":
            start = value
        else:
            print(f"unknown argument {flag}", file=sys.stderr)
            return 2

    root = repo_root(start)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        outcome = dispatch(message, root)
        if outcome is None:
            continue
        kind, body = outcome
        sys.stdout.write(
            json.dumps({"jsonrpc": "2.0", "id": message["id"], kind: body}, ensure_ascii=False)
            + "\n"
        )
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
