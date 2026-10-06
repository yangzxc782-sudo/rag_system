"""Bounded input-v1 transport contract; engineering admission stays in the CLI.

Validate raw JSON without converting units, filling metadata defaults or changing
numeric representations. Only the original engine may normalize engineering data.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints,
    ValidationError, field_validator, with_config,
)
from typing_extensions import TypedDict

MAX_INPUT_BYTES = 256 * 1024
MAX_JSON_DEPTH = 16
MAX_HOTSPOTS = 16
MAX_RISER_SITES = 24
MAX_INGATE_SITES = 32
MAX_METADATA_ENTRIES = 64


def _finite_number(value: Any) -> int | float:
    if type(value) not in (int, float):
        raise ValueError("需要 JSON 数值，不接受布尔值或文本")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError("需要有限数值")
    return value


Text = Annotated[str, StringConstraints(min_length=1, max_length=512, pattern=r"\S")]
Identifier = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
]
Number = Annotated[int | float, BeforeValidator(_finite_number)]
PositiveNumber = Annotated[Number, Field(gt=0)]
NonNegativeNumber = Annotated[Number, Field(ge=0)]
Dimensions = Annotated[list[PositiveNumber], Field(min_length=3, max_length=3)]

# These paths come from ontology_runtime.metadata(), not from all JSON fields.
METADATA_PATH_PATTERN = (
    r"^(material_family|casting_mass_kg|main_wall_mm|min_wall_mm|max_wall_mm|"
    r"liquid_density_kg_m3|casting_envelope_mm|target_pour_time_s|effective_head_mm|"
    r"pour_temperature_degC|gating_metal_estimate_kg|sand_mass_estimate_kg|"
    r"print_block_mm|sand_cover_mm|riser_sites|ingate_sites|forbidden_gate_regions|"
    r"hotspots\.(0|[1-9][0-9]*)\.(modulus_mm|geometry_ref))$"
)
MetadataPath = Annotated[str, StringConstraints(max_length=128, pattern=METADATA_PATH_PATTERN)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CastingSourceRefs(_StrictModel):
    material: Text
    geometry: Text
    process: Text
    printer: Text


class CastingHotspot(_StrictModel):
    id: Identifier
    modulus_mm: PositiveNumber
    geometry_ref: Text


class CastingRiserSite(_StrictModel):
    id: Identifier
    feeds: list[Identifier] = Field(min_length=1, max_length=MAX_HOTSPOTS)
    max_diameter_mm: PositiveNumber
    geometry_ref: Text


class CastingIngateSite(_StrictModel):
    id: Identifier
    region: Text
    geometry_ref: Text


@with_config(ConfigDict(extra="forbid", strict=True))
class CastingParameterMetadata(TypedDict, total=False):
    # Empty unit is canonical for nonnumeric parameters. Missing is not null.
    unit: Annotated[str, StringConstraints(max_length=32)]
    status: Literal["Confirmed", "Derived", "Proposed", "Missing", "Conflicted"]
    source_ref: Text
    missing_reason: Text


class CastingDesignInput(_StrictModel):
    case_id: Identifier
    snapshot_id: Identifier
    product_name: Text | None = None
    material_family: Text
    manufacturing_process: Text
    pouring_method: Text
    casting_mass_kg: PositiveNumber
    casting_envelope_mm: Dimensions
    main_wall_mm: PositiveNumber
    max_wall_mm: PositiveNumber
    min_wall_mm: PositiveNumber
    liquid_density_kg_m3: PositiveNumber
    effective_head_mm: PositiveNumber
    target_pour_time_s: PositiveNumber
    pour_temperature_degC: PositiveNumber
    gating_metal_estimate_kg: NonNegativeNumber
    sand_mass_estimate_kg: PositiveNumber
    print_block_mm: Dimensions
    sand_cover_mm: PositiveNumber
    # Informational upstream field; this does not confirm thermal/CAE evidence.
    thermal_curve_status: Text | None = None
    source_refs: CastingSourceRefs
    hotspots: list[CastingHotspot] = Field(min_length=1, max_length=MAX_HOTSPOTS)
    riser_sites: list[CastingRiserSite] = Field(max_length=MAX_RISER_SITES)
    ingate_sites: list[CastingIngateSite] = Field(max_length=MAX_INGATE_SITES)
    forbidden_gate_regions: list[Text] = Field(max_length=64)
    parameter_metadata: dict[MetadataPath, CastingParameterMetadata] = Field(
        default_factory=dict, max_length=MAX_METADATA_ENTRIES
    )

    @field_validator("parameter_metadata")
    @classmethod
    def metadata_index_exists(cls, value, info):
        hotspots = info.data.get("hotspots")
        if hotspots is not None:
            for path in value:
                if path.startswith("hotspots.") and int(path.split(".")[1]) >= len(hotspots):
                    raise ValueError("元数据引用了不存在的热节索引")
        return value


@dataclass(frozen=True)
class CastingInputIssue:
    field_path: str
    error_code: str
    message: str


class CastingInputError(ValueError):
    """Transport validation only; distinct from engine AdmissionError.report."""

    def __init__(self, issues: list[CastingInputIssue]):
        self.issues = tuple(issues)
        super().__init__("；".join(f"{x.field_path}: {x.message}" for x in self.issues[:8]))


def _error(code: str, message: str, path: str = "input") -> CastingInputError:
    return CastingInputError([CastingInputIssue(path, code, message)])


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise _error("DuplicateKey", "JSON 对象中存在重复字段")
        obj[key] = value
    return obj


def _reject_constant(value: str) -> Any:
    raise _error("NonFinite", "JSON 不允许 NaN 或 Infinity")


def _check_tree(value: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise _error("TooDeep", f"JSON 嵌套深度不能超过 {MAX_JSON_DEPTH}")
    if isinstance(value, dict):
        for key, item in value.items():
            _check_tree(key, depth + 1)
            _check_tree(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check_tree(item, depth + 1)
    elif isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise _error("InvalidEncoding", "JSON 文本包含无效 Unicode 字符") from None
    elif type(value) in (int, float):
        try:
            _finite_number(value)
        except ValueError:
            raise _error("NonFinite", "JSON 包含超出有限数值范围的数字") from None


def parse_casting_input(raw: bytes) -> dict[str, Any]:
    """Validate bounded UTF-8 JSON and return the original, unnormalized payload.

    This is not an admission pass and does not authorize engine execution. Upload
    ownership, rule selection, work budgets and admission are later service steps.
    """
    if len(raw) > MAX_INPUT_BYTES:
        raise _error("TooLarge", f"JSON 文件不能超过 {MAX_INPUT_BYTES} 字节")
    try:
        value = json.loads(
            raw.decode("utf-8-sig"), object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except CastingInputError:
        raise
    except UnicodeDecodeError:
        raise _error("InvalidEncoding", "输入文件必须为 UTF-8 JSON") from None
    except RecursionError:
        raise _error("TooDeep", f"JSON 嵌套深度不能超过 {MAX_JSON_DEPTH}") from None
    except (ValueError, OverflowError):
        raise _error("InvalidJSON", "JSON 解析失败") from None
    _check_tree(value)
    if not isinstance(value, dict):
        raise _error("InvalidType", "输入根节点必须为 JSON 对象")
    try:
        CastingDesignInput.model_validate(value)
    except ValidationError as exc:
        issues = []
        codes = {
            "missing": ("Missing", "缺少必需字段"),
            "extra_forbidden": ("UnknownField", "不支持此字段"),
            "too_long": ("TooMany", "数组或对象超过数量上限"),
            "literal_error": ("InvalidStatus", "参数状态不在允许列表中"),
        }
        for item in exc.errors(include_url=False, include_input=False, include_context=False)[:100]:
            code, message = codes.get(item["type"], ("Invalid", item["msg"]))
            path = ".".join(["input", *(str(part) for part in item["loc"])])
            issues.append(CastingInputIssue(path, code, message))
        raise CastingInputError(issues) from None
    return value


def casting_input_json_schema() -> dict[str, Any]:
    """Published structural schema; runtime limits and engineering checks also apply."""
    schema = CastingDesignInput.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "urn:rag-system:casting:input:v1"
    schema["description"] = (
        "input-v1 传输结构契约。仍须通过原始引擎 admission；"
        "单位转换、规则适用性、参数确认、字段关系由引擎判定。"
        "文件最多 262144 字节、嵌套深度最多 16，拒绝重复键和非有限数值。"
    )
    return schema
