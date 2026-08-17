#!/usr/bin/env python3
"""The design graph: storage, queries, and how their output is shaped.

A design graph is a declared map of what a project is made of — modules, classes and
interfaces — plus which of them depend on which. It is not extracted from the source. It is
written down, and it grows where work happens.

# Why the output is budgeted

These answers are read by a model, not scrolled by a person. Every result is kept to a few
dozen lines, and when something is cut the text says how much was cut and how to narrow the
query.

Silent truncation is the worst failure available here. A half-listed dependency table looks
exactly like a complete one, so the reader concludes "nothing else uses this" and changes it
safely — which is how you break something while believing you checked.

The same reasoning applies to empty output. A query that matches nothing must still say
something useful, because a blank answer reads as a broken tool, and a broken tool sends the
reader back to grepping the whole repository. That is the cost this graph exists to remove.
"""

import json
import unicodedata
from pathlib import Path

STORE_PATH = Path(".codegraph") / "graph.json"
SCHEMA_VERSION = 1

# Output budgets. These measure how much a reader can take in at once, not how wide a
# terminal is. Raising them re-creates the problem they were chosen to avoid.
FIND_LIMIT = 20
LIST_LIMIT = 15
INDIRECT_LIMIT = 10
SUGGEST_LIMIT = 12
TITLE_COLUMNS = 40
SUMMARY_LIMIT = 400


class GraphUnavailable(Exception):
    """The graph could not be read. The message says what to do instead."""


# ------------------------------------------------------------------ display width


def width(text):
    """Width in display columns: East Asian wide characters occupy two.

    Aligning with `len()` misaligns any table whose titles are not pure ASCII, and titles
    are free text.
    """
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text, columns):
    """Pad to `columns` display columns.

    Column gaps are added by the caller. Padding a trailing space here would make the
    longest row one column wider than the rest, which reads as a crooked table.
    """
    return text + " " * max(0, columns - width(text))


def clip(text, columns):
    if width(text) <= columns:
        return text
    kept, used = [], 0
    for ch in text:
        step = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if used + step > columns - 1:
            break
        kept.append(ch)
        used += step
    return "".join(kept) + "…"


# ------------------------------------------------------------------ the graph


def _text(raw, key):
    value = raw.get(key)
    return value if isinstance(value, str) else ""


class Graph:
    """Nodes addressed by slug path, plus the dependency edges between them.

    Containment and dependency are kept apart on purpose. Containment is the slug path
    itself — `backend/store` is inside `backend` — so it is a tree, and it is what you
    navigate by. Dependency is the edge list: many to many, free to contain cycles, and it
    is what you query. Drawing both as one relation is how architecture diagrams turn into
    hairballs.
    """

    def __init__(self, project, nodes, order, edges, skipped=0):
        self.project = project
        self.nodes = nodes
        # The stored order is preserved so that equally-scored results come back in a
        # stable order between two identical calls.
        self.order = order
        self.edges = edges
        self.skipped = skipped
        self.depends, self.used = self._index()

    def _index(self):
        """Both directions are derived from the one edge list.

        Deriving them beats storing a `used_by` field per node. A writer filling in nodes
        one at a time can state what each depends on, but it cannot know what will later
        depend on it — so a stored `used_by` ends up empty, and `users` answers "nothing
        depends on this" for every node in the graph. That is worse than an error: it is
        confidently backwards, and "what does this affect" is the question the reader most
        needs right.
        """
        depends = {path: set() for path in self.order}
        used = {path: set() for path in self.order}
        for edge in self.edges:
            source, target = edge.get("from"), edge.get("to")
            if source in self.nodes and target in self.nodes:
                depends[source].add(target)
                used[target].add(source)
        return depends, used

    def depends_on(self, path):
        return sorted(self.depends.get(path, ()))

    def users_of(self, path):
        return sorted(self.used.get(path, ()))

    def children_of(self, path):
        """Direct children only — one slug segment deeper.

        Grandchildren are left for the next `show`. Otherwise a top-level node dumps its
        entire subtree, which is the shape these budgets exist to prevent.
        """
        prefix = f"{path}/"
        return [
            candidate
            for candidate in self.order
            if candidate.startswith(prefix) and "/" not in candidate[len(prefix) :]
        ]

    def landmarks(self):
        """Signposts for a query that matched nothing: the top-level nodes.

        If every path is nested, the shallowest ones are used instead. Returning nothing
        would push the reader straight back to grepping.
        """
        tops = [path for path in self.order if "/" not in path]
        if tops:
            return tops
        if not self.order:
            return []
        shallowest = min(path.count("/") for path in self.order)
        return [path for path in self.order if path.count("/") == shallowest]


def from_payload(payload):
    """Normalise a stored graph into a `Graph`, counting whatever had to be skipped."""
    if not isinstance(payload, dict) or not isinstance(payload.get("nodes"), list):
        raise GraphUnavailable('the graph has the wrong shape: expected {"nodes": [...]}')

    nodes, order, skipped = {}, [], 0
    for entry in payload["nodes"]:
        if not isinstance(entry, dict):
            skipped += 1
            continue
        path = _text(entry, "path").strip()
        if not path:
            skipped += 1
            continue
        raw_file = entry.get("file")
        if path not in nodes:
            order.append(path)
        nodes[path] = {
            "path": path,
            "kind": _text(entry, "kind") or "?",
            "title": _text(entry, "title"),
            "summary": _text(entry, "summary"),
            # A grouping node may legitimately have no file of its own.
            "file": raw_file if isinstance(raw_file, str) and raw_file else None,
        }
    edges = [
        edge
        for edge in payload.get("edges", [])
        if isinstance(edge, dict) and edge.get("from") and edge.get("to")
    ]
    return Graph(_text(payload, "project"), nodes, order, edges, skipped)


# ------------------------------------------------------------------ store


def read_store(root):
    """Read the stored graph, or `None` when this project has none yet."""
    path = root / STORE_PATH
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise GraphUnavailable(
            f"{STORE_PATH} is not valid JSON (line {error.lineno}, column {error.colno}: "
            f"{error.msg}). It was most likely interrupted mid-write; do not draw "
            "conclusions from it."
        ) from error
    except OSError as error:
        raise GraphUnavailable(f"{STORE_PATH} could not be read: {error.strerror or error}") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("nodes"), list):
        raise GraphUnavailable(f'{STORE_PATH} has the wrong shape: expected {{"nodes": [...]}}')
    payload.setdefault("edges", [])
    payload.setdefault("project", root.name)
    return payload


def write_store(root, payload):
    """Write the graph, temp file then rename.

    The rename is what stops a concurrent reader from ever seeing a half-written graph.
    """
    path = root / STORE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(path)
    return path


def load(root):
    payload = read_store(root)
    if payload is None:
        raise GraphUnavailable(
            f"this project has no design graph yet ({root}).\n"
            "Call `bootstrap` to draw one — it explains what to read and what to write, and "
            "takes one pass over the repository. Until then, read the code directly."
        )
    return from_payload(payload)


# ------------------------------------------------------------------ rendering


def file_note(root, node):
    """Mark a node whose declared file is not on disk.

    Costs nothing and catches the most common way this map goes wrong: it points
    confidently at a file that was deleted or renamed. Without the mark, a reader follows
    the path, finds nothing, and starts doubting its own spelling — when what deserves
    doubt is the graph.
    """
    relative = node.get("file")
    if not relative:
        return ""
    return "" if (root / relative).exists() else "  ⚠ file missing"


def render_rows(root, graph, paths, limit, indent="  ", show_file=False, more_hint=""):
    """A node list that always states what it left out."""
    shown = paths[:limit]
    if not shown:
        return []
    present = [item for item in shown if item in graph.nodes]
    path_columns = max(width(item) for item in shown)
    kind_columns = max((width(graph.nodes[item]["kind"]) for item in present), default=0)
    # Only pad titles when a file column follows; otherwise every row trails spaces.
    title_columns = (
        max(
            (width(clip(graph.nodes[item]["title"], TITLE_COLUMNS)) for item in present),
            default=0,
        )
        if show_file
        else 0
    )

    lines = []
    for item in shown:
        node = graph.nodes.get(item)
        if node is None:
            # An edge naming a node that is not in the graph. Say so rather than dropping
            # the row: a missing endpoint is exactly the kind of staleness worth seeing.
            lines.append(f"{indent}{item}  ⚠ not in the graph")
            continue
        columns = [pad(item, path_columns), pad(node["kind"], kind_columns)]
        title = clip(node["title"], TITLE_COLUMNS)
        columns.append(pad(title, title_columns) if show_file else title)
        if show_file and node["file"]:
            columns.append(node["file"])
        lines.append(indent + "  ".join(part for part in columns if part).rstrip())
        lines[-1] += file_note(root, node)

    if len(paths) > limit:
        remaining = len(paths) - limit
        tail = f"{indent}… {remaining} more not shown"
        if more_hint:
            tail += f". {more_hint}"
        lines.append(tail)
    return lines


def unknown_path(graph, path, command):
    """What to say when a path does not resolve.

    Deliberately not an error. The reader made an addressing mistake, and the closest
    matches are what lets them fix it in one step. Naming the difference between a slug
    path and a file path is worth the line: it is the mistake people actually make.
    """
    import difflib

    close = difflib.get_close_matches(path, graph.order, n=5, cutoff=0.4)
    if not close:
        close = graph.landmarks()[:SUGGEST_LIMIT]
        header = "top-level nodes"
    else:
        header = "closest matches"
    lines = [f'{command}: no node with path "{path}" in this graph.']
    if close:
        lines.append(f"{header}: {', '.join(close)}")
    lines.append(
        "A path is a slash-separated slug path (like server/store), **not a file path**. "
        "Use find when unsure."
    )
    return "\n".join(lines)


# ------------------------------------------------------------------ queries


def score_node(node, terms):
    """Rank a node against ANDed keywords. Every term must hit something."""
    total = 0
    segments = node["path"].split("/")
    for term in terms:
        hit = 0
        if any(term == segment for segment in segments):
            hit = 5
        elif term in node["path"].lower():
            hit = 4
        elif term in node["title"].lower():
            hit = 3
        elif node["file"] and term in node["file"].lower():
            hit = 2
        elif term in node["summary"].lower():
            hit = 1
        if not hit:
            return 0
        total += hit
    return total


def find(root, graph, keywords):
    terms = [word.lower() for word in keywords if word.strip()]
    if not terms:
        return "find needs at least one keyword, for example: find store"
    quoted = " ".join(keywords)

    ranked = []
    for index, path in enumerate(graph.order):
        score = score_node(graph.nodes[path], terms)
        if score:
            # The index keeps equal scores in stored order, so output is reproducible.
            ranked.append((-score, index, path))
    ranked.sort()
    matches = [path for _, _, path in ranked]

    if not matches:
        lines = [f"find {quoted} → no matches. The graph has {len(graph.order)} node(s)."]
        landmarks = graph.landmarks()[:SUGGEST_LIMIT]
        if landmarks:
            lines.append(f"Top-level nodes: {', '.join(landmarks)}")
            lines.append("Try another word, or show one of those to see what is inside it.")
        else:
            lines.append("The graph is empty — read the code directly, or call bootstrap.")
        return "\n".join(lines)

    header = f"find {quoted} → {len(matches)} match(es)"
    if len(matches) > FIND_LIMIT:
        header = f"find {quoted} → showing the first {FIND_LIMIT} of {len(matches)}"
    lines = [header]
    lines += render_rows(
        root,
        graph,
        matches,
        FIND_LIMIT,
        show_file=True,
        more_hint="Add a more specific word (find store config), or show <path> for one of them.",
    )
    return "\n".join(lines)


def show(root, graph, path):
    node = graph.nodes.get(path)
    if node is None:
        return unknown_path(graph, path, "show")

    lines = [f"{node['path']}  {node['kind']}{file_note(root, node)}"]
    if node["title"]:
        lines.append(f"title    {node['title']}")
    lines.append(f"file     {node['file'] or '—'}")
    lines.append(f"summary  {clip(node['summary'], SUMMARY_LIMIT) if node['summary'] else '—'}")

    for label, paths, hint in (
        ("children", graph.children_of(path), f"show {path}/<child slug> to go deeper"),
        ("depends_on", graph.depends_on(path), "show one of them to see what it depends on"),
        ("used_by", graph.users_of(path), f"users {path} for the full blast radius"),
    ):
        lines.append("")
        if not paths:
            lines.append(f"{label} (0)")
            continue
        lines.append(f"{label} ({len(paths)})")
        lines += render_rows(root, graph, paths, LIST_LIMIT, more_hint=hint)
    return "\n".join(lines)


def users(root, graph, path):
    if path not in graph.nodes:
        return unknown_path(graph, path, "users")

    direct = graph.users_of(path)
    if not direct:
        return (
            f"users {path} → nothing depends on it.\n"
            "Changing it affects only itself — provided the graph is current. Before relying "
            "on that alone, check that this node's file still exists."
        )

    # Indirect reach, walked breadth-first over reverse edges. "What does changing this
    # affect" is not a one-hop question, and reporting only direct dependents systematically
    # understates the answer — which is the exact mistake this query exists to prevent.
    seen = set(direct) | {path}
    frontier = list(direct)
    indirect = []
    while frontier:
        current = frontier.pop(0)
        for user in graph.users_of(current):
            if user in seen:
                continue
            seen.add(user)
            indirect.append(user)
            frontier.append(user)

    lines = [f"users {path} → {len(direct)} direct dependent(s)"]
    lines += render_rows(
        root, graph, direct, LIST_LIMIT, more_hint=f"show {path} to see what it depends on"
    )
    if indirect:
        lines.append("")
        shown = indirect[:INDIRECT_LIMIT]
        suffix = f" (first {len(shown)} shown)" if len(indirect) > len(shown) else ""
        lines.append(f"Indirectly affected: {len(indirect)}{suffix}: {', '.join(shown)}")
    return "\n".join(lines)
