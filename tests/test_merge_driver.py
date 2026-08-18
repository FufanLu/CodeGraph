#!/usr/bin/env python3
"""The git merge driver for the store.

The case that matters most is the one a naive union gets wrong: a node deleted on one side
and merely untouched on the other. A union resurrects it, silently, and the graph then
describes code that was deliberately removed.
"""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))

import merge_driver  # noqa: E402


def node(path, title="Title", summary=""):
    return {"path": path, "kind": "MODULE", "title": title, "summary": summary, "file": "src/"}


def graph(nodes=(), edges=(), changes=(), version=1):
    return {
        "schema_version": version,
        "project": "demo",
        "nodes": list(nodes),
        "edges": [{"from": f, "to": t, "kind": "USES"} for f, t in edges],
        "changes": list(changes),
    }


class Driver(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)

    def run_merge(self, base, ours, theirs):
        paths = []
        for name, payload in (("base", base), ("ours", ours), ("theirs", theirs)):
            path = self.dir / name
            if payload is not None:
                path.write_text(json.dumps(payload), encoding="utf-8")
            paths.append(str(path))
        with contextlib.redirect_stderr(io.StringIO()):
            code = merge_driver.merge(*paths, name="graph.json")
        merged = None
        if (self.dir / "ours").exists():
            try:
                merged = json.loads((self.dir / "ours").read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                merged = "not json"
        return code, merged

    def paths_of(self, merged):
        return [n["path"] for n in merged["nodes"]]

    # ---------------------------------------------------------------- the common case

    def test_two_sides_each_adding_a_node_merge_cleanly(self):
        code, merged = self.run_merge(
            graph([node("base")]),
            graph([node("base"), node("alpha")]),
            graph([node("base"), node("beta")]),
        )
        self.assertEqual(code, 0)
        self.assertEqual(sorted(self.paths_of(merged)), ["alpha", "base", "beta"])

    def test_edges_from_both_sides_survive(self):
        code, merged = self.run_merge(
            graph([node("base")]),
            graph([node("base"), node("alpha")], [("alpha", "base")]),
            graph([node("base"), node("beta")], [("beta", "base")]),
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            sorted((e["from"], e["to"]) for e in merged["edges"]),
            [("alpha", "base"), ("beta", "base")],
        )

    # ---------------------------------------------------------------- removals

    def test_a_removal_beats_an_untouched_side(self):
        # The union bug. `gone` was deleted on our side and simply not mentioned on theirs.
        code, merged = self.run_merge(
            graph([node("keep"), node("gone")]),
            graph([node("keep")]),
            graph([node("keep"), node("gone")]),
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.paths_of(merged), ["keep"])

    def test_a_removed_edge_stays_removed(self):
        code, merged = self.run_merge(
            graph([node("a"), node("b")], [("a", "b")]),
            graph([node("a"), node("b")]),
            graph([node("a"), node("b")], [("a", "b")]),
        )
        self.assertEqual(code, 0)
        self.assertEqual(merged["edges"], [])

    def test_an_edge_cannot_outlive_a_node_it_points_at(self):
        code, merged = self.run_merge(
            graph([node("a"), node("b")], [("a", "b")]),
            graph([node("a")]),
            graph([node("a"), node("b")], [("a", "b")]),
        )
        self.assertEqual(code, 0)
        self.assertEqual(merged["edges"], [])

    # ---------------------------------------------------------------- one-sided edits

    def test_an_edit_on_one_side_only_is_taken(self):
        code, merged = self.run_merge(
            graph([node("a", title="Old")]),
            graph([node("a", title="Old")]),
            graph([node("a", title="New")]),
        )
        self.assertEqual(code, 0)
        self.assertEqual(merged["nodes"][0]["title"], "New")

    def test_the_same_edit_on_both_sides_is_not_a_conflict(self):
        code, merged = self.run_merge(
            graph([node("a", title="Old")]),
            graph([node("a", title="New")]),
            graph([node("a", title="New")]),
        )
        self.assertEqual(code, 0)
        self.assertEqual(merged["nodes"][0]["title"], "New")

    # ---------------------------------------------------------------- real conflicts

    def test_two_different_rewrites_of_one_node_conflict_but_stay_readable(self):
        code, merged = self.run_merge(
            graph([node("a", title="Old")]),
            graph([node("a", title="Ours")]),
            graph([node("a", title="Theirs")]),
        )
        self.assertEqual(code, 1, "git has to be told a human is still needed")
        self.assertNotEqual(merged, "not json", "a graph.json nobody can parse is worse")
        self.assertEqual(merged["nodes"][0]["title"], "Ours")

    def test_a_conflict_still_merges_everything_it_could_determine(self):
        code, merged = self.run_merge(
            graph([node("a", title="Old")]),
            graph([node("a", title="Ours"), node("mine")]),
            graph([node("a", title="Theirs"), node("yours")]),
        )
        self.assertEqual(code, 1)
        self.assertEqual(sorted(self.paths_of(merged)), ["a", "mine", "yours"])

    # ---------------------------------------------------------------- refusals

    def test_an_unreadable_side_is_left_alone_rather_than_guessed_at(self):
        (self.dir / "theirs").write_text("{ broken", encoding="utf-8")
        base = self.dir / "base"
        base.write_text(json.dumps(graph()), encoding="utf-8")
        ours = self.dir / "ours"
        ours.write_text(json.dumps(graph([node("a")])), encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            code = merge_driver.merge(str(base), str(ours), str(self.dir / "theirs"))
        self.assertEqual(code, 1)
        self.assertEqual(self.paths_of(json.loads(ours.read_text(encoding="utf-8"))), ["a"])

    def test_different_schema_versions_are_refused(self):
        code, _ = self.run_merge(graph(), graph([node("a")]), graph([node("b")], version=2))
        self.assertEqual(code, 1)

    def test_a_missing_ancestor_degrades_to_a_union(self):
        # add/add: neither side has a common version of this file, and a union is then the
        # best answer available rather than a wrong one.
        code, merged = self.run_merge(None, graph([node("a")]), graph([node("b")]))
        self.assertEqual(code, 0)
        self.assertEqual(sorted(self.paths_of(merged)), ["a", "b"])

    # ---------------------------------------------------------------- invariants

    def test_parents_come_before_their_children(self):
        code, merged = self.run_merge(
            graph([node("area")]),
            graph([node("area"), node("area/one")]),
            graph([node("area"), node("area/two")]),
        )
        self.assertEqual(code, 0)
        paths = self.paths_of(merged)
        self.assertEqual(paths[0], "area")
        self.assertLess(paths.index("area"), paths.index("area/one"))
        self.assertLess(paths.index("area"), paths.index("area/two"))

    def test_history_is_unioned_deduplicated_and_capped(self):
        shared = {"op": "add", "target_kind": "node", "target_ref": "base", "at": "2026-01-01"}
        mine = {"op": "add", "target_kind": "node", "target_ref": "alpha", "at": "2026-01-02"}
        yours = {"op": "add", "target_kind": "node", "target_ref": "beta", "at": "2026-01-02"}
        code, merged = self.run_merge(
            graph([node("base")], changes=[shared]),
            graph([node("base"), node("alpha")], changes=[mine, shared]),
            graph([node("base"), node("beta")], changes=[yours, shared]),
        )
        self.assertEqual(code, 0)
        refs = [c["target_ref"] for c in merged["changes"]]
        self.assertEqual(refs.count("base"), 1, "the shared entry must not be duplicated")
        self.assertEqual(sorted(refs), ["alpha", "base", "beta"])

    def test_the_result_is_byte_identical_across_runs(self):
        args = (graph([node("base")]),
                graph([node("base"), node("alpha")]),
                graph([node("base"), node("beta")]))
        first = self.run_merge(*args)[1]
        second = self.run_merge(*args)[1]
        self.assertEqual(json.dumps(first), json.dumps(second))


class ThroughGit(unittest.TestCase):
    """The driver as git actually invokes it."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name)
        self.git("init", "-q", "-b", "main", ".")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "Test")
        self.git("config", "merge.codegraph.name", "design graph merge")
        self.git("config", "merge.codegraph.driver",
                 f'{sys.executable} {SKILL / "scripts" / "merge_driver.py"} %O %A %B %P')
        (self.repo / ".gitattributes").write_text(
            ".codegraph/graph.json merge=codegraph\n", encoding="utf-8")
        (self.repo / ".codegraph").mkdir()
        self.write(graph([node("base")]))
        self.git("add", "-A")
        self.git("commit", "-qm", "init")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args],
                              capture_output=True, text=True, timeout=60)

    def write(self, payload):
        (self.repo / ".codegraph" / "graph.json").write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def branch_with(self, name, payload):
        self.git("switch", "-q", "-c", name, "main")
        self.write(payload)
        self.git("add", "-A")
        self.git("commit", "-qm", name)

    def stored(self):
        return json.loads((self.repo / ".codegraph" / "graph.json").read_text(encoding="utf-8"))

    def test_git_merges_two_added_nodes_without_a_conflict(self):
        self.branch_with("a", graph([node("base"), node("alpha")]))
        self.branch_with("b", graph([node("base"), node("beta")]))
        self.git("switch", "-q", "main")
        self.assertEqual(self.git("merge", "-q", "a", "-m", "mA").returncode, 0)
        done = self.git("merge", "b", "-m", "mB")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(sorted(n["path"] for n in self.stored()["nodes"]),
                         ["alpha", "base", "beta"])

    def test_git_still_marks_a_real_disagreement_unresolved(self):
        self.branch_with("a", graph([node("base", title="Ours")]))
        self.branch_with("b", graph([node("base", title="Theirs")]))
        self.git("switch", "-q", "main")
        self.git("merge", "-q", "a", "-m", "mA")
        done = self.git("merge", "b", "-m", "mB")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("base", done.stdout + done.stderr)
        # Still a graph, not a file full of markers.
        self.assertEqual(self.stored()["nodes"][0]["title"], "Ours")


if __name__ == "__main__":
    unittest.main()
