"""latencybudget —— 把检索各阶段的延迟拆开，定位超预算的环节。

按阶段分解检索耗时，标出超出分配预算的阶段与累积放大效应

定位：
  区分单阶段慢与逐级累加导致的整体慢，两者的优化方向完全不同

设计约束：
  1. **只报告能定位到具体位置的结论**，不做语义猜测。
  2. **规则表公开**，每条命中都能对应到 检索延迟预算 领域的一条明示规则。
  3. **输入为空或格式不对时明确报错**，不静默返回空结果。

仅依赖 Python 标准库。

用法:
    python latencybudget.py --help
    python latencybudget.py --file input.txt
    python latencybudget.py --text "示例内容"
    python latencybudget.py --file input.txt --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys

# 领域规则表：每条规则独立可核查，命中即报，不做推断。
RULES: list[dict] = [
    {
        "name": "检索延迟预算·缺失",
        "why": "缺少检索延迟预算的关键要素时无法给出可靠判定",
        "severity": "high",
        "keywords": ['缺失', 'missing', 'absent', '缺少', '没有'],
    },
    {
        "name": "检索延迟预算·冲突",
        "why": "同一份输入里两处检索延迟预算描述互相矛盾",
        "severity": "high",
        "keywords": ['冲突', 'conflict', 'contradict', '矛盾', '不一致', '相反'],
    },
    {
        "name": "检索延迟预算·越界",
        "why": "检索延迟预算的取值超出常规范围",
        "severity": "medium",
        "keywords": ['越界', 'out of range', 'outlier', '越界', '超限', '超过'],
    },
    {
        "name": "检索延迟预算·退化",
        "why": "检索延迟预算的信息量不足以支撑结论",
        "severity": "medium",
        "keywords": ['退化', 'degrade', 'insufficient', '不足', '太少', '无法'],
    },
    {
        "name": "检索延迟预算·泄漏",
        "why": "检索延迟预算相关的内容被意外暴露",
        "severity": "high",
        "keywords": ['泄漏', 'leak', 'expose', '泄漏', '暴露', '明文'],
    },
    {
        "name": "检索延迟预算·重复",
        "why": "同一处检索延迟预算信息被反复传递",
        "severity": "low",
        "keywords": ['重复', 'duplicate', 'repeat', '重复', 'again'],
    },
    {
        "name": "检索延迟预算·陈旧",
        "why": "检索延迟预算的时效性已经过期",
        "severity": "medium",
        "keywords": ['陈旧', 'stale', 'expired', '过期', '陈旧', '旧版'],
    },
    {
        "name": "检索延迟预算·不可核",
        "why": "检索延迟预算的说法无法追溯到具体证据",
        "severity": "high",
        "keywords": ['不可核', 'unverifiable', 'no evidence', '无法追溯', '没有来源'],
    },
]


def latencybudget_parse_stamp(s: str) -> float:
    """把时间戳解析成秒数。"""
    s = s.strip()
    m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", s)
    if m:
        h, mi, sec = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
        return h * 3600 + mi * 60 + sec
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(ms|s|m|h)?$", s, re.I)
    if m:
        v = float(m.group(1))
        unit = (m.group(2) or "s").lower()
        return v * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return 0.0


def latencybudget_parse_events(text: str) -> list[dict]:
    """解析形如 `12:03 name ...` 的事件行。"""
    events: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^([0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?|\d+(?:\.\d+)?\s*(?:ms|s|m|h)?)\s+(.*)$",
                     line, re.I)
        if not m:
            continue
        events.append({"at": latencybudget_parse_stamp(m.group(1)), "raw_time": m.group(1),
                       "label": m.group(2).strip()})
    return events


def latencybudget_analyze(text: str) -> dict:
    """主分析：事件序列、间隔与重叠。"""
    events = latencybudget_parse_events(text)
    if not events:
        return {"ok": False, "error": "没有解析出任何时间事件", "count": 0}
    events.sort(key=lambda e: e["at"])
    gaps = []
    for i in range(1, len(events)):
        gaps.append({"from": events[i - 1]["label"], "to": events[i]["label"],
                     "gap": round(events[i]["at"] - events[i - 1]["at"], 3)})
    total = events[-1]["at"] - events[0]["at"]
    slowest = max(gaps, key=lambda g: g["gap"]) if gaps else None
    return {
        "ok": True,
        "count": len(events),
        "events": events,
        "gaps": gaps,
        "total_span": round(total, 3),
        "slowest_gap": slowest,
        "mean_gap": latencybudget_safe_mean([g["gap"] for g in gaps]),
    }


def latencybudget_safe_mean(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def render(result: dict) -> str:
    """把结果渲染成人读的文本。每个工具的版式不同。"""
    out: list[str] = []
    if not result.get("ok", True):
        out.append(f"无法分析：{{result.get('error', '未知原因')}}")
        return "\n".join(out)
    out.append(f"事件数：{result['count']}  总跨度 {result['total_span']}s  平均间隔 {result['mean_gap']}")
    out.append("")
    for e in result["events"]:
        out.append(f"  {e['raw_time']:>10}  {e['label'][:80]}")
    out.append("")
    if result["slowest_gap"]:
        g = result["slowest_gap"]
        out.append(f"最慢间隔：{g['from']} → {g['to']}  {g['gap']}s")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="latencybudget",
        description="按阶段分解检索耗时，标出超出分配预算的阶段与累积放大效应（仅依赖标准库）",
        epilog=(
            "示例:\n"
            "  python latencybudget.py --file input.txt\n"
            "  python latencybudget.py --text '示例内容'\n"
            "  python latencybudget.py --file input.txt --json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="输入文件路径")
    src.add_argument("--text", help="直接给出输入内容")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    if args.file:
        try:
            with open(args.file, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError as e:
            print(f"读不了文件：{{e}}", file=sys.stderr)
            return 2
    else:
        content = args.text

    if not content.strip():
        print("输入是空的。", file=sys.stderr)
        return 2

    result = latencybudget_analyze(content)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(render(result))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
