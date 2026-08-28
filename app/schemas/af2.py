# TODO: confirm against actual pipeline input spec once the AF2 HPC-side
# submission scripts are finalized. Field names/shapes here are a best-effort
# approximation of common AlphaFold2 (ColabFold/DeepMind) run conventions.
import re

from pydantic import BaseModel, Field, field_validator, model_validator

_SEQUENCE_RE = re.compile(r"^[ACDEFGHIKLMNPQRSTVWYXBZJUO*\n\r]+$", re.IGNORECASE)


class AF2Sequence(BaseModel):
    id: str = Field(..., description="Chain identifier / FASTA header, e.g. 'A'")
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


class AF2Input(BaseModel):
    job_type: str = Field(..., description="'monomer' or 'multimer'")
    sequences: list[AF2Sequence] = Field(..., min_length=1)
    msa_options: MSAOptions = Field(default_factory=MSAOptions)
    num_predicted_models: int = Field(default=5, ge=1, le=25)
    random_seed: int | None = Field(default=None, ge=0)

    @field_validator("job_type")
    @classmethod
    def validate_job_type(cls, v: str) -> str:
        allowed = {"monomer", "multimer"}
        if v not in allowed:
            raise ValueError(f"job_type must be one of {sorted(allowed)}")
        return v

    @field_validator("sequences")
    @classmethod
    def validate_multimer_sequence_count(cls, v: list[AF2Sequence], info) -> list[AF2Sequence]:
        job_type = info.data.get("job_type")
        if job_type == "monomer" and len(v) != 1:
            raise ValueError("monomer jobs must have exactly one sequence")
        if job_type == "multimer" and len(v) < 2:
            raise ValueError("multimer jobs must have at least two sequences")
        return v
