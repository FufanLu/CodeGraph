#!/usr/bin/env python3
"""The structure map: the small, always-present view of the graph.

The queries in `graph.py` are pull — they answer when asked. This module is the push half:
a compact map meant to be placed in front of the reader at the start of every turn, before
it decides how to explore anything.

# Why a pushed map is not the same thing as a query tool

A tool only fires if the reader thinks to use it. A reader that goes straight to grep never
learns the graph exists, and "the map was available" is no comfort — the whole cost this is
meant to remove has already been paid by then. So the map is small enough to show
unconditionally, and its job is to be an index: enough to know what the parts are and which
one to open, not enough to answer detailed questions.

# Three ways a node can be absent, and why they must never share a sentence

- **Held back**: the caller asked for the skeleton only (`skeleton_depth`, normally 1).
  Those nodes are not lost — they are in the store, and `show` opens them.
- **Dropped**: the byte budget forced the deepest level out. Those really are not here.
- **Skipped**: the store holds an entry this code could not read at all. That one is not a
  view being trimmed, it is damage, and it is the only one of the three that says the file
  on disk needs fixing.

A fourth thing has to be said here even though it is not absence at all: a node whose
declared `file` is gone from the repository. `show` marks each one, but only nodes that get
rendered carry their marker, and this map renders the top level — so a stale node one level
down would be invisible in the document that is delivered every turn while `show` has known
about it all along. The count goes in the header for that reason.

Collapsing the first two into "truncated to fit" describes the first as loss. Every project with a
second level hits the first case on every single turn, so that phrasing teaches the reader
that the map is lossy and the graph is not worth querying — which is exactly backwards.

# Why the budget matters at all

This text is paid for on every turn. Left unbounded, one hub node that half the project
depends on renders a `depends on:` line long enough to dwarf everything else.
"""

from pathlib import Path

import graph as graph_lib

# The byte budget for the map. It buys a permanent slot in the context, so it has to stay
# small enough that nobody is tempted to turn it off.
BUDGET = 12 * 1024
# How many recent changes to list. The point is "was this added last week or long ago",
# which a short list answers as well as a long one.
RECENT_LIMIT = 20


def depth_of(path):
    return path.count("/") + 1


def _ordered(graph):
    """Depth-first, children sorted by title, so a parent is followed by what is inside it.

    Sorting by (depth, title) instead would interleave unrelated subtrees and lose the one
    thing this tree exists to show: what contains what.
    """
    by_parent = {}
    for path in graph.order:
        parent = path.rsplit("/", 1)[0] if "/" in path else None
        by_parent.setdefault(parent, []).append(path)

    ordered = []

    def walk(parent):
        for path in sorted(by_parent.get(parent, ()), key=lambda p: graph.nodes[p]["title"]):
            ordered.append(path)
            walk(path)

    walk(None)
    # Anything the walk could not reach (an orphan whose parent is gone) still has to
    # appear. Losing a node quietly is far worse than listing it out of order.
    for path in graph.order:
        if path not in ordered:
            ordered.append(path)
    return ordered


def stale_count(root, graph):
    """Nodes whose declared `file` is not on disk, at any depth.

    Counted over the whole graph rather than over what is rendered: the rendered set is the
    top level, and a node one level down is exactly the one whose staleness would otherwise
    never reach this document.
    """
    return sum(
        1
        for path in graph.order
        if graph.nodes[path]["file"] and not (root / graph.nodes[path]["file"]).exists()
    )


def _render(root, graph, changes, detail, max_depth, skeleton_depth, keep, stale=0):
    """One attempt. Higher `detail` is more verbose (3 = everything)."""
    ordered = _ordered(graph)
    held_back = sum(1 for path in graph.order if depth_of(path) > skeleton_depth)
    dropped_by_depth = sum(
        1 for path in graph.order if max_depth < depth_of(path) <= skeleton_depth
    )
    within = [path for path in ordered if depth_of(path) <= max_depth]
    shown = within[:keep]
    cut_by_budget = dropped_by_depth + max(0, len(within) - len(shown))
    # Edges are drawn only between *visible* nodes. Without this, a top-level node that
    # fifty hidden children depend on renders a fifty-name line, every name of which is
    # unfindable in this document — and the reader has to go grep to resolve any of them.
    visible = set(shown)

    lines = [f"# {graph.project or root.name} — structure map", ""]
    lines.append(
        f"{len(shown)} entries · {len(graph.order)} in the design. This map is "
        f"**regenerated from `{graph_lib.STORE_PATH}` every turn** and is only an index — "
        "read it before exploring the repository, then open what you need."
    )
    if stale:
        # Not a view being trimmed: the graph is wrong on these, and it is the reader who has
        # to decide what to believe. Kept in the header for the same reason as the line
        # below — the last-resort cut trims from the tail.
        lines += [
            "",
            f"⚠️ {stale} node(s) declare a file that is not on disk. The graph is wrong on "
            "those; `show <path>` marks each one. Trust the working tree.",
        ]
    if graph.skipped:
        # A third way a node can be absent, and the only one that means the store itself is
        # damaged rather than the view of it trimmed. It is stated in the same words the
        # query tools use, and it sits in the header on purpose: the last-resort cut in
        # `render` trims from the tail, and a warning that disappears exactly when the map
        # gets lossiest would be worse than not having one.
        lines += [
            "",
            f"⚠️ {graph.skipped} node(s) were skipped: no path, or the wrong shape. The "
            "store is damaged on those entries and the count above does not include them.",
        ]
    if cut_by_budget:
        # Real loss, so it gets the warning voice.
        lines += ["", f"⚠️ Truncated to fit: {len(shown)} of {len(shown) + cut_by_budget} entries are shown."]
    if held_back:
        # Deliberate, so it must not read as loss.
        lines += [
            "",
            f"ℹ️ Top level only: {held_back} deeper entries are held back on purpose, not "
            "lost. Open one with `show <path>`; the whole graph is on disk in "
            f"`{graph_lib.STORE_PATH}`.",
        ]
    if detail < 3:
        lines += ["", 'Detail reduced to fit; "used by" is omitted (read the other side\'s "depends on").']

    for path in shown:
        node = graph.nodes[path]
        # The heading level *is* the containment level, so nesting is visible at a glance.
        hashes = "#" * min(6, max(2, depth_of(path) + 1))
        lines.append("")
        lines.append(f"{hashes} {node['title']} `{node['kind']}`")
        lines.append(f"`{path}`{graph_lib.file_note(root, node)}")
        if node["file"]:
            lines.append(node["file"])
        if detail >= 2 and node["summary"]:
            lines.append(node["summary"])
        out_edges = sorted(
            f"{graph.nodes[edge['to']]['title']} ({edge['kind']})"
            for edge in graph.edges
            if edge["from"] == path and edge["to"] in visible
        )
        if out_edges:
            lines.append(f"depends on: {', '.join(out_edges)}")
        if detail >= 3:
            in_edges = sorted(
                f"{graph.nodes[edge['from']]['title']} ({edge['kind']})"
                for edge in graph.edges
                if edge["to"] == path and edge["from"] in visible
            )
            if in_edges:
                lines.append(f"used by: {', '.join(in_edges)}")

    # The second section: how this graph recently got here.
    #
    # Current shape alone cannot tell the reader whether a node was added by the last piece
    # of work or has been there for months, and those two facts mean opposite things when
    # deciding whether to touch it.
    if changes:
        lines.append("")
        lines.append("## Recent changes")
        lines.append("")
        if detail == 0:
            # Structure matters more than history, but dropping it silently would not do.
            lines.append("Omitted to fit.")
        else:
            for change in changes[:RECENT_LIMIT]:
                stamp = change.get("commit") or change.get("at", "")
                suffix = f" ({stamp})" if stamp else ""
                lines.append(
                    f"- {change.get('op', '?')} {change.get('target_kind', '?')} "
                    f"`{change.get('target_ref', '?')}`{suffix}"
                )
    return "\n".join(lines) + "\n"


def render(root, graph, changes=(), budget=BUDGET, skeleton_depth=1):
    """The map, guaranteed to fit `budget`, or `None` when the graph is empty.

    Degrades in a fixed order rather than truncating straight away, because losing the
    summaries is much cheaper than losing whole nodes.
    """
    if not graph.order:
        return None
    # Never render deeper than the caller asked for, however deep the graph actually goes.
    deepest = min(max(depth_of(path) for path in graph.order), max(skeleton_depth, 1))
    changes = list(changes)
    # Once, not once per attempt: this text is paid for on every turn and so is the stat call.
    stale = stale_count(root, graph)

    for detail in (3, 2, 1, 0):
        max_depth = max(1, deepest - 1) if detail == 0 else deepest
        attempt = _render(root, graph, changes, detail, max_depth, skeleton_depth,
                          len(graph.order), stale)
        if len(attempt.encode("utf-8")) <= budget:
            return attempt

    # Still too big with everything turned off: drop nodes, halving until it fits.
    keep = len(graph.order)
    max_depth = max(1, deepest - 1)
    while keep > 1:
        keep //= 2
        attempt = _render(root, graph, changes, 0, max_depth, skeleton_depth, keep, stale)
        if len(attempt.encode("utf-8")) <= budget:
            return attempt

    # ⚠️ The fallback must also respect the budget. A single entry can exceed it on its own
    # — one hub node with a very long `depends on:` line is enough — and returning an
    # oversized string here would hand the caller something it believes was already
    # trimmed. So the last resort is a hard cut, and the cut is announced: silent
    # truncation lets the reader take half a dependency list for the whole of it.
    last = _render(root, graph, changes, 0, max_depth, skeleton_depth, 1, stale)
    if len(last.encode("utf-8")) <= budget:
        return last
    notice = "\n\n⚠️ Cut off here: even one entry does not fit the budget.\n"
    room = budget - len(notice.encode("utf-8"))
    cut = last
    while len(cut.encode("utf-8")) > room:
        cut = cut[:-1]
    return cut + notice


def for_repository(root, budget=BUDGET, skeleton_depth=1):
    """The map for a repository, or `None` when it has no graph yet.

    Never raises: this is called from a hook that runs on every turn, and a hook that fails
    loudly on a project which simply has no graph would be worse than no hook at all.
    """
    try:
        payload = graph_lib.read_store(Path(root))
    except graph_lib.GraphUnavailable:
        return None
    if not payload:
        return None
    try:
        graph = graph_lib.from_payload(payload)
    except graph_lib.GraphUnavailable:
        return None
    return render(Path(root), graph, payload.get("changes", []), budget, skeleton_depth)
