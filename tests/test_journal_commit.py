"""deploy/journal-commit.sh against real throwaway git repositories.

The journal service failed every scheduled run from the 2026-08-17 cutover
to 2026-09-21 and said so only in `docker logs`, under a message that named
the wrong cause. These pin the three things that would have surfaced it:
the failure lands in logs/paper-*.log as a CRITICAL, an unreachable origin
is not called a divergence, and unpushed commits are retried.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

# deploy/upgrade.sh runs this suite inside the candidate *engine* image,
# which deliberately ships no git client (the engine holds no git
# credential — see docs/architecture.md); v1.4.2's upgrade failed on
# exactly that. The script under test only ever runs in the journal image.
pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="needs git and bash (absent from the engine image by design)",
)

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "journal-commit.sh"
IDENT = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def git(cwd, *args):
    return subprocess.run(
        ["git", *IDENT, *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", origin], check=True)
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "init", "-q", "-b", "main", checkout], check=True)
    git(checkout, "remote", "add", "origin", str(origin))
    (checkout / "README").write_text("x\n")
    (checkout / ".gitignore").write_text("logs/\n")
    git(checkout, "add", "-A")
    git(checkout, "commit", "-q", "-m", "init")
    git(checkout, "push", "-q", "origin", "main")
    return origin, checkout


def run_script(checkout):
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env={**os.environ, "JOURNAL_REPO": str(checkout), "PAPER_LOG_DIR": ""},
        capture_output=True, text=True,
    )
    logs = sorted((checkout / "logs").glob("paper-*.log"))
    return result.returncode, "".join(p.read_text() for p in logs)


def write_report(checkout, name="2026-09-21.md"):
    path = checkout / "reports" / "paper" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Paper\n")


def test_commits_and_pushes_new_reports(repos):
    origin, checkout = repos
    write_report(checkout)
    rc, log = run_script(checkout)
    assert rc == 0
    assert git(origin, "log", "-1", "--format=%s") == "chore(journal): paper reports"
    assert "job=journal" in log and "pushed 1 commit(s)" in log and "=== end rc=0 ===" in log
    assert "CRITICAL" not in log


def test_quiet_day_is_a_clean_no_op(repos):
    _, checkout = repos
    rc, log = run_script(checkout)
    assert rc == 0 and "nothing to commit" in log and "CRITICAL" not in log


def test_unreachable_origin_is_a_critical_and_not_called_a_divergence(repos):
    _, checkout = repos
    git(checkout, "remote", "set-url", "origin", str(checkout.parent / "missing.git"))
    write_report(checkout)
    rc, log = run_script(checkout)
    assert rc == 1
    assert "CRITICAL: git fetch failed" in log
    assert "has diverged" not in log
    assert "CRITICAL: job=journal exited rc=1" in log


def test_divergence_is_refused_and_names_the_local_commits(repos):
    origin, checkout = repos
    other = checkout.parent / "dev"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    (other / "dev.txt").write_text("dev\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "dev work")
    git(other, "push", "-q", "origin", "main")
    write_report(checkout)
    git(checkout, "add", "-A")
    git(checkout, "commit", "-q", "-m", "auto: journal")
    before = git(checkout, "rev-parse", "HEAD")
    rc, log = run_script(checkout)
    assert rc == 1
    assert "has diverged" in log and "auto: journal" in log
    assert git(checkout, "rev-parse", "HEAD") == before  # never merged, never rebased


def test_unpushed_commits_are_retried_on_a_quiet_day(repos):
    origin, checkout = repos
    write_report(checkout)
    git(checkout, "add", "-A")
    git(checkout, "commit", "-q", "-m", "chore(journal): paper reports")  # last night's failed push
    rc, log = run_script(checkout)
    assert rc == 0
    assert "nothing to commit" in log and "pushed 1 commit(s)" in log
    assert git(origin, "rev-parse", "main") == git(checkout, "rev-parse", "HEAD")
