# MinIO 本地说明

MinIO 只作为本地对象存储基础服务，用于后续保存原始文档、解析结果和中间资源。Docker Compose 不自动创建 bucket。

## 手动创建 bucket

1. 确认基础服务由用户手动启动。
2. 打开 MinIO Console：`http://localhost:9001`。
3. 使用根目录 `.env` 中的 `MINIO_ROOT_USER` 和 `MINIO_ROOT_PASSWORD` 登录；本地开发默认值为 `rag_minio` / `rag_minio_password`。
4. 手动创建 bucket：`rag-documents`。

## Compose 命令格式

所有 Docker Compose 命令都应从项目根目录执行，并统一使用：

```powershell
docker compose --env-file .env -f infra/docker-compose.yml ps
docker compose --env-file .env -f infra/docker-compose.yml logs minio
```