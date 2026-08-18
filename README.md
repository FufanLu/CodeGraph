# code-graph

**A map of your project, handed to the agent before it starts guessing.**

code-graph maintains a *design graph* — the modules, classes and interfaces a codebase is
made of, and which of them depend on which — as one JSON file. It serves that graph over MCP
and pushes the top level into the agent's context at the start of every turn, so the agent
knows what the project is made of before it decides where to look. The graph is **declared,
not parsed out of your source**, which is what lets it describe intent rather than syntax and
stay readable on a repository too large to hold in your head. Pure Python, standard library
only, nothing to install.

[Quickstart](#quickstart) •
[Why](#why) •
[Install](#install) •
[The graph](#the-graph) •
[Tools](#tools) •
[How it works](#how-it-works) •
[Verifying it works](#verifying-it-works)

## Quickstart

With the hook installed, every turn begins with this already in the agent's context — it
does not have to think to ask for it:

```markdown
# example — structure map

2 entries · 4 in the design. This map is **regenerated from `.codegraph/graph.json` every
turn** and is only an index — read it before exploring the repository, then open what you
need.

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

That is 797 bytes for a four-node project; the cap is 12 KiB no matter how large the graph
gets. From there the agent opens what it needs:

```
find storage           # locate nodes by keyword, ANDed
show backend           # what one part is, what is inside it, what it leans on
users backend/config   # blast radius — read before changing anything

bootstrap              # this project has no graph; here is how to draw one
apply_delta            # my work changed the design; record it
```

Real output, not idealised — note the staleness marker, which is the graph admitting it is
wrong rather than quietly misleading you:

```
$ show backend
backend  MODULE
title    Backend
file     src/server/
summary  http surface

children (2)
  backend/storage  MODULE  Storage
  backend/config   MODULE  Config

depends_on (0)

used_by (1)
  web  MODULE  Web  ⚠ file missing
```

## Why

### Why use it

- **"Where does this concept live?" costs one call instead of a grep sweep.** `find` searches
  titles, summaries, slug paths and file locations at once, and answers with the parts rather
  than with file contents.
- **"What breaks if I change this?" has an actual answer.** `users` walks reverse edges
  transitively. Reporting only direct dependents systematically understates the blast radius,
  which is the mistake this query exists to prevent.
- **It describes intent, not syntax.** Because the graph is declared, it can say that three
  directories are one module, and can carry a design that is not fully built yet. No parser
  gets either of those right.
- **The agent does not have to remember to use it.** The hook pushes the map. A tool that is
  merely available is a tool an agent skips on its way to grep.

### Why not use it

- **It can be wrong, and confidently.** Nodes are declared, so one can outlive the code it
  describes. There is a `⚠ file missing` marker for the obvious case, but nothing detects a
  module that quietly changed its job. Trust the working tree.
- **Somebody has to keep it current.** Every change that reshapes the design costs an
  `apply_delta`. On a project small enough to hold in your head, `ls` and reading files is
  cheaper than maintaining a map of it.
- **It stops at classes and interfaces.** No method-level detail, by decision. If your
  question is "which function computes this", this will not answer it.
- **It is not a directory tree.** If you want the file layout, `find .` is right there and
  it is always accurate.

## Install

Python 3.7 or newer (tested on 3.12) and its standard library. No packages, no service, no
API key.

Steps 1 and 2 are required. Step 3 is what makes it push rather than pull.

1. **Install the skill**

   ```bash
   git clone https://github.com/FufanLu/CodeGraph.git
   cp -R CodeGraph ~/.claude/skills/code-graph
   ```

2. **Register the MCP server**

   <details><summary>Claude Code</summary>

   ```bash
   claude mcp add --scope user code-graph \
     -- python3 ~/.claude/skills/code-graph/scripts/mcp_server.py
   ```
   </details>

   <details><summary>Any client that reads mcpServers JSON</summary>

   ```json
   {
     "mcpServers": {
       "code-graph": {
         "command": "python3",
         "args": ["~/.claude/skills/code-graph/scripts/mcp_server.py"]
       }
     }
   }
   ```
   </details>

   The server operates on whatever repository the session is in, located with
   `git rev-parse`. Pass `--repo <path>` to pin it to one instead.

3. **Install the context hook** <sup>(optional, but it is the whole point)</sup>

   In `~/.claude/settings.json`:

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

   Put it in a project's `.claude/settings.json` instead to enable it for just that project.

Then check the install, which matters more than usual here because a hook fails silently by
design:

```bash
python3 ~/.claude/skills/code-graph/run_tests.py
```

## The graph

One file, `.codegraph/graph.json` at the repository root. Plain JSON, written atomically,
meant to be committed — that is how the graph travels with the repository and how a teammate
gets it for free.

### Nodes

A node is addressed by its **slug path**, and this is the one thing worth getting straight
before anything else: `path` is an address in the graph, `file` is a location on disk. They
are not the same field and confusing them is the most common mistake.

```json
{
  "path": "backend/storage",
  "kind": "MODULE",
  "title": "Storage",
  "summary": "persists orders",
  "file": "src/server/db/"
}
```

`kind` is `MODULE`, `CLASS` or `INTERFACE`. A directory is written with a trailing slash;
the slash is what separates it from a file of the same name.

### Edges

Directed, and stored separately from containment.

```json
{ "from": "backend/storage", "to": "backend/config", "kind": "USES" }
```

`kind` is `USES` or `EXTENDS`. Containment is the slug path itself — `backend/storage` is
inside `backend` — so it is a tree you navigate. Dependency is the edge list: many to many,
free to contain cycles, and it is what you query. Drawing both as one relation is how
architecture diagrams turn into hairballs.

### Deltas

The graph only ever changes by delta: what one piece of work added, removed, rewired or
reworded.

```json
{
  "schema_version": 1,
  "adds": {
    "nodes": [{"path": "server/store", "kind": "CLASS", "title": "Store", "file": "src/store.mjs"}],
    "edges": [{"from": "server/store", "to": "server/config", "kind": "USES"}]
  },
  "updates": {"nodes": [{"path": "server/config", "summary": "a better sentence"}]},
  "removes": {"nodes": ["server/legacy"], "edges": []}
}
```

`updates` is how an existing node is corrected, and using it rather than a remove-and-add is
not a style preference: removing a node takes every edge touching it with it, so rewording a
summary the long way costs the graph every dependency that node had. An update changes
`kind`, `title`, `summary` or `file` and leaves the edges standing. It cannot change `path`
— that is the node's address, and a different one is a different node.

When a removal does take edges down, the reply names them one by one, so they can be
re-declared if they still hold.

## Tools

- `find` — Locate nodes by keyword across title, summary, path and file. Keywords are ANDed.
  Returns signposts rather than nothing when there are no matches.
    - `keywords` (string[], required): Terms to match
- `show` — One node's kind, title, summary, file, direct children, what it depends on and
  what uses it.
    - `path` (string, required): Slug path of the node, e.g. `backend/storage`
- `users` — Everything that depends on a node, directly and transitively. The blast radius.
    - `path` (string, required): Slug path of the node
- `map` — The whole design at its top level plus recent changes; the same text the hook
  delivers. Useful when the hook is not installed.
- `bootstrap` — How to draw the graph for a project that has none, and what makes a good
  module. Refuses once a graph exists.
- `apply_delta` — Validate a change to the graph and record it. Any broken rule rejects the
  whole delta and writes nothing.
    - `delta` (object, required): The delta, shaped as above

## How it works

1. **Draw it once.** `bootstrap` returns instructions; the agent reads the repository and
   calls `apply_delta`. Nothing is scanned or parsed — the agent decides what the modules are,
   which is the only way to group three directories doing one job into one module.
2. **Keep it current.** Work that reshapes the design calls `apply_delta` again. Each delta is
   validated as a whole and recorded with the date and the commit it sat on.
3. **Read it every turn.** The hook renders the top level into the agent's context, capped at
   12 KiB, shedding detail in a fixed order — `used by`, then summaries, then history, then
   entries — and announcing whatever it shed.
4. **Open what matters.** Anything below the top level is one `show` away, and the map says so
   rather than pretending it does not exist.

**If the graph is missing or unreadable, nothing breaks.** The hook prints nothing and exits
0; the tools return an error that says to read the code and carry on. code-graph never blocks
a turn — a `UserPromptSubmit` hook that exits non-zero would erase what you typed, and nothing
about a design graph is worth that.

## Verifying it works

The hook is invisible when it succeeds and equally invisible when it never ran, so check it
deliberately:

1. Restart your client and confirm `code-graph` appears in the tool list (`/mcp` in Claude
   Code) with six tools.
2. In a project with no graph, ask the agent to draw one. It should call `bootstrap`, read
   around, then call `apply_delta` — and `.codegraph/graph.json` should appear.
3. Ask "what is this project made of?" in a new session. With the hook working, the answer
   arrives without any tool call at all, because the map is already in context.
4. Check the hook directly:

   ```bash
   echo '{"cwd":"'"$PWD"'"}' | python3 ~/.claude/skills/code-graph/scripts/hook.py
   ```

   A project with a graph prints the map. A project without one prints nothing and exits 0 —
   that is correct, not a failure.

## Design decisions worth knowing

**Truncation is always explicit.** When something is cut, the output says how much and how to
narrow the query. Silent truncation is the worst failure available here: a half-listed
dependency table looks exactly like a complete one, so the reader concludes "nothing else uses
this" and changes it safely — which is how you break something while believing you checked.
Same reasoning for why a query matching nothing returns signposts instead of a blank answer.

**"Held back" and "dropped" never share a sentence.** The map shows the top level only, so
deeper nodes are absent by design — they are in the store, one `show` away. That is not the
same as the byte budget forcing entries out, which is real loss. Collapsing both into
"truncated to fit" describes the first as loss, and every project with a second level hits it
on every turn — teaching the reader the map is lossy and the graph is not worth querying,
which is exactly backwards.

**One broken rule rejects the whole delta.** A delta expresses a single intent: added a class,
added the edge that gives it meaning. Applying the valid half leaves the node without the edge
— a state nobody declared and nobody can interpret.

**There is no redraw.** The graph is maintained by the work that touches the code, so
redrawing it would discard everything every later change declared. `bootstrap` refuses once a
graph exists.

## Troubleshooting

**The tools do not appear.** The MCP server is not registered, or Python is not on the PATH
your client uses. Run `python3 ~/.claude/skills/code-graph/scripts/mcp_server.py < /dev/null`
— it should exit 0 silently.

**Every node reports `⚠ file missing`.** The server resolved the wrong repository root, so the
`file` fields are being checked against the wrong directory. Pin it with `--repo <path>`.

**The map never appears.** Most likely the project has no graph — run the hook by hand as
above. If that prints the map, the hook is not registered; check `~/.claude/settings.json`.

**A delta keeps getting rejected.** The message names the one rule that failed. The usual
causes are an edge pointing at a node that was never declared, and a `path` that is a file
path rather than a slug path.

## Tests

```bash
python3 run_tests.py
```

Standard library only. The runner refuses to start if any `test_*.py` sits outside the
collected directories — a test that never runs looks identical to a test that passes, and that
is the failure mode worth guarding against.

## Agent-facing documentation

[`SKILL.md`](SKILL.md) is what the agent reads. It carries the usage discipline — read `users`
before changing a node, trust the working tree over the graph, declare nothing when your work
did not change the design. This README is for the human deciding whether to install it.
