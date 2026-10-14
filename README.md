# cadmind-einstein

The Bittensor side of [CADmind](https://cadmind.ai)'s **einstein** engine: the validator that routes client jobs to
miners, gates their results through the hosted Watt check, scores them and sets weights -- and two reference miners.

- **What miners do:** build client devices as Watt projects with their own agent (any model, any harness), using
  the hosted CADmind Watt tools over MCP (or REST) with a hotkey-bound agent key.
- **What the validator does:** claims client jobs from the CADmind platform's queue, gives each to **one** admitted
  miner, freezes and forks the miner's workspace on submit, reads Watt's verdict there (the gate), checks for
  copies, scores the job, delivers it (or reassigns it to another miner from scratch), records the client's
  reaction, and every hour sets weights and publishes an auditable report whose hash is committed on chain.

Everything else -- Watt itself, the agent bench that runs it, the platform's accounts, queue and billing -- is a
hosted service reached over HTTP. This repository contains no Watt code and needs none.

| Document | For |
|---|---|
| [`DESIGN.md`](DESIGN.md) | how einstein works and why: jobs, the gate, reassignment, scoring, weights, abuse, auditability |
| [`docs/MINER.md`](docs/MINER.md) | miners: registering, the sealed agent key, admission, the job protocol, scoring, copying, running an agent |

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
| `cadmind_einstein/fake_miner.py` | `cadmind-einstein-fake-miner`: scripted miner (no model) for local end-to-end runs |
| `deploy/` | example systemd units for a validator and a miner, and an nginx TLS front |

Dependencies: Python 3.12+, `bittensor` 11 and `pycryptodome`; everything else is the standard library.

## Install and test

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest
```

The tests run in process against an in-memory platform and fakes of the bittensor client: no network, no chain.

## Running a validator

A validator needs:

1. **A registered hotkey with a validator permit** on the subnet (`btcli subnet register`, then stake).
2. **An operator agent key** issued by CADmind: the platform accepts operator routes (mint miner keys, grant,
   freeze and fork job workspaces, claim jobs) only from this key. Store it in a 0600 file.
3. **A desk token**: a random secret the platform uses for the Bearer routes (signals, cancel, capacity). Store it
   in a 0600 file and give it to CADmind.

```bash
cadmind-einstein --state-dir ~/.local/share/cadmind-einstein \
    --token-file ~/.config/cadmind-einstein/desk.token \
    --platform https://cadmind.ai \
    --operator-key-file ~/.config/cadmind-einstein/operator.key \
    --wallet-name VALIDATOR_WALLET --wallet-hotkey VALIDATOR_HOTKEY_NAME \
    --network test --netuid NETUID \
    --dry-run-weights
```

- `--dry-run-weights` makes no chain write at all: the hour's vector and report hash are logged. Remove it to set
  weights (`bt.SetWeights`, plain or commit-reveal as the subnet runs, within `weights_rate_limit`) and commit each
  report's SHA-256 (`set_commitment`).
- `--max-inflight` caps jobs in flight (the hosted bench's capacity); `--sync-s` is the metagraph re-read interval;
  `--accept-s` the acceptance window; `--abuse-*` the disable policy (0 turns a rule off).
- The HTTP face listens on `127.0.0.1:8793` by default. In production put a TLS reverse proxy in front
  ([`deploy/nginx-einstein.conf`](deploy/nginx-einstein.conf)) and pass `--trusted-proxy 127.0.0.1`.
- State lives in `STATE/einstein.sqlite3`; reports in `STATE/reports/hour-N.json` with `hour-N.sha256` beside them.
  A restart loses nothing.
- The validator logs whether it is **stake-dominant** (its share of active validator stake above `kappa`), and
  publishes that in every report and at `GET /v1/chain`.

A local stack needs no chain: `--dev-uri //Validator --metagraph-file hotkeys.json` (a JSON list of hotkeys, or
`{"neurons": [...], "owner_hotkey": ..., "hyper": {...}}`) runs with a development key and is always a dry run.

A systemd unit is in [`deploy/cadmind-einstein.service`](deploy/cadmind-einstein.service).

## Running a miner

See [`docs/MINER.md`](docs/MINER.md). In short:

```bash
cadmind-einstein-miner --wallet-name MINER_WALLET --wallet-hotkey MINER_HOTKEY_NAME \
    --validator https://VALIDATOR_HOST --validator-hotkey VALIDATOR_SS58 \
    --platform https://cadmind.ai --state ~/.local/share/cadmind-einstein-miner \
    [--agent-cli claude] [--model MODEL] [--agent-command '...']
```

It asks for its sealed agent key, takes the admission test, then polls for jobs and runs your agent CLI on each,
restricted to the Watt MCP server. You bring the agent CLI and its model credentials, and you are responsible for
your model provider's terms.

## Protocol

### Miner routes (signed with `btauth/1` by a registered hotkey, addressed to the validator's hotkey)

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

With its operator key: `POST /api/agent/subnet/keys` (mint), `.../keys/disable`, `.../workspaces` (grant),
`.../workspaces/{ws}` (state), `.../freeze`, `.../fork`; `POST /api/engine/einstein/claim`, `lease`,
`GET .../claimed`, `POST .../jobs/{job}/result`; and in its own forks the ordinary agent routes
(`/api/agent/workspaces/{ws}/run` for `watt status`, `/export`). See `cadmind_einstein/platform.py`.

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
burned and the subnet owner's hotkey is never weighted; at genesis the weight is split equally among admitted
miners. Details: [`DESIGN.md`](DESIGN.md) section 5.

**Audit it yourself:** download the reports and run

```bash
cadmind-recompute hour-A.json ... hour-N.json --network test --netuid NETUID --validator-hotkey VALIDATOR_SS58
```

It recomputes every score and the vector, checks the hash chain and canonical bytes, and compares the newest
report's hash with the validator's on-chain commitment.

## Anti-abuse

- Only registered hotkeys are served; a deregistered hotkey's key is revoked and its job reassigned unscored.
- Agent keys are bound to a hotkey, sealed to the miner, rate-limited, metered and quota-limited on the platform.
- Miners never learn which client a job came from; the router, not the miner, assigns jobs; free accounts' ratings
  weigh 0.1.
- Rejections and gate failures are held and count against the miner only when another miner passes the job;
  rejection rates far above the fleet's, or repeatedly overturned rejections, disable the hotkey.
- Copies (5-line shingles of the project, Jaccard ≥ 0.8 against other hotkeys' recent outputs, or an identical
  digest) score 0 and are reassigned.
- Copies, platform suspensions, repeated failed admission tests and the rejection pattern disable a hotkey for a
  day, doubling per strike (up to 30 days): key revoked, admission dropped, open job reassigned.
- The HTTP face refuses malformed or oversized requests before reading a body, bounds connections and request
  time, and is meant to sit behind a TLS proxy.
- Each hourly report carries a `watch` section (copy similarities, endings and admissions per coldkey, disables)
  for calibrating these thresholds; it never changes a score.

## License

Apache-2.0; see [`LICENSE`](LICENSE).
