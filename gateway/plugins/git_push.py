"""Real git_push plugin - shells out to the real `git` binary against a
mission's own sandbox repo (never the host's own repos - `repo_path` must be
supplied by the caller and is expected to be a path under a mission's
sandbox working directory, per design rule 9: sandbox all generated code).

allowed_roots (KNOWN_LIMITS gap #8) restricts repo_path to resolve inside
one of a fixed set of real directories from policy.yaml's
git_push_allowed_roots - checked by fully RESOLVING repo_path (following
symlinks and ".." segments) and comparing against each resolved root, not
by a naive string prefix check, which "../../../etc" or a symlink could
bypass. subprocess.run is already called with an argument LIST (never
shell=True), so no shell-injection surface exists either.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from gateway.plugins import PluginResult

_TIMEOUT_SECONDS = 60


class GitPushPlugin:
    def __init__(self, allowed_roots: tuple[str, ...] = ()):
        self._allowed_roots = tuple(Path(r).resolve() for r in allowed_roots)

    def _is_within_allowed_roots(self, resolved_path: Path) -> bool:
        if not self._allowed_roots:
            return False
        for root in self._allowed_roots:
            try:
                resolved_path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def execute(self, params: dict) -> PluginResult:
        repo_path = params.get("repo_path", "")
        remote = params.get("remote", "origin")
        branch = params.get("branch", "")

        if not repo_path:
            return PluginResult(ok=False, detail="No repo_path provided - push NOT attempted.")

        resolved = Path(repo_path).resolve()
        if not resolved.is_dir():
            return PluginResult(ok=False, detail=f"repo_path '{repo_path}' does not exist or is not a directory.")
        if not self._is_within_allowed_roots(resolved):
            return PluginResult(
                ok=False,
                detail=f"repo_path '{resolved}' is not inside any allow-listed root (policy.yaml git_push_allowed_roots) - push NOT attempted.",
            )
        if not branch:
            return PluginResult(ok=False, detail="No branch provided - push NOT attempted.")

        cmd = ["git", "push", remote, branch]
        try:
            result = subprocess.run(
                cmd, cwd=str(resolved), capture_output=True, text=True, timeout=_TIMEOUT_SECONDS, shell=False,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            return PluginResult(ok=False, detail=f"git push failed to run: {exc}")

        if result.returncode != 0:
            # stderr can legitimately be long (git's own progress output) -
            # trim it rather than dumping unbounded text into the ledger.
            return PluginResult(ok=False, detail=f"git push exited {result.returncode}: {result.stderr[:500]}")

        return PluginResult(ok=True, detail=f"Pushed {branch} to {remote}.")
