# Security policy

## Reporting a vulnerability

Please report security issues privately via GitHub's
["Report a vulnerability"](../../security/advisories/new) flow (Security
tab → Advisories) rather than opening a public issue. Include enough detail
to reproduce the problem; expect an initial response within a few days.

## Architecture and trust boundaries

This matters more than usual here because two of the three services in
this repo are **deliberately** built with no authentication at all — that
is a documented design choice, not an oversight, and it changes what
counts as a vulnerability report versus expected behavior.

| Service | Network exposure | Auth | Can place an order? |
|---|---|---|---|
| `engine` (trading scheduler) | none (no inbound ports) | holds broker credentials | **yes** |
| `dashboard` (`dashboard/`, port 8787) | LAN-only by convention | **none** | no — statically enforced |
| `mcp-server` (`mcp_server/`, port 8788) | LAN-only by convention | **none** | no — statically enforced |

- **`dashboard` and `mcp-server` are read-only by construction, not by
  policy.** Both are AST-statically tested
  (`tests/dashboard/test_safety.py`, `tests/mcp_server/test_mcp_safety.py`)
  to never import the modules that can reach a broker
  (`engine.execute`, `engine.data`, `scripts.run_daily`,
  `scripts.healthcheck`). Neither container has network access to `.env`
  or broker credentials — that's a Docker Compose mount boundary, not just
  an application-level check. `mcp-server`'s ad hoc SQL tool
  (`query_database`) only accepts `SELECT`/`WITH` against SQLite
  connections opened `mode=ro`.
- **Neither service has a login.** This is intentional — see
  `deploy/docker-compose.yml`'s comments. Anyone who can reach the host on
  its LAN sees live paper-account data (positions, equity, order history)
  with no gate at all. **If you expose either port beyond your own LAN —
  a public IP, a reverse proxy, a tunnel — you must add authentication
  first.** That is a deployment mistake this repo does not protect you
  from, not a bug in the repo.
- **`engine` is the only service with real broker access.** Its trading
  logic runs against Alpaca **paper** accounts as shipped; nothing in this
  repository is configured for live trading. The risk gate
  (`engine/risk.py`) is the load-bearing safety boundary for anything that
  proposes an order — it can reject or shrink a proposal but is asserted
  (at runtime, via `_assert_gate_invariants`) to never enlarge one or
  invent a symbol. A PR that weakens that invariant to accommodate a
  feature will be rejected regardless of the feature's merit; see
  `AGENTS.md`.

## What counts as a vulnerability report here

- A way for `dashboard` or `mcp-server` to mutate broker state, read
  `.env`, or otherwise escape their read-only design.
- A way for the risk gate to enlarge a proposal, invent a symbol, or
  bypass a documented stop/position/exposure control.
- Credential handling issues (e.g. a code path that would log or persist
  `.env` values).
- Standard dependency/supply-chain concerns (a vulnerable pinned package,
  a compromised base image).

**Not** a vulnerability report: "the dashboard has no login" or "the MCP
server has no auth" — both are documented, deliberate LAN-only design
choices covered above, not oversights.
