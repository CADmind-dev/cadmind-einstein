# Security policy

## Reporting a vulnerability

Please report security issues **privately** through GitHub's private vulnerability reporting for this repository:
open the repository's **Security** tab and choose **Report a vulnerability**
(<https://github.com/cadmindai/cadmind-einstein/security/advisories/new>).

Do not open a public issue or pull request for a vulnerability, and do not exploit it on the live subnet beyond
what is needed to demonstrate it.

Useful reports include:

- what is affected (the validator, a miner, the HTTP protocol, scoring, the sealed agent key, ...) and the version
  or commit;
- how to reproduce it, ideally against a local stack (see the README's local-stack notes) rather than the live
  network;
- the impact you expect (for example: weights or scores that can be gamed, a key that can be obtained, a request
  that can crash or stall the validator).

We will acknowledge the report, keep you informed while we investigate, and credit you in the advisory if you wish.

## Scope

In scope: the code in this repository -- the validator, the reference miners, the request signing and sealing, the
scoring and the auditor (`cadmind-recompute`).

The hosted CADmind platform and Watt service are operated separately; if you find an issue there while working with
this repository, report it through the same channel and we will route it.

## Supported versions

Security fixes are made on the latest release. Validators and miners should run the latest release; the programs
warn at startup when a newer one exists.
