# Changelog

This file records every notable change to OpenGenieMaster. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/). While the version is 0.x, a minor release may contain breaking changes.

## [Unreleased]

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
