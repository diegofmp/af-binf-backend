import shlex
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import paramiko

from app.config import get_settings
from app.logging import get_logger
from app.models.job import Job

logger = get_logger(__name__)


def remote_workdir_for(job_id: uuid.UUID) -> str:
    settings = get_settings()
    return f"{settings.hpc_remote_workdir.rstrip('/')}/{job_id}"


def build_slurm_script(job: Job, *, remote_workdir: str) -> str:
    # TODO: confirm the actual AF2/AF3 pipeline entrypoint, module/conda env
    # activation, and resource requirements against the finalized HPC-side
    # scripts. This header is a reasonable placeholder shape only.
    settings = get_settings()
    account_line = f"#SBATCH --account={settings.hpc_slurm_account}\n" if settings.hpc_slurm_account else ""
    return (
        "#!/bin/bash\n"
        f"#SBATCH --job-name=af-{job.id}\n"
        f"#SBATCH --partition={settings.hpc_slurm_partition}\n"
        f"{account_line}"
        f"#SBATCH --output={remote_workdir}/stdout.log\n"
        f"#SBATCH --error={remote_workdir}/stderr.log\n"
        "#SBATCH --gres=gpu:1\n"
        "\n"
        "set -euo pipefail\n"
        f"mkdir -p {remote_workdir}/output\n"
        f"echo 'running {job.version.value} job {job.id}'\n"
        f"# TODO: replace with actual AlphaFold{job.version.value[-1]} pipeline invocation, e.g.\n"
        f"#   run_alphafold_{job.version.value}.sh --input {remote_workdir}/input.json --output_dir {remote_workdir}/output\n"
    )


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


def submit_slurm_job(
    *, remote_workdir: str, job_name: str, script_content: str, input_content: bytes
) -> str:
    """Write the input JSON + sbatch script into `remote_workdir` over SFTP and submit via `sbatch`.

    Blocking (uses paramiko directly) - call this off the event loop, e.g. via
    `fastapi.concurrency.run_in_threadpool`. Returns the SLURM job id.
    """
    script_path = f"{remote_workdir}/submit.sh"
    input_path = f"{remote_workdir}/input.json"
    mkdir_cmd = f"mkdir -p {shlex.quote(remote_workdir)}"

    with _connection() as client:
        exit_code, _out, err = _run(client, mkdir_cmd)
        if exit_code != 0:
            raise RuntimeError(f"mkdir failed (exit {exit_code}): {err.strip()}")

        sftp = client.open_sftp()
        try:
            with sftp.open(input_path, "wb") as f:
                f.write(input_content)
            with sftp.open(script_path, "w") as f:
                f.write(script_content)
            sftp.chmod(script_path, 0o755)
        finally:
            sftp.close()

        cmd = f"sbatch --parsable {shlex.quote(script_path)}"
        exit_code, out, err = _run(client, cmd)

    if exit_code != 0:
        raise RuntimeError(f"sbatch failed (exit {exit_code}): {err.strip() or out.strip()}")

    # --parsable prints "<job_id>" or "<job_id>;<cluster>"
    hpc_job_id = out.strip().split(";")[0]
    logger.info("hpc.submit.ok", hpc_job_id=hpc_job_id, job_name=job_name, script_path=script_path)
    return hpc_job_id
