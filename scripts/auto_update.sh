#!/usr/bin/env bash
# Opt-in auto-update for a git checkout of cadmind-einstein: when a newer tagged release
# (vMAJOR.MINOR.PATCH) exists on the remote, check it out, reinstall it into the virtualenv and run
# your restart command. Nothing runs unless you run (or schedule) this script.
#
#   scripts/auto_update.sh --restart "pm2 restart einstein-validator"            # once
#   scripts/auto_update.sh --restart "systemctl --user restart cadmind-einstein" --loop 1800
#   scripts/auto_update.sh --verify-tag ...   # only accept tags whose signature git verifies
#   scripts/auto_update.sh --no-install \
#     --restart "docker compose -f deploy/docker/compose.yml --profile validator up -d --build"
#
# Options:
#   --repo DIR        the checkout (default: this script's repository)
#   --venv DIR        the virtualenv to reinstall into (default: DIR/.venv)
#   --no-install      do not pip-install (Docker: the restart command rebuilds the image)
#   --remote NAME     the git remote to read tags from (default: origin)
#   --restart CMD     run after a successful update (e.g. pm2/systemctl/docker compose)
#   --verify-tag      refuse a tag that `git verify-tag` does not accept
#   --loop SECONDS    keep checking every SECONDS instead of once
#   --dry-run         say what would happen, change nothing
#
# A dirty working tree is never touched. Only release tags are considered, never a branch head.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
venv="" remote="origin" restart="" verify=0 loop=0 dry=0 install=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) repo="$2"; shift 2 ;;
    --venv) venv="$2"; shift 2 ;;
    --remote) remote="$2"; shift 2 ;;
    --restart) restart="$2"; shift 2 ;;
    --verify-tag) verify=1; shift ;;
    --no-install) install=0; shift ;;
    --loop) loop="$2"; shift 2 ;;
    --dry-run) dry=1; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
venv="${venv:-$repo/.venv}"

say() { echo "[einstein-update] $(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }

check_once() {
  git -C "$repo" fetch --quiet --tags "$remote"
  local latest current
  latest="$(git -C "$repo" tag -l 'v[0-9]*.[0-9]*.[0-9]*' --sort=-v:refname | head -n 1)"
  if [[ -z "$latest" ]]; then say "no release tags on $remote"; return 0; fi
  # the checked-out code's version (what is deployed from this checkout)
  current="v$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$repo/cadmind_einstein/__init__.py")"
  if [[ "$current" == "$latest" ]] \
     || [[ "$(printf '%s\n%s\n' "$current" "$latest" | sort -V | tail -n 1)" == "$current" ]]; then
    say "up to date ($current; latest release $latest)"
    return 0
  fi
  if [[ -n "$(git -C "$repo" status --porcelain --untracked-files=no)" ]]; then
    say "local changes in $repo: not updating $current -> $latest"
    return 1
  fi
  if [[ $verify -eq 1 ]] && ! git -C "$repo" verify-tag "$latest" >/dev/null 2>&1; then
    say "tag $latest does not verify: not updating"
    return 1
  fi
  say "updating $current -> $latest"
  if [[ $dry -eq 1 ]]; then say "(dry run) would check out $latest, reinstall and restart"; return 0; fi
  git -C "$repo" -c advice.detachedHead=false checkout --quiet "refs/tags/$latest"
  if [[ $install -eq 1 ]]; then "$venv/bin/python" -m pip install --quiet -e "$repo"; fi
  if [[ -n "$restart" ]]; then
    say "restarting: $restart"
    bash -c "$restart"
  else
    say "installed $latest; no --restart given: restart the process yourself"
  fi
}

if [[ "$loop" -gt 0 ]]; then
  while true; do check_once || true; sleep "$loop"; done
else
  check_once
fi
