"""Public input boundary and frozen asset integrity (no engine dependencies)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.schemas.casting_design import (
    MAX_INPUT_BYTES, CastingInputError, casting_input_json_schema, parse_casting_input,
)

ASSETS = Path(__file__).resolve().parents[1] / "app/casting"
VENDOR = ASSETS / "vendor/v5_1"
FIXTURES = Path(__file__).parent / "fixtures/casting"


def payload():
    return json.loads((VENDOR / "input-v1.json").read_text(encoding="utf-8"))


def encode(data):
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


@pytest.mark.parametrize("name", ["input-v1.json", "examples/input-v2.json", "examples/input-only-change.json"])
def test_upstream_inputs_are_accepted_without_mutation(name):
    raw = (VENDOR / name).read_bytes()
    parsed = parse_casting_input(raw)
    assert parsed == json.loads(raw)
    assert type(parsed["casting_mass_kg"]) is int
    assert "parameter_metadata" not in parsed


def test_units_and_empty_sites_are_deferred_to_engine():
    for name in ("converted", "no_candidate"):
        raw = (FIXTURES / f"{name}.input.json").read_bytes()
        assert parse_casting_input(raw) == json.loads(raw)
    data = payload()
    data["main_wall_mm"] = 2.4
    data["parameter_metadata"] = {"main_wall_mm": {"unit": "cm"}}
    # Raw main=2.4 < min=18 is valid after engine conversion; no early wall check.
    parsed = parse_casting_input(encode(data))
    assert parsed["main_wall_mm"] == 2.4
    assert parsed["parameter_metadata"] == {"main_wall_mm": {"unit": "cm"}}


@pytest.mark.parametrize("value", [True, "630", None, [], {}, -1, 0])
def test_invalid_engineering_number_is_not_coerced(value):
    data = payload()
    data["casting_mass_kg"] = value
    with pytest.raises(CastingInputError) as caught:
        parse_casting_input(encode(data))
    assert any(x.field_path == "input.casting_mass_kg" for x in caught.value.issues)


@pytest.mark.parametrize("raw,code", [
    (b"{", "InvalidJSON"),
    (b"[]", "InvalidType"),
    (b"null", "InvalidType"),
    (b'{"a":1,"a":2}', "DuplicateKey"),
    (b'{"a":{"b":1,"b":2}}', "DuplicateKey"),
    (b'{"a":NaN}', "NonFinite"),
    (b'{"a":Infinity}', "NonFinite"),
    (b'{"a":1e999}', "NonFinite"),
    (b'{"a":1e-999}', "Missing"),  # Finite numeric zero; required input is still absent.
    (b"\xff", "InvalidEncoding"),
    (b'{"a":"\\ud800"}', "InvalidEncoding"),
    (b" " * (MAX_INPUT_BYTES + 1), "TooLarge"),
    (b"[" * 17 + b"0" + b"]" * 17, "TooDeep"),
    (b"[" * 2000 + b"0" + b"]" * 2000, "TooDeep"),
], ids=["invalid-syntax", "array-root", "null-root", "duplicate", "nested-duplicate",
        "nan", "infinity", "overflow", "underflow", "invalid-encoding", "surrogate",
        "file-limit", "depth-limit", "parser-depth-limit"])
def test_malformed_json_has_stable_errors(raw, code):
    with pytest.raises(CastingInputError) as caught:
        parse_casting_input(raw)
    assert caught.value.issues[0].error_code == code


def test_bom_is_accepted_and_optional_fields_are_not_injected():
    data = payload()
    del data["product_name"]
    del data["thermal_curve_status"]
    assert parse_casting_input(b"\xef\xbb\xbf" + encode(data)) == data


@pytest.mark.parametrize("field,count", [
    ("hotspots", 17), ("riser_sites", 25), ("ingate_sites", 33),
    ("forbidden_gate_regions", 65),
])
def test_arrays_are_bounded(field, count):
    data = payload()
    data[field] = [data[field][0]] * count
    with pytest.raises(CastingInputError) as caught:
        parse_casting_input(encode(data))
    assert caught.value.issues[0].error_code == "TooMany"
    assert caught.value.issues[0].field_path == f"input.{field}"


@pytest.mark.parametrize("path,metadata", [
    ("casting_mass_kg", {"unit": []}),
    ("casting_mass_kg", {"unit": None}),
    ("casting_mass_kg", {"status": "Unconfirmed"}),
    ("casting_mass_kg", {"source_ref": " "}),
    ("casting_mass_kg", {"invented": "value"}),
    ("hotspots.99.modulus_mm", {"unit": "mm"}),
    ("riser_sites.0.max_diameter_mm", {"unit": "cm"}),
])
def test_bad_metadata_never_reaches_unsafe_engine_types(path, metadata):
    data = payload()
    data["parameter_metadata"] = {path: metadata}
    with pytest.raises(CastingInputError):
        parse_casting_input(encode(data))


@pytest.mark.parametrize("status", ["Confirmed", "Derived", "Proposed", "Missing", "Conflicted"])
def test_known_status_is_structural_only(status):
    data = payload()
    data["parameter_metadata"] = {"casting_mass_kg": {"status": status}}
    assert parse_casting_input(encode(data)) == data


def test_missing_unknown_fields_dimensions_and_string_limits():
    data = payload()
    del data["casting_mass_kg"]
    data["unrecognized"] = "value"
    data["casting_envelope_mm"] = [1, 2]
    data["source_refs"]["material"] = "x" * 513
    with pytest.raises(CastingInputError) as caught:
        parse_casting_input(encode(data))
    issues = {x.field_path: x.error_code for x in caught.value.issues}
    assert issues["input.casting_mass_kg"] == "Missing"
    assert issues["input.unrecognized"] == "UnknownField"
    assert "input.casting_envelope_mm" in issues
    assert "input.source_refs.material" in issues


def test_published_schema_matches_runtime_model():
    published = json.loads((ASSETS / "schemas/input-v1.schema.json").read_text(encoding="utf-8"))
    assert published == casting_input_json_schema()
    assert published["additionalProperties"] is False
    assert published["properties"]["hotspots"]["maxItems"] == 16


def test_vendor_and_active_rule_are_byte_exact_and_registry_is_consistent():
    manifest = json.loads((ASSETS / "engine-manifest.json").read_text(encoding="utf-8"))
    actual = {p.relative_to(VENDOR).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in VENDOR.rglob("*") if p.is_file()}
    assert len(actual) == manifest["file_count"] == 17
    assert actual == manifest["files"]
    fingerprint = hashlib.sha256(json.dumps(actual, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    assert fingerprint == manifest["content_sha256"]
    upstream_hashes = (VENDOR / "PACKAGE-SHA256.txt").read_text(encoding="utf-8").splitlines()
    assert len(upstream_hashes) == 16
    for row in upstream_hashes:
        digest, name = row.split(maxsplit=1)
        assert actual[name] == digest
    registry = json.loads((ASSETS / "rules/registry.json").read_text(encoding="utf-8"))
    entries = {entry["key"]: entry for entry in registry["entries"]}
    assert len(entries) == len(registry["entries"])
    entry = entries[registry["project_defaults"]["project-default"]]
    assert entry["enabled"] is True
    assert entry["usage"] == "active_project_rule"
    path = (ASSETS / "rules" / entry["relative_path"]).resolve()
    assert path.is_relative_to((ASSETS / "rules").resolve())
    raw = path.read_bytes()
    assert raw == (VENDOR / "rules-v1.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
    rules = json.loads(raw)
    assert entry["rule_id"] == rules["rule_set_id"]
    assert entry["rule_version"] == rules["version"]
    assert entry["engine_content_sha256"] == manifest["content_sha256"]
    assert entry["upstream_notice"] == rules["notice"]
    assert entry["scope"] == {
        "material_family": rules["scope_material_family"],
        "manufacturing_process": rules["scope_process"],
        "pouring_method": rules["scope_pouring_method"],
    }


def test_golden_fixture_provenance_hashes():
    provenance = json.loads((FIXTURES / "provenance.json").read_text(encoding="utf-8"))
    manifest = json.loads((ASSETS / "engine-manifest.json").read_text(encoding="utf-8"))
    assert provenance["engine_content_sha256"] == manifest["content_sha256"]
    for case in provenance["cases"]:
        assert hashlib.sha256((FIXTURES / f"{case['case']}.recommendation.json").read_bytes()).hexdigest() == case["fixture_sha256"]
    for name, digest in provenance["input_sha256"].items():
        assert hashlib.sha256((FIXTURES / f"{name}.input.json").read_bytes()).hexdigest() == digest
