import io
import uuid

import paramiko
import pytest
from pydantic import ValidationError

import app.hpc as hpc_module
from app.config import Settings, get_settings
from app.hpc import DispatchScriptError, HPCConnection, HPCUnavailableError
from app.models.job import Job, JobVersion


class _FakeChannel:
    def __init__(self, exit_code: int, out: bytes = b"", err: bytes = b"") -> None:
        self.exit_code, self.out, self.err = exit_code, out, err
        self.command: str | None = None

    def settimeout(self, timeout):
        pass

    def exec_command(self, command):
        self.command = command

    def makefile(self, mode):
        return io.BytesIO(self.out)

    def makefile_stderr(self, mode):
        return io.BytesIO(self.err)

    def recv_exit_status(self):
        return self.exit_code

    def close(self):
        pass


class _FakeTransport:
    def __init__(self, client):
        self.client = client

    def is_active(self):
        return self.client.active

    def open_session(self, timeout=None):
        channel = self.client.next_channels.pop(0)
        self.client.channels.append(channel)
        return channel


class _FakeSSHClient:
    def __init__(self, next_channels):
        self.next_channels = next_channels
        self.channels: list[_FakeChannel] = []
        self.active = True

    def get_transport(self):
        return _FakeTransport(self)

    def close(self):
        self.active = False


def _job() -> Job:
    return Job(id=uuid.uuid4(), version=JobVersion.AF2)


def test_fake_dispatch_mode_never_opens_ssh(monkeypatch):
    def _no_ssh(self):
        raise AssertionError("fake dispatch must not open an SSH connection")

    monkeypatch.setattr(HPCConnection, "_connect", _no_ssh)
    monkeypatch.setattr(get_settings(), "hpc_dispatch_mode", "fake")
    monkeypatch.setattr(get_settings(), "hpc_fake_dispatch_delay_seconds", 0)

    job = _job()
    output = HPCConnection().dispatch(job)

    assert "FAKE" in output
    assert str(job.id) in output


def test_fake_dispatch_mode_refused_in_production():
    with pytest.raises(ValidationError, match="not allowed with APP_ENV=production"):
        Settings(app_env="production", hpc_dispatch_mode="fake")


def test_connection_is_reused_across_dispatches(monkeypatch):
    clients = []

    def _connect(self):
        client = _FakeSSHClient([_FakeChannel(0, b"ok 1\n"), _FakeChannel(0, b"ok 2\n")])
        clients.append(client)
        return client

    monkeypatch.setattr(HPCConnection, "_connect", _connect)
    conn = HPCConnection()
    first, second = _job(), _job()

    assert conn.dispatch(first) == "ok 1"
    assert conn.dispatch(second) == "ok 2"

    assert len(clients) == 1
    commands = [c.command for c in clients[0].channels]
    assert commands == [
        f"bash {get_settings().hpc_dispatch_script_af2} {first.id}",
        f"bash {get_settings().hpc_dispatch_script_af2} {second.id}",
    ]


def test_reconnects_when_connection_dropped(monkeypatch):
    clients = []

    def _connect(self):
        client = _FakeSSHClient([_FakeChannel(0, b"ok\n")])
        clients.append(client)
        return client

    monkeypatch.setattr(HPCConnection, "_connect", _connect)
    conn = HPCConnection()
    conn.dispatch(_job())
    clients[0].active = False  # server closed it while idle

    conn.dispatch(_job())

    assert len(clients) == 2


def test_nonzero_exit_raises_script_error(monkeypatch):
    monkeypatch.setattr(
        HPCConnection,
        "_connect",
        lambda self: _FakeSSHClient([_FakeChannel(3, b"", b"no such sample\n")]),
    )

    with pytest.raises(DispatchScriptError, match=r"exit 3\): no such sample"):
        HPCConnection().dispatch(_job())


def test_connect_failure_raises_unavailable(monkeypatch):
    def _refuse(self, **kwargs):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(paramiko.SSHClient, "connect", _refuse)
    monkeypatch.setattr(paramiko.SSHClient, "load_system_host_keys", lambda self: None)

    with pytest.raises(HPCUnavailableError, match="connection refused"):
        HPCConnection().dispatch(_job())


def test_close_if_idle(monkeypatch):
    client = _FakeSSHClient([_FakeChannel(0)])
    monkeypatch.setattr(HPCConnection, "_connect", lambda self: client)
    conn = HPCConnection()
    conn.dispatch(_job())

    conn.close_if_idle(idle_seconds=3600)
    assert client.active

    monkeypatch.setattr(hpc_module.time, "monotonic", lambda: 10**9)
    conn.close_if_idle(idle_seconds=3600)
    assert not client.active
