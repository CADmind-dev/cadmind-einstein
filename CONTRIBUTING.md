# Contributing

Thank you for helping. Issues and pull requests are welcome.

## Before you start

- For anything larger than a small fix, open an issue first to agree on the approach.
- Security issues go through private reporting, never a public issue: see [`SECURITY.md`](SECURITY.md).
- [`DESIGN.md`](DESIGN.md) explains how the validator, scoring and anti-abuse work and why. A change to scoring,
  weights or the protocol changes what miners are paid for: explain the effect in the pull request, and expect it
  to be weighed against the design's principles.

## Development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

The tests run in process with fakes of the platform and of the bittensor client: no network and no chain.
CI runs the same commands on every push and pull request.

## Pull requests

- Keep each pull request to one change, with tests for new behaviour.
- Keep ruff clean (the configuration is in `pyproject.toml`; line length 100).
- Do not break existing command-line flags: deployments depend on them. Add new names as aliases.
- Add a line to [`CHANGELOG.md`](CHANGELOG.md) under the unreleased version.
- Never commit secrets: wallets, agent keys, desk tokens or state directories.

## Releases

Releases are tagged `vMAJOR.MINOR.PATCH` from `main` and published as GitHub releases. The package version lives in
`cadmind_einstein/__init__.py`; the spec version (`10000*MAJOR + 100*MINOR + PATCH`) is derived from it and is the
`version_key` the validator sets weights with, so the subnet's `weights_version` hyperparameter can require a minimum
release.

By contributing you agree that your contribution is released into the public domain under the
[Unlicense](LICENSE), like the rest of this repository.
