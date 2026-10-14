"""The release check: is this the newest published release?

The package's ``__version__`` (``MAJOR.MINOR.PATCH``) is encoded as ``__spec_version__``
(``10000*MAJOR + 100*MINOR + PATCH``), which the validator sets its weights with as the
``version_key``. At startup, and once a day after, the validator and the reference miner read the
repository's latest GitHub release and log a WARNING when it is newer than the running code. The
check only reads a version string: it never downloads or runs anything (updating is the operator's
choice -- see ``scripts/auto_update.sh``, which is opt-in). ``--no-version-check`` turns it off; any
failure to read the release is logged once at debug level and ignored.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
from collections.abc import Callable

from . import __spec_version__, __version__, _spec

RELEASES_URL = "https://api.github.com/repos/cadmindai/cadmind-einstein/releases/latest"
_TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

log = logging.getLogger("cadmind_einstein.version")


def spec_of(tag: str) -> int | None:
    """A release tag (``v0.2.0`` or ``0.2.0``) as a spec version; None if it is not one."""
    m = _TAG.match(tag.strip())
    if not m:
        return None
    try:
        return _spec(".".join(m.groups()))
    except ValueError:
        return None


def fetch_latest_tag(url: str = RELEASES_URL, timeout: float = 10.0) -> str:
    """The latest release's tag name (GitHub's ``releases/latest``)."""
    req = urllib.request.Request(  # noqa: S310 -- a fixed https URL
        url, headers={"Accept": "application/vnd.github+json", "User-Agent": "cadmind-einstein"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        data = json.loads(r.read(65536) or b"{}")
    return str(data.get("tag_name") or "")


def check_latest(
    fetch: Callable[[], str] = fetch_latest_tag,
    *,
    say: Callable[[str], None] | None = None,
    current: int = __spec_version__,
) -> str:
    """Compare the latest release with the running code; answers ``"older"`` (a WARNING was
    said), ``"current"`` or ``"unknown"``. Never raises."""
    try:
        tag = fetch()
    except Exception as e:  # noqa: BLE001 -- a failed check is never fatal
        log.debug("release check failed: %s: %s", type(e).__name__, e)
        return "unknown"
    latest = spec_of(tag)
    if latest is None:
        log.debug("release check: no usable release tag (%r)", tag)
        return "unknown"
    if latest > current:
        line = (
            f"WARNING: running {__version__} (spec {current}), but release {tag} "
            f"(spec {latest}) is out: update (see the README, 'Running and updating')"
        )
        (say or log.warning)(line)
        return "older"
    log.debug("release check: %s is current (latest release %s)", __version__, tag)
    return "current"
