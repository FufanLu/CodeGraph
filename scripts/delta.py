#!/usr/bin/env python3
"""Validating and applying a change to the design graph.

A delta states what one piece of work did to the design: what it added, removed or rewired.
It is the only way the graph changes — there is no redraw.

# Why the whole delta is rejected when one rule fails

A delta expresses a single intent: added `UserService`, and added the edge that gives it
meaning. Applying the valid half leaves the node without the edge — a state nobody declared
and nobody can interpret. Rejecting the whole thing and asking for a rewrite is the only
option that keeps the graph a record of things somebody actually said.

# Why the rules are this strict about paths and file locations

Both values are compared verbatim and one of them is joined onto a directory and used to
touch the filesystem. Normalising instead of rejecting would give one location two spellings
and one node two identities. The details are on each function.
"""

SCHEMA_VERSION = 1

NODE_KINDS = ("MODULE", "CLASS", "INTERFACE")
EDGE_KINDS = ("EXTENDS", "USES")

# An unknown field rejects the delta for the same reason an unknown schema version does: the
# part that cannot be read is quite likely the part that mattered.
NODE_FIELDS = {"path", "kind", "title", "summary", "file"}
NODE_REQUIRED = {"path", "kind", "title"}
EDGE_FIELDS = {"from", "to", "kind"}


class DeltaRejected(Exception):
    """Whole-delta rejection. `str(error)` is the one sentence the writer gets to read."""


def valid_path(path):
    """Every segment of a slug path must be `[a-z0-9-]+`.

    Not fussiness. The path is the addressing key and it is compared verbatim, so allowing
    case would make `Backend` and `backend` two different nodes that look identical.
    """
    if not isinstance(path, str) or not path:
        return False
    return all(
        segment
        and all((c.islower() and c.isascii()) or c.isdigit() or c == "-" for c in segment)
        for segment in path.split("/")
    )


def parent_of(path):
    return path.rsplit("/", 1)[0] if "/" in path else None


def valid_repo_location(location):
    """`file` must be a location inside the repository: relative, forward slashes, no `.` or
    `..` segment. A directory is written with a trailing slash.

    Absolute paths and `..` have to be stopped here, because this value is joined onto the
    repository root and used to touch the filesystem. Python's `Path` *replaces* the whole
    path when the right operand is absolute — `Path("/repo") / "/etc/passwd"` is
    `/etc/passwd` — and `..` walks straight out of the project. This is the gate on the one
    path that reaches the disk, not a formatting preference.

    The trailing slash is meaningful rather than decorative: it is the only thing separating
    a directory from a file of the same name, and it is what makes prefix matching correct.
    Without it, `src/backendish/x.py` matches `src/backend`.

    Rejected rather than normalised: `./src` and `src` would otherwise be two names for one
    location, and this value gets compared verbatim.

    There is deliberately no separate "must not start with a slash" rule. Absolute paths are
    already rejected by the empty-segment check below, since `/etc/passwd` splits with an
    empty first segment. A second rule that never fires reads like a gate and is not one.
    """
    if not isinstance(location, str):
        return False
    if not location or location.strip() != location or "\\" in location:
        return False
    # Strip only the trailing slash; every remaining segment has to be a real one.
    body = location[:-1] if location.endswith("/") else location
    if not body:
        return False
    return all(segment.strip() and segment not in (".", "..") for segment in body.split("/"))


def _typed_list(container, key, where):
    value = container.get(key, [])
    if not isinstance(value, list):
        raise DeltaRejected(f"{where}.{key} must be a list")
    return value


def _check_fields(item, allowed, required, where):
    if not isinstance(item, dict):
        raise DeltaRejected(f"{where} must be an object")
    extra = set(item) - allowed
    if extra:
        raise DeltaRejected(
            f"{where} has unknown field(s) {sorted(extra)}; allowed are {sorted(allowed)}"
        )
    missing = required - set(item)
    if missing:
        raise DeltaRejected(f"{where} is missing {sorted(missing)}")


def validate(current, marker, require_file=False):
    """Validate one delta. Any failing rule rejects all of it (raises `DeltaRejected`).

    `current` is the set of slug paths already in the graph. Returns a plan ready to apply.

    `require_file` applies while the graph is still empty. The first delta is drawing the
    map, and a node with no location is a claim with nothing behind it: nothing can check it
    against the repository, and no file can be traced back to the design it belongs to.
    Later deltas do not carry that burden — they are describing a change, not a survey.
    """
    if not isinstance(marker, dict):
        raise DeltaRejected("a delta must be a JSON object")
    unknown = set(marker) - {"schema_version", "adds", "removes"}
    if unknown:
        raise DeltaRejected(f"the delta has unknown field(s) {sorted(unknown)}")

    version = marker.get("schema_version")
    if version != SCHEMA_VERSION:
        raise DeltaRejected(
            f"schema_version {version!r} is not supported (this tool speaks {SCHEMA_VERSION})"
        )

    adds = marker.get("adds") or {}
    removes = marker.get("removes") or {}
    for name, section in (("adds", adds), ("removes", removes)):
        if not isinstance(section, dict):
            raise DeltaRejected(f"{name} must be an object")
        extra = set(section) - {"nodes", "edges"}
        if extra:
            raise DeltaRejected(f"{name} has unknown field(s) {sorted(extra)}")

    add_nodes = _typed_list(adds, "nodes", "adds")
    add_edges = _typed_list(adds, "edges", "adds")
    remove_nodes = _typed_list(removes, "nodes", "removes")
    remove_edges = _typed_list(removes, "edges", "removes")

    # A removal target must exist. Removing something that is not there is *not* a harmless
    # no-op: it means the writer's picture of the graph disagrees with the stored one, and
    # the edges it goes on to add are probably built on the same misunderstanding.
    for path in remove_nodes:
        if not isinstance(path, str):
            raise DeltaRejected("removes.nodes must contain slug paths")
        if path not in current:
            raise DeltaRejected(f"cannot remove `{path}`: no such node in the graph")

    # Additions are checked against a set that grows as we go, so declaring a parent and
    # then its child inside one delta is legal.
    live = set(current) - set(remove_nodes)
    for index, spec in enumerate(add_nodes):
        _check_fields(spec, NODE_FIELDS, NODE_REQUIRED, f"adds.nodes[{index}]")
        path = spec["path"]
        if not valid_path(path):
            raise DeltaRejected(
                f"`{path}` is not a valid node path "
                "(lowercase letters, digits and dashes, slash-separated)"
            )
        if spec["kind"] not in NODE_KINDS:
            raise DeltaRejected(
                f"`{path}` has kind `{spec['kind']}`; expected one of {list(NODE_KINDS)}"
            )
        if not isinstance(spec["title"], str) or not spec["title"].strip():
            raise DeltaRejected(f"`{path}` has no title")
        file = spec.get("file")
        if file is None and require_file:
            raise DeltaRejected(
                f"`{path}` has no `file`: the first delta draws the map, so every node must "
                "say where it lives (a directory with a trailing slash, or the one file)"
            )
        if file is not None and not valid_repo_location(file):
            raise DeltaRejected(
                f"`{path}` says it lives at `{file}`, which is not a location inside the "
                "repository (relative, forward slashes, no `.` or `..` segment; write a "
                "directory with a trailing slash)"
            )
        if path in live:
            raise DeltaRejected(f"`{path}` already exists in the graph")
        parent = parent_of(path)
        if parent is not None and parent not in live:
            raise DeltaRejected(
                f"`{path}` has no parent: `{parent}` is neither in the graph nor added "
                "earlier in this delta"
            )
        live.add(path)

    # Edges: both endpoints must still be present once additions and removals are applied.
    for label, edges in (("add", add_edges), ("remove", remove_edges)):
        for index, edge in enumerate(edges):
            _check_fields(edge, EDGE_FIELDS, EDGE_FIELDS, f"{label}s.edges[{index}]")
            if edge["kind"] not in EDGE_KINDS:
                raise DeltaRejected(
                    f"edge `{edge['from']}` -> `{edge['to']}` has kind `{edge['kind']}`; "
                    f"expected one of {list(EDGE_KINDS)}"
                )
            if edge["from"] == edge["to"]:
                raise DeltaRejected(f"`{edge['from']}` cannot depend on itself")
            for endpoint in (edge["from"], edge["to"]):
                if endpoint not in live:
                    raise DeltaRejected(
                        f"cannot {label} the edge `{edge['from']}` -> `{edge['to']}`: "
                        f"`{endpoint}` is not in the graph"
                    )

    return {
        # Marker order is preserved: a parent must be inserted before its children.
        "add_nodes": add_nodes,
        "add_edges": add_edges,
        "remove_nodes": remove_nodes,
        "remove_edges": remove_edges,
    }


def apply(store, plan):
    """Apply a validated plan, returning a new graph (the input is left alone).

    **Removals happen before additions, and that is load-bearing**: it is what lets one
    delta replace `server/legacy` with `server/store`. In the other order the new node
    collides with the old one before it has gone.
    """
    nodes = {node["path"]: dict(node) for node in store.get("nodes", [])}
    order = [node["path"] for node in store.get("nodes", [])]
    edges = [dict(edge) for edge in store.get("edges", [])]

    def same(edge, spec):
        return (
            edge["from"] == spec["from"]
            and edge["to"] == spec["to"]
            and edge["kind"] == spec["kind"]
        )

    for spec in plan["remove_edges"]:
        edges = [edge for edge in edges if not same(edge, spec)]
    for path in plan["remove_nodes"]:
        nodes.pop(path, None)
        order = [item for item in order if item != path]
        # The node is gone, so edges touching it cannot stay: an edge with one end missing
        # is not a fact about anything.
        edges = [edge for edge in edges if path not in (edge["from"], edge["to"])]
    for spec in plan["add_nodes"]:
        if spec["path"] not in nodes:
            order.append(spec["path"])
        nodes[spec["path"]] = {
            "path": spec["path"],
            "kind": spec["kind"],
            "title": spec["title"],
            "summary": spec.get("summary", ""),
            "file": spec.get("file"),
        }
    for spec in plan["add_edges"]:
        if not any(same(edge, spec) for edge in edges):
            edges.append({"from": spec["from"], "to": spec["to"], "kind": spec["kind"]})

    return {
        "schema_version": SCHEMA_VERSION,
        "project": store.get("project", ""),
        "nodes": [nodes[path] for path in order],
        "edges": edges,
    }
