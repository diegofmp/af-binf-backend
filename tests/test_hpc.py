import uuid

import pytest
from pydantic import ValidationError

import app.hpc as hpc_module
from app.config import Settings, get_settings
from app.models.job import Job, JobVersion


def test_fake_dispatch_mode_never_opens_ssh(monkeypatch):
    def _no_ssh():
        raise AssertionError("fake dispatch must not open an SSH connection")

    monkeypatch.setattr(hpc_module, "_connection", _no_ssh)
    monkeypatch.setattr(get_settings(), "hpc_dispatch_mode", "fake")
    monkeypatch.setattr(get_settings(), "hpc_fake_dispatch_delay_seconds", 0)

    job = Job(id=uuid.uuid4(), version=JobVersion.AF2)
    output = hpc_module.dispatch_job(job)

    assert "FAKE" in output
    assert str(job.id) in output


def test_fake_dispatch_mode_refused_in_production():
    with pytest.raises(ValidationError, match="not allowed with APP_ENV=production"):
        Settings(app_env="production", hpc_dispatch_mode="fake")
