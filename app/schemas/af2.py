# TODO: confirm against actual pipeline input spec once the AF2 HPC-side
# submission scripts are finalized. Field names/shapes here are a best-effort
# approximation of common AlphaFold2 (ColabFold/DeepMind) run conventions.
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class MSAOptions(BaseModel):
    use_precomputed_msa: bool = Field(
        default=False, description="If true, msa_path must point to an existing MSA/A3M archive"
    )
    msa_path: str | None = Field(
        default=None, description="Path/URI to a precomputed MSA, required if use_precomputed_msa"
    )
    generate_msa: bool = Field(
        default=True, description="Run the standard MSA search pipeline (jackhmmer/hhblits/etc.)"
    )

    @model_validator(mode="after")
    def validate_msa_path(self) -> "MSAOptions":
        # A model_validator (not a field_validator keyed off info.data) is
        # used here because msa_path is normally omitted entirely (using its
        # None default) rather than passed explicitly - field validators
        # don't run against unsupplied default values, so this cross-field
        # check would silently no-op with a field_validator.
        if self.use_precomputed_msa and not self.msa_path:
            raise ValueError("msa_path is required when use_precomputed_msa is true")
        return self


class AF2Sample(BaseModel):
    """The 'sample' tab: sub-tabs (database/model/sequence) plus optional
    direct fields, sitting as siblings on the same object."""

    model_config = ConfigDict(populate_by_name=True)

    database: DatabaseTab
    model: ModelTab
    sequence: SequenceTab
    msa_options: MSAOptions = Field(default_factory=MSAOptions, alias="msaOptions")
    num_predicted_models: int = Field(default=5, ge=1, le=25, alias="numPredictedModels")
    random_seed: int | None = Field(default=None, ge=0, alias="randomSeed")


class AF2Input(BaseModel):
    general: GeneralInfo
    sample: AF2Sample
