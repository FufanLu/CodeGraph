#!/usr/bin/env python3
"""Run every test in this skill with one command.

    python3 skills/code-graph/run_tests.py

Works from any directory, standard library only. `-v` lists each case, `-q` shows only the
result.

# Why the layout is checked before anything runs

The failure that actually hurts is not "a test went red" — it is "a test never ran", which
looks identical to everything passing. `unittest discover` takes one starting directory and
silently collects nothing from a file it does not match, so every `test_*.py` under the skill
is located first and the run is refused if one of them falls outside the collected set.

Exit codes: **2 = this runner is broken, 1 = tests genuinely failed.** Keeping them apart
matters, because "nothing was checked" must not look like "everything passed".
"""

import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent

# The single list of where tests live. A new directory goes here — without it, check_layout()
# stops the run rather than letting the new tests quietly not run.
TEST_DIRS = (SKILL_DIR / "tests",)


def check_layout():
    """Return every file that should be collected but would not be."""
    problems = []
    known = ", ".join(f"{d.relative_to(SKILL_DIR)}/" for d in TEST_DIRS)

    for path in sorted(SKILL_DIR.rglob("test_*.py")):
        if path.parent not in TEST_DIRS:
            problems.append(
                f"{path.relative_to(SKILL_DIR)} is not in a collected directory — move it "
                f"into {known}, or add its directory to TEST_DIRS"
            )

    # discover's default pattern is `test_*.py`. A file named `*_test.py` is never collected
    # and looks entirely normal sitting in the directory, so it has to be called out.
    for path in sorted(SKILL_DIR.rglob("*_test.py")):
        problems.append(
            f"{path.relative_to(SKILL_DIR)} must be named test_*.py or discover will skip it"
        )

    return problems


def main(argv):
    problems = check_layout()
    if problems:
        print("Test layout is wrong — these tests would not run:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2

    suite = unittest.TestSuite()
    for test_dir in TEST_DIRS:
        found = unittest.TestLoader().discover(str(test_dir), top_level_dir=str(test_dir))
        print(f"{str(test_dir.relative_to(SKILL_DIR)) + '/':<16} {found.countTestCases():>4}")
        suite.addTests(found)
    # flush: unittest writes results to stderr while these lines go to stdout, and a
    # block-buffered pipe would otherwise print the counts after the results.
    print(f"{'total':<16} {suite.countTestCases():>4}\n", flush=True)

    verbosity = 2 if "-v" in argv else 0 if "-q" in argv else 1
    return 0 if unittest.TextTestRunner(verbosity=verbosity).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
