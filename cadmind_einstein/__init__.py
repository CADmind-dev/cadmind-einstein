"""CADmind einstein: the Bittensor side of CADmind's einstein engine.

Miners run their own agents and build client devices through the hosted CADmind Watt tools (MCP or
REST). The validator gives each client job to one miner (:mod:`.desk`); its output passes the hosted
Watt gate or the job goes to another miner. A job is scored by the gate, efficiency, timeliness and
the client's own reaction -- there is no LLM grader: clients grade (:mod:`.scoring`) -- into hourly
weights that are never burned. Agent keys are bound to hotkeys and sealed to the miner
(:mod:`.sealing`); job workspaces are granted, frozen and forked on the platform (:mod:`.platform`);
copies are caught (:mod:`.similarity`); state is durable (:mod:`.store`); the chain side is
:mod:`.chain`, the HTTP face :mod:`.service`, the process :mod:`.daemon`.
"""

__version__ = "0.1.0"
