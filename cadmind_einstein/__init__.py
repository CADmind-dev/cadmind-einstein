"""CADmind einstein: the Bittensor side of CADmind's einstein engine.

Miners run their own agents and build client devices through the hosted CADmind Watt tools (MCP or
REST). The validator gives each client job to one miner (:mod:`.desk`); its output passes the hosted
Watt gate or the job goes to another miner. A job is scored by the gate, efficiency, timeliness and
the client's own reaction -- there is no LLM grader: clients grade (:mod:`.scoring`) -- into hourly
weights that are never burned. Agent keys are bound to hotkeys and sealed to the miner
(:mod:`.sealing`); job workspaces are granted, frozen and forked on the platform (:mod:`.platform`);
copies are caught (:mod:`.similarity`); state is durable (:mod:`.store`); the chain side is
:mod:`.chain`, the HTTP face :mod:`.service`, the process :mod:`.daemon`; the command-line
conventions shared by the programs are :mod:`.cli` and the release check :mod:`.version`.
"""


def _spec(version: str) -> int:
    """``MAJOR.MINOR.PATCH`` as one integer, ``10000*MAJOR + 100*MINOR + PATCH`` (each part below
    100): the version key the validator sets its weights with."""
    parts = [int(p) for p in version.split(".")[:3]]
    parts += [0] * (3 - len(parts))
    if any(not 0 <= p < 100 for p in parts):
        raise ValueError(f"version part out of range for the spec encoding: {version}")
    major, minor, patch = parts
    return 10000 * major + 100 * minor + patch


__version__ = "0.3.1"
__spec_version__ = _spec(__version__)
