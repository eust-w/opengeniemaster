# Contributing to OpenGenieMaster

> 中文：欢迎用中文或英文提交 Issue 和 Pull Request。下面的流程对两种语言都适用。

Thanks for your interest in OpenGenieMaster. It is an independent, community-maintained project, and contributions of every size are welcome: bug reports, documentation fixes, new domains and scenarios, execution adapters, and robot backends.

## Ways to contribute

- **Report a bug or propose a feature** with the issue templates. For security problems, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.
- **Improve the documentation.** `README.md` (Chinese) and `README.en.md` (English) should stay in sync.
- **Connect more backends.** Add an execution adapter for a physics simulator (for example Genie Sim, Isaac, or MuJoCo), or a robot backend that implements the EEP HTTP contract in [docs/eep-spec.md](docs/eep-spec.md).
- **Extend the benchmark** with domains, workflows, scenarios, or experiments.

## Development setup

You need Python 3.10 or newer.

```bash
git clone https://github.com/eust-w/opengeniemaster.git
cd opengeniemaster
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[claude]"
```

The `claude` extra is needed only for the built-in Claude agent. The core and the test suite use the standard library alone.

## Checks to run before opening a pull request

```bash
python -m unittest discover -s tests -v
python -m opengeniemaster demo --rounds 2 --trials 3
python -m opengeniemaster bench --experiment capabilities --trials 5
```

CI runs the same checks on Python 3.10–3.13 and also builds the package.

## Ground rules

- **No new core dependencies.** The core must keep running on the standard library. Optional integrations go behind an extra in `pyproject.toml`.
- **Test behaviour changes.** New behaviour needs tests, and a bug fix needs a regression test that fails without the fix.
- **Label simulation results.** Benchmark numbers come from the semantic simulator. Never present them as physical-robot results.
- **Specify protocol changes.** A change to `open-eep` needs an update to [docs/eep-spec.md](docs/eep-spec.md). Within a minor version it must stay backward compatible; otherwise bump the protocol version.
- **Keep safety behaviour.** Do not weaken pause-on-intrusion, keep-gripper-closed, the operator stop, or the admission checks. A change in these areas needs tests and an explanation in the pull request.
- **Respect licenses.** Do not add third-party code, model weights, images, or videos unless their license allows redistribution alongside MIT-licensed code. Name the license of every new dependency or asset in the pull request.

## Commits and pull requests

- Write commit messages in the [Conventional Commits](https://www.conventionalcommits.org/) style: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `build:`, `chore:`.
- Keep each pull request to one topic and fill in the template.
- Add an entry under "Unreleased" in [CHANGELOG.md](CHANGELOG.md) for user-visible changes.
- Pull requests are squash-merged once CI passes and a maintainer has reviewed them.

## Releasing (maintainers)

1. Update `__version__` in `opengeniemaster/__init__.py`; the package version is read from there. Move the "Unreleased" entries in `CHANGELOG.md` under the new version.
2. Merge the change into `main`.
3. Publish a GitHub release whose tag is `v` plus the version, for example `v0.2.0`. The [release workflow](.github/workflows/release.yml) checks that the tag matches the package version, runs the tests, builds the sdist and wheel, and uploads them to PyPI through Trusted Publishing.
