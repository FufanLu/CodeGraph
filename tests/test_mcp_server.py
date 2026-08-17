#!/usr/bin/env python3
"""The MCP server: protocol frames, the transport contract, and how tools report failure."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import mcp_server  # noqa: E402


def text_of(result):
    return result["content"][0]["text"]


class Protocol(unittest.TestCase):
    def dispatch(self, message):
        return mcp_server.dispatch(message, Path("/nowhere"))

    def test_initialize_echoes_the_clients_protocol_version(self):
        # A client speaking a newer revision should not be told it reached the wrong server.
        kind, body = self.dispatch(
            {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
        )
        self.assertEqual(kind, "result")
        self.assertEqual(body["protocolVersion"], "2025-06-18")
        self.assertEqual(body["serverInfo"]["name"], "code-graph")

    def test_initialize_falls_back_when_the_client_names_no_version(self):
        _, body = self.dispatch({"id": 1, "method": "initialize"})
        self.assertEqual(body["protocolVersion"], mcp_server.PROTOCOL_VERSION)

    def test_a_notification_gets_no_reply(self):
        # No id means no response frame. Replying to one desynchronises the stream.
        self.assertIsNone(self.dispatch({"method": "notifications/initialized"}))

    def test_ping_is_answered(self):
        self.assertEqual(self.dispatch({"id": 1, "method": "ping"}), ("result", {}))

    def test_tools_list_matches_the_handlers(self):
        _, body = self.dispatch({"id": 1, "method": "tools/list"})
        self.assertEqual(
            sorted(tool["name"] for tool in body["tools"]), sorted(mcp_server.HANDLERS)
        )

    def test_every_tool_declares_a_schema_and_a_description(self):
        _, body = self.dispatch({"id": 1, "method": "tools/list"})
        for tool in body["tools"]:
            self.assertEqual(tool["inputSchema"]["type"], "object", tool["name"])
            self.assertTrue(tool["description"].strip(), tool["name"])

    def test_an_unknown_method_is_a_json_rpc_error(self):
        kind, body = self.dispatch({"id": 1, "method": "resources/list"})
        self.assertEqual((kind, body["code"]), ("error", -32601))

    def test_an_unknown_tool_is_an_error_result_not_a_crash(self):
        _, body = self.dispatch(
            {"id": 1, "method": "tools/call", "params": {"name": "nope", "arguments": {}}}
        )
        self.assertTrue(body["isError"])


class Transport(unittest.TestCase):
    """stdout carries the protocol, so nothing else may ever be written to it."""

    def server(self, stdin, cwd, args=()):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "mcp_server.py"), *args],
            input=stdin,
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=60,
        )

    def test_one_json_line_per_request_and_nothing_else_on_stdout(self):
        frames = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "find", "arguments": {"keywords": ["x"]}},
            },
        ]
        with tempfile.TemporaryDirectory() as workdir:
            done = self.server(
                "".join(json.dumps(frame) + "\n" for frame in frames), workdir
            )
        lines = [line for line in done.stdout.splitlines() if line]
        # Three requests and one notification: exactly three frames come back.
        self.assertEqual(len(lines), 3)
        self.assertEqual([json.loads(line)["id"] for line in lines], [1, 2, 3])
        self.assertEqual(done.returncode, 0)

    def test_a_query_with_no_graph_writes_nothing_to_stderr(self):
        # A tool reporting "no graph yet" is a normal answer, not a server fault.
        with tempfile.TemporaryDirectory() as workdir:
            done = self.server(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "find", "arguments": {"keywords": ["x"]}},
                    }
                )
                + "\n",
                workdir,
            )
        self.assertEqual(done.stderr, "")
        self.assertTrue(json.loads(done.stdout)["result"]["isError"])

    def test_a_malformed_line_is_skipped_rather_than_killing_the_server(self):
        with tempfile.TemporaryDirectory() as workdir:
            done = self.server(
                'not json\n{"jsonrpc":"2.0","id":7,"method":"ping"}\n', workdir
            )
        lines = [line for line in done.stdout.splitlines() if line]
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["id"], 7)

    def test_a_blank_line_is_skipped(self):
        with tempfile.TemporaryDirectory() as workdir:
            done = self.server('\n\n{"jsonrpc":"2.0","id":1,"method":"ping"}\n', workdir)
        self.assertEqual(len([line for line in done.stdout.splitlines() if line]), 1)

    def test_an_unknown_argument_exits_two_without_touching_stdout(self):
        with tempfile.TemporaryDirectory() as workdir:
            done = self.server("", workdir, args=("--wat", "1"))
        self.assertEqual(done.returncode, 2)
        self.assertEqual(done.stdout, "")

    def test_repo_can_be_pinned_with_a_flag(self):
        with tempfile.TemporaryDirectory() as pinned, tempfile.TemporaryDirectory() as elsewhere:
            root = Path(pinned)
            (root / ".codegraph").mkdir()
            (root / ".codegraph" / "graph.json").write_text(
                json.dumps(
                    {
                        "project": "pinned",
                        "nodes": [
                            {"path": "found-me", "kind": "MODULE", "title": "T", "file": None}
                        ],
                        "edges": [],
                    }
                ),
                encoding="utf-8",
            )
            done = self.server(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "find", "arguments": {"keywords": ["found"]}},
                    }
                )
                + "\n",
                elsewhere,
                args=("--repo", str(root)),
            )
        self.assertIn("found-me", json.loads(done.stdout)["result"]["content"][0]["text"])


class Tools(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)

    def call(self, name, arguments=None):
        return mcp_server.call_tool({"name": name, "arguments": arguments or {}}, self.root)

    def first_delta(self):
        return {
            "schema_version": 1,
            "adds": {
                "nodes": [
                    {"path": "backend", "kind": "MODULE", "title": "Backend", "file": "src/"}
                ],
                "edges": [],
            },
        }

    def test_querying_with_no_graph_is_an_error_that_says_what_to_do(self):
        result = self.call("find", {"keywords": ["anything"]})
        self.assertTrue(result["isError"])
        self.assertIn("bootstrap", text_of(result))

    def test_an_unknown_path_is_guidance_not_a_tool_failure(self):
        # Flagging this as an error sends the reader back to grepping, which is the one
        # outcome this tool exists to prevent. The suggestions are the payload.
        self.call("apply_delta", {"delta": self.first_delta()})
        result = self.call("show", {"path": "src/backend"})
        self.assertNotIn("isError", result)
        self.assertIn("not a file path", text_of(result))

    def test_show_without_a_path_explains_itself(self):
        self.assertIn("node path", text_of(self.call("show", {})))

    def test_a_valid_delta_creates_the_store(self):
        self.call("apply_delta", {"delta": self.first_delta()})
        store = json.loads((self.root / ".codegraph" / "graph.json").read_text())
        self.assertEqual([n["path"] for n in store["nodes"]], ["backend"])

    def test_a_rejected_delta_writes_nothing_at_all(self):
        broken = self.first_delta()
        broken["adds"]["edges"] = [{"from": "backend", "to": "ghost", "kind": "USES"}]
        result = self.call("apply_delta", {"delta": broken})
        self.assertIn("rejected", text_of(result).lower())
        self.assertFalse((self.root / ".codegraph").exists())

    def test_a_delta_passed_as_a_json_string_is_accepted(self):
        self.call("apply_delta", {"delta": json.dumps(self.first_delta())})
        self.assertTrue((self.root / ".codegraph" / "graph.json").is_file())

    def test_a_delta_that_is_not_json_is_an_error(self):
        self.assertTrue(self.call("apply_delta", {"delta": "{not json"})["isError"])

    def test_the_first_delta_must_say_where_each_node_lives(self):
        naked = self.first_delta()
        del naked["adds"]["nodes"][0]["file"]
        self.assertIn("has no `file`", text_of(self.call("apply_delta", {"delta": naked})))

    def test_a_later_delta_does_not_need_file(self):
        self.call("apply_delta", {"delta": self.first_delta()})
        second = {
            "schema_version": 1,
            "adds": {"nodes": [{"path": "backend/store", "kind": "CLASS", "title": "S"}]},
        }
        self.assertNotIn("rejected", text_of(self.call("apply_delta", {"delta": second})).lower())

    def test_a_delta_is_queryable_immediately(self):
        self.call("apply_delta", {"delta": self.first_delta()})
        self.assertIn("backend", text_of(self.call("find", {"keywords": ["backend"]})))

    def test_skipped_nodes_are_reported_alongside_the_answer(self):
        (self.root / ".codegraph").mkdir()
        (self.root / ".codegraph" / "graph.json").write_text(
            json.dumps(
                {
                    "project": "demo",
                    "nodes": [{"path": "a", "kind": "MODULE", "title": "A"}, {"kind": "MODULE"}],
                    "edges": [],
                }
            ),
            encoding="utf-8",
        )
        self.assertIn("1 node(s) were skipped", text_of(self.call("find", {"keywords": ["a"]})))


class Bootstrap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)

    def guide(self):
        return text_of(mcp_server.call_tool({"name": "bootstrap", "arguments": {}}, self.root))

    def test_it_explains_what_to_draw_and_where_the_result_goes(self):
        guide = self.guide()
        self.assertIn("MODULE", guide)
        self.assertIn(".codegraph/graph.json", guide)

    def test_it_never_names_the_kinds_it_does_not_want_drawn(self):
        # Options offered in a list get used. Naming all three kinds here collapses the
        # output to one node per file — the directory tree wearing a graph's clothes.
        guide = self.guide()
        self.assertNotIn("CLASS", guide)
        self.assertNotIn("INTERFACE", guide)

    def test_it_refuses_to_redraw_an_existing_graph(self):
        mcp_server.call_tool(
            {
                "name": "apply_delta",
                "arguments": {
                    "delta": {
                        "schema_version": 1,
                        "adds": {
                            "nodes": [
                                {"path": "a", "kind": "MODULE", "title": "A", "file": "src/"}
                            ]
                        },
                    }
                },
            },
            self.root,
        )
        self.assertIn("already has a design graph", self.guide())


class RepoRoot(unittest.TestCase):
    def test_a_git_checkout_resolves_to_its_top_level(self):
        with tempfile.TemporaryDirectory() as workdir:
            root = Path(workdir).resolve()
            subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
            nested = root / "deep" / "deeper"
            nested.mkdir(parents=True)
            self.assertEqual(mcp_server.repo_root(nested), root)

    def test_outside_a_checkout_the_answer_still_contains_the_start(self):
        # A temp dir can itself sit inside somebody's checkout, so only the containment
        # relation is safe to assert.
        with tempfile.TemporaryDirectory() as workdir:
            root = Path(workdir).resolve()
            self.assertTrue(str(root).startswith(str(mcp_server.repo_root(root))))


if __name__ == "__main__":
    unittest.main()
