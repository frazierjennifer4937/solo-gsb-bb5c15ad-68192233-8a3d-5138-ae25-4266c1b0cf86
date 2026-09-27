"""限时豁免：对破坏性发现按 API 标识登记有时效的豁免。

匹配规则（刻意不使用发现序号）：
  同一 API 标识下，规则 rule_id、方向 direction、端点 endpoint、
  语义位置 location 及旧值 / 新值完全相同的破坏项才会被豁免覆盖。
  旧值 / 新值以规范化 JSON 比较：对象键顺序忽略，数组顺序与值类型保留。
  因此同一位置的变更值一旦变化，旧豁免不再匹配，须重新处理。

生效规则：
  豁免只是读取审查时实时叠加的覆盖层——原始发现、统计与历史对账保持原样。
  当前时间达到到期时间（expires_at）或已撤销（revoked_at 非空）的豁免即失效；
  未设置 API 标识的审查不应用豁免。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

ACTIVE = "active"
EXPIRED = "expired"
REVOKED = "revoked"

STATUS_CN = {ACTIVE: "生效中", EXPIRED: "已过期", REVOKED: "已撤销"}

BREAKING = "breaking"


# ---------------------------------------------------------------------- #
# 时间工具（统一 UTC）
# ---------------------------------------------------------------------- #
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def format_utc(dt: datetime) -> str:
    """规范化为 UTC ISO 8601（秒级、Z 后缀），便于存储与展示。"""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(text: Any) -> datetime:
    """解析 ISO 8601 时间；缺时区按 UTC 处理。非法输入抛 ValueError。"""
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("到期时间不能为空")
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        raise ValueError(
            f"到期时间格式无效：{text!r}（应为 ISO 8601，如 2026-10-01T00:00:00Z）"
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_future_utc(text: Any) -> str:
    """校验并规范化"未来的 UTC 到期时间"；不合法时抛 ValueError（可读原因）。"""
    dt = parse_utc(text)
    if dt <= now_utc():
        raise ValueError("到期时间必须晚于当前时间（限时豁免须有时效）")
    return format_utc(dt)


# ---------------------------------------------------------------------- #
# 匹配身份
# ---------------------------------------------------------------------- #
def canonical_value(value: Any) -> str:
    """值的规范化 JSON：对象键排序（键顺序忽略），数组顺序与值类型保留。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def finding_identity(finding: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    """发现的豁免匹配身份：规则 / 方向 / 端点 / 语义位置 / 旧值 / 新值。

    刻意不包含发现序号（id）：同一文档重新审查、序号变化不影响匹配。
    """
    return (
        str(finding.get("rule_id") or ""),
        str(finding.get("direction") or ""),
        str(finding.get("endpoint") or ""),
        str(finding.get("location") or ""),
        canonical_value(finding.get("old_value")),
        canonical_value(finding.get("new_value")),
    )


def exemption_identity(exemption: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    """豁免记录的匹配身份（old_value / new_value 库存即为规范化 JSON 文本）。"""
    return (
        str(exemption.get("rule_id") or ""),
        str(exemption.get("direction") or ""),
        str(exemption.get("endpoint") or ""),
        str(exemption.get("location") or ""),
        str(exemption.get("old_value") or "null"),
        str(exemption.get("new_value") or "null"),
    )


def exemption_status(exemption: dict[str, Any], now: datetime | None = None) -> str:
    """豁免当前状态：已撤销 > 已过期（当前时间达到到期时间）> 生效中。"""
    now = now or now_utc()
    if exemption.get("revoked_at"):
        return REVOKED
    try:
        expires = parse_utc(exemption.get("expires_at"))
    except ValueError:
        return EXPIRED
    return ACTIVE if now < expires else EXPIRED


# ---------------------------------------------------------------------- #
# 读取时叠加豁免覆盖层
# ---------------------------------------------------------------------- #
def apply_exemptions(
    review: dict[str, Any],
    exemptions: list[dict[str, Any]] | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """在审查副本上叠加豁免信息（不改动数据库中的原始发现与统计）。

    返回新字典：破坏项发现附带 `exemption`（匹配状态 / 理由 / 到期时间），
    顶层附带 `exemptions`（该 API 标识下全部豁免及状态）与
    `exemption_summary`（已豁免 / 未豁免破坏项数量与放行结论）。
    无 API 标识的审查不应用豁免（exemption_summary 为 None）。
    """
    now = now or now_utc()
    result = dict(review)
    findings = [dict(f) for f in review.get("findings") or []]
    result["findings"] = findings

    api_id = str(review.get("api_id") or "").strip()
    if not api_id:
        result["exemptions"] = []
        result["exemption_summary"] = None
        return result

    # 豁免条目：计算当前状态，并按匹配身份索引
    entries: list[dict[str, Any]] = []
    by_identity: dict[tuple, list[dict[str, Any]]] = {}
    for ex in exemptions or []:
        entry = dict(ex)
        entry["status"] = exemption_status(entry, now)
        entry["status_label"] = STATUS_CN[entry["status"]]
        entry["match_count"] = 0
        entries.append(entry)
        by_identity.setdefault(exemption_identity(entry), []).append(entry)

    exempted = 0
    unexempted = 0
    for f in findings:
        if f.get("severity") != BREAKING:
            continue
        matches = by_identity.get(finding_identity(f))
        if not matches:
            unexempted += 1
            continue
        for m in matches:
            m["match_count"] += 1
        # 展示用豁免：优先生效中（取最新一条），否则取最新创建的一条
        chosen = matches[-1]
        for m in matches:
            if m["status"] == ACTIVE:
                chosen = m
        f["exemption"] = {
            "id": chosen["id"],
            "reason": chosen["reason"],
            "expires_at": chosen["expires_at"],
            "created_at": chosen["created_at"],
            "revoked_at": chosen.get("revoked_at"),
            "status": chosen["status"],
            "status_label": STATUS_CN[chosen["status"]],
        }
        if chosen["status"] == ACTIVE:
            exempted += 1
        else:
            unexempted += 1

    releasable = unexempted == 0
    result["exemptions"] = entries
    result["exemption_summary"] = {
        "api_id": api_id,
        "total_breaking": exempted + unexempted,
        "exempted_breaking": exempted,
        "unexempted_breaking": unexempted,
        "releasable": releasable,
        "conclusion": (
            "✅ 未豁免破坏项为零，可放行"
            if releasable
            else f"❌ 仍有 {unexempted} 项未豁免破坏项，不可放行（未豁免为零才可放行）"
        ),
    }
    return result
