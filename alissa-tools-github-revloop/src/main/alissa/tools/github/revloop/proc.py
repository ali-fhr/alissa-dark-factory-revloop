"""Subprocess helpers. Everything shells out to `gh` and `alissa` so both keep
their own auth handling.

One exception, and it is the point of `env`: the review daemon runs in a
container that holds SEVERAL GitHub identities, and `gh` with no explicit
credential silently picks up whatever `GH_TOKEN`/`GITHUB_TOKEN` the process
inherited -- which is how a reviewer's verdict ended up on GitHub under the
IMPLEMENTER's login (issue #51). Callers that must be sure which identity a
`gh` call carries pass a fully-resolved environment here; everything else
passes None and inherits, exactly as before.

A child that cannot even be STARTED is a CommandError too (issue #169): the
container's cgroup pids.max counts threads, so a busy fleet can make `fork`
fail with EAGAIN, and an OSError escaping here reads as an unexplained crash
class rather than "gh could not start". Folding it keeps one failure type for
every caller (each already handles a CommandError the way it handles a
timeout) and puts the errno name in the message the poll firewall logs.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import subprocess
from typing import Mapping, Sequence

log = logging.getLogger(__name__)


class CommandError(RuntimeError):
    def __init__(self, argv: Sequence[str], returncode: int, stderr: str):
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"{argv[0]} exited {returncode}: {stderr.strip()[:400]}")


def run(
    argv: Sequence[str],
    *,
    timeout: int = 60,
    check: bool = True,
    cwd: "str | os.PathLike[str] | None" = None,
    env: "Mapping[str, str] | None" = None,
    stdin: "str | None" = None,
) -> str:
    """Run a command, return stdout. Never uses shell=True.

    `env`, when given, REPLACES the child's environment rather than adding to
    it -- that is what makes it an identity guarantee: a credential the caller
    did not put in the mapping cannot reach the child by inheritance.

    `stdin` feeds the child's standard input. It exists for `gh api --input -`:
    `gh`'s `-f key=value` fields are encoded differently across `gh` versions
    (`key[]=v` is a JSON array only on modern builds, a string field named
    `key[]` on the 2.4.0 this client targets), so a request whose body must
    have a particular JSON SHAPE is built by the caller and piped in, where no
    version gets a say in it.
    """
    log.debug("exec: %s", " ".join(argv))
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            input=stdin,
        )
    except subprocess.TimeoutExpired as exc:
        raise CommandError(argv, -1, f"timed out after {timeout}s") from exc
    except OSError as exc:
        raise CommandError(argv, -1, f"could not start {argv[0]}: {_os_error_text(exc)}") from exc

    if check and proc.returncode != 0:
        raise CommandError(argv, proc.returncode, proc.stderr)
    return proc.stdout


def _os_error_text(exc: OSError) -> str:
    """`EAGAIN: Resource temporarily unavailable` -- the errno's symbolic name
    first, since that is what an operator greps for; the bare message when the
    error carries no errno."""
    name = errno.errorcode.get(exc.errno) if exc.errno is not None else None
    if name is None:
        return str(exc)
    return f"{name}: {exc.strerror or exc}"


def run_json(
    argv: Sequence[str],
    *,
    timeout: int = 60,
    env: "Mapping[str, str] | None" = None,
    stdin: "str | None" = None,
):
    """Run a command whose stdout is JSON."""
    out = run(argv, timeout=timeout, env=env, stdin=stdin).strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise CommandError(argv, 0, f"expected JSON, got: {out[:300]}") from exc
