import shlex
import time

import paramiko

from app.config import get_settings
from app.logging import get_logger
from app.models.job import Job, JobVersion

logger = get_logger(__name__)

_DISPATCH_SCRIPT_SETTING = {
    JobVersion.AF2: "hpc_dispatch_script_af2",
    JobVersion.AF3: "hpc_dispatch_script_af3",
}


class HPCUnavailableError(Exception):
    """Couldn't open the SSH connection, so no command was sent and nothing ran
    on the HPC side. Safe to retry the same job later.
    """


class DispatchScriptError(RuntimeError):
    """The dispatch script ran and exited non-zero. Retrying won't help."""


def dispatch_script_for(version: JobVersion) -> str:
    settings = get_settings()
    return getattr(settings, _DISPATCH_SCRIPT_SETTING[version])


def _fake_dispatch(job: Job) -> str:
    """Stand-in for a real dispatch, for local/internal testing without cluster
    access: wait `hpc_fake_dispatch_delay_seconds` (so the frontend's
    "dispatching" state is visible) and report success. The job is never run.
    """
    delay = get_settings().hpc_fake_dispatch_delay_seconds
    logger.warning("hpc.dispatch.fake", job_id=str(job.id), delay_seconds=delay)
    time.sleep(delay)
    return f"FAKE dispatch (HPC_DISPATCH_MODE=fake), job {job.id} was not sent to the cluster"


class HPCConnection:
    """One SSH connection to the HPC login node, reused across dispatches.

    Every dispatch logs in as the same service account, so opening a fresh
    connection per job is what gets throttled first (sshd MaxStartups,
    fail2ban, ...). The dispatcher (app.dispatcher) owns a single instance and
    calls it one job at a time. Not thread-safe, and doesn't need to be.

    All methods block (paramiko) - call them off the event loop, e.g. via
    `asyncio.to_thread`.
    """

    def __init__(self) -> None:
        self._client: paramiko.SSHClient | None = None
        self._last_used: float = 0.0

    def _is_connected(self) -> bool:
        if self._client is None:
            return False
        transport = self._client.get_transport()
        return transport is not None and transport.is_active()

    def _connect(self) -> paramiko.SSHClient:
        settings = get_settings()
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.load_system_host_keys()
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
        try:
            client.connect(**connect_kwargs)
        except (paramiko.SSHException, OSError) as exc:
            # OSError covers socket.timeout, connection refused, DNS failure, ...
            client.close()
            raise HPCUnavailableError(
                f"could not connect to {settings.hpc_ssh_host}: {exc}"
            ) from exc
        logger.info("hpc.ssh.connected", host=settings.hpc_ssh_host)
        return client

    def _ensure_connected(self) -> paramiko.SSHClient:
        if not self._is_connected():
            self.close()
            self._client = self._connect()
        assert self._client is not None
        return self._client

    def _open_channel(self, client: paramiko.SSHClient) -> paramiko.Channel:
        """Open an exec channel. Failing here means the command wasn't sent, so
        it's still an "unavailable" error, not an unknown outcome. A cached
        connection the server silently dropped shows up here: reconnect once.
        """
        try:
            return client.get_transport().open_session(timeout=15)
        except (paramiko.SSHException, OSError, AttributeError):
            self.close()
            client = self._ensure_connected()
            try:
                return client.get_transport().open_session(timeout=15)
            except (paramiko.SSHException, OSError, AttributeError) as exc:
                self.close()
                raise HPCUnavailableError(f"could not open SSH channel: {exc}") from exc

    def dispatch(self, job: Job) -> str:
        """Run the HPC-side dispatch script for `job`: `bash <script> <job_id>`.

        The script owns everything past this point: pulling the job's input
        JSON from GET /jobs/{id}/input.json, parsing it, and building/submitting
        the sbatch script. We only care whether the dispatch call itself
        succeeded, not the eventual SLURM outcome.

        Returns the script's stdout (stripped). Raises:
          - HPCUnavailableError: nothing was sent; safe to retry.
          - DispatchScriptError: the script ran and exited non-zero.
          - anything else: the command may or may not have run (e.g. the
            connection dropped mid-command). Don't retry blindly.

        With HPC_DISPATCH_MODE=fake, nothing is contacted: see `_fake_dispatch`.
        """
        if get_settings().hpc_dispatch_mode == "fake":
            return _fake_dispatch(job)

        script_path = dispatch_script_for(job.version)
        command = f"bash {shlex.quote(script_path)} {shlex.quote(str(job.id))}"

        client = self._ensure_connected()
        channel = self._open_channel(client)
        try:
            channel.settimeout(60)
            channel.exec_command(command)
            out = channel.makefile("rb").read().decode()
            err = channel.makefile_stderr("rb").read().decode()
            exit_code = channel.recv_exit_status()
        except TimeoutError:
            # The command was sent; drop the connection rather than reuse a
            # channel in an unknown state.
            self.close()
            raise
        finally:
            channel.close()
            self._last_used = time.monotonic()

        if exit_code != 0:
            raise DispatchScriptError(
                f"dispatch failed (exit {exit_code}): {err.strip() or out.strip()}"
            )

        logger.info("hpc.dispatch.ok", job_id=str(job.id), script_path=script_path)
        return out.strip()

    def close_if_idle(self, idle_seconds: float) -> None:
        """Close the connection if it hasn't been used for `idle_seconds`, so we
        don't hold a login session open while the queue is empty.
        """
        if self._client is not None and time.monotonic() - self._last_used >= idle_seconds:
            logger.info("hpc.ssh.idle_close")
            self.close()

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None
