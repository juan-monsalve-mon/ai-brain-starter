#!/usr/bin/env python3
"""
test_drift_audit_frontmatter.py — the generated Drift Audit must parse as YAML.

drift-detection.py writes `Meta/Drift Audit.md` with a frontmatter block. The
`purpose:` value carries both ": " and "'":

    purpose: Multi-edit drift audit. ... Include: '*.md'.

An unquoted YAML scalar containing ": " is read as a nested mapping, so
yaml.safe_load raised "mapping values are not allowed here" and the file was
invisible to every consumer that parses frontmatter — Dataview queries, the
metadata extractors, any vault-wide frontmatter audit. Nothing errored; the
file simply stopped counting. A generator that emits unparseable frontmatter
and a generator that works report the same green.

This suite runs the real script against a throwaway git vault and asserts the
frontmatter it produces round-trips through yaml.safe_load, including for an
--include value chosen to break naive quoting.

Run:
  python3 scripts/test_drift_audit_frontmatter.py
  python3 scripts/test_drift_audit_frontmatter.py --verbose

Exits 0 on all-pass, 1 on any failure. CI-runnable.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "drift-detection.py"

try:
    import yaml  # type: ignore
except ImportError:
    print("ERROR: PyYAML required. Install with: python3 -m pip install --user pyyaml")
    sys.exit(1)


def git(vault: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(vault),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def build_vault(vault: Path, filename: str = "Note.md", edits: int = 6) -> None:
    """A git vault with one file edited enough times to land in the audit."""
    git(vault, "init", "-q")
    git(vault, "config", "user.email", "test@example.com")
    git(vault, "config", "user.name", "Test")
    (vault / "Meta").mkdir(parents=True, exist_ok=True)
    note = vault / filename
    for i in range(edits):
        note.write_text(f"# Note\n\nrevision {i}\n", encoding="utf-8")
        git(vault, "add", filename)
        git(vault, "commit", "-q", "-m", f"edit {i}")


def run_script(vault: Path, include: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, VAULT_ROOT=str(vault))
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--include", include, "--min-edits", "2"],
        cwd=str(vault),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def read_frontmatter(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise AssertionError("generated file has no frontmatter block")
    end = text.find("\n---", 3)
    if end == -1:
        raise AssertionError("frontmatter block is never closed")
    return text[3:end]


# (case name, filename to create, --include glob). The glob is also a git
# pathspec, so it has to actually match the file or the script exits before
# writing anything.
CASES = [
    ("default glob", "Note.md", "*.md"),
    # An apostrophe inside the value: the purpose line wraps --include in
    # single quotes, so this is the shape that a naive single-quoted scalar
    # cannot survive. json.dumps can.
    ("glob with an apostrophe", "Note's.md", "*'s.md"),
]


def run_case(name: str, filename: str, include: str, verbose: bool) -> bool:
    tmp = Path(tempfile.mkdtemp(prefix="drift-audit-test-"))
    try:
        build_vault(tmp, filename)
        proc = run_script(tmp, include)
        out = tmp / "Meta" / "Drift Audit.md"
        if not out.exists():
            print(f"  FAIL {name}: no Meta/Drift Audit.md written")
            if verbose:
                print("    stdout:", proc.stdout.strip()[:400])
                print("    stderr:", proc.stderr.strip()[:400])
            return False

        fm = read_frontmatter(out)
        try:
            data = yaml.safe_load(fm)
        except yaml.YAMLError as e:
            print(f"  FAIL {name}: frontmatter is not valid YAML")
            print(f"    {str(e).splitlines()[0]}")
            if verbose:
                print("    frontmatter was:")
                for line in fm.strip().splitlines():
                    print("      " + line)
            return False

        if not isinstance(data, dict):
            print(f"  FAIL {name}: frontmatter parsed to {type(data).__name__}, not a mapping")
            return False
        for key in ("creationDate", "type", "purpose", "generator"):
            if key not in data:
                print(f"  FAIL {name}: frontmatter lost the '{key}' key")
                return False
        if include not in str(data["purpose"]):
            print(f"  FAIL {name}: purpose does not round-trip the --include value")
            print(f"    got: {data['purpose']!r}")
            return False

        if verbose:
            print(f"    purpose -> {data['purpose']!r}")
        print(f"  ok   {name}")
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if not SCRIPT.exists():
        print(f"ERROR: {SCRIPT} not found")
        return 1

    print(f"drift-detection.py frontmatter: {len(CASES)} case(s)")
    failures = 0
    for name, filename, include in CASES:
        if not run_case(name, filename, include, args.verbose):
            failures += 1

    print()
    if failures:
        print(f"{failures} of {len(CASES)} case(s) FAILED")
        return 1
    print(f"all {len(CASES)} case(s) passed")
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
