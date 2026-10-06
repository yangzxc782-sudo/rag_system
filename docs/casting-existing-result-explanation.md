# 已有浇冒系统结果追问增强

日期：2026-09-30。

## 实现范围

已有 `explain_existing` 路由现在把当次运行保存的完整 input、rules、recommendation 和当前问题交给模型，允许生成自然语言解释及解释性算式。

实际链路：`CastingAnswerNodes.generate_casting_answer` → `CastingDesignService.explanation_sources` → `explanation_request` → 已有 Provider → 已有答案快照与消息发布流程。

没有改变 LangGraph 拓扑、路由决策、ToolNode、casting engine、数据库表或迁移。没有增加 analysis projection、二级路由或新的工具。原计算完成后的 fact_refs 摘要和普通 RAG 保持原路径。既有已发布回答不自动改写。

## 冻结来源

`CastingDesignService.explanation_sources(session_id, run_id)` 只读：

- `CastingFiles.artifact_bytes(..., "input.json")`：run 执行时保存的原始输入快照，校验 `run.input_sha256`。
- `CastingFiles.artifact_bytes(..., "rules.json")`：run 冻结规则，校验 `run.rule_sha256`。
- `CastingDesignService.recommendation(...)`：原结果对象，校验会话、run、execution_no、kind、大小及 `run.result_sha256`。

继续复用 `CastingRepository.run/artifact/file` 的会话归属和执行编号校验，以及 `CastingFiles.read` 的对象大小、SHA256 校验。读取不调用 `prepare`、`reserve_run`、规则目录选择或 `engine.execute`，也不访问当前 vendor/input-v1.json、rules-v1.json。即使追问消息另选了新文件，解释来源仍是已选中的历史 run。

缺失、损坏或非法 JSON 快照会沿现有错误通道反馈，绝不以最新规则或文件替代。历史快照本来没有保存完整时，需恢复原快照，不能静默补造。

## Prompt 与预算

`casting_prompt.py:explanation_request` 序列化完整三个 JSON，不筛选字段、不删除候选、不截断。问题及简短作答要求放在 JSON 后部，避免大段 recommendation 埋没当前问题。

总上下文上限为 **96 KiB UTF-8 字节**，含系统 Prompt、三个 JSON、问题及作答要求；这是字节保护，并非精确 token 计数。输出上限 3072 tokens，模型请求超时 60 秒，不携带工具定义。

超限不发送部分事实给模型：保留原结果模板，并明确显示完整上下文超过上限，记录 `CASTING_EXPLANATION_TOO_LARGE`。原 24 KiB 摘要预算仍只控制原有摘要/模板，不限制历史完整 JSON 能否进入新解释 Prompt。

Prompt 明确 recommendation 是实际规格、工程参数、候选排序、检查结论的事实源；input/rules 可用于解释性复核，不能覆盖实际结果。约束包括目录尺寸来源、热节与位置映射、单位转换、系数只乘一次、local-min 的选择含义、无候选只解释 rejected_attempts，以及不把未做 CAE 等同于验证通过。

## 回答保存与兼容

此前模型只能选择 fact_refs，因此单独增加输入上下文不足以产生解释。现在仅 `explain_existing` 的成功/无候选结果允许 `summary_mode="llm_explanation"`；其正文使用既有答案快照保存，renderer 标记为 `casting_explanation_v1`。

`CastingGenerationDetails.answer_text_sha256` 保存解释正文哈希，旧记录默认空值兼容。来源校验限制解释模式只能用于历史成功 run；恢复时验证正文哈希、原 run 和生成产物链接，不强行通过原确定性 renderer 重建自由文本。新计算仍使用原模板/事实引用约束。前端仅补充 summary_mode 类型，无 UI 重写。

正文哈希证明内容未在保存、恢复和发布之间改变，**不证明模型解释在工程语义上正确**。三个 JSON 只进入本次请求内存，不进入 checkpoint，也不再次复制进业务答案快照。

## 文件清单

| 文件 | 变更 |
|---|---|
| `backend/app/services/casting_design.py` | 增加只读 `explanation_sources` |
| `backend/app/rag/casting_prompt.py` | 完整 JSON 解释请求、事实边界、上下文保护 |
| `backend/app/rag/casting_answer_nodes.py` | 历史解释正文生成、模板退回、恢复兼容 |
| `backend/app/schemas/casting_answer.py` | 解释模式、renderer 标记、正文哈希；JSON 产物兼容扩展 |
| `backend/app/services/casting_provenance.py` | 解释模式来源与正文哈希校验 |
| `frontend/lib/casting-design.ts` | 补充解释模式联合类型 |
| `backend/tests/test_casting_explanation.py` | 完整上下文、预算、快照和完整性单测 |
| `backend/tests/phase13_integration/test_casting_answers_postgresql.py` | 追问、重试、来源冻结、无候选、恢复和篡改回归 |

## 验证

- 离线协议、来源、摘要、存储合同及恢复输入测试：**58 passed**。
- 真实隔离 PostgreSQL + 原 Python 引擎 + 合成模型/内存对象存储回归：**29 passed，1 skipped**。跳过项是未启动的独立 MinIO 验证，不计为通过。
- 最终上下文的定向复查：**14 passed**，与上述用例重叠，不累加为独立总数。
- 前端 TypeScript 检查：通过。

集成回归实际验证：计算一次后，追问及模型超时重试均不执行 prepare/reserve_run/engine.execute；会话中仍只有一个 run；选入新文件、禁用当前规则读取都不改变解释上下文；历史第二候选拿到完整推荐结果；无候选拿到原全部淘汰记录；超限明确退回；缺失快照停止解释；正文被改写且只重算快照哈希时拒绝发布；checkpoint 不含完整工程数据。

初轮测试中的失败来自新测试错误地假设 Graph 会原样抛出内部异常类型/文案，以及错误访问错误对象的 category 属性。已改为核对现有 Graph 的公开 error_code 和 safe_detail，并重跑通过。

## 真实 GPT-4o-mini 语义核验及未通过项

使用当前配置 Provider 与仓库 baseline 测试 input/rules/recommendation，覆盖用户提出的六类问题；没有发送业务会话数据或触发新工程计算。根据观察迭代了 Prompt，但未更换模型或配置。

最后定向样本中，高度问题能定位 RS-01/R170/220 mm，34.5 mm 问题能说明 HS-01 的 `30 × 1.15`，R160 问题能纠正前提并说明它已用于 RS-05。

**候选比较的语义验收尚未全部通过。** C01/C02 实测能复制规格并区分 local-min 与 local-plus-one，但仍出现“C02 为满足更高要求模数”的误述；实际对应位置的 required_modulus_mm 相同。较早样本还出现过位置混用、重复乘系数、错误字段路径和未经验证的性能推断。因此不能宣称六类真实模型回答稳定全部正确，也不能将上述程序回归视为工程解释正确率验收。

本轮按要求保持“完整 JSON + 直接 LLM 解释”，未额外加入语义校验模型或复杂分析层。确定性 recommendation 文件及其结果参数不受这些自然语言误述影响。

本地实测留存（被 Git 忽略，保留审计）：`backend/.casting-explanation-live-targeted-8fa9f2073a8a439489ad1b5e5a4ca0d2.tmp/`。`1.json`、`3.json`、`4.json` 为最后高度/规格/算式样本；`6-comparison.json` 为最终候选比较未通过样本，先前版本也保留。

业务库未做迁移，实际 .env 与模型配置未修改，业务服务未重启。本轮只复用了专用 PostgreSQL 测试容器，新测试库与数据保留；测试结束后恢复容器停止状态。
