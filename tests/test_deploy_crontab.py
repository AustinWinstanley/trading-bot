"""Static validation of deploy/crontab against scripts/paper.sh's own job
table — the failure mode this guards against is the one the old host-cron
EDT/EST convention warned about repeatedly: a crontab line referencing a
job paper.sh doesn't recognize, a slot argument that doesn't match its own
schedule, or (now that supercronic replaces the old two-line-per-slot
pattern) a missing CRON_TZ line silently reverting every ET-sensitive job
to UTC. None of these would fail loudly at container startup — supercronic
happily schedules a job with a wrong slot argument or a nonexistent job
name; the job itself would just always skip or always fail. See
docs/architecture.md for the job/lock table this file's comments describe
in prose.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CRONTAB = REPO_ROOT / "deploy" / "crontab"
PAPER_SH = REPO_ROOT / "scripts" / "paper.sh"

# scripts/paper.sh's case statement in its job-dispatch block (the second
# `case "$JOB" in`) is the ground truth for what a valid job name is.
_CASE_ARM_RE = re.compile(r"^\s*([a-zA-Z0-9_]+)\)")

CRON_LINE_RE = re.compile(
    r"^(?P<min>\S+)\s+(?P<hour>\S+)\s+(?P<dom>\S+)\s+(?P<mon>\S+)\s+(?P<dow>\S+)\s+"
    r"(?P<command>.+)$"
)


def _known_jobs_from_paper_sh() -> set[str]:
    text = PAPER_SH.read_text()
    # The dispatch case statement is the second `case "$JOB" in` block (the
    # first assigns LOCK/TIMEOUT); both list the same job names, but the
    # dispatch block is what actually determines "unknown job" at runtime.
    dispatch_start = text.index("case \"$JOB\" in", text.index("case \"$JOB\" in") + 1)
    dispatch_block = text[dispatch_start:text.index("esac", dispatch_start)]
    jobs = set()
    for line in dispatch_block.splitlines():
        m = _CASE_ARM_RE.match(line)
        if m and m.group(1) != "*":
            jobs.add(m.group(1))
    return jobs


def _parse_crontab():
    """Return (cron_tz, [(fields_dict, job, slot_arg_or_None), ...])."""
    cron_tz = None
    entries = []
    for raw_line in CRONTAB.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("CRON_TZ="):
            cron_tz = line.split("=", 1)[1]
            continue
        m = CRON_LINE_RE.match(line)
        assert m, f"line does not parse as a 5-field cron entry: {raw_line!r}"
        command = m.group("command")
        parts = command.split()
        # e.g. "/app/scripts/paper.sh daily 09:47" or "... weekly" (no slot).
        assert parts[0] == "/app/scripts/paper.sh", (
            f"crontab line invokes something other than paper.sh: {raw_line!r}"
        )
        job = parts[1]
        slot = parts[2] if len(parts) > 2 else None
        fields = {
            "min": m.group("min"), "hour": m.group("hour"),
            "dom": m.group("dom"), "mon": m.group("mon"), "dow": m.group("dow"),
        }
        entries.append((fields, job, slot))
    return cron_tz, entries


def test_cron_tz_is_set_to_eastern():
    cron_tz, _ = _parse_crontab()
    assert cron_tz == "America/New_York", (
        "deploy/crontab must set CRON_TZ=America/New_York — without it, "
        "supercronic falls back to the container's local timezone (UTC in "
        "the shipped image) and every ET-sensitive job silently fires at "
        "the wrong wall-clock time"
    )


def test_every_crontab_job_is_a_known_paper_sh_job():
    known = _known_jobs_from_paper_sh()
    assert known, "expected to find at least one job in scripts/paper.sh's dispatch case"
    _, entries = _parse_crontab()
    assert entries, "expected deploy/crontab to contain at least one job line"
    for fields, job, _slot in entries:
        assert job in known, (
            f"deploy/crontab references job {job!r}, which is not a case arm "
            f"in scripts/paper.sh's dispatch block (known jobs: {sorted(known)})"
        )


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def test_slotted_jobs_carry_a_slot_matching_their_own_schedule():
    """A market-hours job's slot argument must equal the ET time its own
    cron fields fire at — scripts/paper.sh's guard compares "now" against
    this argument and silently no-ops if they diverge by more than five
    minutes, so a copy-paste error here reads as a job that mysteriously
    never runs, not a loud failure."""
    _, entries = _parse_crontab()
    for fields, job, slot in entries:
        if slot is None:
            continue
        # Only single, concrete hour/minute values are checked — jobs with
        # a slot argument are always scheduled at one specific time here.
        assert re.fullmatch(r"\d{1,2}", fields["min"]), (
            f"{job}: slotted job's cron minute field must be a literal "
            f"number, not {fields['min']!r}"
        )
        assert re.fullmatch(r"\d{1,2}", fields["hour"]), (
            f"{job}: slotted job's cron hour field must be a literal "
            f"number, not {fields['hour']!r}"
        )
        scheduled = f"{int(fields['hour']):02d}:{int(fields['min']):02d}"
        assert scheduled == slot, (
            f"{job}: cron fields fire at {scheduled} ET but the slot "
            f"argument says {slot} — scripts/paper.sh's guard will treat "
            "every firing as a mismatch and this job will never run"
        )


def test_no_duplicate_job_slot_pairs():
    """The old EDT/EST convention needed two lines per slot on purpose;
    supercronic needs exactly one. A duplicate here means either a
    leftover copy-paste from that pattern or two jobs racing the same
    lock unnecessarily."""
    _, entries = _parse_crontab()
    seen = set()
    for _fields, job, slot in entries:
        key = (job, slot)
        assert key not in seen, f"duplicate crontab entry for job={job} slot={slot}"
        seen.add(key)


def test_unslotted_jobs_are_the_documented_ones():
    """Jobs with no market-clock sensitivity stay unslotted deliberately
    (see scripts/paper.sh's own comments) — this pins the current set so
    an accidental missing slot argument on a market-hours job is caught
    rather than silently accepted as 'must be unslotted on purpose'."""
    _, entries = _parse_crontab()
    unslotted = {job for _fields, job, slot in entries if slot is None}
    assert unslotted <= {"weekly", "momls2x", "shadows2x", "iwmfwd"}, (
        f"unexpected unslotted job(s): {unslotted - {'weekly', 'momls2x', 'shadows2x', 'iwmfwd'}} "
        "— market-hours jobs need a slot argument; if this job is genuinely "
        "not market-clock sensitive, add it to this test's allowed set"
    )
