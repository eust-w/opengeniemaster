# Changelog

This file records every notable change to OpenGenieMaster. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/). While the version is 0.x, a minor release may contain breaking changes.

## [Unreleased]

### Added

- `ogm doctor`: pre-flight checks of the installation and state directory and, with `--fleet`, of every robot service (reachable, token accepted, protocol version, robot id, calibration). Exits non-zero when a check fails; `--json` for machine-readable output.
- `ExecutionAdapter.health()` and a `retries` field per robot in fleet files.
- `ogm robot-service --token-env` (default `OGM_ROBOT_TOKEN`) so that the token does not appear in the process list.
- Ruff lint configuration and a CI lint job.
- Event log rotation: a persistent `events.jsonl` rotates at 64 MiB and keeps 5 backups; sequence numbers continue across rotations. `EventLog.load(..., include_rotated=True)` and `ogm replay` / `ogm dataset --include-rotated` read the rotated files too. The in-memory copy keeps at most 50,000 recent rows.

### Changed

- `HttpAdapter` retries idempotent calls (observe, check, state, health, stop, resume) with exponential backoff on transport errors and 5xx answers; `execute` is never retried. `state` is cached for 0.5 s, so the harness clock no longer costs one HTTP request per robot per event.
- `ogm doctor` probes all robots concurrently, so unreachable robots no longer add up their timeouts.
- The README PyPI badge no longer points at a package that has not been published.
- `ExecutionAdapter.stop()` / `resume()` return whether the robot confirmed. `eep_robot_stop` reports `confirmed` and a warning when the stop was not confirmed (the robot stays blocked in the harness); an unconfirmed resume leaves the robot stopped.

### Fixed

- A safety stop that failed to reach the robot was silently ignored.
- Malformed JSON from a robot service crashed `state()`; it is now reported as an adapter error.
- Robot service: bearer tokens are compared in constant time, bodies over 8 MiB get 413, an invalid `Content-Length` gets 400 instead of an exception, and bodies from unauthenticated clients are not parsed. It warns when listening beyond localhost without a token.

## [0.1.0] - 2026-10-30

First public release.

### Added

- `open-eep/0.1`: an open Embodied Execution Protocol data model with the four published contracts (capability, guidance, feedback, continuity), JSON schemas, and error codes.
- Harness core:
  - task continuity and multi-robot dispatch;
  - shared admission with a person-in-workspace safety interlock;
  - completion criteria that override the policy's own success report;
  - a guidance builder that adds reference images, target boxes, placement points, and detailed language;
  - a recovery playbook, a scripted orchestrator, and idle-time practice.
- Memory: execution records, versioned long-term memory with diff and rollback, and rule-based and LLM reflectors. Several processes can share one state directory.
- Agents: EEP served as an MCP stdio server for Claude Code and Codex, and an optional built-in Claude agent.
- Embodiment: robot profiles and skill bindings; simulator, HTTP, and replay execution adapters; a robot-side HTTP execution service; a human-in-the-loop example backend.
- Simulation and evaluation:
  - a semantic multi-robot simulator with six benchmark scenarios and a 24-hour store deployment;
  - five experiments;
  - replay-based offline regression tests;
  - policy-training data export.
- The `ogm` command line, docs (architecture mapping, EEP specification, benchmarks, research notes), an English README, community files, CI, and a release workflow that publishes to PyPI through Trusted Publishing.

[Unreleased]: https://github.com/eust-w/opengeniemaster/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/eust-w/opengeniemaster/releases/tag/v0.1.0
