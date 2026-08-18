#!/usr/bin/env python3
"""Rules of the delta validator.

Every rule gets a case, because these rules are the only thing standing between "one bad
line was written" and "somebody else's nodes are gone".
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import delta as delta_rules  # noqa: E402


def marker(adds_nodes=(), adds_edges=(), removes_nodes=(), removes_edges=(), version=1):
    return {
        "schema_version": version,
        "adds": {"nodes": list(adds_nodes), "edges": list(adds_edges)},
        "removes": {"nodes": list(removes_nodes), "edges": list(removes_edges)},
    }


def node(path, kind="MODULE", title="Title", **extra):
    return {"path": path, "kind": kind, "title": title, **extra}


def edge(source, target, kind="USES"):
    return {"from": source, "to": target, "kind": kind}


def with_updates(delta, nodes):
    delta["updates"] = {"nodes": list(nodes)}
    return delta


class Validate(unittest.TestCase):
    def reject(self, current, delta, needle, **options):
        with self.assertRaises(delta_rules.DeltaRejected) as caught:
            delta_rules.validate(set(current), delta, **options)
        self.assertIn(needle, str(caught.exception))

    def test_a_plain_addition_is_accepted(self):
        plan = delta_rules.validate(set(), marker([node("backend")]))
        self.assertEqual([spec["path"] for spec in plan["add_nodes"]], ["backend"])

    def test_marker_order_is_preserved_so_a_parent_precedes_its_child(self):
        plan = delta_rules.validate(set(), marker([node("backend"), node("backend/store")]))
        self.assertEqual(
            [spec["path"] for spec in plan["add_nodes"]], ["backend", "backend/store"]
        )

    def test_a_parent_added_earlier_in_the_same_delta_counts_as_present(self):
        delta_rules.validate(set(), marker([node("backend"), node("backend/store")]))

    def test_a_child_whose_parent_is_nowhere_is_rejected(self):
        self.reject(set(), marker([node("backend/store")]), "has no parent")

    def test_a_grandchild_may_be_declared_after_its_parent(self):
        delta_rules.validate(
            set(), marker([node("a"), node("a/b"), node("a/b/c")])
        )

    def test_an_unknown_schema_version_rejects_everything(self):
        self.reject(set(), marker([node("backend")], version=2), "not supported")

    def test_a_missing_schema_version_rejects_everything(self):
        self.reject(set(), {"adds": {"nodes": []}}, "not supported")

    def test_an_uppercase_path_is_rejected(self):
        # Otherwise `Backend` and `backend` are two nodes that look identical to a reader.
        self.reject(set(), marker([node("Backend")]), "not a valid node path")

    def test_a_path_with_a_space_is_rejected(self):
        self.reject(set(), marker([node("back end")]), "not a valid node path")

    def test_an_empty_path_segment_is_rejected(self):
        self.reject(set(), marker([node("backend//store")]), "not a valid node path")

    def test_an_unknown_node_kind_is_rejected(self):
        self.reject(set(), marker([node("backend", kind="PACKAGE")]), "expected one of")

    def test_a_blank_title_is_rejected(self):
        self.reject(set(), marker([node("backend", title="   ")]), "has no title")

    def test_adding_a_node_that_already_exists_is_rejected(self):
        self.reject({"backend"}, marker([node("backend")]), "already exists")

    def test_adding_the_same_node_twice_in_one_delta_is_rejected(self):
        self.reject(set(), marker([node("backend"), node("backend")]), "already exists")

    def test_removing_a_node_that_is_not_there_is_rejected(self):
        # Not a harmless no-op: it means the writer's picture disagrees with the graph.
        self.reject(set(), marker(removes_nodes=["ghost"]), "no such node")

    def test_an_edge_pointing_at_an_undeclared_node_is_rejected(self):
        self.reject(
            set(),
            marker([node("backend")], [edge("backend", "nowhere")]),
            "`nowhere` is not in the graph",
        )

    def test_an_edge_pointing_at_a_node_this_delta_removes_is_rejected(self):
        self.reject(
            {"a", "b"},
            marker(adds_edges=[edge("a", "b")], removes_nodes=["b"]),
            "`b` is not in the graph",
        )

    def test_a_self_edge_is_rejected(self):
        self.reject(
            set(),
            marker([node("backend")], [edge("backend", "backend")]),
            "cannot depend on itself",
        )

    def test_an_unknown_edge_kind_is_rejected(self):
        self.reject({"a", "b"}, marker(adds_edges=[edge("a", "b", "CALLS")]), "expected one of")

    def test_an_unknown_node_field_rejects_everything(self):
        self.reject(set(), marker([node("backend", colour="red")]), "unknown field")

    def test_an_unknown_top_level_field_rejects_everything(self):
        delta = marker([node("backend")])
        delta["notes"] = "hello"
        self.reject(set(), delta, "unknown field")

    def test_a_node_missing_a_required_field_rejects_everything(self):
        self.reject(set(), marker([{"path": "backend", "kind": "MODULE"}]), "is missing")

    def test_a_node_may_be_replaced_in_one_delta_because_removes_come_first(self):
        # Load-bearing: swapping one node for another is only expressible in a single delta
        # if removals are applied before additions.
        plan = delta_rules.validate(
            {"server", "server/legacy"},
            marker([node("server/store")], removes_nodes=["server/legacy"]),
        )
        self.assertEqual(plan["remove_nodes"], ["server/legacy"])

    def test_an_empty_delta_is_accepted(self):
        plan = delta_rules.validate(set(), marker())
        self.assertEqual(plan["add_nodes"], [])


class FileLocation(unittest.TestCase):
    def test_a_directory_with_a_trailing_slash_is_accepted(self):
        self.assertTrue(delta_rules.valid_repo_location("src/server/db/"))

    def test_a_plain_file_is_accepted(self):
        self.assertTrue(delta_rules.valid_repo_location("src/store.mjs"))

    def test_an_absolute_path_is_rejected(self):
        # Path joining replaces the whole path when the right operand is absolute, so this
        # would let a node's file point anywhere on the machine.
        self.assertFalse(delta_rules.valid_repo_location("/etc/passwd"))

    def test_a_parent_segment_is_rejected(self):
        self.assertFalse(delta_rules.valid_repo_location("../../etc/passwd"))

    def test_a_dot_segment_is_rejected(self):
        self.assertFalse(delta_rules.valid_repo_location("./src"))

    def test_a_backslash_is_rejected(self):
        self.assertFalse(delta_rules.valid_repo_location("src\\server"))

    def test_surrounding_whitespace_is_rejected(self):
        self.assertFalse(delta_rules.valid_repo_location(" src/"))

    def test_a_lone_slash_is_rejected(self):
        self.assertFalse(delta_rules.valid_repo_location("/"))

    def test_a_bad_file_rejects_the_whole_delta(self):
        with self.assertRaises(delta_rules.DeltaRejected) as caught:
            delta_rules.validate(set(), marker([node("backend", file="/etc/passwd")]))
        self.assertIn("not a location inside the", str(caught.exception))

    def test_file_is_optional_once_the_graph_exists(self):
        delta_rules.validate({"backend"}, marker([node("backend/store", kind="CLASS")]))

    def test_file_is_required_by_the_first_delta(self):
        with self.assertRaises(delta_rules.DeltaRejected) as caught:
            delta_rules.validate(set(), marker([node("backend")]), require_file=True)
        self.assertIn("has no `file`", str(caught.exception))


class Updates(unittest.TestCase):
    """Changing what a node says without taking its edges down with it.

    Before this existed the only way to fix a summary was to remove the node and add it
    back, which silently discarded every edge that node had — the graph losing exactly the
    facts it is kept for, as the price of a typo.
    """

    def plan(self, current, nodes):
        return delta_rules.validate(set(current), with_updates(marker(), nodes))

    def reject(self, current, nodes, needle):
        with self.assertRaises(delta_rules.DeltaRejected) as caught:
            self.plan(current, nodes)
        self.assertIn(needle, str(caught.exception))

    def test_a_summary_can_be_rewritten(self):
        plan = self.plan({"a"}, [{"path": "a", "summary": "what it is for"}])
        self.assertEqual(plan["update_nodes"], [{"path": "a", "summary": "what it is for"}])

    def test_the_target_must_exist(self):
        self.reject(set(), [{"path": "a", "summary": "x"}], "no such node")

    def test_a_node_removed_by_the_same_delta_cannot_be_updated(self):
        delta = with_updates(marker(removes_nodes=["a"]), [{"path": "a", "summary": "x"}])
        with self.assertRaises(delta_rules.DeltaRejected) as caught:
            delta_rules.validate({"a"}, delta)
        self.assertIn("no such node", str(caught.exception))

    def test_an_update_naming_nothing_to_change_is_rejected(self):
        # It reads as an intent that was lost on the way, not as a no-op worth applying.
        self.reject({"a"}, [{"path": "a"}], "nothing to update")

    def test_an_unknown_field_is_rejected(self):
        self.reject({"a"}, [{"path": "a", "colour": "red"}], "unknown field")

    def test_a_bad_kind_is_rejected(self):
        self.reject({"a"}, [{"path": "a", "kind": "PACKAGE"}], "expected one of")

    def test_an_empty_title_is_rejected(self):
        self.reject({"a"}, [{"path": "a", "title": "  "}], "no title")

    def test_a_file_outside_the_repository_is_rejected(self):
        self.reject({"a"}, [{"path": "a", "file": "../etc/passwd"}], "not a location inside")

    def test_a_file_can_be_cleared(self):
        # A grouping node may legitimately have no location of its own.
        plan = self.plan({"a"}, [{"path": "a", "file": None}])
        self.assertEqual(plan["update_nodes"][0]["file"], None)


class Apply(unittest.TestCase):
    def store(self):
        return {"project": "demo", "nodes": [], "edges": []}

    def build(self, current, delta, store=None):
        plan = delta_rules.validate(set(current), delta)
        return delta_rules.apply(store or self.store(), plan)

    def test_additions_land_in_order(self):
        updated = self.build(set(), marker([node("a"), node("a/b")]))
        self.assertEqual([n["path"] for n in updated["nodes"]], ["a", "a/b"])

    def test_a_summary_defaults_to_empty_rather_than_missing(self):
        updated = self.build(set(), marker([node("a")]))
        self.assertEqual(updated["nodes"][0]["summary"], "")

    def test_removing_a_node_takes_its_edges_with_it(self):
        first = self.build(set(), marker([node("a"), node("b")], [edge("a", "b")]))
        self.assertEqual(len(first["edges"]), 1)
        second = self.build({"a", "b"}, marker(removes_nodes=["b"]), store=first)
        self.assertEqual(second["edges"], [])

    def test_edges_taken_by_a_removed_node_are_counted_not_silently_dropped(self):
        first = self.build(set(), marker([node("a"), node("b")], [edge("a", "b")]))
        plan = delta_rules.validate({"a", "b"}, marker(removes_nodes=["b"]))
        cascaded = delta_rules.cascade_edges(first, plan)
        self.assertEqual([(e["from"], e["to"]) for e in cascaded], [("a", "b")])

    def test_an_edge_cannot_be_named_for_removal_alongside_its_own_node(self):
        # Both endpoints of a named edge must survive the delta, so a cascaded edge can
        # never also be a named one. That is what lets `cascade_edges` skip deduplicating.
        with self.assertRaises(delta_rules.DeltaRejected) as caught:
            delta_rules.validate(
                {"a", "b"}, marker(removes_nodes=["b"], removes_edges=[edge("a", "b")])
            )
        self.assertIn("not in the graph", str(caught.exception))

    def test_a_cascaded_removal_is_written_into_the_history(self):
        # Otherwise "Recent changes" says the node went and stays silent about the edges,
        # which is the same untruth one layer down.
        first = self.build(set(), marker([node("a"), node("b")], [edge("a", "b")]))
        second = self.build({"a", "b"}, marker(removes_nodes=["b"]), store=first)
        edges = [c["target_ref"] for c in second["changes"] if c["target_kind"] == "edge"]
        self.assertIn("a>USES>b", edges)

    def test_an_update_changes_the_node_and_leaves_its_edges_standing(self):
        first = self.build(set(), marker([node("a"), node("b")], [edge("a", "b")]))
        plan = delta_rules.validate({"a", "b"}, with_updates(marker(), [{"path": "a", "summary": "new"}]))
        second = delta_rules.apply(first, plan)
        self.assertEqual(second["nodes"][0]["summary"], "new")
        self.assertEqual(len(second["edges"]), 1)

    def test_an_update_is_recorded_in_the_history(self):
        first = self.build(set(), marker([node("a")]))
        plan = delta_rules.validate({"a"}, with_updates(marker(), [{"path": "a", "title": "A2"}]))
        second = delta_rules.apply(first, plan)
        self.assertIn(("update", "node", "a"), [
            (c["op"], c["target_kind"], c["target_ref"]) for c in second["changes"]
        ])

    def test_removing_an_edge_leaves_its_nodes_alone(self):
        first = self.build(set(), marker([node("a"), node("b")], [edge("a", "b")]))
        second = self.build({"a", "b"}, marker(removes_edges=[edge("a", "b")]), store=first)
        self.assertEqual(len(second["nodes"]), 2)
        self.assertEqual(second["edges"], [])

    def test_the_same_edge_applied_twice_is_not_duplicated(self):
        first = self.build(set(), marker([node("a"), node("b")], [edge("a", "b")]))
        again = delta_rules.apply(
            first,
            {"add_nodes": [], "add_edges": [edge("a", "b")], "remove_nodes": [], "remove_edges": []},
        )
        self.assertEqual(len(again["edges"]), 1)

    def test_a_replacement_within_one_delta_keeps_only_the_new_node(self):
        first = self.build(set(), marker([node("server"), node("server/legacy")]))
        second = self.build(
            {"server", "server/legacy"},
            marker([node("server/store")], removes_nodes=["server/legacy"]),
            store=first,
        )
        self.assertEqual(
            [n["path"] for n in second["nodes"]], ["server", "server/store"]
        )

    def test_the_input_store_is_never_mutated(self):
        store = self.store()
        self.build(set(), marker([node("backend")]), store=store)
        self.assertEqual(store["nodes"], [])


if __name__ == "__main__":
    unittest.main()
