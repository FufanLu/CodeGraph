#!/usr/bin/env python3
"""The pushed structure map, and the hook that delivers it.

Most of these guard wording rather than data. That is deliberate: the map's failure mode is
not a wrong number, it is a true statement phrased so the reader draws the wrong conclusion
and stops trusting the graph.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import graph as graph_lib  # noqa: E402
import structure_map  # noqa: E402


def node(path, title=None, summary="", file=None, kind="MODULE"):
    return {
        "path": path,
        "kind": kind,
        "title": title or path.replace("/", " ").title(),
        "summary": summary,
        "file": file,
    }


def build(nodes, edges=()):
    return graph_lib.from_payload(
        {"project": "demo", "nodes": list(nodes), "edges": list(edges)}
    )


NOWHERE = Path("/nonexistent-root-for-tests")


class Shape(unittest.TestCase):
    def test_an_empty_graph_renders_nothing_at_all(self):
        # Nothing to say beats saying "nothing to say" on every single turn.
        self.assertIsNone(structure_map.render(NOWHERE, build([])))

    def test_the_header_reports_shown_and_total_separately(self):
        # One number would hide that this is a subset.
        out = structure_map.render(NOWHERE, build([node("a"), node("a/b")]))
        self.assertIn("1 entries · 2 in the design", out)

    def test_the_heading_level_encodes_containment_depth(self):
        out = structure_map.render(NOWHERE, build([node("a"), node("a/b")]), skeleton_depth=2)
        self.assertIn("\n## A `MODULE`", out)
        self.assertIn("\n### A B `MODULE`", out)

    def test_a_parent_is_immediately_followed_by_its_children(self):
        # Sorting by depth instead would interleave subtrees and lose what contains what.
        graph = build([node("b"), node("a"), node("a/x"), node("b/y")])
        out = structure_map.render(NOWHERE, graph, skeleton_depth=2)
        order = [line for line in out.splitlines() if line.startswith("`")]
        self.assertEqual(order, ["`a`", "`a/x`", "`b`", "`b/y`"])

    def test_the_summary_is_included_at_full_detail(self):
        out = structure_map.render(NOWHERE, build([node("a", summary="does one job")]))
        self.assertIn("does one job", out)

    def test_a_stale_file_is_flagged_in_the_map_too(self):
        out = structure_map.render(NOWHERE, build([node("a", file="src/gone.py")]))
        self.assertIn("⚠ file missing", out)


class AbsenceWording(unittest.TestCase):
    """The two reasons a node can be missing must never share a sentence."""

    def test_depth_held_back_is_described_as_deliberate_not_lost(self):
        # Every project with a second level hits this on every turn. Calling it truncation
        # teaches the reader the map is lossy and the graph is not worth querying.
        out = structure_map.render(NOWHERE, build([node("a"), node("a/b")]))
        self.assertIn("held back on purpose, not lost", out)
        self.assertNotIn("Truncated to fit", out)

    def test_it_says_how_to_open_what_is_held_back(self):
        out = structure_map.render(NOWHERE, build([node("a"), node("a/b")]))
        self.assertIn("show <path>", out)

    def test_a_single_level_graph_says_nothing_about_depth(self):
        out = structure_map.render(NOWHERE, build([node("a"), node("b")]))
        self.assertNotIn("held back", out)

    def test_budget_loss_is_announced_as_truncation(self):
        many = [node(f"m{i}", summary="x" * 200) for i in range(200)]
        out = structure_map.render(NOWHERE, build(many), budget=2000)
        self.assertIn("Truncated to fit", out)

    def test_a_skipped_node_is_announced_rather_than_quietly_uncounted(self):
        # A third way a node can be absent, and the only one that means the store itself is
        # damaged. The count in the header excludes it, so without this line nothing in the
        # document would let a reader notice.
        out = structure_map.render(NOWHERE, build([node("a"), {"kind": "MODULE"}]))
        self.assertIn("1 node(s) were skipped", out)

    def test_a_clean_graph_says_nothing_about_skipped_nodes(self):
        self.assertNotIn("skipped", structure_map.render(NOWHERE, build([node("a")])))

    def test_the_skipped_warning_survives_a_budget_squeeze(self):
        # It sits in the header for this reason: the last-resort cut trims from the tail,
        # and a warning that is dropped exactly when the map gets lossiest is worthless.
        many = [node(f"m{i}", summary="x" * 200) for i in range(200)] + [{"kind": "MODULE"}]
        out = structure_map.render(NOWHERE, build(many), budget=2000)
        self.assertIn("1 node(s) were skipped", out)

    def test_dropped_detail_is_announced(self):
        many = [node(f"m{i}", summary="y" * 300) for i in range(60)]
        out = structure_map.render(NOWHERE, build(many), budget=3000)
        self.assertIn("Detail reduced to fit", out)


class Budget(unittest.TestCase):
    def test_a_large_graph_still_fits(self):
        many = [node(f"m{i}", summary="z" * 400) for i in range(400)]
        out = structure_map.render(NOWHERE, build(many))
        self.assertLessEqual(len(out.encode("utf-8")), structure_map.BUDGET)

    def test_detail_is_shed_before_nodes_are(self):
        # Losing summaries is much cheaper than losing whole nodes.
        many = [node(f"m{i}", summary="w" * 120) for i in range(40)]
        out = structure_map.render(NOWHERE, build(many), budget=2600)
        self.assertEqual(out.count("`MODULE`"), 40)
        self.assertNotIn("w" * 120, out)

    def test_even_a_single_oversized_entry_respects_the_budget(self):
        # This is the case that once returned four times the limit, because the fallback
        # returned unconditionally and the caller trusted it to be trimmed. The title is
        # used rather than the summary: detail degradation can shed a summary, so a long
        # one never reaches the hard cut.
        graph = build([node("hub", title="H" * 5000)])
        out = structure_map.render(NOWHERE, graph, budget=500)
        self.assertLessEqual(len(out.encode("utf-8")), 500)
        self.assertIn("Cut off here", out)

    def test_a_hard_cut_never_splits_a_character(self):
        graph = build([node("hub", title="中" * 2000)])
        out = structure_map.render(NOWHERE, graph, budget=400)
        out.encode("utf-8").decode("utf-8")
        self.assertLessEqual(len(out.encode("utf-8")), 400)


class Edges(unittest.TestCase):
    def test_edges_between_visible_nodes_are_drawn_both_ways(self):
        graph = build([node("a"), node("b")], [{"from": "b", "to": "a", "kind": "USES"}])
        out = structure_map.render(NOWHERE, graph)
        self.assertIn("depends on: A (USES)", out)
        self.assertIn("used by: B (USES)", out)

    def test_an_edge_to_a_hidden_node_is_not_drawn(self):
        # Otherwise the map names entries that cannot be found anywhere in it, and the only
        # way to resolve a name is to go back to grep.
        graph = build(
            [node("a"), node("b"), node("b/hidden")],
            [{"from": "a", "to": "b/hidden", "kind": "USES"}],
        )
        out = structure_map.render(NOWHERE, graph)
        self.assertNotIn("depends on:", out)


class RecentChanges(unittest.TestCase):
    def changes(self, count=3):
        return [
            {"op": "add", "target_kind": "node", "target_ref": f"n{i}", "commit": "abc1234"}
            for i in range(count)
        ]

    def test_recent_changes_are_listed(self):
        out = structure_map.render(NOWHERE, build([node("a")]), self.changes())
        self.assertIn("## Recent changes", out)
        self.assertIn("add node `n0` (abc1234)", out)

    def test_no_changes_means_no_section(self):
        out = structure_map.render(NOWHERE, build([node("a")]))
        self.assertNotIn("Recent changes", out)

    def test_the_list_is_capped(self):
        out = structure_map.render(NOWHERE, build([node("a")]), self.changes(50))
        self.assertEqual(out.count("add node"), structure_map.RECENT_LIMIT)

    def test_history_never_vanishes_silently_at_any_budget(self):
        # Asserted across the whole budget range rather than at one tuned number: the
        # invariant is "the history is never simply gone", and a single magic budget would
        # stop exercising it the moment the rendering got a few bytes cheaper.
        graph = build([node(f"m{i}", summary="v" * 100) for i in range(30)])
        changes = self.changes(20)
        for budget in range(400, 6000, 100):
            out = structure_map.render(NOWHERE, graph, changes, budget=budget)
            if "add node `n0`" in out:
                continue
            self.assertTrue(
                "Omitted to fit" in out or "Cut off here" in out,
                f"history disappeared with no notice at budget={budget}",
            )


class ForRepository(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)

    def write(self, text):
        (self.root / ".codegraph").mkdir(exist_ok=True)
        (self.root / ".codegraph" / "graph.json").write_text(text, encoding="utf-8")

    def test_no_store_is_none_not_an_error(self):
        self.assertIsNone(structure_map.for_repository(self.root))

    def test_a_broken_store_is_none_rather_than_an_exception(self):
        # This runs on every turn; it must never be the reason a prompt fails.
        self.write("{ broken")
        self.assertIsNone(structure_map.for_repository(self.root))

    def test_a_store_of_the_wrong_shape_is_none(self):
        self.write('{"nodes": "nope"}')
        self.assertIsNone(structure_map.for_repository(self.root))

    def test_a_real_store_renders(self):
        self.write(json.dumps({"project": "demo", "nodes": [node("a")], "edges": []}))
        self.assertIn("structure map", structure_map.for_repository(self.root))


class Hook(unittest.TestCase):
    """The hook must never be worse than not having it."""

    def run_hook(self, stdin):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "hook.py")],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_a_project_with_a_graph_gets_the_map(self):
        with tempfile.TemporaryDirectory() as workdir:
            root = Path(workdir).resolve()
            (root / ".codegraph").mkdir()
            (root / ".codegraph" / "graph.json").write_text(
                json.dumps({"project": "demo", "nodes": [node("a")], "edges": []}),
                encoding="utf-8",
            )
            done = self.run_hook(json.dumps({"cwd": str(root)}))
        self.assertIn("structure map", done.stdout)
        self.assertEqual(done.returncode, 0)

    def test_a_project_with_no_graph_says_nothing(self):
        # Most repositories have none. A hook that announced its irrelevance every turn
        # would be uninstalled within a day.
        with tempfile.TemporaryDirectory() as workdir:
            done = self.run_hook(json.dumps({"cwd": workdir}))
        self.assertEqual(done.stdout, "")
        self.assertEqual(done.returncode, 0)

    def test_garbage_on_stdin_still_exits_zero(self):
        # Exit code 2 on UserPromptSubmit erases what the user typed. Nothing here is worth
        # that, so every failure path ends in exit 0 and silence.
        done = self.run_hook("not json at all")
        self.assertEqual((done.returncode, done.stdout), (0, ""))

    def test_empty_stdin_still_exits_zero(self):
        done = self.run_hook("")
        self.assertEqual((done.returncode, done.stdout), (0, ""))

    def test_a_nonexistent_cwd_still_exits_zero(self):
        done = self.run_hook(json.dumps({"cwd": "/no/such/place"}))
        self.assertEqual((done.returncode, done.stdout), (0, ""))

    def test_a_broken_store_still_exits_zero_and_silent(self):
        with tempfile.TemporaryDirectory() as workdir:
            root = Path(workdir)
            (root / ".codegraph").mkdir()
            (root / ".codegraph" / "graph.json").write_text("{ broken", encoding="utf-8")
            done = self.run_hook(json.dumps({"cwd": str(root)}))
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))

    def test_the_map_never_looks_like_json_to_the_hook_reader(self):
        # Plain stdout is parsed as JSON when it starts with `{`. The map starts with `# `.
        with tempfile.TemporaryDirectory() as workdir:
            root = Path(workdir).resolve()
            (root / ".codegraph").mkdir()
            (root / ".codegraph" / "graph.json").write_text(
                json.dumps({"project": "demo", "nodes": [node("a")], "edges": []}),
                encoding="utf-8",
            )
            done = self.run_hook(json.dumps({"cwd": str(root)}))
        self.assertTrue(done.stdout.startswith("# "))


if __name__ == "__main__":
    unittest.main()
