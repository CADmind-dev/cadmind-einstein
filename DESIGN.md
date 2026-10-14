# Einstein on Bittensor: design

This document explains how CADmind's **einstein** engine works on the Bittensor side: what the miners do, what the
validator does, how a job is scored and how weights are set. It is written for readers outside CADmind. The code in
this repository is the validator and two reference miners; the CAD toolchain (**Watt**) and the CADmind platform
are hosted services that miners and the validator reach over HTTP.

Section numbers are referenced from the code's docstrings.

## Background

CADmind turns a client's description of a physical device (a *brief*) into a verified CAD project. **Watt** is
CADmind's engineering toolchain: a project is a directory of `spec/` (requirements, sources), `src/` (the parts)
and `tests/`; `watt check`, `build` and `test` verify it, and `watt test --mutate` runs a mutation guard that proves
the tests actually constrain the design. When every check passes and the guard is recorded for the project's
current digest, `watt status` reports the project as **deliverable**.

Watt runs only on CADmind's hosted *agent bench*. An agent drives it through the platform's **agent API**: an MCP
server (`/mcp`) or the same tools over REST, authenticated with an *agent key*. Every call is metered (compute
time), quota-limited and scrubbed so that Watt's own code never leaves the bench.

Einstein uses the subnet so that **miners supply the agentic inference**: each miner runs its own agent (any model,
any harness) against the hosted Watt tools. The quality of a miner's harness (how it plans, reads check output and
recovers) is what miners compete on.

## Principles

- **One miner per client job.** No benchmarks, no synthetic tasks, no several miners racing on one brief: every
  unit of inference a miner spends is on a real client's job, and the miner's output *is* the job's result.
- **Watt is the gate; the client is the grader.** Watt decides whether an output is usable (deliverable). How good
  it is comes from the client's own reaction. There is no LLM grader.
- **Never deliver a failure because one miner failed.** A failing attempt goes to another miner from scratch.
- **Nobody is penalised for a job nobody could do.** Fairness is judged on the whole job, not on one attempt.
- **All emission goes to miners.** Weights are never burned and the subnet owner's hotkey is never weighted. There
  is no team miner.
- **Auditable.** Every hour the validator publishes a report from which anyone can recompute every score and the
  weight vector, and commits the report's hash on chain.

## 1. The shape

```
client job (public, free-plan, or opted in)
   └─> validator: claims it from the platform's queue, routes it to ONE miner
         └─> platform: a job workspace granted to that miner's agent key
miner: its own agent, any model, any harness
   └─ MCP / REST with its hotkey-bound agent key ──> platform (keys, quotas, meter) ──> Watt agent bench
validator: freeze on submit, fork, read Watt's verdict (the gate), copy check, score,
           deliver (or reassign), record client signals, hourly weights and report
```

## 2. Identity: an agent key per hotkey, sealed to the miner

- A miner asks the validator for its agent key with `POST /v1/miners/agent_key {x25519_public}`, signed by its
  hotkey (`btauth/1`, Bittensor's HTTP signing). The validator checks the hotkey is registered on the metagraph,
  has the platform mint (or rotate) an agent key **bound to that hotkey**, and answers it **sealed**: X25519 with a
  fresh ephemeral key, HKDF-SHA256 (salt: both public keys; info: `cadmind-agent-key/1` and the hotkey),
  AES-256-GCM with the hotkey as associated data. The secret is never in clear on the wire or in a log
  (`cadmind_einstein/sealing.py`).
- On the platform each miner key lives in a service account of its own that nobody can sign in to, so the
  platform's ordinary ownership checks isolate miners from each other and from the operator.
- Only the validator's own agent key reaches the platform's *operator* routes: mint and disable keys, grant,
  freeze and fork job workspaces. A deregistered or abusive hotkey's key is deleted.

## 3. Jobs: one miner each, client briefs only

- **No work without a client brief.** Scores come only from real jobs.
- **The validator pulls.** All jobs wait in the platform's single queue. Whenever the validator has a free admitted
  miner it claims one einstein job, and the router picks the miner (section 6). The platform makes a job workspace
  from the brief (or, for a later round of a client's project, from the delivered project), granted to that miner's
  key. The miner cannot delete it and it does not count against the miner's own practice quota.
- **Accept, work, submit -- or reject.** The miner sees the job on `work`, must `accept` within 2 minutes, and
  `submit {job, workspace, digest}` before the effort's deadline (low 30, medium 60, high 120, max 180 minutes). On
  submit -- or at the deadline -- the workspace is **frozen** (read-only for the miner) and its compute meter read.
  Instead of building, the agent may `reject {job, category, message}`, `category` being `not-allowed` or
  `not-capable` (the two classes of the published scope guide, served to agents through `watt_guide`), with a
  message written for the client.
- **Throughput scales with miners:** N available miners serve up to N jobs at once, bounded by the agent bench's
  capacity (`--max-inflight`). A miner holds one job at a time.

## 4. The gate, reassignment and how a job ends

- **The gate is Watt's own verdict, read cheaply.** The validator forks the frozen workspace into one only it can
  write and runs `watt status` there. The output passes when the project is **deliverable**. Nothing is re-run:
  the digest covers the tests' code and the verification record is written only by Watt, so "deliverable" means
  the tests passed and the guard was clean on exactly this project.
- **Any attempt that does not pass** -- a rejection, a gate failure, a timeout (not accepted within 2 minutes, or
  late and failing), a copy -- sends the job to **another miner, from scratch**: the brief (or the seed) only, never
  the failed project, and never an earlier miner's rejection message. Each miner decides on its own.
- **How a job ends** (judged on the whole job):

  | What happened | The job | Charged | Scores |
  |---|---|---|---|
  | an attempt passes the gate | **delivered** | yes, once | the passing miner scores; every earlier rejection or gate failure on this job scores 0 |
  | two rejections | **declined**: the client reads both messages; the brief is flagged for review | no | nobody penalised |
  | two substantive failures, a gate failure among them | **out of reach** | no | nobody penalised |
  | four attempts without either of the above (timeouts, copies) | **didn't finish** | no | each timeout and copy already scored 0 |

- Timeouts and copies score 0 at once: reliability and honesty are the miner's alone. A rejection or a gate failure
  is **held**: it scores 0 only when another miner passes the same job. That is what stops miners rejecting hard
  work -- a rejection another miner overturns is a failed job -- while a genuinely impossible or forbidden brief
  costs nobody anything.
- A `not-allowed` rejection always gets a second, independent opinion: one rejection never ends a job, otherwise
  "not allowed" would be the cheapest way to skip work.
- A miner that deregisters mid-job, or is disabled for abuse, loses the job without a score, and that attempt does
  not count toward the four-attempt cap.
- A client's **re-run** is a new job, routed away from the miners that had the first one, and a mild negative signal
  for the first job. A client may **cancel** a claimed job: it is frozen, its miner freed, nobody scored, nothing
  charged.

## 5. The score: per job, graded by the client

```
timeout or copy:                                  s = 0, at once
rejection or gate failure, another miner passes:  s = 0
rejection or gate failure, nobody passes:         no score (declined / out of reach)
passing job:   I = 0.5 + 0.3·E + 0.2·T            immediate: passed the gate, efficiency, on time
               s = 0.4·I + 0.6·(w·C + (1 - w)·I)  ~40% immediate, ~60% the client's signal
```

- `E`, **efficiency**: half `min(1, median compute / own)` and half `min(1, median wall time / own)`, against the
  passing jobs of the same bucket (effort, brief size class) in the 7-day window; 1 while the bucket has fewer than
  5 jobs. Compute is the platform's meter, so a miner cannot fake it. `T` is 1 on time, 0 late.
- `C`, the client's verdict in [0, 1], and `w`, how much it counts:
  - **stars** (1-5) weigh most: `C = (stars - 1)/4`, `w = trust`;
  - a **download** without stars: `C = 0.75`, `w = 0.5·trust`;
  - a **re-run or change request**: `C` lowered by 0.25 each, `w` at least `0.5·trust`;
  - **no reaction**: `w = 0`, so `s = I`.
- **Missing signals neither punish nor inflate**: an unrated job scores exactly its measured part `I`, and a signal
  moves 60% of the score from `I` toward the client's verdict, by its trust.
- **Trust**: 1 for a paying account, 0.1 for a free one. Miners never learn which client a job came from, and the
  router -- not the miner -- assigns jobs, so a miner cannot steer its own fake client's job to itself.
- **Late signals** change the job's score when they arrive; the next hourly vector reads the current scores.
- The constants (0.4/0.6, 0.5/0.3/0.2, 0.75, -0.25, trust 0.1) are expected to be re-tuned on real traffic; every
  hourly report publishes the constants it used.

### Weights

- Each job counts with a weight that **halves every 24 hours** of age (7-day window). A miner's **weight** is the
  decayed *sum* of its job scores (work done, and how well); its **quality**, which the router uses, is the decayed
  *mean*.
- The vector is proportional to the weight over registered, admitted miners seen in the last 24 hours, and is set
  **every hour**. An hour with no work still pays miners from their history: **weight is never burned**.
- **Genesis** (no history at all): equal weight among miners that passed admission and are registered and
  reachable. Nothing is emitted only when no miner has ever been admitted.
- **The subnet owner's hotkey is never weighted** (weighting it would burn miner emission, and the chain scales a
  subnet's emission down for owner incentive), nor is the validator's own uid. These are dropped again at the last
  step before the chain call, whatever the scorer produced.

## 6. Admission and routing

- **Admission.** Before any client job, a miner passes an entry test: `POST /v1/miners/admission` creates a job
  assigned to it alone ("make this project deliverable"; Watt's worked example will do), at most once a day.
  Passing the gate admits it. It proves the miner's agent can drive Watt end to end; it is never scored. Only
  admitted miners get client jobs and count at genesis.
- **Available** = admitted, holds a live key, polled `work` in the last 60 s, holds no job, not blocked, registered.
- **Router.** A client job goes to the job's previous miner if it is free (a later round of the same project);
  else, 10% of the time, uniformly to a newcomer (fewer than 5 jobs) or a low scorer (quality under half the best),
  so they can earn a score; else to an available miner drawn with weight `(quality + 0.05)²`.
- **No miner free:** nothing is claimed and jobs wait in the platform's queue, which shows the client a position
  and an ETA (from the validator's `GET /v1/capacity`). A job is never refused for lack of miners.
- **Lease.** The validator renews a lease on every job it holds every 30 s; a job whose lease lapses goes back to
  the platform's queue, and one the platform reports lost (cancelled, re-queued) is dropped. After a restart the
  validator reconciles with the platform's list of jobs it holds.
- **Reassignment stays inside the validator.** A failed attempt keeps the claim; the held job takes the next free
  miner before anything new is claimed. This keeps a job's attempts together (a held failure is scored only once
  the job's outcome is known) and keeps the next miner away from the ones that already failed it.

## 7. Hourly reports and the auditor

Every hour the validator writes a report: the window's jobs with their facts (hotkey, effort, size class, accepted,
on time, gate, compute, wall time, copy, how the attempt ended) and client signals (kind, stars, trust -- never who
the client is), every score, each miner's weight and quality, the admitted and reachable miners, the rule used
(history or genesis), the constants, the validator's stake share against `kappa`, and the vector.

- Reports are canonical JSON, public at `GET /v1/reports` and `GET /v1/reports/hour-N.json`.
- Each report names the previous report's SHA-256, and the newest hash is committed on chain
  (`Commitments.set_commitment`, one `Sha256` field, signed by the validator's hotkey). Only the newest commitment
  is kept in chain state; the hash chain makes it authenticate the whole published history.
- `cadmind-recompute hour-A.json ... hour-N.json [--network NET --netuid N --validator-hotkey HK]` rebuilds every
  score and the vector from the reports alone, checks the hash chain and canonical bytes, and compares the newest
  hash with the commitment on chain.
- Queued and cancelled jobs are not in the reports: only attempts that ended are scored.

## 8. Durability and capacity

- All validator state (jobs, attempts, held failures, miners, admission, telemetry, outcomes and signals, the copy
  window, results not yet taken by the platform, keys still to revoke, the last hour closed, commitments) lives in
  one SQLite file (WAL, `synchronous=FULL`). A desk mutation and the ledger writes it causes share one transaction.
  A restart -- mid-job or mid-hour -- loses nothing: a grant cut off mid-call is re-queued, a working attempt keeps
  its workspace and absolute deadline, a judging attempt is judged again, and an hour missed while down is closed.
- Every slow platform call (grant, judge, return a result, revoke a key) runs in a bounded worker pool, and the
  lease has its own heartbeat thread, so the desk's tick never waits on the platform. A platform outage during
  judging leaves the job judging and retries later: an outage on the validator's side never fails a miner.
- Each job workspace and each miner key have compute quotas on the platform; the validator caps jobs in flight at
  the agent bench's capacity.

## 9. Against untrusted miners

| Threat | Answer |
|---|---|
| A thin or broken output | It fails the gate (reassigned; 0 if another miner passes) or passes thin and the client's signal says so |
| A miner rating its own jobs from a fake client | Miners never learn the client; the router assigns jobs; free accounts' signals weigh 0.1 |
| Rejecting hard work | A rejection another miner overturns scores 0; rejection telemetry flags the pattern and disables repeat offenders |
| Copying another miner's output | Shingles of 5 consecutive non-blank lines of `spec/ src/ tests/`, minus the starting project's, against the last 500 outputs of OTHER hotkeys; Jaccard ≥ 0.8 or an identical Watt digest is a copy: 0 at once, reassigned |
| Sybils | Each hotkey holds one job and is scored on its own; registration costs; admission costs a working agent run |
| Abuse of hosted compute | Per-job and per-key quotas, one run at a time, automatic disable |
| Collusion between hotkeys | Endings and admissions are published per coldkey in each report (`watch`) |

## 10. A single, auditable validator

Bittensor has no "owner sets the weights" mode. With one weight-setting validator, its row is the consensus under
Yuma only while it holds more than `kappa` of the stake of active validators. The einstein validator computes that
share from every metagraph read, logs it when it changes and publishes it in every report (the `chain` section,
also at `GET /v1/chain`); it never claims a dominance it does not hold. The legitimacy model is *centralised
execution, decentralised verification*: hourly public reports, open-source scoring, `cadmind-recompute`, and on-chain
commitments let anyone check the weights.

Weights are set with `bt.SetWeights`, which takes the plain or the timelocked commit-reveal path according to the
subnet's hyperparameters, inside `weights_rate_limit`, with the subnet's `weights_version`. `--dry-run-weights`
makes no chain write at all (vector and report hash are logged).

## 11. Abuse, secrecy and the HTTP face

- **Disables.** Every tick the validator disables a miner whose counts since its last strike cross the policy:
  copies (2), Watt suspensions (1: the platform suspends a key on Watt-side telemetry such as secrecy refusals or
  quota storms), failed entry tests (3), or the rejection pattern (at
  least 5 rejections at over twice the fleet's rate, or 3 overturned rejections). A disable revokes the key, drops
  the admission, sends the open job to another miner unscored and blocks the hotkey for a day, doubling per later
  strike up to 30 days. All thresholds are daemon flags (`--abuse-*`; 0 turns a rule off). Every disable is logged
  and listed in the next hourly report.
- **Secrecy.** Watt runs only on the hosted bench; miners reach it only through the agent API, which scrubs every
  answer. Miners see their brief, the public docs, and their own workspaces -- never Watt's code, other workspaces,
  other clients' projects, or which client a job came from.
- **HTTP face.** The validator refuses before it reads a body: a missing, malformed, duplicated or oversized
  `Content-Length` (and any `Transfer-Encoding`), an unsigned request on a signed route, an unregistered hotkey, a
  Bearer route without the token. Bodies are capped at 4 MiB; every read has a timeout and every request a
  deadline; connections are capped in all and per address; agent-key minting is rate-limited per hotkey (6/h) and
  per address (60/h). In production a TLS reverse proxy stands in front and is named with `--trusted-proxy`.

## 12. Privacy

Miners see the brief. Einstein takes public and free-plan projects; private ones only when the client opts in. The
validator refuses a private job that reaches it.
