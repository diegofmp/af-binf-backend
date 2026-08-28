# TODO: confirm against actual pipeline input spec once the AF3 HPC-side
# submission scripts are finalized. Field names/shapes here are a best-effort
# approximation of the public AlphaFold3 server/CLI JSON input conventions
# (entities list with type-specific fields, bonded_atom_pairs, seeds).
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

EntityType = Literal["protein", "dna", "rna", "ligand", "ion"]


class TemplateOverride(BaseModel):
    template_id: str
    mmcif_path: str | None = None
    disabled: bool = False


class CovalentBond(BaseModel):
    entity1_id: str = Field(..., description="id of the first bonded entity")
    atom1: str = Field(..., description="atom name in entity1, e.g. 'SG'")
    entity2_id: str = Field(..., description="id of the second bonded entity")
    atom2: str = Field(..., description="atom name in entity2, e.g. 'C1'")


class Entity(BaseModel):
    id: str = Field(..., description="Unique entity identifier within the job, e.g. 'A'")
    type: EntityType
    count: int = Field(default=1, ge=1, description="Number of copies of this entity")

    # protein / dna / rna
    sequence: str | None = Field(default=None, description="Required for protein/dna/rna")

    # ligand
    ccd_code: str | None = Field(default=None, description="PDB Chemical Component Dictionary code")
    smiles: str | None = Field(default=None, description="SMILES string, alternative to ccd_code")

    # ion
    ion_code: str | None = Field(default=None, description="e.g. 'ZN', 'MG', 'CA'")

    use_msa: bool = Field(default=True, description="Whether to compute/use an MSA for this entity")
    msa_path: str | None = Field(default=None, description="Precomputed MSA override for this entity")
    templates: list[TemplateOverride] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_type_specific_fields(self) -> "Entity":
        if self.type in ("protein", "dna", "rna") and not self.sequence:
            raise ValueError(f"entity '{self.id}' of type {self.type} requires 'sequence'")
        elif self.type == "ligand" and not self.ccd_code and not self.smiles:
            raise ValueError(
                f"entity '{self.id}' of type ligand requires either 'ccd_code' or 'smiles'"
            )
        elif self.type == "ion" and not self.ion_code:
            raise ValueError(f"entity '{self.id}' of type ion requires 'ion_code'")
        return self


class AF3Input(BaseModel):
    entities: list[Entity] = Field(..., min_length=1)
    covalent_bonds: list[CovalentBond] = Field(default_factory=list)
    random_seed: int | None = Field(default=None, ge=0)
    num_diffusion_samples: int = Field(default=5, ge=1, le=25)

    @field_validator("entities")
    @classmethod
    def validate_unique_entity_ids(cls, v: list[Entity]) -> list[Entity]:
        ids = [e.id for e in v]
        if len(ids) != len(set(ids)):
            raise ValueError("entity ids must be unique within a job")
        return v

    @model_validator(mode="after")
    def validate_covalent_bond_references(self) -> "AF3Input":
        ids = {e.id for e in self.entities}
        for bond in self.covalent_bonds:
            if bond.entity1_id not in ids or bond.entity2_id not in ids:
                raise ValueError(
                    f"covalent bond references unknown entity id "
                    f"({bond.entity1_id!r} / {bond.entity2_id!r})"
                )
        return self
