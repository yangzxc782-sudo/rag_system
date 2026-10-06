"""Caller-owned short SQL transactions. Never performs object storage or engine IO."""
from uuid import UUID, uuid4

from sqlalchemy import select, tuple_

from app.models.casting_design_file import CastingDesignFile
from app.models.casting_design_run import CastingDesignRun
from app.models.qa_turn import QATurn
from app.schemas.casting_storage import CastingStorageError
from app.services.conversation_repository import ConversationRepository, fingerprint


class CastingRepository:
    def __init__(self, db):
        self.db = db
        self.chat = ConversationRepository(db)

    def file(self, sid: UUID, fid: UUID, *, ready=False, input_only=False):
        self.chat.get_session(sid)
        row = self.db.scalar(select(CastingDesignFile).where(
            CastingDesignFile.session_id == sid, CastingDesignFile.id == fid))
        if row is None or (input_only and row.kind != "input"):
            raise CastingStorageError("CASTING_FILE_NOT_FOUND", "当前会话中未找到工程文件", status=404, category="file")
        if ready and row.storage_state != "ready":
            raise CastingStorageError("CASTING_FILE_NOT_READY", "工程文件尚未保存完成，请重试上传", status=409, category="file", retryable=True)
        return row

    def reserve_input(self, sid, request_id, filename, raw_hash, size, bucket):
        self.chat.get_session(sid, for_update=True)
        row = self.db.scalar(select(CastingDesignFile).where(
            CastingDesignFile.session_id == sid, CastingDesignFile.upload_request_id == request_id))
        if row is not None:
            if (row.sha256, row.size_bytes, row.original_filename) != (raw_hash, size, filename):
                raise CastingStorageError("CASTING_UPLOAD_CONFLICT", "上传 request_id 已用于其他内容", status=409, category="file")
            return row
        fid = uuid4()
        row = CastingDesignFile(id=fid, session_id=sid, upload_request_id=request_id, kind="input",
            original_filename=filename, content_type="application/json", sha256=raw_hash, size_bytes=size,
            bucket=bucket, object_key=f"casting/{sid}/inputs/{fid}.json", storage_state="pending")
        self.db.add(row)
        self.db.flush()
        return row

    def list_inputs(self, sid, *, limit=50, before_id=None):
        self.chat.get_session(sid)
        query = select(CastingDesignFile).where(CastingDesignFile.session_id == sid, CastingDesignFile.kind == "input")
        if before_id:
            cursor = self.file(sid, before_id, input_only=True)
            query = query.where(tuple_(CastingDesignFile.created_at, CastingDesignFile.id) < tuple_(cursor.created_at, cursor.id))
        return list(self.db.scalars(query.order_by(CastingDesignFile.created_at.desc(), CastingDesignFile.id.desc()).limit(limit)))

    def select_input(self, sid, requested_id):
        """Call while holding the QA session lock, before inserting a new turn."""
        if requested_id is not None:
            return self.file(sid, requested_id, ready=True, input_only=True).id
        return self.db.scalar(select(CastingDesignFile.id).join(QATurn,
            (QATurn.session_id == CastingDesignFile.session_id) &
            (QATurn.requested_casting_input_file_id == CastingDesignFile.id)).where(
                QATurn.session_id == sid, CastingDesignFile.storage_state == "ready",
                CastingDesignFile.kind == "input", CastingDesignFile.admission_passed.is_(True)
            ).order_by(QATurn.turn_no.desc()).limit(1))

    def run(self, sid, rid):
        self.chat.get_session(sid)
        row = self.db.scalar(select(CastingDesignRun).where(CastingDesignRun.session_id == sid, CastingDesignRun.id == rid))
        if row is None:
            raise CastingStorageError("CASTING_RUN_NOT_FOUND", "当前会话中未找到计算记录", status=404)
        return row

    def run_for_turn(self, sid, tid):
        self.chat.get_turn(sid, tid)
        return self.db.scalar(select(CastingDesignRun).where(CastingDesignRun.session_id == sid, CastingDesignRun.turn_id == tid))

    def reserve_run(self, sid, tid, rules):
        self.chat.get_session(sid, for_update=True)
        turn = self.chat.get_turn(sid, tid)
        existing = self.run_for_turn(sid, tid)
        if existing:
            return existing
        if turn.effective_casting_input_file_id is None:
            raise CastingStorageError("CASTING_INPUT_REQUIRED", "请先为本轮选择有效的工程输入 JSON", status=422, category="file")
        file = self.file(sid, turn.effective_casting_input_file_id, ready=True, input_only=True)
        from app.casting.execution_protocol import digest
        manifest_hash = digest(rules.engine_manifest_bytes)
        row = CastingDesignRun(id=uuid4(), session_id=sid, turn_id=tid, input_file_id=file.id,
            input_sha256=file.sha256, rule_id=rules.rule_id, rule_version=rules.version, rule_sha256=rules.sha256,
            registry_sha256=rules.registry_sha256, project_key=rules.project_key,
            engine_id=rules.engine_manifest["engine_id"], engine_version=rules.engine_manifest["engine_version"],
            engine_sha256=rules.engine_manifest["content_sha256"], engine_manifest_sha256=manifest_hash,
            dependency_manifest_sha256=fingerprint(rules.engine_manifest["runtime_dependencies"]),
            call_key=fingerprint({"tool": "generate_casting_design", "turn_id": str(tid), "input": file.sha256,
                                  "rules": rules.sha256, "engine": manifest_hash}), status="pending", execution_no=1)
        self.db.add(row)
        self.db.flush()
        return row

    def artifact(self, sid, rid, name):
        run = self.run(sid, rid)
        return self.db.scalar(select(CastingDesignFile).where(CastingDesignFile.run_id == rid,
            CastingDesignFile.session_id == sid, CastingDesignFile.execution_no == run.execution_no,
            CastingDesignFile.artifact_name == name))

    def reserve_artifact(self, sid, rid, name, kind, raw_hash, size, bucket):
        self.chat.get_session(sid, for_update=True)
        run = self.run(sid, rid)
        row = self.artifact(sid, rid, name)
        if row:
            if (row.sha256, row.size_bytes, row.kind) != (raw_hash, size, kind):
                raise CastingStorageError("CASTING_ARTIFACT_CONFLICT", "审计产物与已有记录不一致", status=409, category="integrity")
            return row
        row = CastingDesignFile(id=uuid4(), session_id=sid, run_id=rid, execution_no=run.execution_no,
            artifact_name=name, kind=kind, original_filename=name.rsplit("/", 1)[-1],
            content_type="application/json" if name.endswith(".json") else "application/octet-stream",
            sha256=raw_hash, size_bytes=size, bucket=bucket,
            object_key=f"casting/{sid}/runs/{rid}/{run.execution_no}/{name}", storage_state="pending")
        self.db.add(row)
        self.db.flush()
        return row
