"""FORK-ONLY: run the Desktop's ``/api/git/*`` in the requesting profile's SSH workspace.

Upstream ``web_git`` shells out to git on the gateway's own filesystem, which an SSH profile's
workspace is not part of (the Review pane comes back empty, the preview diff 403s). Here a route
class binds the profile's ``SshWorkspaceFs`` for the request, and ``web_git._run`` / ``_is_dir``
run through it. With no SSH workspace bound every patched name calls the original, so non-SSH
profiles behave exactly as upstream.

Known degradations: untracked files count +0 lines and show no all-add diff (``web_git`` reads
them locally), new worktree folders are named without checking the remote for collisions, and
``gh`` runs remotely only when the gateway itself has ``gh`` on PATH.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import os
import shlex
import subprocess
from contextvars import ContextVar

from fastapi.routing import APIRoute

from hermes_cli import web_git as _web_git

_WORKSPACE: ContextVar = ContextVar("web_git_ssh_workspace", default=None)
_STDERR_MARK = "__HERMES_GIT_STDERR__"
_FORWARDED_ENV_PREFIXES = ("GIT_", "GH_", "GCM_")
# The Bot's own global/system config stays visible (its safe.directory, identity, credential
# helpers); the GIT_CONFIG_KEY_n hardening overrides still win over it.
_KEPT_REMOTE_ENV = frozenset({"GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"})

# A rename upstream must fail the import, not turn this patch into a silent no-op.
assert callable(getattr(_web_git, "_run", None)), "web_git._run moved: update hermes_cli/web_git_ssh.py"
assert callable(getattr(_web_git, "_is_dir", None)), "web_git._is_dir moved: update hermes_cli/web_git_ssh.py"

_local_run = _web_git._run
_local_is_dir = _web_git._is_dir


class SshWorkspaceRoute(APIRoute):
    """Binds the ``?profile=`` SSH workspace (None for a host-local profile) around the handler."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def route(request):
            from hermes_cli.web_routers.files import _fs_backend

            # The first request after a restart dials the host; keep that off the event loop.
            workspace = await asyncio.to_thread(_fs_backend, request.query_params.get("profile"))
            token = _WORKSPACE.set(workspace)
            try:
                return await handler(request)
            finally:
                _WORKSPACE.reset(token)

        return route


def local_only(guard):
    """``_hosted_fs_read_guard`` for host paths only: an SSH workspace's boundary is its account."""

    def wrapped(target, request=None):
        return None if _WORKSPACE.get() is not None else guard(target, request)

    return wrapped


def _env_prefix(env: dict) -> str:
    """The hardening ``noninteractive_git_env`` adds, as ``env K=V`` for the remote command."""
    changed = {
        key: value
        for key, value in env.items()
        if key.startswith(_FORWARDED_ENV_PREFIXES) or key == "PAGER"
        if key not in _KEPT_REMOTE_ENV and os.environ.get(key) != value
    }
    return "env " + " ".join(shlex.quote(f"{k}={v}") for k, v in changed.items()) if changed else ""


def _remote_run(workspace, argv: list[str], cwd: str, timeout: int, env: dict) -> subprocess.CompletedProcess | None:
    # stdout rides base64 so `-z` NULs survive; stderr follows a marker so the two stay apart.
    command = f"""
cd {shlex.quote(cwd)} || exit 128
err=$(mktemp) || exit 1
set -o pipefail
{_env_prefix(env)} {shlex.join(argv)} 2>"$err" </dev/null | base64
code=$?
printf '\\n{_STDERR_MARK}\\n'
base64 < "$err"; rm -f "$err"
exit $code
"""
    try:
        result = workspace._execute(command, timeout=timeout)
    except Exception:
        return None
    output = result.get("output") or ""
    code = int(result.get("returncode") or 0)
    out64, mark, err64 = output.partition(f"\n{_STDERR_MARK}\n")
    if not mark:
        return subprocess.CompletedProcess(argv, code or 1, "", output)
    try:
        stdout = base64.b64decode("".join(out64.split()), validate=True)
        stderr = base64.b64decode("".join(err64.split()), validate=True)
    except (ValueError, binascii.Error):
        return subprocess.CompletedProcess(argv, code or 1, "", output)
    return subprocess.CompletedProcess(
        argv, code, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")
    )


def _run(argv, cwd, timeout, env):
    workspace = _WORKSPACE.get()
    if workspace is None:
        return _local_run(argv, cwd, timeout, env)
    return _remote_run(workspace, argv, cwd, timeout, env)


def _is_dir(cwd):
    workspace = _WORKSPACE.get()
    if workspace is None:
        return _local_is_dir(cwd)
    try:
        return workspace._execute(f"test -d {shlex.quote(cwd)}").get("returncode") == 0
    except Exception:
        return False


_web_git._run = _run
_web_git._is_dir = _is_dir
