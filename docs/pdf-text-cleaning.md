# PDF 文本清洗服务

## 当前接入边界（结构感知恢复 S4）

当前 PDF 清洗必需且默认开启，显式关闭会拒绝解析；旧非 PDF/关闭 fallback 不再适用。
清洗块由共享 renderer 渲染后冻结为 canonical_text/source-map v2。构图完成后，唯一
block_chunker 从冻结目录读取章节/格式/资产及精确区间，不再从可变 DocumentBlock 重渲染切片。
chunk.content 始终等于 canonical_text[start:end]，不存在旧 renderer adapter 或 sequential 分支。
正文、表格、公式及尽力 overlap 使用完整六字段配置，详见 pdf-only-kg-pipeline.md 的 S4/S2 节。
下文保留清洗器初次接入的历史设计与测试记录；默认关闭、parse 后立即创建 chunk 等描述已被上述流程替代。
本轮不修改 cleaner 规则、真实配置或任何历史数据。

## 接入与启用

清洗接入 `document_parsing._parse_document_with_mineru`：现有 MinerU 请求完成后，先由 `normalize_mineru_result` 生成结构块，再调用 `clean_pdf_blocks`。每次解析请求仍只调用一次 MinerU。清洗仅适用于 PDF，默认关闭，非 PDF 和关闭时使用原路径。

后端通过现有 `Settings` 读取配置，示例位于 `backend/.env.example`：

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `PDF_CLEANING_ENABLED` | `false` | 开启新解析任务的 PDF 清洗 |
| `PDF_CLEANING_PROFILE` | `auto` | 可选 `auto`、`gb_zh`、`iso_en` |
| `PDF_CLEANING_BACKFILL_ENABLED` | `true` | 允许对已有文本块内部进行保守补录 |

依赖声明新增 `pdfplumber>=0.11,<0.12`，只用于读取上传 PDF 的独立文本层和物理页边区域。启用时需要设置 `PDF_CLEANING_ENABLED=true` 并重启后端；配置不会追溯修改已有解析、chunk 或索引。本次实施未改动实际 `.env`，未自动开启功能。

## 结构块与 Markdown 同源

`clean_pdf_blocks` 对 `NormalizedDocumentBlock` 做深拷贝，返回清洗后的内存对象；不会写入文件、修改原始解析结果或创建新任务。原始块的 `block_key`、`block_index`、页码、bbox、父块来源、图片资产引用继续保留；过滤后不重排来源编号。标题修复后按保留标题重建章节路径；完全没有标题证据时保留现有路径。

服务用 `render_cleaned_block` 渲染最终结构块并生成 `cleaned.md`，随后把同一批块交给现有 `add_document_blocks`。新增可选 renderer 参数让 block-aware chunker 对持久化后的块使用同一个内容渲染函数，避免旧 `text` 和新 `markdown` 同时进入 chunk。chunk 继续关联数据库生成的真实 block UUID，并沿用现有分组、超长块拆分、来源映射和 Embedding 状态机制。

这里的一致性指 Markdown 与切分输入使用相同的清洗内容。启用 chunk 的长文本拆分、重叠或表格拆分后，不要求把所有 chunk 直接拼接就逐字等于 Markdown；不得因此改变现有切分策略。

## 保留的规则与限制

GB/ISO 规则在 `app/ingestion/pdf_cleaner_rules.py` 中维护，通过项目配置选择，无 Text_cleaner preset/context 体系。选择性改写结构、空白、HTML/公式保护及交叉核验的有用规则，不整包移植源工具。

- **文档识别**：自动模式要求封面完整标准号、国家标准标识，或以完整标准号开头的文件名。支持 GB、GB/T、ISO、ISO/ASTM、ISO/IEC；不会因为正文提及 ISO、单独 ASTM/JB 标准号或版权行而判定。冲突证据或未知文档不启用 GB/ISO 的章节、封面过滤，仍保守整理结构和文本。
- **标题**：修正已有标题或独占块的 Markdown 标题；依据条款编号和附录修正层级。不从普通句子猜测标题；代码/算法中的 `#` 注释保持原类型。
- **章节过滤**：整标题匹配目录/目次、前言、引言、参考文献及对应英文名称，删除该标题与其范围内内容。遇到同级或更高层标题、范围/规范性引用文件/术语和定义、附录时结束删除。明确保留规范性引用文件，正文中出现这些词不触发删除。缺少可靠标题结构时不对整个 Markdown 按关键词截断。
- **页眉页脚和封面**：删除解析器明确标注的 header/footer；ICS、CCS、发布机构等只按整块规则作用于已识别文档的首页封面。普通标准号块须在多页物理页边重复、每页全文仅有一次完全匹配、且不在规范性引用章节，才作为页边内容删除；同页存在同样正文引用时保留。
- **段落与空白**：仅在单个文本块内合并可判定的软换行，中文间多余空格和普通重复空白可归一。保留列表、编号、硬换行、代码缩进；不跨块、跨页拼接段落，不拼接英文单词或断词，不改技术参数。
- **表格和公式**：表格块优先保留完整 HTML、caption/脚注，保留 rowspan/colspan、元素符号、数值及图片；相同表格也不去重。文本内 HTML 表格、Markdown 表格、公式、代码、图片先保护再还原。已有 LaTeX 公式仅统一明确可配对的外层定界符；不改 `\wedge` 等数学符号或公式主体，不猜测修复不完整表达式。无法安全判定时保留原文。
- **图片**：保留现有资产和来源，生成相对于同一解析目录的 Markdown 图片链接，并编码空格等路径字符。拒绝越过解析目录的资产引用。

## 原文核验和块内补录

仅在 MinerU V4 明确的零基页码契约下读取对应 PDF 页。候选必须来自同一物理页的可提取文本层，落在当前已有文本块的首尾唯一精确锚点之间；内部差异必须全部是插入，插入点前后仍须有唯一精确锚点。只复制 PDF 文本，不通过模型生成。

整段缺失、整页缺失、跨页块、页码基准未知、扫描页无文本层、PDF 文本不可读、重复锚点、替换/删除差异、部分数值或单位修正、比较符/否定词风险、与同页其他已知块（包括已过滤块）重合的补录均跳过。不会向文末追加候选，不创建新块、补录 JSON 或页码回填产物。bbox 保持 MinerU 来源值；不猜测其坐标体系，不用它生成新的补录位置。

这属于保守的内容修复，不是完整性保证；复杂阅读顺序和缺失整块需要更强证据，当前不会自动恢复。无证据时保留 MinerU 内容。预期的 PDF 文本层不可用不会阻止清洗；未预期的程序异常按现有解析失败机制处理。

## 产物、事务和失败行为

唯一新增文件是 `{parse_run.output_prefix}/cleaned.md`，通过现有 `document_assets` 注册为 `markdown`。不新增清洗后 JSON、报告、候选、操作记录、质量文件或专用存储目录。

现有 `output.md`、`output.json` 和图片保持原内容和现有归档方式；原始 JSON 虽不被 chunker 重新读取，仍属于当前解析结果保存、输出键、资产清单和删除清单的既有契约。本次不删除或更改这些原始文件。middle/model 等中间产物仍由现有 `MINERU_SAVE_INTERMEDIATE` 控制，不更改其默认值。

清洗和 Markdown 渲染先在内存完成，再进入现有最终文档锁与删除保护，依次上传原始资产和 `cleaned.md`、写入块和 chunk、提交解析状态。清洗失败发生在上传之前时，不保证原始产物已归档。若上传后数据库事务失败，数据库写入回滚，但对象存储可能保留该解析目录的文件，沿用现有删除清单/目录处理；没有新增跨系统事务或自动重试机制。最终锁确认文档已进入删除状态时，阻止上传。

失败沿用现有 `parse_failed`、解析任务和错误处理；再次发起既有失败重试可能重新调用 MinerU，不提供独立清洗重试、JSON 检查点或解析恢复系统。全部内容被过滤时直接失败，避免成功写入空知识库。正常关闭功能即可让后续 PDF 回到原路径，不需要数据库迁移。

## 明确未引入的职责

不移植 `export/page_backfill.py`、`core/result.py`、`pipeline/presets.py`、`pipeline/context.py`、领域术语和数据元标准化模块、任何 quality 模块。没有质量评分、完整性评分、清洗报告、质量阈值、清洗日志/统计/审计系统，也没有对应数据模型、数据库字段或 JSON 导出。

现有解析任务错误处理继续工作；保护片段原样还原、来源映射合法性、拒绝空结果和事务测试属于基本正确性保障。

## 验证范围

- `tests/test_pdf_cleaner.py`：GB/ISO 识别及章节边界、标准号保护、HTML/公式/代码/图片、来源保留、幂等性、真实内存 PDF 的文本提取和块内补录，以及数值/单位/否定词/邻块污染拒绝。
- `tests/test_pdf_cleaning_pipeline.py`：SQLite 实际 block/chunk/映射写入；使用同源渲染对照 `cleaned.md`；仅新增一个文件；MinerU 单次调用；关闭/非 PDF 兼容；清洗/上传/写块/提交失败回滚；删除竞态及输出路径冲突。
- 原有解析、切分、资产、来源映射和 API 测试继续覆盖兼容性。测试替身不代表真实 MinerU、MinIO、Embedding 或检索联调验收。

上线前应使用获准的真实 GB/ISO PDF 样本检查前言/引言过滤边界、规范性引用、附录、表格公式、图片路径与实际检索结果。本次没有发起真实解析请求、批量重新解析、数据库迁移或外部服务写入。

本地验证结果：后端回归 **1691 passed**（排除 `tests/integration`、`tests/phase13_integration`、`tests/phase13_browser`）；`git diff --check`、新增 Python 文件语法检查和 `pip check` 均通过。真实 MinerU/MinIO/Embedding/检索联调未执行。
