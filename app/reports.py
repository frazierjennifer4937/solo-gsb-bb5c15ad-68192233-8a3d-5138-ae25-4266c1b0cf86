"""审查报告生成：Markdown（人读）与 JSON（机器读）。"""
from __future__ import annotations

import json
from typing import Any

from .diff import BREAKING, DEPRECATION, INFO, NON_BREAKING

SEV_CN = {
    BREAKING: "🔴 破坏性",
    NON_BREAKING: "🟢 兼容",
    DEPRECATION: "🟡 废弃提示",
    INFO: "⚪ 信息",
}
CHANGE_CN = {"added": "新增", "removed": "删除", "changed": "修改"}
DIRECTION_CN = {
    "request": "请求",
    "response": "响应",
    "shared": "请求/响应",
    "none": "—",
}


def build_markdown(review: dict[str, Any]) -> str:
    summary = review["summary"]
    counts = summary["counts"]
    ex_summary = review.get("exemption_summary")
    ex_records = review.get("exemptions") or []
    lines: list[str] = []

    lines.append(f"# OpenAPI 兼容性审查报告")
    lines.append("")
    title = review.get("title") or "未命名审查"
    lines.append(f"- **审查标题**：{title}")
    lines.append(f"- **审查时间**：{review.get('created_at', '')}")
    lines.append(
        f"- **版本对比**：{review.get('old_version') or '未知'}  →  "
        f"{review.get('new_version') or '未知'}"
    )
    if review.get("old_filename") or review.get("new_filename"):
        lines.append(
            f"- **文档来源**：{review.get('old_filename') or '(粘贴)'}  →  "
            f"{review.get('new_filename') or '(粘贴)'}"
        )
    if review.get("api_id"):
        lines.append(f"- **API 标识**：{_md_escape(review['api_id'])}")
    verdict = "❌ 存在破坏性变更，不建议直接发布" if not summary["compatible"] else "✅ 未发现破坏性变更"
    lines.append(f"- **结论**：{verdict}")
    if ex_summary:
        lines.append(
            f"- **破坏项豁免**：已豁免 {ex_summary['exempted_breaking']} 项 / "
            f"未豁免 {ex_summary['unexempted_breaking']} 项 → "
            f"{ex_summary['conclusion']}"
        )
    lines.append("")

    lines.append("## 统计概览")
    lines.append("")
    lines.append("| 级别 | 数量 |")
    lines.append("| --- | ---: |")
    lines.append(f"| 🔴 破坏性变更 | {counts.get(BREAKING, 0)} |")
    lines.append(f"| 🟢 兼容性变更 | {counts.get(NON_BREAKING, 0)} |")
    lines.append(f"| 🟡 废弃提示 | {counts.get(DEPRECATION, 0)} |")
    lines.append(f"| ⚪ 信息性变更 | {counts.get(INFO, 0)} |")
    lines.append(f"| **合计** | **{summary['total']}** |")
    if ex_summary:
        lines.append(f"| 🛡️ 已豁免破坏项 | {ex_summary['exempted_breaking']} |")
        lines.append(f"| ⛔ 未豁免破坏项 | {ex_summary['unexempted_breaking']} |")
    lines.append("")

    if summary.get("by_category"):
        lines.append("### 按分类")
        lines.append("")
        for cat, n in summary["by_category"].items():
            lines.append(f"- {cat}：{n}")
        lines.append("")

    findings: list[dict[str, Any]] = review["findings"]
    breaking = [f for f in findings if f["severity"] == BREAKING]
    if breaking:
        lines.append("## 🔴 破坏性变更明细")
        lines.append("")
        _render_finding_table(lines, breaking, with_exemption=bool(ex_summary))

    other = [f for f in findings if f["severity"] != BREAKING]
    if other:
        lines.append("## 其他变更")
        lines.append("")
        _render_finding_table(lines, other)

    if ex_summary and ex_records:
        lines.append("## 🛡️ 豁免明细")
        lines.append("")
        lines.append(
            "| # | 状态 | 规则 | 端点 | 定位 | 理由 | 到期时间（UTC） | 匹配发现数 |"
        )
        lines.append("| ---: | --- | --- | --- | --- | --- | --- | ---: |")
        status_icon = {"active": "🛡️ 生效中", "expired": "⏰ 已过期", "revoked": "🚫 已撤销"}
        for ex in ex_records:
            lines.append(
                "| {idx} | {status} | `{rule}` | {endpoint} | `{loc}` | {reason} | {expires} | {matches} |".format(
                    idx=ex["id"],
                    status=status_icon.get(ex.get("status"), ex.get("status", "")),
                    rule=_md_escape(ex.get("rule_id", "")),
                    endpoint=_md_escape(ex.get("endpoint") or "—"),
                    loc=_md_escape(ex.get("location", "")),
                    reason=_md_escape(ex.get("reason", "")),
                    expires=ex.get("expires_at", ""),
                    matches=ex.get("match_count", 0),
                )
            )
        lines.append("")

    lines.append("")
    lines.append("---")
    lines.append("*由 OpenAPI 兼容性审查工具生成。规则按请求/响应两个方向分别判定。*")
    return "\n".join(lines)


def _exemption_cell(f: dict[str, Any]) -> str:
    """破坏性发现对应的豁免状态单元格（匹配状态 + 到期时间）。"""
    ex = f.get("exemption")
    if not ex:
        return "—"
    label = {"active": "🛡️ 已豁免", "expired": "⏰ 已过期", "revoked": "🚫 已撤销"}.get(
        ex.get("status"), ex.get("status", "")
    )
    return f"{label}（至 {ex.get('expires_at', '')}）"


def _render_finding_table(
    lines: list[str], findings: list[dict[str, Any]], *, with_exemption: bool = False
) -> None:
    head_extra = " 豁免 |" if with_exemption else ""
    sep_extra = " --- |" if with_exemption else ""
    lines.append(f"| # | 级别 | 分类 | 方向 | 变更 | 端点 | 定位 | 原因 |{head_extra}")
    lines.append(f"| ---: | --- | --- | --- | --- | --- | --- | --- |{sep_extra}")
    for i, f in enumerate(findings, 1):
        row = (
            "| {idx} | {sev} | {cat} | {direction} | {change} | {endpoint} | `{loc}` | {msg} |".format(
                idx=i,
                sev=SEV_CN.get(f["severity"], f["severity"]),
                cat=_md_escape(f.get("category", "")),
                direction=DIRECTION_CN.get(f.get("direction"), f.get("direction", "")),
                change=CHANGE_CN.get(f.get("change"), f.get("change", "")),
                endpoint=_md_escape(f.get("endpoint") or "—"),
                loc=_md_escape(f.get("location", "")),
                msg=_md_escape(f.get("message", "")),
            )
        )
        if with_exemption:
            row += f" {_exemption_cell(f)} |"
        lines.append(row)
    lines.append("")

    # 附录：旧值/新值与 JSON 路径，便于精确定位
    lines.append("<details><summary>JSON 路径与取值明细</summary>")
    lines.append("")
    for i, f in enumerate(findings, 1):
        lines.append(f"**{i}. {f.get('rule_id')}**")
        if f.get("old_json_path"):
            lines.append(f"- 旧版路径：`{f['old_json_path']}`")
        if f.get("new_json_path"):
            lines.append(f"- 新版路径：`{f['new_json_path']}`")
        if f.get("old_ref"):
            lines.append(f"- 旧版引用：`{f['old_ref']}`")
        if f.get("new_ref"):
            lines.append(f"- 新版引用：`{f['new_ref']}`")
        if f.get("old_value") is not None or f.get("new_value") is not None:
            lines.append(
                f"- 旧值：`{json.dumps(f.get('old_value'), ensure_ascii=False)}`"
            )
            lines.append(
                f"- 新值：`{json.dumps(f.get('new_value'), ensure_ascii=False)}`"
            )
        ex = f.get("exemption")
        if ex:
            lines.append(
                f"- 豁免：{ex.get('status_label', ex.get('status'))}"
                f"（理由：{ex.get('reason')}；到期：{ex.get('expires_at')} UTC）"
            )
        lines.append("")
    lines.append("</details>")
    lines.append("")


def build_json(review: dict[str, Any]) -> str:
    payload = {
        "id": review.get("id"),
        "title": review.get("title"),
        "created_at": review.get("created_at"),
        "old_filename": review.get("old_filename"),
        "new_filename": review.get("new_filename"),
        "old_version": review.get("old_version"),
        "new_version": review.get("new_version"),
        "api_id": review.get("api_id"),
        "summary": review["summary"],
        "exemption_summary": review.get("exemption_summary"),
        "exemptions": review.get("exemptions") or [],
        "findings": review["findings"],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _md_escape(text: Any) -> str:
    if text is None:
        return "—"
    return str(text).replace("|", "\\|").replace("\n", " ")
