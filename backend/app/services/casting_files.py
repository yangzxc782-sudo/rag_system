"""Independent engineering objects. No Document/ingestion dependencies."""
from dataclasses import asdict
from collections.abc import Callable
import logging
from pathlib import PurePosixPath
from typing import Protocol
from uuid import UUID

import certifi
from minio import Minio
from urllib3 import PoolManager, Timeout
from urllib3.util.retry import Retry
from sqlalchemy.orm import Session

from app.casting.execution_protocol import digest
from app.schemas.casting_design import MAX_INPUT_BYTES, CastingInputError, parse_casting_input
from app.schemas.casting_storage import CastingInputFilesView, CastingStorageError, input_view
from app.models.casting_design_file import CastingDesignFile
from app.services.casting_repository import CastingRepository
from app.services.object_storage import get_object_bytes_from_minio, upload_bytes_to_minio

logger = logging.getLogger(__name__)


class EngineeringStorage(Protocol):
    def put(self, row: CastingDesignFile, raw: bytes) -> None: ...
    def get(self, row: CastingDesignFile) -> bytes: ...


class CastingObjectStorage:
    def __init__(self, settings):
        self.http = PoolManager(timeout=Timeout(connect=5, read=30), retries=Retry(total=0),
            cert_reqs="CERT_REQUIRED" if settings.minio_secure else "CERT_NONE", ca_certs=certifi.where())
        self.client = Minio(settings.minio_endpoint, access_key=settings.minio_root_user,
            secret_key=settings.minio_root_password, secure=settings.minio_secure, http_client=self.http)

    def put(self, row: CastingDesignFile, raw: bytes) -> None:
        upload_bytes_to_minio(bucket_name=row.bucket, object_key=row.object_key, content=raw,
                              content_type=row.content_type, client=self.client)

    def get(self, row: CastingDesignFile) -> bytes:
        return get_object_bytes_from_minio(bucket_name=row.bucket, object_key=row.object_key,
                                           client=self.client, max_bytes=row.size_bytes)

    def close(self) -> None:
        self.http.clear()


class CastingFiles:
    def __init__(self, session_factory: Callable[[], Session], storage: EngineeringStorage, *, bucket: str,
                 after_put: Callable[[CastingDesignFile], None] | None = None):
        self.session_factory, self.storage, self.bucket = session_factory, storage, bucket
        self.after_put = after_put  # Fault injection seam; never request data.

    def read(self, row: CastingDesignFile) -> bytes:
        try:
            raw = self.storage.get(row)
        except Exception:
            raise CastingStorageError("CASTING_STORAGE_UNAVAILABLE", "工程文件存储暂不可用", retryable=True) from None
        if len(raw) != row.size_bytes or digest(raw) != row.sha256:
            raise CastingStorageError("CASTING_FILE_INTEGRITY", "工程文件大小或 SHA256 校验失败", category="integrity")
        return raw

    def persist(self, row: CastingDesignFile, raw: bytes) -> CastingDesignFile:
        if len(raw) != row.size_bytes or digest(raw) != row.sha256:
            raise CastingStorageError("CASTING_FILE_INTEGRITY", "待保存工程文件与记录不一致", category="integrity")
        if row.storage_state != "ready":
            try:
                self.storage.put(row, raw)
            except Exception:
                raise CastingStorageError("CASTING_STORAGE_UNAVAILABLE", "工程文件保存中断，请使用原 request_id 重试", retryable=True) from None
        self.read(row)  # Both new writes and idempotent replay verify exact bytes.
        if self.after_put:
            self.after_put(row)
        with self.session_factory() as db, db.begin():
            repo = CastingRepository(db)
            repo.chat.get_session(row.session_id, for_update=True)
            saved = repo.file(row.session_id, row.id)
            saved.storage_state = "ready"
            db.flush()
            db.expunge(saved)
        logger.info("Casting file ready: session=%s file=%s kind=%s", row.session_id, row.id, row.kind)
        return saved

    def upload(self, sid: UUID, request_id: UUID, filename: str, raw: bytes):
        if not isinstance(request_id, UUID):
            raise CastingStorageError("CASTING_REQUEST_INVALID", "上传 request_id 必须为 UUID", status=422, category="file")
        if len(raw) > MAX_INPUT_BYTES:
            raise CastingStorageError("CASTING_FILE_TOO_LARGE", "工程 JSON 不能超过 256 KiB", status=413, category="file")
        if (not filename or len(filename) > 255 or "/" in filename or "\\" in filename
                or any(ord(c) < 32 for c in filename) or PurePosixPath(filename).suffix.lower() != ".json"):
            raise CastingStorageError("CASTING_FILE_TYPE", "请上传具有有效文件名的 .json 文件", status=422, category="file")
        with self.session_factory() as db, db.begin():
            CastingRepository(db).chat.get_session(sid)
        try:
            parse_casting_input(raw)
        except CastingInputError as exc:
            raise CastingStorageError("CASTING_INPUT_INVALID", "工程输入 JSON 未通过结构检查", status=422, category="file",
                                      issues=[asdict(issue) for issue in exc.issues][:100]) from None
        with self.session_factory() as db, db.begin():
            row = CastingRepository(db).reserve_input(sid, request_id, filename, digest(raw), len(raw), self.bucket)
            db.expunge(row)
        return input_view(self.persist(row, raw))

    def list_inputs(self, sid: UUID, *, limit: int = 50, before_id: UUID | None = None) -> CastingInputFilesView:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise CastingStorageError("CASTING_PAGE_INVALID", "分页数量应为 1 至 50", status=422, category="file")
        with self.session_factory() as db, db.begin():
            repo = CastingRepository(db)
            rows = repo.list_inputs(sid, limit=limit + 1, before_id=before_id)
            reusable = repo.select_input(sid, None)
            return CastingInputFilesView(items=[input_view(row) for row in rows[:limit]],
                next_before_id=rows[limit - 1].id if len(rows) > limit else None,
                reusable_input=input_view(repo.file(sid, reusable, ready=True, input_only=True)) if reusable else None)

    def input_bytes(self, sid: UUID, fid: UUID) -> bytes:
        with self.session_factory() as db, db.begin():
            row = CastingRepository(db).file(sid, fid, ready=True, input_only=True)
            db.expunge(row)
        return self.read(row)

    def save_artifact(self, sid, rid, name, kind, raw):
        # Names originate from the fixed engine protocol, never from HTTP arguments.
        if not name or ".." in name or name.startswith("/") or "\\" in name or len(name) > 100:
            raise ValueError("Invalid internal artifact name")
        with self.session_factory() as db, db.begin():
            row = CastingRepository(db).reserve_artifact(sid, rid, name, kind, digest(raw), len(raw), self.bucket)
            db.expunge(row)
        return self.persist(row, raw)

    def artifact_bytes(self, sid, rid, name):
        with self.session_factory() as db, db.begin():
            row = CastingRepository(db).artifact(sid, rid, name)
            if row is None or row.storage_state != "ready":
                raise CastingStorageError("CASTING_SNAPSHOT_NOT_READY", "运行快照尚未保存完成", status=409, retryable=True)
            db.expunge(row)
        return self.read(row)
