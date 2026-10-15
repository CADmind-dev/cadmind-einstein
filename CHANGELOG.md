# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Each release's spec version (`10000*MAJOR + 100*MINOR + PATCH`) is the
`version_key` the validator sets weights with.

## [0.3.0] - unreleased (proposed)

### Added
- The client's answers: while a job is built its client sees the builder's open client decisions as questions and
  may answer them (or ask for a change); the platform writes each into the job workspace's `brief/` as the dated
  file Watt's own designer gets (`brief/answer-<date>-<decision>-<HHMMSS>.md`, `brief/change-<date>-<HHMMSS>.md`).
  The validator's gate now checks, on its own export of its fork (`cadmind_einstein/answers.py`), that every answer
  that arrived before the hand-over is recorded in its decision (not open, an `answer.from` quoting the file, the
  option picked as its choice); an answer after the hand-over never counts against the miner.
- No-questions jobs: a claim's `no_questions: true` (the client asked not to be asked; every developer-API job) is
  on the miner's `work` answer, and the gate refuses such a delivery while a client decision is open.
- The miner's receipt lists its recent attempts that did not pass, with why (`failures`).
- The reference miner's prompt checks `client_answers` after each `watt` command, records each answer and re-runs
  what it reaches, checks once more before it submits, and in a no-questions job decides every would-be question
  itself (`{questions}` and `{no_questions}` in a `--prompt-file` template).
- `fetch_to_workspace`, a local fetch tool for the reference miner's agent (`cadmind_einstein/fetch_mcp.py`, a stdio
  MCP server named `cadmind-fetch`, also `cadmind-einstein-fetch-mcp`): it downloads a source from the miner's own
  connection, which many sites answer when they refuse the platform's address, and uploads it into the job's
  workspace through the platform's upload route (`upload_source`). Standard library only; http and https to public
  addresses only, every redirect checked, 5 s to connect, 20 s and 8 MB per download. The reference miner writes it
  into its MCP config and allows it beside the Watt MCP tools; `--no-local-fetch` leaves it out.

### Changed
- The reference miner's prompt prefers `fetch_to_workspace` for makers' pages and datasheets (the hosted `fetch`
  remains), and asks for `fetched_by = "builder"` on the source records of what it stored.
- The reference miner's prompt runs the full mutation guard only after a passing scoped guard that covers every
  must requirement, never as a first check.

### Compatibility
- Miners must check the client's answers (the platform's `client_answers` tool) and record them, and decide every
  question in a no-questions job: a delivery that ignores an answer no longer passes the gate. Deploy the
  platform's answer relay before this validator.

## [0.2.0] - 2026-10-14

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

### Fixed
- A validator whose port is already taken now reports the bind error instead of an `AttributeError`.

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
