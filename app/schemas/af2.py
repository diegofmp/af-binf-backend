# TODO: confirm against actual pipeline input spec once the AF2 HPC-side
# submission scripts are finalized. Field names/shapes here are a best-effort
# approximation of common AlphaFold2 (ColabFold/DeepMind) run conventions.
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import GeneralInfo

# The 20 standard amino acids. Same rule as the HPC side (AF2/validate_input.py
# in af2_automatic_backend), which has the last word on what it can run.
_AMINO_ACIDS = set("ACDEFGHIKLMNPQRSTVWY")

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
        # Whitespace (line breaks, spaces) is dropped, as the HPC side does.
        cleaned = re.sub(r"\s+", "", v).upper()
        if not cleaned:
            raise ValueError("sequence must not be empty")
        if cleaned.startswith(">"):
            raise ValueError("must contain only the sequence, without a FASTA header ('>...')")
        invalid = sorted(set(cleaned) - _AMINO_ACIDS)
        if invalid:
            raise ValueError(
                f"contains invalid characters: {' '.join(invalid)} "
                "(only the 20 standard amino acids are allowed)"
            )
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
