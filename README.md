# code-graph

A Claude Code skill and MCP server for a project's **design graph**: the modules, classes and
interfaces a codebase is made of, and which of them depend on which.

The graph is **declared, not extracted** — nothing parses your source. That is what lets it
describe intent rather than syntax, cover code that is not written yet, and stay readable on
a repository too large to hold in your head.

It answers three questions grep is bad at:

- **Where does this concept live?** → `find`
- **What is this part for, and what does it lean on?** → `show`
- **What breaks if I change it?** → `users`

Two that keep it true:

- **This project has no graph yet.** → `bootstrap`
- **My work changed the design.** → `apply_delta`

And one that gets read before the agent decides anything at all:

- **What is this project made of?** → `map`, delivered automatically by the hook below.

## Install

```bash
git clone https://github.com/FufanLu/CodeGraph.git
cp -R CodeGraph ~/.claude/skills/code-graph
claude mcp add --scope user code-graph \
  -- python3 ~/.claude/skills/code-graph/scripts/mcp_server.py
```

Python 3 and its standard library are the only requirements. Nothing to install, no service
to run, no API key. The server operates on whatever repository the session is in, found via
`git rev-parse`; `--repo <path>` pins it to one instead.

### Push the map, do not wait to be asked

Tools are pull. They answer when asked, and only if the agent thinks to ask — an agent that
goes straight to grep never learns the graph exists, and by then the cost this is meant to
remove has already been paid. "The map was available" is no comfort.

The hook makes it push. Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "matcher": "*",
        "hooks": [
          { "type": "command", "command": "python3 ~/.claude/skills/code-graph/scripts/hook.py" }
        ]
      }
    ]
  }
}
```

Every turn now starts with something like:

```markdown
# example — structure map

2 entries · 4 in the design. This map is **regenerated from `.codegraph/graph.json` every
turn** and is only an index — read it before exploring the repository, then open what you need.

ℹ️ Top level only: 2 deeper entries are held back on purpose, not lost. Open one with
`show <path>`; the whole graph is on disk in `.codegraph/graph.json`.

## Backend `MODULE`
`backend`
src/server/
http surface
used by: Web (USES)

## Recent changes

- add node `backend` (f60d8b3)
```

The hook prints nothing for a project without a graph, and **exits 0 on every failure
path** — a `UserPromptSubmit` hook that exits 2 erases what the user typed, and nothing
about a design graph is worth that.

## How it works

The graph lives in `.codegraph/graph.json` at the repository root — plain JSON, written
atomically, meant to be committed so it travels with the repository.

```json
{
  "schema_version": 1,
  "project": "example",
  "nodes": [
    {"path": "backend", "kind": "MODULE", "title": "Backend", "summary": "http surface", "file": "src/server/"},
    {"path": "backend/store", "kind": "CLASS", "title": "Store", "summary": "persists orders", "file": "src/server/db/store.py"}
  ],
  "edges": [{"from": "backend/store", "to": "backend/config", "kind": "USES"}]
}
```

Containment and dependency are stored separately on purpose. Containment is the slug path
itself — `backend/store` is inside `backend` — so it is a tree, and it is what you navigate
by. Dependency is the edge list: many to many, free to contain cycles, and it is what you
query. Drawing both as one relation is how architecture diagrams turn into hairballs.

Changes arrive one delta at a time, from the work that made them:

```json
{
  "schema_version": 1,
  "adds": {
    "nodes": [{"path": "server/store", "kind": "CLASS", "title": "Store", "summary": "one sentence", "file": "src/store.mjs"}],
    "edges": [{"from": "server/store", "to": "server/config", "kind": "USES"}]
  },
  "removes": {"nodes": ["server/legacy"], "edges": []}
}
```

## Three design decisions worth knowing

**One broken rule rejects the whole delta.** A delta expresses a single intent — added a
class, added the edge that gives it meaning. Applying the valid half leaves the node without
the edge: a state nobody declared and nobody can interpret. Rejecting all of it keeps the
graph a record of things somebody actually said.

**There is no redraw.** The graph is maintained by the work that touches the code, so
redrawing it would discard everything every later change declared. `bootstrap` refuses once a
graph exists.

**Truncation is always explicit.** Results are budgeted, and when something is cut the output
says how much and how to narrow the query. Silent truncation is the worst failure available
here: a half-listed dependency table looks exactly like a complete one, so the reader
concludes "nothing else uses this" and changes it safely — which is how you break something
while believing you checked. The same reasoning is why a query that matches nothing still
returns signposts instead of a blank answer.

**"Held back" and "dropped" never share a sentence.** The map shows the top level only, so
deeper nodes are absent by design — they are in the store and one `show` away. That is not
the same as the byte budget forcing entries out, which is real loss. Collapsing both into
"truncated to fit" describes the first as loss, and every project with a second level hits it
on every single turn — teaching the reader that the map is lossy and the graph is not worth
querying, which is exactly backwards.

## What it does not do

- **No method-level detail**, by decision. It would cost an order of magnitude more to
  maintain, and "which parts are involved" is already answered one level up.
- **It can go stale.** Nodes are declared, so one can outlive the code it describes. A node
  whose file is gone from disk is flagged `⚠ file missing`, and an edge naming an unknown
  node is flagged too. Both mean the graph is wrong on that point — trust the working tree.
- **It is not a picture of the directory tree.** Two modules may name the same directory. A
  module is the group of files that does one job, which is not the same thing as a folder.

## Tests

```bash
python3 run_tests.py
```

Standard library only. The runner refuses to start if any `test_*.py` sits outside the
collected directories — a test that never runs looks identical to a test that passes, and
that is the failure mode worth guarding against.
