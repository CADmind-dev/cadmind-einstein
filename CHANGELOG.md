# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Each release's spec version (`10000*MAJOR + 100*MINOR + PATCH`) is the
`version_key` the validator sets weights with.

## [0.2.0] - unreleased (proposed)

### Added
- Bittensor-style command-line flags on the validator and the reference miner: `--wallet.name`, `--wallet.hotkey`,
  `--wallet.path`, `--subtensor.network`, `--subtensor.chain_endpoint`, `--netuid`, `--logging.debug`,
  `--logging.trace`, and `--version`. `cadmind-recompute` accepts `--subtensor.network` and
  `--subtensor.chain_endpoint`.
- `__spec_version__`, derived from `__version__`, used as the weights `version_key`; a warning when the subnet's
  `weights_version` hyperparameter requires a newer validator.
- A release check: at startup (and daily on the validator) the latest GitHub release is read and a newer one is logged
  as a warning. It never downloads or runs anything; `--no-version-check` turns it off.
- The reference miner, given `--netuid`, checks once at startup that its hotkey is registered (a warning only).
- Docker: a `Dockerfile` and `deploy/docker/compose.yml` (validator and miner profiles).
- pm2: `deploy/pm2/ecosystem.config.js`.
- An opt-in updater, `scripts/auto_update.sh`, with a systemd user service and timer.
- `min_compute.yml`, GitHub Actions CI (ruff and pytest), `SECURITY.md`, `CONTRIBUTING.md` and this changelog.

### Changed
- Log output goes through the standard `logging` module (bittensor 11 has no `bittensor.logging`), with the same line
  prefixes as before.
- The weights' `version_key` is the validator's spec version rather than the subnet's current `weights_version`.

### Compatibility
- Every earlier flag still works: `--wallet-name`, `--wallet-hotkey` and `--network` are aliases of the new names.

## [0.1.0]

The first public release.

- **Validator** (`cadmind-einstein`): claims client jobs from the CADmind platform's queue and routes each to one
  admitted miner; grants, freezes and forks job workspaces; gates the miner's result on the hosted Watt check,
  including the validator's own run of the mutation guard; reassigns failed, timed-out or copied jobs to another
  miner; delivers passing results; records the client's reaction; scores jobs from the gate, efficiency, timeliness
  and the client's signals; sets weights hourly within the rate limit (plain or commit-reveal); publishes hourly
  reports and commits each report's SHA-256 on chain.
- **Miners**: a reference agent miner (`cadmind-einstein-miner`) that runs an agent CLI restricted to the hosted Watt
  MCP server, and a scripted miner (`cadmind-einstein-fake-miner`) for local end-to-end runs.
- **Identity**: hotkey-bound agent keys, sealed to the miner (X25519, HKDF-SHA256, AES-256-GCM); every answer on a
  miner route signed by the validator's hotkey.
- **Anti-abuse**: copy detection on normalised features, second opinions across coldkeys, rejection telemetry,
  admission tests, escalating disables, and a `watch` section in every report.
- **Auditability**: `cadmind-recompute` rebuilds every score and the weight vector from the published reports and
  checks them against the on-chain commitment.
- Durable state in one SQLite file; operator credentials split by role; a hardened HTTP face meant to sit behind a
  TLS reverse proxy.
