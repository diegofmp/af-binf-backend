import pytest
from pydantic import ValidationError

from app.schemas.af2 import AF2Input
from app.schemas.af3 import AF3Input

GENERAL = {"email": "diego@example.com", "ppmsProject": "my_project", "sampleId": "my_sample"}


def _af2_sample(**overrides):
    sample = {
        "database": {"database": "full_dbs"},
        "model": {"model": "monomer"},
        "sequence": {
            "sequence": "MKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQAPILSRVGDGTQDNLSGAEKAVQVKVKALPDAQFEVVHSLAKWKR"
        },
    }
    sample.update(overrides)
    return sample


def test_af2_monomer_valid():
    payload = AF2Input(general=GENERAL, sample=_af2_sample())
    assert payload.general.ppms_project == "my_project"
    assert payload.sample.model.model == "monomer"
    assert payload.sample.database.database == "full_dbs"


def test_af2_accepts_multimer():
    payload = AF2Input(general=GENERAL, sample=_af2_sample(model={"model": "multimer"}))
    assert payload.sample.model.model == "multimer"


def test_af2_rejects_invalid_sequence_characters():
    with pytest.raises(ValidationError):
        AF2Input(general=GENERAL, sample=_af2_sample(sequence={"sequence": "MKTAYIAKQ123!!!"}))


@pytest.mark.parametrize("sequence", ["MKTAYXAKQ", "MKTAYBAKQ", "MKTAY*", ">chain_A\nMKTAYIAKQ"])
def test_af2_rejects_non_standard_residues_and_fasta_headers(sequence):
    with pytest.raises(ValidationError):
        AF2Input(general=GENERAL, sample=_af2_sample(sequence={"sequence": sequence}))


def test_af2_sequence_whitespace_is_removed_and_upper_cased():
    payload = AF2Input(
        general=GENERAL, sample=_af2_sample(sequence={"sequence": " mktay iakq\r\nRQIS\t"})
    )
    assert payload.sample.sequence.sequence == "MKTAYIAKQRQIS"


def test_af2_ppms_project_is_optional():
    general = {k: v for k, v in GENERAL.items() if k != "ppmsProject"}
    assert AF2Input(general=general, sample=_af2_sample()).general.ppms_project is None
    general["ppmsProject"] = ""
    assert AF2Input(general=general, sample=_af2_sample()).general.ppms_project == ""


@pytest.mark.parametrize("email", ["not-an-email", "a@b", "a b@example.com", "@example.com"])
def test_af2_rejects_invalid_email(email):
    with pytest.raises(ValidationError):
        AF2Input(general={**GENERAL, "email": email}, sample=_af2_sample())


def test_af2_email_is_stripped():
    payload = AF2Input(general={**GENERAL, "email": " diego@example.com "}, sample=_af2_sample())
    assert payload.general.email == "diego@example.com"


@pytest.mark.parametrize("sample_id", ["   ", "it's", 'a"b', "a\\b", "-sample", "a" * 65, "a\x00b"])
def test_af2_rejects_invalid_sample_id(sample_id):
    with pytest.raises(ValidationError):
        AF2Input(general={**GENERAL, "sampleId": sample_id}, sample=_af2_sample())


def test_af2_sample_id_whitespace_is_collapsed():
    payload = AF2Input(general={**GENERAL, "sampleId": "  my\n sample\t 1 "}, sample=_af2_sample())
    assert payload.general.sample_id == "my sample 1"
    assert AF2Input(general={**GENERAL, "sampleId": "a" * 64}, sample=_af2_sample())


def test_af2_rejects_invalid_model():
    with pytest.raises(ValidationError):
        AF2Input(general=GENERAL, sample=_af2_sample(model={"model": "dimer"}))


def test_af2_rejects_invalid_database():
    with pytest.raises(ValidationError):
        AF2Input(general=GENERAL, sample=_af2_sample(database={"database": "nonexistent_dbs"}))


def test_af2_requires_general_info():
    with pytest.raises(ValidationError):
        AF2Input(general={"email": "diego@example.com"}, sample=_af2_sample())


def test_af3_protein_ligand_valid():
    payload = AF3Input(
        entities=[
            {"id": "A", "type": "protein", "sequence": "MKTAYIAKQRQISFVKSHFSRQLEERLGLI"},
            {"id": "L", "type": "ligand", "ccd_code": "ATP"},
            {"id": "I", "type": "ion", "ion_code": "MG"},
        ],
        covalent_bonds=[{"entity1_id": "A", "atom1": "SG", "entity2_id": "L", "atom2": "C1"}],
    )
    assert len(payload.entities) == 3
    assert payload.covalent_bonds[0].entity1_id == "A"


def test_af3_ligand_requires_ccd_or_smiles():
    with pytest.raises(ValidationError):
        AF3Input(entities=[{"id": "L", "type": "ligand"}])


def test_af3_protein_requires_sequence():
    with pytest.raises(ValidationError):
        AF3Input(entities=[{"id": "A", "type": "protein"}])


def test_af3_rejects_duplicate_entity_ids():
    with pytest.raises(ValidationError):
        AF3Input(
            entities=[
                {"id": "A", "type": "protein", "sequence": "MKT"},
                {"id": "A", "type": "protein", "sequence": "MKT"},
            ]
        )


def test_af3_rejects_covalent_bond_to_unknown_entity():
    with pytest.raises(ValidationError):
        AF3Input(
            entities=[{"id": "A", "type": "protein", "sequence": "MKT"}],
            covalent_bonds=[{"entity1_id": "A", "atom1": "SG", "entity2_id": "ZZZ", "atom2": "C1"}],
        )


async def test_submitting_af2_job_with_malformed_input_is_rejected(authed_client):
    resp = await authed_client.post(
        "/jobs",
        json={
            "version": "af2",
            "name": "bad job",
            "input": {"general": GENERAL, "sample": _af2_sample(sequence={"sequence": ""})},
        },
    )
    assert resp.status_code == 422


async def test_submitting_af3_job_with_malformed_input_is_rejected(authed_client):
    resp = await authed_client.post(
        "/jobs",
        json={
            "version": "af3",
            "name": "bad job",
            "input": {"entities": [{"id": "L", "type": "ligand"}]},
        },
    )
    assert resp.status_code == 422


async def test_submitting_valid_af2_job_creates_submitted_job(authed_client):
    resp = await authed_client.post(
        "/jobs",
        json={
            "version": "af2",
            "name": "my first fold",
            "input": {"general": GENERAL, "sample": _af2_sample()},
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["version"] == "af2"
    assert body["name"] == "my first fold"
    assert body["status"] == "submitted"
    # the SSH dispatch call itself is stubbed in tests - see
    # tests/conftest.py::patch_hpc_dispatch and test_job_flow.py.
