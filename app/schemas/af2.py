# TODO: confirm against actual pipeline input spec once the AF2 HPC-side
# submission scripts are finalized. Field names/shapes here are a best-effort
# approximation of common AlphaFold2 (ColabFold/DeepMind) run conventions.
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import GeneralInfo

_SEQUENCE_RE = re.compile(r"^[ACDEFGHIKLMNPQRSTVWYXBZJUO*\n\r]+$", re.IGNORECASE)

DatabasePreset = Literal["full_dbs", "reduced_dbs"]
ModelPreset = Literal["monomer", "multimer", "monomer_casp14", "monomer_ptm"]


class DatabaseTab(BaseModel):
    database: DatabasePreset = Field(..., description="MSA database preset")


class ModelTab(BaseModel):
    model: ModelPreset = Field(..., description="AlphaFold2 model preset")


class SequenceTab(BaseModel):
    sequence: str = Field(..., min_length=1, description="Amino acid sequence, one-letter codes")

    @field_validator("sequence")
    @classmethod
    def validate_sequence(cls, v: str) -> str:
        cleaned = v.strip().upper()
        if not cleaned:
            raise ValueError("sequence must not be empty")
        if not _SEQUENCE_RE.match(cleaned):
            raise ValueError("sequence contains characters outside the standard amino acid alphabet")
        return cleaned


class AF2Sample(BaseModel):
    """The 'sample' tab: sub-tabs (database/model/sequence), sitting as
    siblings on the same object."""

    model_config = ConfigDict(populate_by_name=True)

    database: DatabaseTab
    model: ModelTab
    sequence: SequenceTab


class AF2Input(BaseModel):
    general: GeneralInfo
    sample: AF2Sample
