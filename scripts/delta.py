#!/usr/bin/env python3
"""Validating and applying a change to the design graph.

A delta states what one piece of work did to the design: what it added, removed, rewired or
reworded. It is the only way the graph changes — there is no redraw.

# Why `updates` exists as well as `adds` and `removes`

Correcting a summary is the most ordinary maintenance there is, and without an update it had
to be spelled remove-then-add. That takes the node's edges down with it — an edge with one
end missing is not a fact about anything — so fixing a sentence quietly cost every dependency
that node had. The graph lost precisely what it is kept for, as the price of a typo.

`path` addresses the node and cannot be changed by an update: a different path is a different
node, and that really is a removal and an addition.

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

# `path` addresses the node being updated; the rest are what an update may change.
UPDATE_FIELDS = {"path", "kind", "title", "summary", "file"}
UPDATE_REQUIRED = {"path"}


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
    unknown = set(marker) - {"schema_version", "adds", "removes", "updates"}
    if unknown:
        raise DeltaRejected(f"the delta has unknown field(s) {sorted(unknown)}")

    version = marker.get("schema_version")
    if version != SCHEMA_VERSION:
        raise DeltaRejected(
            f"schema_version {version!r} is not supported (this tool speaks {SCHEMA_VERSION})"
        )

    adds = marker.get("adds") or {}
    removes = marker.get("removes") or {}
    updates = marker.get("updates") or {}
    for name, section in (("adds", adds), ("removes", removes), ("updates", updates)):
        if not isinstance(section, dict):
            raise DeltaRejected(f"{name} must be an object")
        # No `updates.edges`: an edge is three fields and all three are its identity, so
        # changing any of them is removing one edge and adding another.
        allowed = {"nodes"} if name == "updates" else {"nodes", "edges"}
        extra = set(section) - allowed
        if extra:
            raise DeltaRejected(f"{name} has unknown field(s) {sorted(extra)}")

    update_nodes = _typed_list(updates, "nodes", "updates")
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

    # Updates are checked last, against the same `live` set, so an update naming a node this
    # delta removes is rejected rather than silently doing nothing.
    for index, spec in enumerate(update_nodes):
        _check_fields(spec, UPDATE_FIELDS, UPDATE_REQUIRED, f"updates.nodes[{index}]")
        path = spec["path"]
        if not valid_path(path):
            raise DeltaRejected(
                f"`{path}` is not a valid node path "
                "(lowercase letters, digits and dashes, slash-separated)"
            )
        if path not in live:
            raise DeltaRejected(f"cannot update `{path}`: no such node in the graph")
        if set(spec) == UPDATE_REQUIRED:
            raise DeltaRejected(
                f"`{path}` has nothing to update: name at least one of kind, title, "
                "summary or file"
            )
        if "kind" in spec and spec["kind"] not in NODE_KINDS:
            raise DeltaRejected(
                f"`{path}` has kind `{spec['kind']}`; expected one of {list(NODE_KINDS)}"
            )
        if "title" in spec and (
            not isinstance(spec["title"], str) or not spec["title"].strip()
        ):
            raise DeltaRejected(f"`{path}` has no title")
        if "summary" in spec and not isinstance(spec["summary"], str):
            raise DeltaRejected(f"`{path}` has a summary that is not text")
        # `None` is allowed and means "this node has no location of its own", which a
        # grouping node legitimately does not.
        if spec.get("file") is not None and not valid_repo_location(spec["file"]):
            raise DeltaRejected(
                f"`{path}` says it lives at `{spec['file']}`, which is not a location "
                "inside the repository (relative, forward slashes, no `.` or `..` segment; "
                "write a directory with a trailing slash)"
            )

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
        "update_nodes": update_nodes,
        "remove_nodes": remove_nodes,
        "remove_edges": remove_edges,
    }


def cascade_edges(store, plan):
    """Edges that will disappear because a node they touch is being removed.

    Dropping them is right — an edge with one end missing is not a fact about anything — but
    it happens to edges nobody named, so somebody has to count them. Without this the
    receipt reports "0 edge(s) removed" while the graph quietly loses every dependency the
    removed node had, and "Recent changes" tells the same untruth one layer down.

    Pure, and safe to call before `apply`: it reads the store as it stands.

    These can never overlap with `remove_edges`. Naming an edge for removal requires both
    of its endpoints to survive the delta, so an edge whose node is going cannot also have
    been named — which is why nothing here has to be deduplicated against it.
    """
    gone = set(plan["remove_nodes"])
    if not gone:
        return []
    return [
        dict(edge)
        for edge in store.get("edges", [])
        if edge.get("from") in gone or edge.get("to") in gone
    ]


def describe(plan, cascaded=()):
    """Flatten a plan into change rows, in the order they will be applied.

    These are what the structure map's "Recent changes" section reads. Current shape alone
    cannot tell a reader whether a node arrived with the last piece of work or has been
    there for months, and those two facts mean opposite things when deciding whether to
    touch it.
    """
    rows = []
    for spec in list(plan["remove_edges"]) + list(cascaded):
        rows.append(("remove", "edge", f"{spec['from']}>{spec['kind']}>{spec['to']}"))
    for path in plan["remove_nodes"]:
        rows.append(("remove", "node", path))
    for spec in plan["add_nodes"]:
        rows.append(("add", "node", spec["path"]))
    for spec in plan.get("update_nodes", ()):
        rows.append(("update", "node", spec["path"]))
    for spec in plan["add_edges"]:
        rows.append(("add", "edge", f"{spec['from']}>{spec['kind']}>{spec['to']}"))
    # Addressed by slug path, never by an internal id: the path is what the writer used to
    # declare the change and what a reader can look up afterwards.
    return [{"op": op, "target_kind": kind, "target_ref": ref} for op, kind, ref in rows]


def apply(store, plan, stamp=None, history_limit=50):
    """Apply a validated plan, returning a new graph (the input is left alone).

    **Removals happen before additions, and that is load-bearing**: it is what lets one
    delta replace `server/legacy` with `server/store`. In the other order the new node
    collides with the old one before it has gone.

    `stamp` is merged into every recorded change — a timestamp and, where there is one, the
    commit the work sat on.
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
    # Updates land after additions and before edges: by here every node this delta will
    # ever touch exists, and nothing an update does can invalidate an edge, because it
    # cannot change a path.
    for spec in plan.get("update_nodes", ()):
        node = nodes.get(spec["path"])
        if node is None:
            continue
        for field in ("kind", "title", "summary", "file"):
            if field in spec:
                node[field] = spec[field]
    for spec in plan["add_edges"]:
        if not any(same(edge, spec) for edge in edges):
            edges.append({"from": spec["from"], "to": spec["to"], "kind": spec["kind"]})

    # Newest first, and capped: this is a "what happened lately" list, not an audit log.
    # An unbounded one would grow without ever being read past its first few entries.
    recorded = [{**row, **(stamp or {})} for row in describe(plan, cascade_edges(store, plan))]
    history = recorded + list(store.get("changes", []))

    return {
        "schema_version": SCHEMA_VERSION,
        "project": store.get("project", ""),
        "nodes": [nodes[path] for path in order],
        "edges": edges,
        "changes": history[:history_limit],
    }
