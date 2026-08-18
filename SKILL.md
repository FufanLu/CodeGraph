---
name: code-graph
description: Query and maintain a project's design graph — the modules, classes and interfaces it is made of, and which of them depend on which — instead of grepping the repository. Use it to locate the code that owns a concept, to read what a part is for, to find out what a change would affect before making it, and to draw the graph for a project that has none.
---

# Design Graph

A design graph is a map of what a project is *made of*: modules, classes and interfaces, and
the dependencies between them. It is **declared, not extracted** — nothing parses your
source. That is what lets it describe intent, cover code that is not written yet, and stay
readable on a repository too large to hold in your head.

It answers three questions grep is bad at: *where does this concept live*, *what is this
part for*, and *what breaks if I change it*.

## Reading the graph

- **`map`** — the whole design at its top level, plus what changed recently. This is the
  index: read it before exploring, then open what you need. When the hook below is
  installed it arrives on its own and you rarely need to ask for it.
- **`find`** — locate nodes by keyword across title, summary, path and file. Multiple
  keywords are ANDed. Start here when you do not know a node's path.
- **`show`** — one node's kind, title, summary, file, direct children, what it depends on,
  and what uses it.
- **`users`** — everything that depends on a node, directly and indirectly. **Read this
  before changing a node**; it is the blast radius, and it is not a one-hop question.

A node is addressed by its **slug path**: slash-separated and lowercase, like `server/store`.
That is its address in the graph, **not a file path**. Passing a file path is the most
common mistake; the tool says so and offers the closest matches. When unsure, `find` first.

Results are budgeted to a few dozen lines. When something is cut, the output says how much
and how to narrow the query — a truncated list is never presented as if it were complete.

## Drawing and changing the graph

- **`bootstrap`** — call this when a project has no graph. It explains what to draw and what
  makes a good module. Read the repository, then call `apply_delta`.
- **`apply_delta`** — declare what your work added, removed, rewired or reworded.

```json
{
  "schema_version": 1,
  "adds": {
    "nodes": [{"path": "server/store", "kind": "CLASS", "title": "Store", "summary": "one sentence", "file": "src/store.mjs"}],
    "edges": [{"from": "server/store", "to": "server/config", "kind": "USES"}]
  },
  "updates": {"nodes": [{"path": "server/config", "summary": "a better sentence"}]},
  "removes": {"nodes": ["server/legacy"], "edges": []}
}
```

**To correct what an existing node says, use `updates` — never remove it and add it back.**
An update changes `kind`, `title`, `summary` or `file` and leaves the node's edges standing.
Removing a node takes every edge touching it as well, so spelling a reworded summary as a
removal costs the graph every dependency that node had. `path` is the node's address and an
update cannot change it: a different path is a different node, which really is a removal and
an addition.

Node kind is `MODULE`, `CLASS` or `INTERFACE`; edge kind is `EXTENDS` or `USES`. A node's
parent must already exist or appear earlier in the same delta, and both ends of every edge
must exist once the delta is applied. `file` is a location inside the repository — a
directory written with a trailing slash, or a single file.

**Most work does not change the design. Declare nothing when yours does not.** A rename, a
bug fix, a new test usually has nothing to say here. A delta records intent, so an empty one
is the honest answer most of the time.

**One broken rule rejects the whole delta and writes nothing.** Deliberate: applying the
valid half would leave the node without the edge that gave it meaning — a state nobody
declared and nobody can interpret. Fix it and call again.

**There is no redraw.** A graph is maintained one delta at a time by the work that touches
the code; redrawing it would discard everything every later change declared. `bootstrap`
refuses once a graph exists.

## Where it is stored

`.codegraph/graph.json` at the repository root, found via `git rev-parse`. It is plain JSON,
written atomically, and meant to be committed — that is how the graph travels with the
repository and how a teammate gets it for free. A worktree is its own repository root, so it
carries its own graph; a branch carries whatever it committed.

**When a merge leaves this file conflicted, never hand-edit the JSON.** A merge driver
resolves the file as data — nodes by `path`, edges as a set, history unioned — and only stops
when both branches rewrote the *same* node differently. When it stops it keeps our side, says
which paths disagreed, and leaves valid JSON behind. Re-declare those paths with
`apply_delta` and stage the file. If the file does hold conflict markers, the driver is not
installed on this clone: take either side whole, then re-declare what the other side added.

## What the graph does not tell you

- **It stops at classes and interfaces.** No method-level detail, by decision: it would cost
  an order of magnitude more to maintain, and "which parts are involved" is already answered
  one level up.
- **It can be stale.** Nodes are declared, so a node can outlive the code it describes. One
  whose file is gone from disk is flagged `⚠ file missing`, and an edge naming an unknown
  node is flagged too. Both mean the graph is wrong on that point: **trust the working tree,
  and say so in your answer.**
- **It is a design model, not a picture of the directory tree.** Two modules may name the
  same directory. A module is the group of files that does one job, which is not the same
  thing as a folder.

If a tool reports that no graph exists, that is not a failure — most projects have none yet.
Either call `bootstrap` to draw one, or read the code and carry on.

## Installing

```bash
cp -R code-graph ~/.claude/skills/
claude mcp add --scope user code-graph \
  -- python3 ~/.claude/skills/code-graph/scripts/mcp_server.py
```

Python 3 and its standard library are the only requirements — nothing to install, no service
to run. The server works on whatever repository the session is in; `--repo <path>` pins it to
one instead.

### The map hook (recommended)

The tools above are *pull*: they answer when asked, and only if you think to ask. Going
straight to grep means never learning the graph exists — and by then the cost this is meant
to remove has already been paid.

This hook makes it *push*: the structure map is placed in front of the agent at the start of
every turn, before it decides how to explore anything. Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/skills/code-graph/scripts/hook.py"
          }
        ]
      }
    ]
  }
}
```

It prints nothing for a project with no graph, and it exits 0 on every failure path —
including a corrupt store or unreadable input. That is deliberate: a `UserPromptSubmit` hook
exiting 2 would erase what you typed, and nothing about a design graph is worth that.

The map is capped at 12 KiB and shows only the top level, so it stays cheap enough to keep
switched on. Everything below the top level is one `show` away.
