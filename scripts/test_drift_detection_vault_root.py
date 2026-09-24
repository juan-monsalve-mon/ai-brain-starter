#!/usr/bin/env python3
"""
test_drift_detection_vault_root.py — drift-detection.py must not let a
globally-exported VAULT_ROOT silently outrank the vault you actually ran it
from.

drift-detection.py's `git()` helper runs with `cwd=str(VAULT_ROOT)`, and its
report is written under `VAULT_ROOT / Meta`. The naive
`os.environ.get("VAULT_ROOT") or os.getcwd()` read meant that once VAULT_ROOT
is set anywhere -- a shell profile, or Claude Code's settings.json `env`
block, which every hook subprocess inherits -- it ALWAYS won, even when the
caller `cd`-ed into a different git-tracked vault and ran the script there.
Both the git history scanned and the file written would silently resolve
against the wrong vault, with no error (scripts/check-vault-root-reads.py's
SEV-B-cwd bug class).

This suite runs the real script as a subprocess against two throwaway git
vaults -- one as cwd, one as $VAULT_ROOT -- and asserts:

  1. mismatch (both are git repos, neither forced): the script audits cwd,
     not $VAULT_ROOT, and warns on stderr.
  2. VAULT_ROOT_FORCE=1: the script audits $VAULT_ROOT, matching the
     pre-existing (deliberate override) behavior.
  3. cwd is NOT a git repo but $VAULT_ROOT is: the script falls back to
     $VAULT_ROOT with no warning -- the pre-existing behavior for this case
     is unchanged.
  4. $VAULT_ROOT unset: the script uses cwd (today's documented default).

Run:
  python3 scripts/test_drift_detection_vault_root.py
  python3 scripts/test_drift_detection_vault_root.py --verbose

Exits 0 on all-pass, 1 on any failure. CI-runnable. No third-party deps.
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
    vault.mkdir(parents=True, exist_ok=True)
    git(vault, "init", "-q")
    git(vault, "config", "user.email", "test@example.com")
    git(vault, "config", "user.name", "Test")
    note = vault / filename
    for i in range(edits):
        note.write_text(f"# Note\n\nrevision {i}\n", encoding="utf-8")
        git(vault, "add", filename)
        git(vault, "commit", "-q", "-m", f"edit {i}")


def run_script(cwd: Path, extra_env: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("VAULT_ROOT", None)
    env.pop("VAULT_ROOT_FORCE", None)
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--min-edits", "2"],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )


def check(name: str, condition: bool, detail: str, verbose: bool) -> bool:
    if condition:
        print(f"  ok   {name}")
        return True
    print(f"  FAIL {name}: {detail}")
    return False


def case_mismatch_prefers_cwd(verbose: bool) -> bool:
    tmp = Path(tempfile.mkdtemp(prefix="drift-vaultroot-test-"))
    try:
        cwd_vault = tmp / "cwd-vault"
        env_vault = tmp / "env-vault"
        build_vault(cwd_vault, "CwdNote.md")
        build_vault(env_vault, "EnvNote.md")

        proc = run_script(cwd_vault, {"VAULT_ROOT": str(env_vault)})
        ok = True
        cwd_report = cwd_vault / "Meta" / "Drift Audit.md"
        env_report = env_vault / "Meta" / "Drift Audit.md"
        ok &= check(
            "mismatch: writes under cwd, not VAULT_ROOT",
            cwd_report.exists() and not env_report.exists(),
            f"cwd_report exists={cwd_report.exists()} env_report exists={env_report.exists()}",
            verbose,
        )
        if cwd_report.exists():
            body = cwd_report.read_text(encoding="utf-8")
            ok &= check(
                "mismatch: audit content is the cwd vault's own file",
                "CwdNote" in body and "EnvNote" not in body,
                f"body head: {body[:200]!r}",
                verbose,
            )
        ok &= check(
            "mismatch: warns on stderr naming the ignored VAULT_ROOT",
            "VAULT_ROOT" in proc.stderr and str(env_vault) in proc.stderr,
            f"stderr: {proc.stderr.strip()[:300]!r}",
            verbose,
        )
        if verbose:
            print(f"    stdout: {proc.stdout.strip()[:200]!r}")
        return ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def case_force_prefers_vault_root(verbose: bool) -> bool:
    tmp = Path(tempfile.mkdtemp(prefix="drift-vaultroot-test-"))
    try:
        cwd_vault = tmp / "cwd-vault"
        env_vault = tmp / "env-vault"
        build_vault(cwd_vault, "CwdNote.md")
        build_vault(env_vault, "EnvNote.md")

        run_script(cwd_vault, {"VAULT_ROOT": str(env_vault), "VAULT_ROOT_FORCE": "1"})
        env_report = env_vault / "Meta" / "Drift Audit.md"
        return check(
            "VAULT_ROOT_FORCE=1: writes under VAULT_ROOT",
            env_report.exists(),
            f"env_report exists={env_report.exists()}",
            verbose,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def case_non_git_cwd_falls_back(verbose: bool) -> bool:
    tmp = Path(tempfile.mkdtemp(prefix="drift-vaultroot-test-"))
    try:
        non_git_cwd = tmp / "not-a-repo"
        non_git_cwd.mkdir(parents=True)
        env_vault = tmp / "env-vault"
        build_vault(env_vault, "EnvNote.md")

        proc = run_script(non_git_cwd, {"VAULT_ROOT": str(env_vault)})
        env_report = env_vault / "Meta" / "Drift Audit.md"
        ok = check(
            "non-git cwd: falls back to VAULT_ROOT",
            env_report.exists(),
            f"env_report exists={env_report.exists()} stderr={proc.stderr.strip()[:200]!r}",
            verbose,
        )
        ok &= check(
            "non-git cwd: no spurious mismatch warning",
            "WARNING: VAULT_ROOT" not in proc.stderr,
            f"stderr: {proc.stderr.strip()[:200]!r}",
            verbose,
        )
        return ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def case_unset_uses_cwd(verbose: bool) -> bool:
    tmp = Path(tempfile.mkdtemp(prefix="drift-vaultroot-test-"))
    try:
        cwd_vault = tmp / "cwd-vault"
        build_vault(cwd_vault, "CwdNote.md")

        run_script(cwd_vault, {})
        cwd_report = cwd_vault / "Meta" / "Drift Audit.md"
        return check(
            "VAULT_ROOT unset: uses cwd",
            cwd_report.exists(),
            f"cwd_report exists={cwd_report.exists()}",
            verbose,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


CASES = [
    ("mismatch prefers cwd", case_mismatch_prefers_cwd),
    ("VAULT_ROOT_FORCE=1 prefers VAULT_ROOT", case_force_prefers_vault_root),
    ("non-git cwd falls back to VAULT_ROOT", case_non_git_cwd_falls_back),
    ("VAULT_ROOT unset uses cwd", case_unset_uses_cwd),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if not SCRIPT.exists():
        print(f"ERROR: {SCRIPT} not found")
        return 1

    print(f"drift-detection.py VAULT_ROOT resolution: {len(CASES)} case(s)")
    failures = 0
    for name, fn in CASES:
        if not fn(args.verbose):
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
