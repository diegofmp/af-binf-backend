"""Shared building blocks for pipeline input schemas.

The frontend submits `input` as nested tabs, e.g.:
    { "general": {...}, "sample": { "database": {...}, "model": {...}, ... } }
A tab's own fields and its nested sub-tabs sit as siblings on the same
object. `general` is shared across pipeline versions (af2, af3, ...); each
version defines its own `sample` shape in its schema module.
"""

from pydantic import BaseModel, ConfigDict, Field


class GeneralInfo(BaseModel):
    """The 'general' tab, common to every pipeline version."""

    model_config = ConfigDict(populate_by_name=True)

    email: str = Field(..., min_length=1)
    ppms_project: str = Field(..., alias="ppmsProject", min_length=1)
    sample_id: str = Field(..., alias="sampleId", min_length=1)
