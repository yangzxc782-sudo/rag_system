"""Persisted casting orchestration, shared by the API and bound LangGraph tool."""
from datetime import datetime, timezone
import logging
from uuid import UUID

from app.casting.execution_protocol import (
    OUTPUT_FILES, PROTOCOL_VERSION, CastingExecutionError, digest, read_bounded, strict_json,
)
from app.casting.process_control import calculation_slot
from app.schemas.casting_design import parse_casting_input
from app.schemas.casting_storage import CastingRunView, CastingStorageError, run_view
from app.services.casting_engine import CastingEngine, verify_completion
from app.services.casting_files import CastingFiles
from app.models.casting_design_run import CastingDesignRun
from app.services.casting_repository import CastingRepository
from app.services.casting_rules import FrozenRules


ROOT_ARTIFACTS = ("input.json", "request.json", "execution.json", "runtime.json", "capacity.json",
                  "stdout.log", "stderr.log", "worker-result.json", "admission-error.json")
SUCCESS = {"succeeded", "no_feasible_candidate"}
FAILURE = {"admission_failed", "engine_failed", "timed_out", "interrupted"}
logger = logging.getLogger(__name__)


class CastingDesignService:
    def __init__(self, files: CastingFiles, engine: CastingEngine, *, project_key: str = "project-default", before_finish=None):
        self.files, self.engine, self.project_key = files, engine, project_key
        self.session_factory = files.session_factory
        self.before_finish = before_finish  # Crash/commit fault testing only.

    def bind_session_factory(self, session_factory):
        files = CastingFiles(session_factory, self.files.storage, bucket=self.files.bucket, after_put=self.files.after_put)
        return CastingDesignService(files, self.engine, project_key=self.project_key, before_finish=self.before_finish)

    def _row(self, sid: UUID, rid: UUID) -> CastingDesignRun:
        with self.session_factory() as db, db.begin():
            row = CastingRepository(db).run(sid, rid)
            db.expunge(row)
        return row

    def get_run(self, sid: UUID, rid: UUID) -> CastingRunView:
        return run_view(self._row(sid, rid))

    def _snapshot_rules(self, row, candidate=None):
        try:
            raw = self.files.artifact_bytes(row.session_id, row.id, "rules.json")
            manifest = self.files.artifact_bytes(row.session_id, row.id, "engine-manifest.json")
        except CastingStorageError as exc:
            if exc.code != "CASTING_SNAPSHOT_NOT_READY" or row.status != "pending":
                raise
            # A crash during initial snapshot upload can only continue with the
            # exact frozen version. Never silently replace it with current rules.
            candidate = candidate or self.engine.rules.select(
                parse_casting_input(self.files.input_bytes(row.session_id, row.input_file_id)), row.project_key)
            raw, manifest = candidate.raw, candidate.engine_manifest_bytes
            if digest(raw) != row.rule_sha256 or digest(manifest) != row.engine_manifest_sha256:
                raise CastingStorageError("CASTING_SNAPSHOT_NOT_READY", "请恢复本次运行冻结的规则和引擎快照后重试", status=409) from None
            self.files.save_artifact(row.session_id, row.id, "rules.json", "rules", raw)
            self.files.save_artifact(row.session_id, row.id, "engine-manifest.json", "engine", manifest)
        if digest(raw) != row.rule_sha256 or digest(manifest) != row.engine_manifest_sha256:
            raise CastingStorageError("CASTING_SNAPSHOT_INTEGRITY", "运行快照 SHA256 不一致", category="integrity")
        return FrozenRules(raw, row.rule_id, row.rule_version, row.rule_sha256, row.registry_sha256,
                           row.project_key, strict_json(manifest), manifest, self.engine.rules.assets)

    def prepare(self, sid: UUID, tid: UUID) -> CastingDesignRun:
        with self.session_factory() as db, db.begin():
            repo = CastingRepository(db)
            row = repo.run_for_turn(sid, tid)
            fid = repo.chat.get_turn(sid, tid).effective_casting_input_file_id
            if row:
                db.expunge(row)
                return row
        if fid is None:
            raise CastingStorageError("CASTING_INPUT_REQUIRED", "本轮没有有效的工程输入 JSON", status=422, category="file")
        raw = self.files.input_bytes(sid, fid)
        rules = self.engine.rules.select(parse_casting_input(raw), self.project_key)
        with self.session_factory() as db, db.begin():
            row = CastingRepository(db).reserve_run(sid, tid, rules)
            db.expunge(row)
        return row

    def _save_audit(self, row, directory):
        for name in ROOT_ARTIFACTS + tuple("output/" + name for name in OUTPUT_FILES):
            path = directory / name
            if path.is_file():
                raw = read_bounded(path, self.engine.limits.max_output_bytes)
                kind = "recommendation" if name == "output/recommendation.json" else "audit"
                self.files.save_artifact(row.session_id, row.id, name, kind, raw)

    def _failure(self, row, error, directory):
        public = error.public_dict()
        public["run_id"] = str(row.id)
        status = ("admission_failed" if error.category == "admission" else "timed_out"
                  if error.code == "CASTING_TIMEOUT" else "interrupted"
                  if error.code == "CASTING_RUN_INTERRUPTED" else "engine_failed")
        with self.session_factory() as db, db.begin():
            repo = CastingRepository(db)
            repo.chat.get_session(row.session_id, for_update=True)
            saved = repo.run(row.session_id, row.id)
            saved.status, saved.error, saved.finished_at = status, public, datetime.now(timezone.utc)
        if directory.is_dir():
            self._save_audit(row, directory)
        logger.warning("Casting execution failed: session=%s run=%s code=%s", row.session_id, row.id, error.code)
        return self.get_run(row.session_id, row.id)

    def execute_turn(self, sid: UUID, tid: UUID) -> CastingRunView:
        """Server-only entry. No model-provided paths, rules, run IDs or execution numbers."""
        row = self.prepare(sid, tid)
        lock_root = self.engine.work_root / ".persistence-locks" / str(row.id)
        lock_root.mkdir(parents=True, exist_ok=True)
        with calculation_slot(lock_root):
            row = self._row(sid, row.id)
            if row.status in SUCCESS:
                self.recommendation(sid, row.id)  # Verify storage before replay.
                return run_view(row)
            directory = self.engine.work_root / str(row.id) / str(row.execution_no)
            if row.status in FAILURE:
                if directory.is_dir():
                    self._save_audit(row, directory)
                return run_view(row)
            rules = self._snapshot_rules(row)
            raw = self.files.input_bytes(sid, row.input_file_id)
            if digest(raw) != row.input_sha256:
                raise CastingStorageError("CASTING_INPUT_INTEGRITY", "运行输入与冻结的 SHA256 不一致", category="integrity")
            with self.session_factory() as db, db.begin():
                saved = CastingRepository(db).run(sid, row.id)
                if saved.status == "pending":
                    saved.status, saved.started_at = "running", datetime.now(timezone.utc)
            try:
                if directory.exists():
                    if not (directory / "worker-result.json").is_file():
                        raise CastingExecutionError("CASTING_RUN_INTERRUPTED", "system", "上次运行中断，缺少完整完成标记；保留审计且不自动重算")
                    request = dict(protocol_version=PROTOCOL_VERSION, run_id=str(row.id), execution_no=row.execution_no,
                        input_sha256=row.input_sha256, rules_sha256=row.rule_sha256,
                        engine_manifest_sha256=row.engine_manifest_sha256)
                    marker = strict_json(read_bounded(directory / "worker-result.json", 256 * 1024))
                    result = verify_completion(directory, request, rules, parse_casting_input(raw),
                        1 if marker.get("status") == "failed" else 0, self.engine.limits)
                else:
                    result = self.engine.execute(raw, run_id=row.id, execution_no=row.execution_no, frozen_rules=rules)
            except CastingExecutionError as exc:
                # Busy before launch is retryable and must not consume the run.
                if exc.code == "CASTING_BUSY" and not directory.exists():
                    raise
                return self._failure(row, exc, directory)
            except (OSError, ValueError, KeyError, TypeError):
                return self._failure(row, CastingExecutionError("CASTING_OUTPUT_INVALID", "integrity", "已有计算产物或标记校验失败"), directory)
            with self.session_factory() as db, db.begin():
                saved = CastingRepository(db).run(sid, row.id)
                saved.status = "persisting"
            self._save_audit(row, directory)
            if self.before_finish:
                self.before_finish(row)
            with self.session_factory() as db, db.begin():
                repo = CastingRepository(db)
                repo.chat.get_session(sid, for_update=True)
                saved = repo.run(sid, row.id)
                file = repo.artifact(sid, row.id, "output/recommendation.json")
                if file is None or file.storage_state != "ready" or file.sha256 != result.result_sha256:
                    raise CastingStorageError("CASTING_OUTPUT_INVALID", "推荐结果尚未保存或校验未通过", category="integrity")
                saved.status = "succeeded" if result.status == "success" else "no_feasible_candidate"
                saved.result_file_id, saved.result_sha256 = file.id, file.sha256
                saved.candidate_count = len(result.recommendation["candidates"])
                saved.recommended_candidate_id = result.recommendation["recommended_candidate_id"]
                saved.normalized_input_sha256 = result.recommendation["input_fingerprint"]
                saved.finished_at, saved.error = datetime.now(timezone.utc), None
                repo.file(sid, row.input_file_id, input_only=True).admission_passed = True
                db.flush()
                view = run_view(saved)
            logger.info("Casting execution persisted: session=%s run=%s status=%s", sid, row.id, view.status)
            return view

    def recommendation(self, sid: UUID, rid: UUID) -> tuple[bytes, str]:
        row = self._row(sid, rid)
        if row.status not in SUCCESS:
            raise CastingStorageError("CASTING_RESULT_NOT_READY", "该运行尚无完整推荐结果", status=409, run_id=rid)
        with self.session_factory() as db, db.begin():
            file = CastingRepository(db).file(sid, row.result_file_id, ready=True)
            if (file.run_id != rid or file.execution_no != row.execution_no or file.kind != "recommendation"
                    or file.sha256 != row.result_sha256):
                raise CastingStorageError("CASTING_OUTPUT_INVALID", "推荐结果归属或 SHA256 不一致", category="integrity")
            db.expunge(file)
        return self.files.read(file), row.result_sha256

    def explanation_sources(self, sid: UUID, rid: UUID) -> dict:
        """Read only the completed run's immutable objects, never current assets."""
        row = self._row(sid, rid)
        if row.status not in SUCCESS:
            raise CastingStorageError("CASTING_RESULT_NOT_READY", "该运行尚无完整推荐结果", status=409, run_id=rid)
        sources = {}
        for key, name, expected in (("input", "input.json", row.input_sha256),
                                    ("rules", "rules.json", row.rule_sha256)):
            raw = self.files.artifact_bytes(sid, rid, name)
            if digest(raw) != expected:
                raise CastingStorageError("CASTING_SNAPSHOT_INTEGRITY", "运行快照 SHA256 不一致", category="integrity")
            sources[key] = raw
        raw, sha = self.recommendation(sid, rid)
        if sha != row.result_sha256 or digest(raw) != sha:
            raise CastingStorageError("CASTING_OUTPUT_INVALID", "推荐结果与运行记录不一致", category="integrity")
        sources["recommendation"] = raw
        try:
            parsed = {key: strict_json(value) for key, value in sources.items()}
            if any(not isinstance(value, dict) for value in parsed.values()):
                raise ValueError("Expected JSON objects")
        except (ValueError, TypeError, OverflowError, RecursionError):
            raise CastingStorageError("CASTING_SNAPSHOT_INTEGRITY", "运行快照不是有效的 JSON 对象", category="integrity") from None
        return parsed
