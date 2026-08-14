"""Static enforcement of the trust-tier boundaries deploy/docker-compose.yml
documents in prose: engine is the only service with broker credentials or
write access, dashboard/mcp-server stay secretless and read-only. See
tests/dashboard/test_safety.py and tests/mcp_server/test_mcp_safety.py for
the equivalent guarantee enforced at the Python-import level; this is the
compose-file-level sibling — a YAML edit that gave the dashboard service an
env_file, for instance, would break the Python-level test's *code* boundary
not at all, since the leak would be entirely a runtime/deployment mistake
outside the Python source these other tests inspect.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "deploy" / "docker-compose.yml"

READ_ONLY_SERVICES = ("dashboard", "mcp-server")
SECRET_BEARING_SERVICES = ("engine", "journal")


def _load_compose() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text())


def _volume_targets(volumes: list) -> list[tuple[str, bool]]:
    """Return [(target_path, is_read_only), ...] for a service's `volumes:`
    list, handling both the short "host:container[:ro]" string form and the
    long mapping form."""
    out = []
    for v in volumes:
        if isinstance(v, str):
            parts = v.split(":")
            target = parts[1] if len(parts) > 1 else parts[0]
            ro = len(parts) > 2 and parts[2] == "ro"
            out.append((target, ro))
        elif isinstance(v, dict):
            out.append((v.get("target", ""), bool(v.get("read_only", False))))
    return out


def test_read_only_services_have_no_env_file_or_environment():
    """dashboard/mcp-server must stay structurally unable to read broker
    credentials — no env_file pointing at .env, no inline environment
    block that could name one."""
    compose = _load_compose()
    for name in READ_ONLY_SERVICES:
        service = compose["services"][name]
        assert "env_file" not in service, (
            f"{name} must never have env_file — it is documented (SECURITY.md, "
            "docs/architecture.md) as structurally secretless"
        )
        assert "environment" not in service, (
            f"{name} must never have an inline environment block for the same reason"
        )


def test_read_only_services_mount_everything_read_only():
    compose = _load_compose()
    for name in READ_ONLY_SERVICES:
        service = compose["services"][name]
        volumes = service.get("volumes", [])
        assert volumes, f"expected {name} to have at least one volume mount"
        for target, ro in _volume_targets(volumes):
            assert ro, f"{name} mounts {target} read-write — it must be :ro"


def test_only_designated_services_hold_secrets():
    """engine and journal are the only services with a real credential
    (broker keys via env_file; a git deploy key via `secrets:`,
    respectively) — anything else gaining one is a trust-boundary
    regression, not a feature."""
    compose = _load_compose()
    for name, service in compose["services"].items():
        has_env_file = "env_file" in service
        has_secrets = bool(service.get("secrets"))
        if has_env_file or has_secrets:
            assert name in SECRET_BEARING_SERVICES, (
                f"{name} has {'env_file' if has_env_file else 'secrets'} but is not "
                f"in the documented secret-bearing set {SECRET_BEARING_SERVICES}"
            )


def test_engine_env_file_points_at_the_real_env():
    compose = _load_compose()
    env_file = compose["services"]["engine"]["env_file"]
    paths = [env_file] if isinstance(env_file, str) else env_file
    assert any(str(p).endswith("/.env") or str(p) == "../.env" for p in paths), (
        f"engine's env_file should reference the repo-root .env, got {paths!r}"
    )


def test_engine_and_journal_are_the_only_writers():
    """state/reports/logs are the engine's runtime output and the paper-
    trading record — nothing else should mount them read-write. journal
    additionally mounts the whole checkout read-write (it needs .git/ to
    commit and push), which is intentionally broader than engine's three
    directories and is its own, narrower trust tier (a git deploy key,
    never a broker credential — see docs/architecture.md)."""
    compose = _load_compose()
    for name, service in compose["services"].items():
        for target, ro in _volume_targets(service.get("volumes", [])):
            if ro:
                continue
            assert name in SECRET_BEARING_SERVICES, (
                f"{name} mounts {target} read-write but is not one of the "
                f"documented writer services {SECRET_BEARING_SERVICES}"
            )


def test_ports_are_only_published_by_the_read_only_services():
    """engine and journal never listen on a port — they have no reason to
    accept inbound connections, and publishing one would be a new,
    undocumented attack surface on the trusted tier."""
    compose = _load_compose()
    for name in SECRET_BEARING_SERVICES:
        assert "ports" not in compose["services"][name], (
            f"{name} must not publish any port"
        )
