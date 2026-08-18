#!/usr/bin/env python3
"""Two agents writing one repository's graph at the same time.

Not hypothetical: a repository is routinely worked on by more than one session, and every
one of them writes the same `.codegraph/graph.json`.

The failure that matters here is not a lost node. It is a **corrupt store**, which takes the
graph away from everybody at once and stays invisible until the next read — exactly the
shape of failure the rest of this skill is built to refuse.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
SERVER = SKILL / "scripts" / "mcp_server.py"
sys.path.insert(0, str(SKILL / "scripts"))

import graph as graph_lib  # noqa: E402

# Enough writers that overlap is certain rather than lucky.
WRITERS = 8


def frame(delta):
    return (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "apply_delta", "arguments": {"delta": delta}},
            }
        )
        + "\n"
    )


def add_node(path, title):
    return {
        "schema_version": 1,
        "adds": {
            "nodes": [{"path": path, "kind": "MODULE", "title": title, "file": "src/"}]
        },
    }


class ConcurrentWriters(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "src").mkdir()
        # The parent every writer will hang its node off, written serially first.
        self.assertIn("applied", self.apply(add_node("root", "Root")))

    def apply(self, delta):
        done = subprocess.run(
            [sys.executable, str(SERVER), "--repo", str(self.root)],
            input=frame(delta),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def race(self):
        """Run WRITERS deltas at once and return the store they left behind."""
        writers = [
            subprocess.Popen(
                [sys.executable, str(SERVER), "--repo", str(self.root)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(WRITERS)
        ]
        for index, writer in enumerate(writers):
            writer.stdin.write(frame(add_node(f"root/node-{index}", f"Node {index}")))
            writer.stdin.flush()
            writer.stdin.close()
        for writer in writers:
            self.assertEqual(writer.wait(timeout=60), 0)
            writer.stdout.close()
            writer.stderr.close()
        return (self.root / ".codegraph" / "graph.json").read_text(encoding="utf-8")

    def test_the_store_is_still_readable_afterwards(self):
        """True on every platform, because it does not depend on the lock.

        A shared temp name let two writers interleave their bytes into one file, and the
        atomic rename then published the mixture. Per-writer temp files fix that with no
        help from `fcntl`, which is why this assertion is not skipped anywhere.
        """
        try:
            json.loads(self.race())
        except json.JSONDecodeError as error:
            self.fail(f"concurrent writers corrupted the store: {error}")

    @unittest.skipIf(
        graph_lib.fcntl is None, "no flock here, so the lock degrades to nothing by design"
    )
    def test_no_writer_is_lost_where_the_store_can_be_locked(self):
        paths = {node["path"] for node in json.loads(self.race())["nodes"]}
        missing = sorted({f"root/node-{index}" for index in range(WRITERS)} - paths)
        self.assertFalse(missing, f"writes were lost: {missing}")

    def test_no_temporary_file_is_left_behind(self):
        # A shared temp name is what let two writers interleave into one file, so a
        # leftover one is worth catching on its own.
        self.apply(add_node("root/one", "One"))
        strays = sorted(p.name for p in (self.root / ".codegraph").glob("*.tmp"))
        self.assertEqual(strays, [])


if __name__ == "__main__":
    unittest.main()
