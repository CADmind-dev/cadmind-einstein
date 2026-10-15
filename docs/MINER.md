# Mining einstein

How to mine CADmind's einstein engine on its Bittensor subnet: registering, getting your agent key, the job
protocol, how a job is scored, what counts as copying, and how to run an agent CLI (or any MCP client) as your miner.
The design behind it is in [`../DESIGN.md`](../DESIGN.md).

The placeholders below are `$NETUID` (the subnet), `$NETWORK` (`test` or `finney`), `$VALIDATOR_URL` and
`$VALIDATOR_HOTKEY` (the validator's address and SS58 hotkey), and `$PLATFORM` (the CADmind platform,
`https://cadmind.ai`). The subnet's announcement gives their values.

## What you do

A client describes a device. The validator gives the job to **one** miner: you. Your agent builds the device as a
Watt project (`spec/`, `src/`, `tests/`) in a job workspace on CADmind's hosted Watt agent bench. It reaches the
bench only through the platform's agent API: MCP, or the same tools over REST. You submit when `watt status` says
**deliverable**. Your output is the client's result. Nobody else builds that job unless yours fails.

- **Any model and any harness.** Only the outcome is measured. The harness (how you plan, check and recover) is
  where miners compete.
- **No work without a client.** There are no benchmarks and no synthetic tasks. When there are no client jobs, you
  poll and wait, and your weight comes from your recent history (see [Weights](#weights)).
- **One job at a time per hotkey.**

## Your model provider's terms are your responsibility

You choose and pay for your own inference, and you must follow your model provider's terms. Many consumer plans do
not allow automated or commercial use, and reselling access to a closed model in breach of its terms is not allowed
on this subnet. Use a plan or an API agreement that permits what you do. Which model you use is not checked.

## 1. Register a hotkey

```bash
btcli wallet new_coldkey --wallet.name miner
btcli wallet new_hotkey  --wallet.name miner --wallet.hotkey m1
btcli subnet register --netuid "$NETUID" --subtensor.network "$NETWORK" --wallet.name miner --wallet.hotkey m1
```

Only registered hotkeys are served. Each request is checked against the validator's metagraph, which it re-reads
every few minutes. **If your hotkey deregisters, its agent key is revoked**, its admission is dropped, and the job it
holds goes to another miner without scoring you. If you register again, ask for a new key and pass admission again.
The subnet owner's hotkey and the validator's own hotkey cannot mine.

## 2. Get your agent key (hotkey-bound, sealed to you)

Every call to the validator is an HTTP POST signed by your hotkey with `btauth/1`
(`bittensor.http_auth.sign(keypair, method="POST", path=..., body=..., receiver_ss58=$VALIDATOR_HOTKEY)`, its
headers added to the request).

Every answer is **signed by the validator's hotkey**: `X-Einstein-Validator` (its ss58) and
`X-Einstein-Signature` (sr25519, hex) over these newline-joined UTF-8 lines: `cadmind-einstein-response/1`, the
HTTP status, `POST`, the path, your request's `X-Bittensor-Nonce`, your hotkey, the validator's hotkey, and the
lowercase hex SHA-256 of the answer's exact bytes. Check it (`cadmind_einstein.service.verify_response`; the
reference miners' `signed()` does) and drop an answer that does not verify: it was forged, altered, replayed from
another request or meant for another miner.

`POST $VALIDATOR_URL/v1/miners/agent_key` with body `{"x25519_public": "<base64 of a fresh 32-byte X25519 public
key>"}` answers:

```json
{"key_id": "...", "sealed": {"scheme": "x25519-hkdf-sha256-aes256gcm/1",
 "ephemeral": "<b64>", "nonce": "<b64>", "ciphertext": "<b64: ciphertext || 16-byte tag>"}}
```

To open it:

1. Compute X25519(your private key, `ephemeral`).
2. Run HKDF-SHA256 for 32 bytes, with salt = `ephemeral` ‖ your public key and info = `cadmind-agent-key/1` ‖ your
   hotkey's SS58 address (UTF-8).
3. Decrypt with AES-256-GCM, using `nonce` and your SS58 address as associated data.

`cadmind_einstein/sealing.py` is a reference implementation:

```python
from cadmind_einstein.sealing import new_keypair, open_sealed

priv, pub = new_keypair()
# ... POST the base64 of `pub`, signed ...
agent_key = open_sealed(answer["sealed"], priv, my_hotkey_ss58)
```

Asking again rotates the key and the old key stops working. Key requests are rate-limited (6 per hotkey per hour,
429 past it), so keep the one you have, and keep it secret: it is your quota and your score. Each key has a daily
compute quota and each job workspace a per-job compute cap on the platform (`usage` shows yours); quota storms or
secrecy refusals can suspend a key automatically. The validator also disables a hotkey for a day (longer on a
repeat) on copies, a suspension, repeated failed admission tests, or a pattern of rejections that other miners
overturn: its key is revoked, its admission dropped and its open job given to another miner.

## 3. Pass admission (once)

`POST /v1/miners/admission` (signed, empty body) creates a job assigned to you alone: *make this project
deliverable*. Watt's worked example will do: `watt example`, bring it into the workspace, then `watt test --mutate`
until `watt status` says deliverable, then submit. Passing admits you. Admission is never scored, and you can try
once a day. Only admitted miners get client jobs.

## 4. The job protocol

All calls are signed POSTs to `$VALIDATOR_URL`.

| Call | Body | Answer / rule |
|---|---|---|
| `/v1/jobs/work` | `{}` | `{job, workspace, brief, effort, accepted, accept_by_ms, deadline_ms, no_questions}` or `{job: null}`. **Poll every few seconds.** Polling is how the validator knows you are alive: you are routed jobs only if you polled in the last 60 s. |
| `/v1/jobs/accept` | `{job}` | Within **2 minutes** of the assignment. Your deadline then starts: low 30, medium 60, high 120, max 180 minutes. |
| `/v1/jobs/submit` | `{job, workspace, digest}` | When `watt status` in the workspace says `deliverable`. `digest` is the digest it reports. The workspace is frozen (read-only to you) at once. |
| `/v1/jobs/reject` | `{job, category, message}` | Instead of building. `category` is `not-allowed` or `not-capable`, the two classes of the scope guide (served through `watt_guide`). `message` is written for the client. |
| `/v1/miners/receipt` | `{}` | Your recent jobs with their scores, your weight and your quality, and your recent attempts that did not pass with why (`failures`). |

- The job workspace is **created for you** from the client's brief (or, for a later round of a client's project,
  from the delivered project) and granted to your key. You cannot delete it, and it does not count against your
  practice quota. Work in it, not in a workspace of your own.
- At the deadline, an accepted job is judged as it stands (frozen and gated). Late and failing counts as a timeout.
- You never see which client a job came from, and you never see another miner's rejection message.

### The client's questions and answers

What only the client can decide is an **open client decision** (`by = "client"`, `status = "open"`, built on your
best sourced assumption). The client sees it in their chat as soon as you write it -- the question, the options, the
assumption you build on, why, and what it affects -- and may answer while you work, or ask for a change.

- **Keep building on the assumption.** Never stop to wait for an answer.
- **After each `watt` command, call `client_answers`** (MCP; REST `GET /api/agent/workspaces/{ws}/answers?after=N`)
  with the last `cursor` it gave you. Each new answer is a file under `brief/` that the platform wrote (the client's
  words; you cannot write `brief/`): `brief/answer-<date>-<decision>-<HHMMSS>.md`, or `brief/change-<date>-<HHMMSS>.md`
  for a change request.
- **Record each answer in its decision**: `status = "answered"`, `choice` = the option they picked, and
  `answer = { from = "client: <their words, quoted from the file> (cadmind.ai, <date>)" }`; change what hangs on it
  and re-run what `watt impact` names for that decision. Do what a change request asks.
- **Check once more before you submit.** The gate refuses a delivery that leaves an answer that arrived before your
  submit unrecorded (section 5). An answer that arrives after your submit never counts against you.
- `wait_for_answer` waits briefly for an answer (at most 100 s a call, a few minutes per job in all); use it only
  when no useful work can go on under the assumptions.
- **`no_questions: true`** (on `work`, and on the workspace): the client asked not to be asked -- every job opened
  through the developer API is one. Leave no client decision open: decide each would-be question yourself
  (`by = "agent"`, `status = "decided"`, its `choice` and `because`); the client sees it as an assumption on the
  result. A change the client sends mid-job still arrives through `client_answers`.

## 5. The gate, reassignment and how a job ends

**The gate is Watt's own verdict.** The validator forks your frozen workspace and reads `watt status` there: the
project must be **deliverable** (every check passes and the mutation guard is recorded for the current digest). It
then runs the mutation guard again on its fork, with its own draw of the mutants, and you pass only if that run
passes too. Your record is one sample of the mutants; a design that passes only a lucky sample fails the gate. Make
every must requirement's tests kill its mutants, and whatever made it deliverable must be in the workspace.

**The client's answers are checked too**, on the validator's own copy: every answer the platform wrote into `brief/`
before your submit (or your deadline) must be recorded in its decision (not open, an `answer.from` quoting the file,
the option the client picked as its choice; for a decision answered twice, the latest answer), and a `no_questions`
job must leave no client decision open. Otherwise the attempt is a gate failure, and your receipt's `failures` says
which answer or decision it was.

If an attempt does not pass, the job goes to **another miner, from scratch**: the brief only, never your project.

| What happened | The job | Scores |
|---|---|---|
| an attempt passes | **delivered**, charged once | the passing miner scores; every earlier rejection or gate failure on this job scores 0 |
| two rejections, by miners of two coldkeys | **declined** (the client reads both messages; the brief is reviewed) | nobody is penalised |
| two substantive failures by two coldkeys, a gate failure among them | **out of reach** | nobody is penalised |
| four attempts without either of the above | **didn't finish** | each timeout and copy already scored 0; held rejections and failures count (0) unless two coldkeys gave them |

A job you rejected or failed goes next to a miner of **another coldkey** (after 30 minutes, to any other miner).
Hotkeys of one coldkey count as one opinion: they cannot end a job declined or out of reach between them.

- A **timeout** (not accepted within 2 minutes, or late and failing) or a **copy** scores 0 at once.
- A **rejection** or a **gate failure** is *held*: it scores 0 only if another miner then passes the same job. A
  rejection someone else overturns counts against you. Rejection rates over twice the fleet's (with at least 5
  rejections), or 3 overturned rejections, disable a hotkey for a day (doubling per strike); the same rule applies
  to all the hotkeys of one coldkey together.
- Leaving the subnet mid-job scores nothing, and the job is reassigned.

## 6. How a passing job scores

There is no LLM grader. **The clients are the graders.**

```
I = 0.5 + 0.3·E + 0.2·T              immediate: passed the gate, efficiency, on time
s = 0.4·I + 0.6·(w·C + (1 - w)·I)    about 40% immediate, about 60% the client's reaction
```

- `E`, **efficiency**, is half `min(1, median compute / yours)` and half `min(1, median wall time / yours)`,
  measured against passing jobs of the same effort and brief size over the last 7 days. It is 1 while there are
  fewer than 5 of those. Compute is the platform's meter, so it cannot be faked.
- `T` is 1 if the job was on time and 0 if late.
- `C` and `w` come from the **client's reaction**: stars weigh most (`C = (stars - 1)/4`, `w = trust`); a download
  without stars gives `C = 0.75`, `w = 0.5·trust`; each re-run or change request lowers `C` by 0.25 with `w` at least
  `0.5·trust`; no reaction gives `w = 0`, so `s = I`.
- `trust` is 1 for a paying account and 0.1 for a free one. Signals that arrive later still change the job's score.

These constants may be re-tuned; every hourly report states the ones it used.

## 7. What counts as copying

Copying means handing in another hotkey's output: a delivered project you saw, or your sybil's work. For each
passing output, the validator:

1. reduces `spec/`, `src/` and `tests/` to **features** that cosmetic edits do not change: each Python statement
   (and each sizeable call in it) with comments, docstrings, layout and every name and string normalised and
   numbers in one spelling; each TOML value under its key; each other line. Renaming, reordering, reformatting,
   commenting or padding a copy changes none of its features;
2. removes the features of the project the job started from (the seed, for a later round), of public example
   projects, and those common to outputs of 3 or more other coldkeys (the primer's idioms);
3. compares what is left with the recent outputs (the last 500) of **other** hotkeys.

An **identical Watt digest**, the **same normalised project**, or a **containment of 0.7 or more** -- 70% of the
smaller output's distinctive features found in the other, when both have at least 8 -- is a copy. It scores 0 at
once and the job is reassigned. Your own earlier outputs never count against you. Independent builds of one brief
share the primer's idioms, which are removed; measured on real builds they stay far below 0.7. Starting a client
job from Watt's worked example and changing little is likely to collide with someone who did the same, so build
the client's device. Details: [`../DESIGN.md`](../DESIGN.md) section 9.

## Weights

- Every hour the validator computes your **weight**: the sum of your job scores, each halving every 24 hours of age
  (a 7-day window). Your **quality** is the decayed mean of your job scores, which the router uses: jobs go to
  available admitted miners drawn with weight `(quality + 0.05)²`, 10% of jobs go to newcomers and low scorers so
  they can earn a score, and a client's next round goes to the miner who did the previous one if it is free.
- The vector covers registered, admitted miners seen in the last 24 hours. **Weight is never burned:** an hour with
  no jobs still pays miners from their history. Before anyone has any history (genesis), it is split equally per
  **coldkey** among admitted, registered and reachable miners that have delivered at least one scored client job;
  admission alone earns nothing. **The subnet owner's hotkey is never weighted.** There is no team
  miner.
- **Everything is auditable.** Each hour's report is public at `$VALIDATOR_URL/v1/reports`: the window's jobs with
  their facts and signals (never the client), every score, the vector and the validator's stake share. Each report
  names the previous report's SHA-256, and the newest hash is committed on chain. Check it yourself:

  ```bash
  cadmind-recompute hour-A.json ... hour-N.json --subtensor.network "$NETWORK" --netuid "$NETUID" \
      --validator-hotkey "$VALIDATOR_HOTKEY"
  ```

## Running an agent as your miner

Your agent needs two things. The **platform's MCP server** (`$PLATFORM/mcp`, streamable HTTP, `Authorization:
Bearer <agent key>`) runs Watt. A **small poller** speaks the signed validator protocol, which an MCP client cannot
sign. The MCP tools are `watt_guide`, `workspace_list`, `list_files`, `read_file`, `write_file`, `edit_file`,
`delete_file`, `watt` / `watt_wait` / `watt_cancel` (check, build, test, `test` with `mutate`, status, report),
`view_image`, `fetch` (datasheets), `upload_source` (a document you downloaded yourself), `client_answers` and
`wait_for_answer` (the client's answers, section 4) and `usage`. The same
tools are under `$PLATFORM/api/agent/...` for a REST harness, and `$PLATFORM/llms.txt` describes them.

### Fetching sources from your own connection

Many sites (makers' sites among them) refuse automated fetches from the platform's address, but answer a home or
office connection. So a miner may download a source **on its own machine** and upload it into the job workspace:
`POST $PLATFORM/api/agent/workspaces/{ws}/upload {url, save_as, data_base64, retrieved?, content_type?}` (MCP:
`upload_source`). The platform stores it exactly as `fetch` stores a document: `assets/<name>` with its sha256
(a plain name, `assets/` only; the extension follows the content; pdf, html, text, json, images or STEP; 8 MB at
most; a bot-check page refused), and it counts as **one fetch of the workspace's research budget**, the same as a
hosted fetch. The answer has `via: "builder"`; write the `source` record as for a fetch (url, sha256, asset,
retrieved) and add `fetched_by = "builder"`, so the delivery shows which sources you fetched. These uploads are
trusted: keep them honest (the bytes you got from that url), as a copied or invented source is scored as one.

The reference miner does this for you with a local tool, **`fetch_to_workspace(workspace, url, save_as)`**: a small
stdio MCP server in this package (`cadmind_einstein/fetch_mcp.py`, also `cadmind-einstein-fetch-mcp`; server name
`cadmind-fetch`). It downloads with the standard library from your connection and uploads with your agent key. It is
not a shell: http and https only, no credentials in urls, every address the host resolves to must be public (no
loopback, private, link-local or CGNAT address, so your own network stays out of reach of a brief that tries to
steer the agent), the connection goes to the checked address, every redirect (at most 5) is checked again, 5 s to
connect, 20 s and 8 MB per download. To give it to another MCP client:

```json
{"mcpServers": {"cadmind-fetch": {"type": "stdio", "command": "python",
  "args": ["-m", "cadmind_einstein.fetch_mcp", "--platform", "https://cadmind.ai",
           "--key-file", "/path/to/agent.key"]}}}
```

This repository ships two miners:

- **`cadmind-einstein-miner`** (`cadmind_einstein/agent_miner.py`): a complete reference miner. It gets and stores
  the key (mode 0600), writes the MCP config, requests admission, polls, accepts, runs your agent CLI in an empty
  directory restricted to the Watt MCP server and the local `fetch_to_workspace` tool, keeps polling while the agent
  works, and submits `DONE <digest>` or sends `REJECT <category>: <message>`. By default it runs Claude Code's CLI
  (`claude -p`, `--strict-mcp-config`, `--allowedTools mcp__cadmind-watt,mcp__cadmind-fetch__fetch_to_workspace`);
  `--no-local-fetch` leaves the local tool out; `--agent-cli`, `--model` and `--agent-arg` configure it, and
  `--agent-command` runs any other agent CLI from an argv template. Your CLI uses whatever credentials you have
  configured for it.

  ```bash
  pip install -e .                 # from a checkout of this repository
  cadmind-einstein-miner --wallet.name miner --wallet.hotkey m1 \
      --subtensor.network "$NETWORK" --netuid "$NETUID" \
      --validator "$VALIDATOR_URL" --validator-hotkey "$VALIDATOR_HOTKEY" \
      --platform "$PLATFORM" --state ~/.local/share/cadmind-einstein-miner --model <your model>
  ```

- **`cadmind-einstein-fake-miner`** (`cadmind_einstein/fake_miner.py`): a scripted miner with no model that rebuilds
  a reference Watt project file by file through MCP. It is the shortest complete example of the protocol side, and
  its `signed()` helper does the request signing. Replace its `build()` with your agent.

Restrict the agent to the MCP tools (and, if you like, the local fetch tool): it needs no shell and no files of
yours, because everything happens in the job
workspace. Accept **before** starting the agent, and keep polling `work` while it runs, or you stop being reachable.
The harness is where you compete: planning, reading `watt check` output, recovering from a failed guard and keeping
compute low all show up in your score.

The miner takes Bittensor's usual flags (`--wallet.name`, `--wallet.hotkey`, `--wallet.path`, `--subtensor.network`,
`--netuid`, `--logging.debug`); the dashed `--wallet-name` and `--wallet-hotkey` of earlier releases still work. With
`--netuid` it checks once at startup that your hotkey is registered.

To run it under a supervisor: a systemd user unit is in
[`../deploy/cadmind-einstein-miner.service`](../deploy/cadmind-einstein-miner.service), a pm2 file in
[`../deploy/pm2/ecosystem.config.js`](../deploy/pm2/ecosystem.config.js), and a Docker Compose profile in
[`../deploy/docker/compose.yml`](../deploy/docker/compose.yml); see the README's "Running and updating" for these and
the opt-in updater. Hardware needs are small (the CAD work runs on the hosted Watt service): see
[`../min_compute.yml`](../min_compute.yml).
