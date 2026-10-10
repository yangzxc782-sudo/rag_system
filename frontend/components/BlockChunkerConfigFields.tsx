"use client";

import { configError, type BlockChunkerConfig } from "@/lib/chunk-sets";

type Props = { value: BlockChunkerConfig | null; onChange: (value: BlockChunkerConfig) => void; disabled: boolean };

export default function BlockChunkerConfigFields({ value, onChange, disabled }: Props) {
  if (!value) return <p role="status">等待后端六字段切分配置；取得配置前不可提交。</p>;
  const error = configError(value);
  return <fieldset disabled={disabled} className="grid gap-3 rounded border p-3">
    <legend>PDF 结构感知切分配置</legend>
    <div className="flex flex-wrap gap-3">
      <label>正文目标长度 <input type="number" min={1} max={60000} step={1} className="w-24 border p-1"
        value={Number.isNaN(value.max_chunk_chars) ? "" : value.max_chunk_chars}
        onChange={e => onChange({ ...value, max_chunk_chars: e.target.valueAsNumber })} /></label>
      <label>小尾块阈值 <input type="number" min={0} max={value.max_chunk_chars} step={1} className="w-24 border p-1"
        value={Number.isNaN(value.min_chunk_chars) ? "" : value.min_chunk_chars}
        onChange={e => onChange({ ...value, min_chunk_chars: e.target.valueAsNumber })} /></label>
      <label>期望重叠长度（尽量） <input type="number" min={0} max={value.max_chunk_chars - 1} step={1} className="w-24 border p-1"
        value={Number.isNaN(value.overlap_chars) ? "" : value.overlap_chars}
        onChange={e => onChange({ ...value, overlap_chars: e.target.valueAsNumber })} /></label>
      <label>表格分组阈值 <input type="number" min={1} step={1} className="w-24 border p-1"
        value={Number.isNaN(value.max_table_chars) ? "" : value.max_table_chars}
        onChange={e => onChange({ ...value, max_table_chars: e.target.valueAsNumber })} /></label>
      <label><input type="checkbox" checked={value.keep_table_intact}
        onChange={e => onChange({ ...value, keep_table_intact: e.target.checked })} /> 保持整表</label>
      <label><input type="checkbox" checked={value.keep_formula_with_context}
        onChange={e => onChange({ ...value, keep_formula_with_context: e.target.checked })} /> 短公式与相邻说明合并</label>
    </div>
    <p>长度按 Unicode code point 计算。正文目标不是所有切片的统一上限；小尾块阈值仅用于同一长正文块的尾部，不跨章节或结构凑长度，0 表示关闭。</p>
    <p>{value.keep_table_intact ? "已开启整表保护：表格可超过正文目标和表格阈值，表格阈值不参与拆表。"
      : "已关闭整表保护：不超过表格阈值仍整块保留；超长表按完整物理行分组，普通片段可超过正文目标，超长单行不拆，不保证每片是独立有效 HTML。"}</p>
    <p>{value.keep_formula_with_context ? "短独立公式可按容量和章节条件与相邻说明合并。" : "独立公式单独保留。"} 两种设置都不会拆断独立公式。</p>
    <p>重叠仅在同一超长普通正文块的剩余容量和安全边界允许时尽量添加，实际重叠可能更少或为 0；表格和独立公式不添加重叠。</p>
    {error ? <p role="alert" className="text-amber-800">{error}</p> : null}
  </fieldset>;
}
