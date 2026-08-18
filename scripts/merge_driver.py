#!/usr/bin/env python3
"""A git merge driver for `.codegraph/graph.json`.

Two branches that each declare something both rewrite this one file, and git's line-based
merge resolves that badly: the common case — a node added on either side — collides in the
`nodes` array and leaves conflict markers in a JSON document. Measured on a three-node
graph, two branches each adding one module conflict every time.

The store does not need line-based merging, because its three parts are all keyed:

- **nodes** are addressed by `path`, which is unique and never rewritten in place
- **edges** are a set of `(from, to, kind)`
- **changes** is a capped "what happened lately" list, by its own definition not an audit log

So this driver merges them as data. Everything git cannot decide is *named on stderr* rather
than papered over, and the exit code is what tells git the human still has to look.

# What counts as a conflict here

Only one thing: both sides changed the *same* node to two different shapes. Everything else
has a determinate answer. In particular a removal wins over an untouched side — that is why
this is a three-way merge and not a union. A union would resurrect every node either branch
deliberately deleted, silently, which is the exact failure this codebase exists to refuse.

# Why a conflict still leaves valid JSON

Git's convention is to write conflict markers into the result. That would leave the project
holding a `graph.json` that parses as nothing, and every tool would then correctly refuse to
answer until somebody hand-stitched a JSON file. So on a conflict this driver writes the
merge it *could* determine, keeps our side of whatever it could not, exits non-zero so git
marks the path unresolved, and prints the disagreeing paths. The graph stays readable the
whole way through, and re-declaring the one node it named is cheaper than editing JSON.

Install (see README):

    git config merge.codegraph.name "design graph merge"
    git config merge.codegraph.driver "python3 <path>/merge_driver.py %O %A %B %P"
    echo '.codegraph/graph.json merge=codegraph' >> .gitattributes
"""

import json
import sys

HISTORY_LIMIT = 50


def load(path, label):
    """Read one side of the merge, or `None` when it cannot be read as a graph."""
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        print(f"code-graph merge: {label} is not readable as JSON ({error})", file=sys.stderr)
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("nodes"), list):
        print(f'code-graph merge: {label} is not a graph (expected {{"nodes": [...]}})',
              file=sys.stderr)
        return None
    return payload


def by_path(payload):
    return {
        node["path"]: node
        for node in payload.get("nodes", [])
        if isinstance(node, dict) and isinstance(node.get("path"), str)
    }


def edge_keys(payload):
    return {
        (edge.get("from"), edge.get("to"), edge.get("kind"))
        for edge in payload.get("edges", [])
        if isinstance(edge, dict)
    }


def merge_keyed(base, ours, theirs):
    """Three-way merge of one keyed collection.

    Returns `(chosen, conflicts)`. A key present in base and gone from one side is gone from
    the result: a removal is a declaration, not an omission.
    """
    keys = (
        (set(base) - (set(base) - set(ours)) - (set(base) - set(theirs)))
        | (set(ours) - set(base))
        | (set(theirs) - set(base))
    )
    chosen, conflicts = {}, []
    for key in keys:
        mine, yours = ours.get(key), theirs.get(key)
        if mine is not None and yours is not None:
            if mine == yours or yours == base.get(key):
                chosen[key] = mine
            elif mine == base.get(key):
                chosen[key] = yours
            else:
                # Both sides rewrote it, differently. Nobody but a human knows which
                # description is now true.
                chosen[key] = mine
                conflicts.append(key)
        else:
            chosen[key] = mine if mine is not None else yours
    return chosen, conflicts


def ordered(nodes, ours, theirs, base):
    """Node order for the result: parents before their children.

    `apply` relies on that ordering when it inserts, and a child listed before its parent
    reads as an orphan. Depth is a stable key that guarantees it; within one depth the
    sides are laid down in a fixed sequence so the same merge always produces the same file.
    """
    sequence = list(base) + list(ours) + list(theirs)
    seen, sortable = set(), []
    for path in sequence:
        if path in nodes and path not in seen:
            seen.add(path)
            sortable.append(path)
    sortable.sort(key=lambda path: path.count("/"))
    return [nodes[path] for path in sortable]


def merge_changes(ours, theirs):
    """Union of the two histories, newest side first, deduplicated and capped.

    Ordering inside a day is approximate because a change records only a date and the commit
    it sat on. That is acceptable precisely here: this list answers "was this recent", and
    the module that renders it says so.
    """
    merged, seen = [], set()
    for entry in list(ours.get("changes", [])) + list(theirs.get("changes", [])):
        token = json.dumps(entry, sort_keys=True)
        if token not in seen:
            seen.add(token)
            merged.append(entry)
    return merged[:HISTORY_LIMIT]


def merge(base_path, ours_path, theirs_path, name="graph.json"):
    """Merge the three files, overwriting `ours_path`. Returns a process exit code."""
    ours = load(ours_path, "our side")
    theirs = load(theirs_path, "their side")
    if ours is None or theirs is None:
        print(f"code-graph merge: leaving {name} for you to resolve by hand", file=sys.stderr)
        return 1
    # A missing or unreadable ancestor is normal (an add/add merge has none). Treating it as
    # empty degrades this to a union for that one merge, which is the best available answer.
    base = load(base_path, "the common ancestor") or {"nodes": [], "edges": []}

    if ours.get("schema_version") != theirs.get("schema_version"):
        print("code-graph merge: the two sides use different schema versions; "
              f"leaving {name} for you to resolve by hand", file=sys.stderr)
        return 1

    nodes, conflicts = merge_keyed(by_path(base), by_path(ours), by_path(theirs))
    edges = {key: {"from": key[0], "to": key[1], "kind": key[2]} for key in
             merge_keyed({k: k for k in edge_keys(base)},
                         {k: k for k in edge_keys(ours)},
                         {k: k for k in edge_keys(theirs)})[0]}
    # An edge cannot survive a node it points at.
    edges = {key: edge for key, edge in edges.items() if key[0] in nodes and key[1] in nodes}

    result = {
        "schema_version": ours.get("schema_version"),
        "project": ours.get("project", theirs.get("project", "")),
        "nodes": ordered(nodes, by_path(ours), by_path(theirs), by_path(base)),
        "edges": [edges[key] for key in sorted(edges)],
        "changes": merge_changes(ours, theirs),
    }
    with open(ours_path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(result, indent=2, ensure_ascii=False) + "\n")

    if conflicts:
        print(f"code-graph merge: {len(conflicts)} node(s) were rewritten differently on "
              "both sides. Our description was kept and the graph is still valid JSON; "
              "re-declare these with `apply_delta` if ours is not the true one:",
              file=sys.stderr)
        for path in sorted(conflicts):
            print(f"  {path}", file=sys.stderr)
        return 1
    print(f"code-graph merge: {name} merged as data "
          f"({len(result['nodes'])} nodes, {len(result['edges'])} edges)", file=sys.stderr)
    return 0


def main(argv):
    if not 3 <= len(argv) <= 5:
        print("usage: merge_driver.py %O %A %B [%P]", file=sys.stderr)
        return 2
    return merge(argv[0], argv[1], argv[2], argv[3] if len(argv) > 3 else "graph.json")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
