"""Server-owned rule selection. No client paths and no implicit version fallback."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.casting.execution_protocol import (
    CastingExecutionError, confined_path, digest, read_bounded, strict_json, verify_engine,
)

ASSETS = Path(__file__).resolve().parents[1] / "casting"


@dataclass(frozen=True)
class FrozenRules:
    raw: bytes
    rule_id: str
    version: str
    sha256: str
    registry_sha256: str
    project_key: str
    engine_manifest: dict[str, Any]
    engine_manifest_bytes: bytes
    assets: Path


class CastingRuleSelector:
    def __init__(self, assets: Path = ASSETS):
        self.assets = assets.resolve()

    def select(self, data: dict[str, Any], project_key: str) -> FrozenRules:
        try:
            manifest_raw = read_bounded(self.assets / "engine-manifest.json", 64 * 1024)
            manifest = strict_json(manifest_raw)
            verify_engine(self.assets, manifest)
            if (not isinstance(manifest["runtime_dependencies"], dict) or not manifest["runtime_dependencies"]
                    or not all(isinstance(k, str) and isinstance(v, str) and v for k, v in manifest["runtime_dependencies"].items())
                    or not isinstance(manifest["supported_scopes"], list)):
                raise ValueError("Invalid engine runtime contract")
            registry_raw = read_bounded(self.assets / "rules/registry.json", 256 * 1024)
            registry = strict_json(registry_raw)
            if registry["registry_version"] != 1:
                raise ValueError("Unsupported registry")
            entries = registry["entries"]
            if len({entry["key"] for entry in entries}) != len(entries):
                raise ValueError("Duplicate rule identity")
            default = registry["project_defaults"].get(project_key)
            scope = {name: data[name] for name in ("material_family", "manufacturing_process", "pouring_method")}
            candidates = [entry for entry in entries if entry["enabled"] is True
                          and entry["project_key"] == project_key and entry["scope"] == scope]
            if len(candidates) > 1:
                raise CastingExecutionError("CASTING_RULE_AMBIGUOUS", "system", "项目存在多个适用规则版本，请检查规则配置")
            if not candidates or default is None or candidates[0]["key"] != default:
                raise CastingExecutionError("CASTING_RULE_NOT_APPLICABLE", "admission", "当前项目没有已启用且适用的规则版本")
            entry = candidates[0]
            if entry["engine_id"] != manifest["engine_id"] or entry["engine_content_sha256"] != manifest["content_sha256"]:
                raise ValueError("Rule engine binding mismatch")
            if scope not in manifest["supported_scopes"]:
                raise CastingExecutionError("CASTING_ENGINE_UNSUPPORTED", "admission", "当前引擎尚未验证此材料、造型或浇注方式")
            raw = read_bounded(confined_path(self.assets / "rules", entry["relative_path"]), 256 * 1024)
            if digest(raw) != entry["sha256"]:
                raise ValueError("Rule hash mismatch")
            rules = strict_json(raw)
            if (rules["rule_set_id"] != entry["rule_id"] or rules["version"] != entry["rule_version"]
                    or rules.get("status", "Active") != "Active"
                    or rules["source_ref"] != entry["source_ref"]
                    or rules["source_edition"] != entry["source_edition"]
                    or rules["notice"] != entry["upstream_notice"]
                    or {"material_family": rules["scope_material_family"], "manufacturing_process": rules["scope_process"],
                        "pouring_method": rules["scope_pouring_method"]} != scope):
                raise ValueError("Rule metadata mismatch")
            return FrozenRules(raw, entry["rule_id"], entry["rule_version"], entry["sha256"],
                               digest(registry_raw), project_key, manifest, manifest_raw, self.assets)
        except CastingExecutionError:
            raise
        except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError) as exc:
            raise CastingExecutionError("CASTING_RULE_INTEGRITY", "integrity", "引擎或规则配置完整性校验失败") from exc
