"""Shared building blocks for pipeline input schemas.

The frontend submits `input` as nested tabs, e.g.:
    { "general": {...}, "sample": { "database": {...}, "model": {...}, ... } }
A tab's own fields and its nested sub-tabs sit as siblings on the same
object. `general` is shared across pipeline versions (af2, af3, ...); each
version defines its own `sample` shape in its schema module.
"""

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Same check as the HPC side (AF2/validate_input.py in af2_automatic_backend),
# which re-validates the input before running the job.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_EMAIL_LENGTH = 254
# The sample ID is passed through shell tools on the HPC side, so it must stay on
# one line, must not contain quotes or backslashes, and must not look like an option.
_SAMPLE_ID_FORBIDDEN = set("'\"\\")
MAX_SAMPLE_ID_LENGTH = 64


class GeneralInfo(BaseModel):
    """The 'general' tab, common to every pipeline version."""

    model_config = ConfigDict(populate_by_name=True)

    email: str = Field(..., min_length=1)
    ppms_project: str | None = Field(None, alias="ppmsProject")
    sample_id: str = Field(..., alias="sampleId", min_length=1)

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        cleaned = v.strip()
        if len(cleaned) > MAX_EMAIL_LENGTH or not _EMAIL_RE.match(cleaned):
            raise ValueError("is not a valid email address")
        return cleaned

    @field_validator("sample_id")
    @classmethod
    def validate_sample_id(cls, v: str) -> str:
        # Any whitespace (line breaks, tabs, runs of spaces) becomes a single space.
        cleaned = " ".join(v.split())
        if not cleaned:
            raise ValueError("is required")
        if any(not c.isprintable() for c in cleaned):
            raise ValueError("must not contain control characters")
        if _SAMPLE_ID_FORBIDDEN & set(cleaned):
            raise ValueError("must not contain quotes (' or \") or backslashes (\\)")
        if cleaned.startswith("-"):
            raise ValueError("must not start with '-'")
        if len(cleaned) > MAX_SAMPLE_ID_LENGTH:
            raise ValueError(f"must be at most {MAX_SAMPLE_ID_LENGTH} characters")
        return cleaned
