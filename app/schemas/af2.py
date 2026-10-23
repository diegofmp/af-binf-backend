# TODO: confirm against actual pipeline input spec once the AF2 HPC-side
# submission scripts are finalized. Field names/shapes here are a best-effort
# approximation of common AlphaFold2 (ColabFold/DeepMind) run conventions.
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.common import GeneralInfo

# The 20 standard amino acids. Same rule as the HPC side (AF2/validate_input.py
# in af2_automatic_backend), which has the last word on what it can run.
_AMINO_ACIDS = set("ACDEFGHIKLMNPQRSTVWY")
MIN_SEQUENCE_LENGTH = 4
MAX_SEQUENCE_LENGTH = 2500
# One sequence per chain; more than one is only valid with the multimer model.
MAX_SEQUENCES = 6

DatabasePreset = Literal["full_dbs", "reduced_dbs"]
ModelPreset = Literal["monomer", "multimer", "monomer_casp14", "monomer_ptm"]


class DatabaseTab(BaseModel):
    database: DatabasePreset = Field(..., description="MSA database preset")


class ModelTab(BaseModel):
    model: ModelPreset = Field(..., description="AlphaFold2 model preset")


def _clean_sequence(v: str, position: int) -> str:
    # Whitespace (line breaks, spaces) is dropped, as the HPC side does.
    cleaned = re.sub(r"\s+", "", v).upper()
    name = f"sequence {position}"
    if not cleaned:
        raise ValueError(f"{name} must not be empty")
    if cleaned.startswith(">"):
        raise ValueError(f"{name} must contain only the sequence, without a FASTA header ('>...')")
    invalid = sorted(set(cleaned) - _AMINO_ACIDS)
    if invalid:
        raise ValueError(
            f"{name} contains invalid characters: {' '.join(invalid)} "
            "(only the 20 standard amino acids are allowed)"
        )
    if len(cleaned) < MIN_SEQUENCE_LENGTH:
        raise ValueError(f"{name} is too short ({len(cleaned)} aa, minimum {MIN_SEQUENCE_LENGTH})")
    if len(cleaned) > MAX_SEQUENCE_LENGTH:
        raise ValueError(f"{name} is too long ({len(cleaned)} aa, maximum {MAX_SEQUENCE_LENGTH})")
    return cleaned


class SequenceTab(BaseModel):
    sequences: list[str] = Field(
        ...,
        min_length=1,
        max_length=MAX_SEQUENCES,
        description="Amino acid sequences, one-letter codes, one per chain",
    )

    @field_validator("sequences")
    @classmethod
    def validate_sequence(cls, v: list[str]) -> list[str]:
        return [_clean_sequence(s, i) for i, s in enumerate(v, start=1)]


class AF2Sample(BaseModel):
    """The 'sample' tab: sub-tabs (database/model/sequence), sitting as
    siblings on the same object."""

    model_config = ConfigDict(populate_by_name=True)

    database: DatabaseTab
    model: ModelTab
    sequence: SequenceTab

    @model_validator(mode="after")
    def model_matches_sequence_count(self) -> "AF2Sample":
        # AlphaFold's monomer pipeline refuses a FASTA with more than one sequence.
        # Multimer with one sequence would run, but slower and ranked by a
        # meaningless ipTM, so a single sequence must use a monomer model.
        count = len(self.sequence.sequences)
        if count > 1 and self.model.model != "multimer":
            raise ValueError("more than one sequence requires the 'multimer' model")
        if count == 1 and self.model.model == "multimer":
            raise ValueError("the 'multimer' model requires at least two sequences")
        return self


class AF2Input(BaseModel):
    general: GeneralInfo
    sample: AF2Sample
