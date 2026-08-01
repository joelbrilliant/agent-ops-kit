"""Safe subprocess helpers - never enable shell mode, never untrusted interpolation."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional, Sequence


class RunnerError(RuntimeError):
    """External process failed or misconfigured."""


@dataclass
class ProcResult:
    argv: List[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_argv(
    argv: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    timeout: Optional[int] = None,
    stdin_data: Optional[str] = None,
    check: bool = False,
) -> ProcResult:
    if not argv:
        raise RunnerError("empty argv")
    if not all(isinstance(x, str) for x in argv):
        raise RunnerError("argv must be strings only")
    # Hard ban on shell metacharacter single-string commands
    try:
        completed = subprocess.run(
            list(argv),
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            input=stdin_data,
            text=True,
            capture_output=True,
            timeout=timeout,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RunnerError(f"command timed out: {argv[0]}") from exc
    except OSError as exc:
        raise RunnerError(f"command could not start: {argv[0]}: {exc.__class__.__name__}") from exc
    result = ProcResult(
        argv=list(argv),
        returncode=int(completed.returncode),
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )
    if check and not result.ok:
        raise RunnerError(
            f"command failed ({result.returncode}): {argv[0]}: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result
