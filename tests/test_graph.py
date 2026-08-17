#!/usr/bin/env python3
"""The query engine: what it answers, and the ways its output must not fail quietly."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import graph as graph_lib  # noqa: E402


def node(path, title="Title", kind="MODULE", summary="", file=None):
    return {"path": path, "kind": kind, "title": title, "summary": summary, "file": file}


def build(nodes, edges=()):
    return graph_lib.from_payload(
        {"project": "demo", "nodes": list(nodes), "edges": list(edges)}
    )


def edge(source, target, kind="USES"):
    return {"from": source, "to": target, "kind": kind}


NOWHERE = Path("/nonexistent-root-for-tests")


class Structure(unittest.TestCase):
    def test_stored_order_is_preserved_so_results_are_reproducible(self):
        graph = build([node("b"), node("a")])
        self.assertEqual(graph.order, ["b", "a"])

    def test_both_edge_directions_are_derived_from_the_one_edge_list(self):
        # A writer can say what a node depends on but cannot know what will later depend on
        # it, so a stored `used_by` would be empty and `users` would answer backwards.
        graph = build([node("a"), node("b")], [edge("b", "a")])
        self.assertEqual(graph.depends_on("b"), ["a"])
        self.assertEqual(graph.users_of("a"), ["b"])

    def test_an_edge_naming_an_unknown_node_is_ignored_in_the_index(self):
        graph = build([node("a")], [edge("a", "ghost")])
        self.assertEqual(graph.depends_on("a"), [])

    def test_children_are_direct_only(self):
        graph = build([node("a"), node("a/b"), node("a/b/c")])
        self.assertEqual(graph.children_of("a"), ["a/b"])

    def test_landmarks_are_top_level_nodes(self):
        graph = build([node("a"), node("a/b"), node("z")])
        self.assertEqual(graph.landmarks(), ["a", "z"])

    def test_landmarks_fall_back_to_the_shallowest_when_nothing_is_top_level(self):
        # Returning nothing here would push the reader straight back to grepping.
        graph = build([node("a/b"), node("a/b/c")])
        self.assertEqual(graph.landmarks(), ["a/b"])

    def test_a_node_with_no_path_is_skipped_and_counted(self):
        graph = graph_lib.from_payload({"nodes": [node("a"), {"kind": "MODULE"}, "junk"]})
        self.assertEqual((graph.order, graph.skipped), (["a"], 2))

    def test_a_payload_of_the_wrong_shape_is_unavailable_not_a_crash(self):
        with self.assertRaises(graph_lib.GraphUnavailable):
            graph_lib.from_payload({"oops": True})


class Find(unittest.TestCase):
    def graph(self):
        return build(
            [
                node("server", "Server", summary="http surface"),
                node("server/store", "Store", summary="persists orders", file="src/store.py"),
                node("client", "Client", summary="talks to the server"),
            ]
        )

    def test_a_match_on_a_path_segment_outranks_a_match_in_a_summary(self):
        out = graph_lib.find(NOWHERE, self.graph(), ["server"])
        first_row = out.splitlines()[1]
        self.assertIn("server", first_row)
        self.assertNotIn("client", first_row)

    def test_keywords_are_anded(self):
        out = graph_lib.find(NOWHERE, self.graph(), ["store", "orders"])
        self.assertIn("server/store", out)
        self.assertNotIn("client", out)

    def test_a_keyword_that_matches_nothing_narrows_the_whole_query(self):
        out = graph_lib.find(NOWHERE, self.graph(), ["store", "zzz"])
        self.assertIn("no matches", out)

    def test_no_match_still_offers_signposts_rather_than_nothing(self):
        # Blank output reads as a broken tool, and a broken tool sends the reader to grep.
        out = graph_lib.find(NOWHERE, self.graph(), ["zzz"])
        self.assertIn("no matches", out)
        self.assertIn("Top-level nodes", out)

    def test_an_empty_graph_says_so_instead_of_offering_signposts(self):
        out = graph_lib.find(NOWHERE, build([]), ["zzz"])
        self.assertIn("The graph is empty", out)

    def test_no_keywords_explains_itself(self):
        self.assertIn("at least one keyword", graph_lib.find(NOWHERE, self.graph(), []))

    def test_blank_keywords_count_as_none(self):
        self.assertIn("at least one keyword", graph_lib.find(NOWHERE, self.graph(), ["  "]))

    def test_truncation_states_the_count_and_how_to_narrow(self):
        # Silent truncation is the worst failure here: a half list looks like a whole one.
        many = [node(f"m{index}", "Module", summary="thing") for index in range(40)]
        out = graph_lib.find(NOWHERE, build(many), ["thing"])
        self.assertIn(f"showing the first {graph_lib.FIND_LIMIT} of 40", out)
        self.assertIn("more not shown", out)
        self.assertIn("more specific word", out)

    def test_results_are_stable_between_identical_calls(self):
        graph = self.graph()
        self.assertEqual(
            graph_lib.find(NOWHERE, graph, ["server"]),
            graph_lib.find(NOWHERE, graph, ["server"]),
        )


class Show(unittest.TestCase):
    def graph(self):
        return build(
            [node("a", "A", summary="root thing"), node("a/b", "B"), node("c", "C")],
            [edge("c", "a")],
        )

    def test_it_reports_children_dependencies_and_dependents(self):
        out = graph_lib.show(NOWHERE, self.graph(), "a")
        self.assertIn("children (1)", out)
        self.assertIn("depends_on (0)", out)
        self.assertIn("used_by (1)", out)

    def test_an_empty_section_is_shown_as_zero_rather_than_omitted(self):
        self.assertIn("depends_on (0)", graph_lib.show(NOWHERE, self.graph(), "a"))

    def test_an_unknown_path_offers_the_closest_matches(self):
        out = graph_lib.show(NOWHERE, self.graph(), "ab")
        self.assertIn("closest matches", out)

    def test_an_unknown_path_names_the_slug_versus_file_mistake(self):
        # This is the mistake people actually make, so it is worth a line every time.
        out = graph_lib.show(NOWHERE, self.graph(), "src/a.py")
        self.assertIn("not a file path", out)

    def test_a_wildly_wrong_path_falls_back_to_signposts(self):
        out = graph_lib.show(NOWHERE, self.graph(), "zzzzzzzz")
        self.assertIn("top-level nodes", out)


class Users(unittest.TestCase):
    def test_indirect_reach_is_walked_not_just_direct_dependents(self):
        # Reporting one hop systematically understates the blast radius, which is the exact
        # mistake this query exists to prevent.
        graph = build(
            [node("a"), node("b"), node("c")], [edge("b", "a"), edge("c", "b")]
        )
        out = graph_lib.users(NOWHERE, graph, "a")
        self.assertIn("1 direct dependent", out)
        self.assertIn("Indirectly affected: 1", out)
        self.assertIn("c", out)

    def test_a_dependency_cycle_terminates(self):
        graph = build([node("a"), node("b")], [edge("a", "b"), edge("b", "a")])
        self.assertIn("direct dependent", graph_lib.users(NOWHERE, graph, "a"))

    def test_nothing_depending_on_it_is_stated_with_its_caveat(self):
        graph = build([node("a")])
        out = graph_lib.users(NOWHERE, graph, "a")
        self.assertIn("nothing depends on it", out)
        self.assertIn("still exists", out)

    def test_an_unknown_path_is_guidance_not_silence(self):
        self.assertIn("no node with path", graph_lib.users(NOWHERE, build([node("a")]), "b"))


class Staleness(unittest.TestCase):
    def test_a_node_whose_file_is_gone_is_flagged(self):
        with tempfile.TemporaryDirectory() as workdir:
            root = Path(workdir)
            graph = build([node("a", file="src/gone.py")])
            self.assertIn("⚠ file missing", graph_lib.show(root, graph, "a"))

    def test_a_node_whose_file_exists_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as workdir:
            root = Path(workdir)
            (root / "src").mkdir()
            (root / "src" / "here.py").touch()
            graph = build([node("a", file="src/here.py")])
            self.assertNotIn("⚠ file missing", graph_lib.show(root, graph, "a"))

    def test_a_node_with_no_file_is_not_flagged(self):
        # A grouping module legitimately has no file of its own.
        graph = build([node("a")])
        self.assertNotIn("⚠ file missing", graph_lib.show(NOWHERE, graph, "a"))

    def test_an_edge_endpoint_missing_from_the_graph_is_flagged_in_a_list(self):
        graph = build([node("a")])
        rows = graph_lib.render_rows(NOWHERE, graph, ["a", "ghost"], 10)
        self.assertTrue(any("not in the graph" in row for row in rows))


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_no_store_reads_as_none(self):
        self.assertIsNone(graph_lib.read_store(self.root))

    def test_loading_with_no_store_says_what_to_do_next(self):
        with self.assertRaises(graph_lib.GraphUnavailable) as caught:
            graph_lib.load(self.root)
        self.assertIn("bootstrap", str(caught.exception))

    def test_a_written_store_round_trips(self):
        graph_lib.write_store(
            self.root, {"project": "demo", "nodes": [node("a")], "edges": []}
        )
        graph = graph_lib.load(self.root)
        self.assertEqual(graph.order, ["a"])

    def test_the_store_is_written_atomically_and_leaves_no_temp_file(self):
        graph_lib.write_store(self.root, {"project": "d", "nodes": [], "edges": []})
        leftovers = list((self.root / ".codegraph").glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_broken_json_is_reported_as_interrupted_rather_than_parsed(self):
        (self.root / ".codegraph").mkdir()
        (self.root / ".codegraph" / "graph.json").write_text("{ oops", encoding="utf-8")
        with self.assertRaises(graph_lib.GraphUnavailable) as caught:
            graph_lib.read_store(self.root)
        self.assertIn("not valid JSON", str(caught.exception))

    def test_a_store_of_the_wrong_shape_is_reported(self):
        (self.root / ".codegraph").mkdir()
        (self.root / ".codegraph" / "graph.json").write_text('{"a":1}', encoding="utf-8")
        with self.assertRaises(graph_lib.GraphUnavailable):
            graph_lib.read_store(self.root)

    def test_edges_default_so_an_older_store_still_loads(self):
        (self.root / ".codegraph").mkdir()
        (self.root / ".codegraph" / "graph.json").write_text(
            json.dumps({"nodes": [node("a")]}), encoding="utf-8"
        )
        self.assertEqual(graph_lib.read_store(self.root)["edges"], [])


class Width(unittest.TestCase):
    def test_wide_characters_count_as_two_columns(self):
        # Aligning with len() misaligns any table whose titles are not pure ASCII.
        self.assertEqual(graph_lib.width("ab"), 2)
        self.assertEqual(graph_lib.width("中文"), 4)

    def test_padding_uses_display_columns(self):
        self.assertEqual(graph_lib.width(graph_lib.pad("中", 4)), 4)

    def test_clipping_never_exceeds_the_budget(self):
        clipped = graph_lib.clip("中文中文中文", 5)
        self.assertLessEqual(graph_lib.width(clipped), 5)
        self.assertTrue(clipped.endswith("…"))

    def test_text_within_budget_is_untouched(self):
        self.assertEqual(graph_lib.clip("short", 40), "short")


if __name__ == "__main__":
    unittest.main()
