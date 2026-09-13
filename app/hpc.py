import shlex
from collections.abc import Iterator
from contextlib import contextmanager

import paramiko

from app.config import get_settings
from app.logging import get_logger
from app.models.job import Job, JobVersion

logger = get_logger(__name__)

_DISPATCH_SCRIPT_SETTING = {
    JobVersion.AF2: "hpc_dispatch_script_af2",
    JobVersion.AF3: "hpc_dispatch_script_af3",
}


def dispatch_script_for(version: JobVersion) -> str:
    settings = get_settings()
    return getattr(settings, _DISPATCH_SCRIPT_SETTING[version])


@contextmanager
def _connection() -> Iterator[paramiko.SSHClient]:
    settings = get_settings()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.load_system_host_keys()
    try:
        connect_kwargs: dict = {
            "hostname": settings.hpc_ssh_host,
            "port": settings.hpc_ssh_port,
            "username": settings.hpc_ssh_username,
            "timeout": 15,
        }
        if settings.hpc_ssh_private_key_path:
            connect_kwargs["key_filename"] = settings.hpc_ssh_private_key_path
        elif settings.hpc_ssh_password:
            connect_kwargs["password"] = settings.hpc_ssh_password
        client.connect(**connect_kwargs)
        yield client
    finally:
        client.close()


def _run(client: paramiko.SSHClient, command: str) -> tuple[int, str, str]:
    _stdin, stdout, stderr = client.exec_command(command, timeout=60)
    exit_code = stdout.channel.recv_exit_status()
    return exit_code, stdout.read().decode(), stderr.read().decode()


def dispatch_job(job: Job) -> str:
    """Run the HPC-side dispatch script for `job` over SSH: `<script> <job_id>`.

    Blocking (uses paramiko directly) - call this off the event loop, e.g. via
    `fastapi.concurrency.run_in_threadpool`.

    The script is responsible for everything past this point: pulling the
    job's input JSON from GET /jobs/{id}/input.json, parsing it, and
    building/submitting the sbatch script. We only care whether the dispatch
    call itself succeeded or failed - not the eventual SLURM outcome, which
    we have no synchronous visibility into here.

    Returns the script's stdout (stripped), for reference/debugging.
    """
    script_path = dispatch_script_for(job.version)
    command = f"{shlex.quote(script_path)} {shlex.quote(str(job.id))}"

    with _connection() as client:
        exit_code, out, err = _run(client, command)

    if exit_code != 0:
        raise RuntimeError(f"dispatch failed (exit {exit_code}): {err.strip() or out.strip()}")

    logger.info("hpc.dispatch.ok", job_id=str(job.id), script_path=script_path)
    return out.strip()
