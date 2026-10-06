# 浇冒系统智能设计第一阶段验收记录

日期：2026-09-29。范围：引擎归档、规则注册、输入契约、黄金样例和独立运行验证。

## 已完成

- 附件全部 17 个文件原样保存于 `backend/app/casting/vendor/v5_1/`。归档前后逐文件 SHA256、附件内 16 条 `PACKAGE-SHA256.txt` 记录、最后一次 ZIP 字节比对均通过。
- 当前正式项目规则注册为 `project-default → PMP-TRIAL-RULES@1`，活动规则与附件 `rules-v1.json` 字节一致，保留原始来源说明。
- 输入结构模型、严格 JSON 解析、容量边界、结构化错误及可发布 JSON Schema 已完成。入站解析不进行单位转换，不补工程参数；原 admission 继续承担工程准入。
- 已创建独立 `backend/.venv-casting/`，安装 `requirements-casting.txt` 中八项锁定依赖；后端运行环境未安装这些引擎依赖。
- 三个完整黄金输出来自此前实际执行的未修改附件程序，覆盖基准、单位换算、无可行方案；新增测试完整比对计算值、排序、规则检查与解释。
- wheel 构建及资源校验通过：17 个原包文件与 manifest、Schema、规则注册表、活动规则共 4 个资源均逐字节一致。

## 执行结果

环境：Windows；Python 3.13.9；Pydantic 2.13.4；pytest 8.4.2；rdflib 7.6.0；pyshacl 0.40.1。其余引擎依赖见锁定文件。

执行的 pytest 文件：

```text
backend/tests/test_casting_contracts.py
backend/tests/test_casting_engine.py
backend/tests/test_llm_messages.py
backend/tests/test_conversations_schema.py
```

**最终结果：117 passed in 21.82s；0 failed、0 skipped。**

其中新增结构/资产契约测试 46 项、独立引擎测试 14 项，现有消息和会话契约回归 57 项。引擎测试运行了真实 Python 计算、RDF 构建与 SHACL；没有用 mock 替代计算。

| 核验项 | 结果 |
| --- | --- |
| 基准完整 recommendation 比较 | 通过；4 个候选，推荐 PMP-S1-C01，出品率 65.51% |
| 630000 g 单位转换 | 通过；转换为 630.0 kg；原输入保持原单位 |
| 无可行方案 | 通过；空候选、null 推荐、拒绝原因和 RDF/SHACL 输出均保留 |
| 缺字段、未确认参数、非法单位、规则范围不符 | 通过；捕获原 AdmissionError.report，未发布 recommendation |
| 包内 v2 / input-only / rules-only 独立样例 | 通过；候选数依次 3 / 3 / 4 |
| 指定 run_id、独立输出目录、任意工作目录 | 通过；使用函数入口，基准输出未被另一运行覆盖 |
| 字节/数组/深度/类型/重复键/Unicode 边界 | 通过 |
| 本体参数路径与输入契约、JSON Schema 同步 | 通过 |
| ZIP/vendor/活动规则/黄金夹具哈希 | 通过 |
| wheel 包构建及资源字节校验 | 通过 |
| `git diff --check` 与新增 Python 静态解析、行尾空白检查 | 通过 |

测试产物位于 `backend/.casting-acceptance-2764bd293d494dc6bccc18bf34846201.tmp/pytest/`；wheel 位于 `backend/.casting-package-check.tmp/`。这些都是 Git 忽略的本机产物，不是工程运行记录存储。

## 保留的已知问题

规则版本变化、输入版本不变且传入 `previous_dir` 时，原程序合并历史图触发 17 项 `numberValue` 的 SHACL maxCount 错误。测试确认异常类型、结构化报告和未生成 recommendation；该项通过表示**成功捕获已知故障**，不是功能修复。第一版接入禁用历史图合并，以独立 run 重算。

基准子进程约 2.35 秒、无候选约 1.25 秒，仅代表当前样例和机器；没有完成并发、压力或最大输入耗时验收。数组上限也不能代替后续组合预算及进程超时。

## 后续范围

尚未实施 Provider/transport 工具调用、LangGraph 分支、工程附件 API、计算服务/worker、数据库持久化、模型结果摘要或前端附件入口。未执行数据库迁移、真实模型请求、浏览器端到端验收或部署；现有 RAG 运行链和 `.env` 没有修改。

下一阶段以 `casting-design-integration.md` 的契约继续实现独立计算服务、运行目录、规则选择与结果保存，再接入模型工具和会话流程。
