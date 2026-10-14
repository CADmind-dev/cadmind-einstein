# cadmind-einstein

[![CI](https://github.com/CADmind-dev/cadmind-einstein/actions/workflows/ci.yml/badge.svg)](https://github.com/CADmind-dev/cadmind-einstein/actions/workflows/ci.yml)
[![License: Unlicense](https://img.shields.io/badge/license-Unlicense-blue.svg)](LICENSE)

The Bittensor subnet of [CADmind](https://cadmind.ai)'s **einstein** engine: real client devices, designed by
miners' AI agents as checked engineering projects.

## What the subnet does

1. **A client describes a device** on the CADmind platform: what it must do, where it goes, how it is made.
2. **A miner designs it.** The validator gives the job to **one** miner. The miner's own agent (any model, any
   harness) builds the device as a **Watt project** -- requirements, sources, parts and tests -- using CADmind's
   hosted Watt engineering tools over MCP (or REST) with an agent key bound to the miner's hotkey. Watt checks,
   builds and tests the project and runs a mutation guard that proves the tests really constrain the design.
3. **The validator checks and scores it.** When the miner submits, the validator freezes and forks the workspace
   and gates the result with the hosted Watt check (running its own sample of the mutation guard). A passing result
   is delivered to the client; a failing, late or copied one goes to another miner from scratch. Each job is scored
   from the gate, efficiency, timeliness and the **client's own reaction** (download, stars, change requests) --
   there is no LLM grader. Every hour the validator sets weights and publishes a report anyone can recompute.

Watt itself, the agent bench that runs it, and the platform's accounts, job queue and billing are hosted services
reached over HTTP. This repository contains no Watt code and needs none: it is the validator and two reference
miners.

## Incentives

**How emissions flow.** The chain emits to the subnet every block and splits that emission between the subnet
owner, the miners, and the validators and their stakers, by Bittensor's rules. The miners' share is divided in
proportion to the weights the validator sets. Einstein's weights are a miner's job scores summed with a 24-hour
half-life over a 7-day window; **weight is never burned and the subnet owner's hotkey is never weighted**, so the
whole miner share goes to miners who did client work. Details in [Scoring and weights](#scoring-and-weights) and
[`DESIGN.md`](DESIGN.md) section 5.

**What miners earn.** Emissions, in proportion to their weight: the more client jobs they deliver, the better
clients rate them and the more efficiently they work, the more they earn. There are no benchmarks or synthetic
tasks; every job is a real client's.

**What miners pay for.** Their own model and agent usage (API or plan of their choice, under their provider's
terms) and the machine that runs their agent. The Watt tools run on CADmind's hosted service under the miner's
hotkey-bound agent key, metered and quota-limited by the platform; the metered compute counts in the efficiency
part of the score. Clients pay CADmind on the platform; client payments are not paid to miners directly.

## Validation today

During the current phase **a single CADmind-operated validator** routes client jobs and sets weights. Jobs, the
hosted Watt check and client payments live on the CADmind platform, which the validator reaches with operator keys.
What keeps this accountable:

- Every hour the validator publishes a report of the window's jobs (never the client), every score and the weight
  vector, chained by hash, and commits the newest report's SHA-256 on chain.
- [`cadmind-recompute`](#scoring-and-weights) rebuilds every score and the vector from those reports and checks them
  against the on-chain commitment. The scoring code is this repository.
- The validator publishes its own stake share and whether its weights are the consensus (`GET /v1/chain`, and in
  every report).

## Roadmap

Planned (no dates):

- **Independent re-checking**: other validators re-score delivered work from the published hourly reports and the
  recompute tool, and set weights from their own recomputation.
- **Child hotkeys and weight copying**: stakers and validators can delegate to, or follow, the einstein validator's
  weights through Bittensor's child-hotkey mechanism while independent validation matures.
- **An open job queue**: opening client jobs to other staked validators, each routing, gating and scoring its share
  with the hosted Watt check.
- **Mainnet**: registration on Bittensor mainnet (netuid to be announced).

## Networks

| Network | `--subtensor.network` | Netuid |
|---|---|---|
| Mainnet (finney) | `finney` | TBA |
| Testnet | `test` | `<TESTNET_NETUID>` |
| Local chain | `local` or `ws://127.0.0.1:9944` | your local subnet's |

The validator's URL and SS58 hotkey (which miners need) are published with each network's announcement. Below,
`<NETUID>` stands for the netuid of the network you use.

## Getting started on Bittensor

Install the Bittensor CLI (`btcli`) and create a wallet: a **coldkey** (keep it offline and backed up; it holds
funds) and a **hotkey** (the identity your miner or validator runs with).

```bash
btcli wallet new_coldkey --wallet.name my_wallet
btcli wallet new_hotkey  --wallet.name my_wallet --wallet.hotkey my_hotkey
```

Fund the coldkey (on testnet, request test TAO through the Bittensor community channels), then register the
hotkey on the subnet. Registration costs a burn in TAO that the chain sets; `btcli` shows it before you confirm.

```bash
btcli subnet register --netuid <NETUID> --subtensor.network test \
    --wallet.name my_wallet --wallet.hotkey my_hotkey
```

**Validators** also need stake on their hotkey to receive a validator permit (the top validators by stake on the
subnet hold permits):

```bash
btcli stake add --netuid <NETUID> --subtensor.network test \
    --wallet.name my_wallet --wallet.hotkey my_hotkey
```

During the current phase client jobs are routed by the CADmind validator only (see
[Validation today](#validation-today)); a validator also needs operator keys issued by CADmind (see
[Running a validator](#running-a-validator)).

**Miners**: once registered, follow [Running a miner](#running-a-miner) and [`docs/MINER.md`](docs/MINER.md).

## Minimum requirements

The heavy CAD work -- Watt's checks, builds, tests and mutation guard -- runs on CADmind's **hosted Watt service**,
not on your machine. The full table is [`min_compute.yml`](min_compute.yml).

| | Miner | Validator |
|---|---|---|
| CPU | 2 cores (4 recommended) | 2 cores (4 recommended) |
| RAM | 2 GB (4 recommended) | 4 GB (8 recommended) |
| Disk | 5 GB SSD (20 recommended) | 20 GB SSD (50 recommended) |
| GPU | none (unless you run your model locally) | none |
| Network | outbound HTTPS only | inbound HTTPS behind a TLS proxy, a stable address |
| Software | Linux, Python 3.12+, your agent CLI | Linux, Python 3.12+, a TLS reverse proxy |

**The main cost of mining is inference.** Your agent calls your model provider for every job; that usage is
yours to choose, pay for and keep within your provider's terms (many consumer plans do not allow automated or
commercial use). An efficient harness -- fewer, better model calls and less Watt compute -- also scores better.

## Documents

| Document | For |
|---|---|
| [`DESIGN.md`](DESIGN.md) | how einstein works and why: jobs, the gate, reassignment, scoring, weights, abuse, auditability |
| [`docs/MINER.md`](docs/MINER.md) | miners: registering, the sealed agent key, admission, the job protocol, scoring, copying, running an agent |
| [`CHANGELOG.md`](CHANGELOG.md) | what changed in each release |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | development setup and pull requests |
| [`SECURITY.md`](SECURITY.md) | reporting a vulnerability privately |

## Install and test

```bash
git clone https://github.com/CADmind-dev/cadmind-einstein.git
cd cadmind-einstein
python3.12 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest -q
```

The tests run in process against an in-memory platform and fakes of the bittensor client: no network, no chain.
Dependencies: Python 3.12+, `bittensor` 11 and `pycryptodome`; everything else is the standard library.

## Command-line flags

The programs take Bittensor's usual flag names:

| Flag | Meaning |
|---|---|
| `--wallet.name`, `--wallet.hotkey`, `--wallet.path` | the wallet, the hotkey and the wallets directory (default `~/.bittensor/wallets`) |
| `--subtensor.network` | `finney`, `test`, `local`, or a `ws(s)://` URL |
| `--subtensor.chain_endpoint` | a `ws(s)://` node endpoint; overrides `--subtensor.network` |
| `--netuid` | the subnet |
| `--logging.debug`, `--logging.trace` | more log output (ours and the bittensor SDK's) |
| `--version`, `--no-version-check` | print the version and spec version; skip the release check |

The dashed names of earlier releases -- `--wallet-name`, `--wallet-hotkey`, `--network` -- still work as aliases,
so existing command lines need no change. `--help` lists every flag.

## Running a validator

A validator needs:

1. **A registered hotkey with a validator permit** on the subnet (see
   [Getting started on Bittensor](#getting-started-on-bittensor)).
2. **Operator agent keys** issued by CADmind, one per role: the **operator** key (grant, freeze and fork job
   workspaces, and run `watt` in the validator's own forks), the **mint** key (mint and disable miners' keys) and
   the **queue** key (claim, lease and complete jobs; a delivered result charges the client). Each reaches only its
   own routes. Store each in a 0600 file. With one key only, pass just `--operator-key-file`: the others fall
   back to it.
3. **A desk token**: a random secret the platform uses for the Bearer routes (signals, cancel, capacity). Store it
   in a 0600 file and give it to CADmind.

```bash
cadmind-einstein --state-dir ~/.local/share/cadmind-einstein \
    --token-file ~/.config/cadmind-einstein/desk.token \
    --platform https://cadmind.ai \
    --operator-key-file ~/.config/cadmind-einstein/operator.key \
    --mint-key-file ~/.config/cadmind-einstein/mint.key \
    --queue-key-file ~/.config/cadmind-einstein/queue.key \
    --wallet.name VALIDATOR_WALLET --wallet.hotkey VALIDATOR_HOTKEY_NAME \
    --subtensor.network test --netuid <NETUID> \
    --dry-run-weights
```

- `--dry-run-weights` makes no chain write at all: the hour's vector and report hash are logged. Remove it to set
  weights (`bt.SetWeights`, plain or commit-reveal as the subnet runs, within `weights_rate_limit`) and commit each
  report's SHA-256 (`set_commitment`).
- `--max-inflight` caps jobs in flight (the hosted bench's capacity); `--sync-s` is the metagraph re-read interval;
  `--accept-s` the acceptance window; `--abuse-*` the disable policy (0 turns a rule off); `--copy-*` the copy
  check (`--copy-containment` 0.7, `--copy-min-features` 8, `--copy-common-coldkeys` 3, `--copy-window` 500, and
  `--copy-corpus PATH`, repeatable: public example projects whose features never count as evidence);
  `--second-opinion-coldkeys` (2) and `--second-opinion-wait-s` (1800) the second-opinion rule; `--genesis`
  (`delivered`, `coldkey` or `hotkey`) who is weighted before anyone has history.
- The HTTP face listens on `127.0.0.1:8793` by default. In production put a TLS reverse proxy in front
  ([`deploy/nginx-einstein.conf`](deploy/nginx-einstein.conf)) and pass `--trusted-proxy 127.0.0.1`.
- State lives in `STATE/einstein.sqlite3`; reports in `STATE/reports/hour-N.json` with `hour-N.sha256` beside them.
  A restart loses nothing.
- The validator logs whether it is **stake-dominant** (its share of active validator stake above `kappa`), and
  publishes that in every report and at `GET /v1/chain`.

A local stack needs no chain: `--dev-uri //Validator --metagraph-file hotkeys.json` (a JSON list of hotkeys, or
`{"neurons": [...], "owner_hotkey": ..., "hyper": {...}}`) runs with a development key and is always a dry run.

## Running a miner

See [`docs/MINER.md`](docs/MINER.md). In short:

```bash
cadmind-einstein-miner --wallet.name MINER_WALLET --wallet.hotkey MINER_HOTKEY_NAME \
    --subtensor.network test --netuid <NETUID> \
    --validator https://VALIDATOR_HOST --validator-hotkey VALIDATOR_SS58 \
    --platform https://cadmind.ai --state ~/.local/share/cadmind-einstein-miner \
    [--agent-cli claude] [--model MODEL] [--agent-command '...']
```

It asks for its sealed agent key, takes the admission test, then polls for jobs and runs your agent CLI on each,
restricted to the Watt MCP server. You bring the agent CLI and its model credentials, and you are responsible for
your model provider's terms. With `--netuid` it checks once at startup that its hotkey is registered (a warning
only); without it, it makes no chain call.

## Running and updating

Each program is one long-running process; run it under whichever supervisor you prefer. In every example below,
replace the placeholders, keep the token and key files mode 0600, and remove `--dry-run-weights` only when the
validator should write weights and report commitments on chain.

### pm2

```bash
npm install -g pm2
pm2 start deploy/pm2/ecosystem.config.js --only einstein-validator   # or einstein-miner
pm2 logs einstein-validator
pm2 save && pm2 startup                                              # start on boot
```

[`deploy/pm2/ecosystem.config.js`](deploy/pm2/ecosystem.config.js) assumes a checkout at `~/cadmind-einstein` with
its `.venv`.

### Docker

```bash
docker build -t cadmind-einstein .
docker run --rm cadmind-einstein cadmind-einstein --version
```

[`deploy/docker/compose.yml`](deploy/docker/compose.yml) runs a validator or a miner from that image, with the
wallets mounted read-only and the state on the host. Copy
[`deploy/docker/example.env`](deploy/docker/example.env) to `deploy/docker/.env`, fill it in, then:

```bash
docker compose -f deploy/docker/compose.yml --profile validator up -d --build
docker compose -f deploy/docker/compose.yml --profile miner up -d --build
```

The image contains no agent CLI. For a miner, install yours at build time with the `AGENT_INSTALL` build argument
(a shell command; see the [`Dockerfile`](Dockerfile)), and mount its configuration if it keeps credentials in
files. The validator container uses host networking so its HTTP face stays on `127.0.0.1:8793` behind the host's
TLS proxy.

### systemd

User units: [`deploy/cadmind-einstein.service`](deploy/cadmind-einstein.service) (validator) and
[`deploy/cadmind-einstein-miner.service`](deploy/cadmind-einstein-miner.service) (miner); a TLS front for the
validator is in [`deploy/nginx-einstein.conf`](deploy/nginx-einstein.conf).

```bash
cp deploy/cadmind-einstein.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now cadmind-einstein
journalctl --user -u cadmind-einstein -f
```

### Updating

Releases are tagged `vMAJOR.MINOR.PATCH` and published as GitHub releases; [`CHANGELOG.md`](CHANGELOG.md) lists
what changed. To update a checkout by hand:

```bash
git fetch --tags && git checkout vX.Y.Z && .venv/bin/pip install -e . && pm2 restart einstein-validator
```

**Version check.** At startup (and daily on the validator) the programs read the latest GitHub release and log a
`WARNING` when it is newer than the running code. The check reads a version string only; it never downloads or
runs anything. `--no-version-check` turns it off. The validator sets its weights with its **spec version**
(`10000*MAJOR + 100*MINOR + PATCH`, `cadmind_einstein.__spec_version__`) as the `version_key`, so the subnet's
`weights_version` hyperparameter can require a minimum validator release; the validator warns when it is older
than that.

**Automatic updates (opt-in).** [`scripts/auto_update.sh`](scripts/auto_update.sh) checks the remote for a newer
release tag; when there is one it checks it out, reinstalls it into the virtualenv and runs the restart command you
give it. It never touches a checkout with local changes, considers release tags only (never a branch head), and with
`--verify-tag` accepts only tags whose signature git verifies. Nothing updates unless you run or schedule it:

```bash
scripts/auto_update.sh --dry-run                                         # what would happen
scripts/auto_update.sh --restart "pm2 restart einstein-validator"        # once
scripts/auto_update.sh --restart "pm2 restart einstein-validator" --loop 1800
scripts/auto_update.sh --no-install \
    --restart "docker compose -f deploy/docker/compose.yml --profile validator up -d --build"
```

Schedules: the `einstein-updater` app in the pm2 file, or the systemd timer
([`deploy/cadmind-einstein-update.service`](deploy/cadmind-einstein-update.service) and
[`.timer`](deploy/cadmind-einstein-update.timer)): `systemctl --user enable --now cadmind-einstein-update.timer`.

## Protocol

### Miner routes (signed with `btauth/1` by a registered hotkey, addressed to the validator's hotkey)

Every answer on these routes is signed by the validator's hotkey (`X-Einstein-Validator`,
`X-Einstein-Signature`; check it with `cadmind_einstein.service.verify_response`, see
[`docs/MINER.md`](docs/MINER.md) section 2).

| Route | Body | Answer |
|---|---|---|
| `POST /v1/miners/agent_key` | `{x25519_public}` | `{key_id, sealed}`: the hotkey-bound agent key, sealed to that X25519 key (rate-limited) |
| `POST /v1/miners/admission` | `{}` | the entry test: a job assigned to this miner alone (once a day) |
| `POST /v1/jobs/work` | `{}` | `{job, workspace, brief, effort, accepted, accept_by_ms, deadline_ms}` or `{job: null}`; also the liveness poll |
| `POST /v1/jobs/accept` | `{job}` | `{job, workspace, deadline_ms}` |
| `POST /v1/jobs/submit` | `{job, workspace, digest}` | `{job, state: "judging"}` |
| `POST /v1/jobs/reject` | `{job, category, message}` | `category`: `not-allowed` or `not-capable` |
| `POST /v1/miners/receipt` | `{}` | the miner's recent jobs and scores, its weight and quality |

### Platform and operator routes (`Authorization: Bearer <desk token>`)

| Route | What |
|---|---|
| `GET /v1/capacity` | active and free admitted miners, median job time (the platform's queue computes ETAs) |
| `POST /v1/cancel {job}` | the client cancelled a claimed job: frozen, miner freed, nobody scored, not charged |
| `POST /v1/signals {job, kind, stars?, trust}` | the client's download, stars (1-5) or change request; `trust`: `paid` or `free` |
| `GET /v1/jobs/{job}` | a job's state and attempts |
| `GET /v1/scores` | scores, weights, quality, miners, rejection telemetry |
| `GET /v1/chain` | the validator's stake share, `kappa`, the weights path |
| `POST /v1/weights/close` | close the hour now (dry runs, tests) |

### Public routes (no auth)

| Route | What |
|---|---|
| `GET /v1/reports` | every hourly report's name, SHA-256 and the block its hash was committed at |
| `GET /v1/reports/hour-N.json` | a report's exact bytes |

### What the validator calls on the platform

With its mint key: `POST /api/agent/subnet/keys` (mint), `.../keys/disable`. With its operator key:
`.../workspaces` (grant), `.../workspaces/{ws}` (state), `.../freeze`, `.../fork`, and in its own forks the
ordinary agent routes (`/api/agent/workspaces/{ws}/run` for `watt status` and the gate's `watt test --mutate`,
`/export`). With its queue key: `POST /api/engine/einstein/claim`, `lease`, `GET .../claimed`,
`POST .../jobs/{job}/result`. See `cadmind_einstein/platform.py`.

- At startup the operator and queue keys are checked (the mint key on its first use); a key that does not hold
  its role (403 `operator_role`) or is refused (401: a wrong key, or an address the queue does not allowlist) is
  logged as an ALERT, and the validator does not start.
- The platform freezes a role's key past its ceilings (429 `operator_ceiling`, then 403 `operator_frozen`)
  until CADmind's operator enables it again. The validator then stops that role, logs one ALERT, asks again every
  5 minutes and resumes by itself; results waiting to be returned stay in its store. A frozen role never counts
  against a miner.
- 429 `too_many_in_flight` and 503 hold the role for the answer's `Retry-After`; the calls are retried on a later
  tick.
- Every grant names its job, and every delivered result names the validator's own fork and the digest its gate
  read, so the platform can check that a charged delivery descends from the job's own grant at that digest. A
  result the platform refuses on provenance (409 `delivery_not_of_job` or `delivery_digest_mismatch`) is logged as
  an ALERT and never sent again; the job is held for review, its scores are withdrawn (the fork is the
  validator's own, so this is never the miner's fault, and a delivery that did not happen earns nothing), and it
  is listed in the hourly report's `watch.delivery_refusals`. 503 `delivery_unverified` is sent again later.

## Scoring and weights

A failed attempt scores 0 (timeouts and copies at once; rejections and gate failures only when another miner then
passes the same job). A passing job scores

```
I = 0.5 + 0.3·E + 0.2·T                 immediate: passed the gate, efficiency, on time
s = 0.4·I + 0.6·(w·C + (1 - w)·I)       the client's reaction C, weighted by w
```

where `E` compares the job's compute and wall time with the median of similar passing jobs, and `C`/`w` come from
the client's stars, download, re-run or change request, weighted by account trust (paid 1, free 0.1). An unrated
job scores exactly `I`. A miner's weight is the sum of its job scores decayed with a 24-hour half-life (7-day
window); the hourly vector is proportional to it over registered, admitted, recently seen miners. Weight is never
burned and the subnet owner's hotkey is never weighted; at genesis (nobody has history) the weight is split
equally per coldkey among admitted miners that have delivered a scored client job, so registering hotkeys earns
nothing on its own. Details: [`DESIGN.md`](DESIGN.md) section 5.

**Audit it yourself:** download the reports and run

```bash
cadmind-recompute hour-A.json ... hour-N.json --subtensor.network test --netuid <NETUID> \
    --validator-hotkey VALIDATOR_SS58
```

It recomputes every score and the vector, checks the hash chain and canonical bytes, and compares the newest
report's hash with the validator's on-chain commitment.

## Anti-abuse

- Only registered hotkeys are served; a deregistered hotkey's key is revoked and its job reassigned unscored.
- Agent keys are bound to a hotkey, sealed to the miner, rate-limited, metered and quota-limited on the platform.
- Miners never learn which client a job came from; the router, not the miner, assigns jobs; free accounts' ratings
  weigh 0.1.
- Rejections and gate failures are held and count against the miner only when another miner passes the job;
  rejection rates far above the fleet's, or repeatedly overturned rejections, disable the hotkey; the same rule
  applies to each coldkey's hotkeys together.
- A rejected or failed job goes next to a miner of another coldkey, and only opinions from two coldkeys end a job
  declined or out of reach: hotkeys of one owner cannot end hard jobs for free.
- Copies score 0 and are reassigned. Outputs are compared as normalised features (Python statements with names,
  strings, comments and layout normalised; TOML values; other lines), so renaming, reformatting, commenting,
  reordering or padding a copy does not hide it; a copy is an identical digest, identical normalised features, or a
  containment of 0.7 of the smaller output in the other. What honest builds share -- the starting project, public
  example projects, and features common to three other coldkeys -- is subtracted first.
- Genesis weight goes per coldkey to miners with a delivered job, never for admission alone.
- Every answer on a miner route is signed by the validator's hotkey.
- Copies, platform suspensions, repeated failed admission tests and the rejection pattern disable a hotkey for a
  day, doubling per strike (up to 30 days): key revoked, admission dropped, open job reassigned.
- The HTTP face refuses malformed or oversized requests before reading a body, bounds connections and request
  time, and is meant to sit behind a TLS proxy.
- Each hourly report carries a `watch` section (every copy comparison's similarity and containment, the copy
  thresholds, endings, rejections, failures, deliveries and admissions per coldkey, disables) for calibrating these
  thresholds; it never changes a score.

## Layout

| Module | What |
|---|---|
| `cadmind_einstein/daemon.py` | `cadmind-einstein`: the validator process (flags, main loop, hourly close) |
| `cadmind_einstein/desk.py` | the job desk: claim, route, accept, deadlines, gate, reassignment, job endings, admission, abuse disables |
| `cadmind_einstein/scoring.py` | per-job scores, decayed weights, the vector, hourly reports; `cadmind-recompute` (the auditor) |
| `cadmind_einstein/chain.py` | metagraph reads, stake dominance, weights within the rate limit, report commitments (bittensor 11) |
| `cadmind_einstein/service.py` | the validator's HTTP face (stdlib `http.server`), hardened before any body is read |
| `cadmind_einstein/platform.py` | the validator's client of the platform's agent and queue APIs |
| `cadmind_einstein/sealing.py` | the agent key sealed to the miner (X25519, HKDF-SHA256, AES-256-GCM) |
| `cadmind_einstein/similarity.py` | copy detection between miners' outputs |
| `cadmind_einstein/store.py` | durable state: one SQLite file (WAL), one transaction per desk mutation |
| `cadmind_einstein/agent_miner.py` | `cadmind-einstein-miner`: reference miner running an agent CLI over the Watt MCP server |
| `cadmind_einstein/cli.py` | the shared Bittensor-style flags and logging setup |
| `cadmind_einstein/version.py` | the spec version and the release check |
| `cadmind_einstein/fake_miner.py` | `cadmind-einstein-fake-miner`: scripted miner (no model) for local end-to-end runs |
| `deploy/` | systemd units (validator, miner, opt-in updater), an nginx TLS front, pm2 and Docker Compose examples |
| `scripts/auto_update.sh` | the opt-in updater |

## Contributing and security

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Report vulnerabilities privately through GitHub's security advisories
for this repository, never in a public issue: see [`SECURITY.md`](SECURITY.md).

## License

Public domain (the Unlicense); see [`LICENSE`](LICENSE).
