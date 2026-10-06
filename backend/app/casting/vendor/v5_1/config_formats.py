"""Read/write equivalent JSON, CSV, or Markdown configuration files.

CSV/Markdown use key/value_json rows. Nested arrays/objects remain JSON values,
so no information about hotspots, sites, or rule catalog entries is lost.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"重复配置键: {key}")
        result[key] = value
    return result


def loads_config_json(text):
    return json.loads(text, object_pairs_hook=unique_pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("非有限数值: " + value)))


def load_config(path):
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".json":
        return loads_config_json(path.read_text(encoding="utf-8-sig"))
    if suffix == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
    elif suffix in (".md", ".markdown"):
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.startswith("|"):
                continue
            cells = [x.strip() for x in line.strip().strip("|").split("|", 1)]
            if len(cells) != 2 or cells[0] in ("key", "---"):
                continue
            rows.append({"key": cells[0], "value_json": cells[1]})
    else:
        raise ValueError("支持 JSON、CSV、Markdown")
    result = {}
    for row in rows:
        key = row["key"]
        if key in result:
            raise ValueError(f"重复配置键: {key}")
        result[key] = loads_config_json(row["value_json"])
    return result

def write_alternates(path):
    path = Path(path)
    data = load_config(path)
    rows = [{"key": key, "value_json": json.dumps(value, ensure_ascii=False, separators=(",", ":"))}
            for key, value in data.items()]
    with path.with_suffix(".csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=("key", "value_json"))
        writer.writeheader()
        writer.writerows(rows)
    lines = [f"# {path.stem}", "", "| key | value_json |", "|---|---|"]
    lines.extend(f"| {row['key']} | {row['value_json']} |" for row in rows)
    path.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("source", type=Path)
    args = p.parse_args()
    write_alternates(args.source)
