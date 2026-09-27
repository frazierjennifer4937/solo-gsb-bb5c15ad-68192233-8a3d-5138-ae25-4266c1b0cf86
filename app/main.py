"""FastAPI 入口：审查、历史、引用展开树、根因聚合、破坏项对账、限时豁免、报告下载、最小复现包导出。"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import database, exemptions, groups, reconcile, reports, repro
from .diff import run_diff, summarize
from .parser import SpecParseError, load_spec
from .refs import build_ref_tree, expand_path_to_pointer, list_root_pointers

app = FastAPI(
    title="OpenAPI 版本兼容性审查",
    description="解析新旧两份 OpenAPI 3.x 文档，按请求/响应语义规则定位破坏性变更。",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    database.init_db()


@app.get("/api/health")
def health() -> dict[str, str]:
    """轻量健康检查（Docker HEALTHCHECK 与容器编排探活使用）。"""
    return {"status": "ok"}


# ---------------------------------------------------------------------- #
# 请求模型
# ---------------------------------------------------------------------- #
class ReviewRequest(BaseModel):
    old_spec: str = Field(..., description="旧版 OpenAPI 文档原文（JSON 或 YAML）")
    new_spec: str = Field(..., description="新版 OpenAPI 文档原文（JSON 或 YAML）")
    old_filename: str | None = None
    new_filename: str | None = None
    title: str | None = None
    api_id: str | None = Field(
        default=None, description="API 标识（可选）；填写后才可创建并应用限时豁免"
    )


class ExemptionRequest(BaseModel):
    finding_id: int = Field(..., description="要豁免的破坏项发现序号（仅用于定位发现）")
    reason: str = Field(..., description="豁免理由（必填）")
    expires_at: str = Field(
        ..., description="UTC 到期时间（ISO 8601，须晚于当前时间），到达即失效"
    )


def _version_of(doc: dict[str, Any]) -> str | None:
    info = doc.get("info")
    if isinstance(info, dict) and info.get("version"):
        return str(info["version"])
    return None


def _with_exemptions(review: dict[str, Any]) -> dict[str, Any]:
    """读取时实时叠加豁免覆盖层（原始发现、统计与历史对账保持原样）。"""
    api_id = str(review.get("api_id") or "").strip()
    records = database.list_exemptions(api_id) if api_id else []
    return exemptions.apply_exemptions(review, records)


# ---------------------------------------------------------------------- #
# 审查
# ---------------------------------------------------------------------- #
@app.post("/api/reviews", status_code=201)
def create_review(req: ReviewRequest) -> dict[str, Any]:
    try:
        old_doc = load_spec(req.old_spec, req.old_filename)
    except SpecParseError as exc:
        raise HTTPException(status_code=422, detail={"side": "old", "message": str(exc)})
    try:
        new_doc = load_spec(req.new_spec, req.new_filename)
    except SpecParseError as exc:
        raise HTTPException(status_code=422, detail={"side": "new", "message": str(exc)})

    findings = run_diff(old_doc, new_doc)
    summary_data = summarize(findings)

    title = req.title or (
        f"{new_doc.get('info', {}).get('title', 'API')} "
        f"{_version_of(old_doc) or '旧版'} → {_version_of(new_doc) or '新版'}"
    )
    api_id = (req.api_id or "").strip() or None

    review_id = database.create_review(
        title=title,
        old_filename=req.old_filename,
        new_filename=req.new_filename,
        old_version=_version_of(old_doc),
        new_version=_version_of(new_doc),
        api_id=api_id,
        summary=summary_data,
        findings=findings,
        old_doc=old_doc,
        new_doc=new_doc,
    )
    review = database.get_review(review_id)
    assert review is not None  # 刚写入
    return _with_exemptions(review)


@app.get("/api/reviews")
def get_reviews() -> dict[str, Any]:
    return {"items": database.list_reviews()}


@app.get("/api/reviews/{review_id}")
def get_review(review_id: int) -> dict[str, Any]:
    review = database.get_review(review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    return _with_exemptions(review)


@app.delete("/api/reviews/{review_id}", status_code=204)
def remove_review(review_id: int) -> Response:
    if not database.delete_review(review_id):
        raise HTTPException(status_code=404, detail="审查记录不存在")
    return Response(status_code=204)


# ---------------------------------------------------------------------- #
# 限时豁免（按 API 标识匹配，读取审查时实时重算状态）
# ---------------------------------------------------------------------- #
@app.post("/api/reviews/{review_id}/exemptions", status_code=201)
def create_exemption(review_id: int, req: ExemptionRequest) -> dict[str, Any]:
    """对某条破坏性发现登记限时豁免：理由 + 未来的 UTC 到期时间。

    豁免记录的是发现的语义身份（规则 / 方向 / 端点 / 语义位置 / 新旧值），
    而非发现序号——同一 API 标识的后续审查中完全相同的发现才会被覆盖。
    """
    review = database.get_review(review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    api_id = str(review.get("api_id") or "").strip()
    if not api_id:
        raise HTTPException(
            status_code=400,
            detail="该审查未设置 API 标识，无法创建豁免（无标识的审查不应用豁免）",
        )
    finding = repro.find_finding(review["findings"], req.finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="发现项不存在")
    if finding.get("severity") != "breaking":
        raise HTTPException(status_code=422, detail="仅破坏性发现可以登记豁免")
    reason = (req.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=422, detail="豁免理由不能为空")
    try:
        expires_at = exemptions.parse_future_utc(req.expires_at)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    record = database.create_exemption(
        api_id=api_id,
        rule_id=str(finding.get("rule_id") or ""),
        direction=str(finding.get("direction") or ""),
        endpoint=str(finding.get("endpoint") or ""),
        location=str(finding.get("location") or ""),
        old_value=exemptions.canonical_value(finding.get("old_value")),
        new_value=exemptions.canonical_value(finding.get("new_value")),
        reason=reason,
        expires_at=expires_at,
        created_at=exemptions.format_utc(exemptions.now_utc()),
    )
    record["status"] = exemptions.exemption_status(record)
    record["status_label"] = exemptions.STATUS_CN[record["status"]]
    return record


@app.post("/api/exemptions/{exemption_id}/revoke")
def revoke_exemption(exemption_id: int) -> dict[str, Any]:
    """撤销豁免（立即失效，幂等）；豁免状态在读取审查时重新计算。"""
    record = database.revoke_exemption(
        exemption_id, exemptions.format_utc(exemptions.now_utc())
    )
    if record is None:
        raise HTTPException(status_code=404, detail="豁免记录不存在")
    record["status"] = exemptions.exemption_status(record)
    record["status_label"] = exemptions.STATUS_CN[record["status"]]
    return record


# ---------------------------------------------------------------------- #
# 根因聚合（有向影响组）
# ---------------------------------------------------------------------- #
@app.get("/api/reviews/{review_id}/groups")
def get_review_groups(review_id: int) -> dict[str, Any]:
    review = database.get_review(review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    # 叠加豁免信息，使聚合视图的发现详情同样展示匹配状态
    review = _with_exemptions(review)
    result = groups.build_impact_groups(review["findings"])
    result["review_id"] = review_id
    return result


# ---------------------------------------------------------------------- #
# 破坏项对账（基线审查 vs 当前审查）
# ---------------------------------------------------------------------- #
def _review_brief(review: dict[str, Any]) -> dict[str, Any]:
    """对账结果中附带的对账双方概要信息。"""
    return {
        "id": review.get("id"),
        "title": review.get("title"),
        "created_at": review.get("created_at"),
        "old_version": review.get("old_version"),
        "new_version": review.get("new_version"),
    }


@app.get("/api/reviews/{review_id}/reconcile")
def reconcile_review(review_id: int, baseline_id: int) -> dict[str, Any]:
    """以 baseline_id 为基线，与当前审查 review_id 对账破坏性发现。

    输出新增 / 延续 / 已消失三类破坏项及数量；基线不存在或与当前
    审查相同时返回可读错误。
    """
    current = database.get_review(review_id)
    if current is None:
        raise HTTPException(status_code=404, detail="当前审查记录不存在")
    if baseline_id == review_id:
        raise HTTPException(
            status_code=400,
            detail="基线审查与当前审查相同，请选择另一条历史审查作为基线",
        )
    baseline = database.get_review(baseline_id)
    if baseline is None:
        raise HTTPException(
            status_code=404,
            detail=f"基线审查 #{baseline_id} 不存在或已被删除",
        )
    result = reconcile.build_reconciliation(baseline["findings"], current["findings"])
    result["current"] = _review_brief(current)
    result["baseline"] = _review_brief(baseline)
    return result


# ---------------------------------------------------------------------- #
# 引用展开树
# ---------------------------------------------------------------------- #
@app.get("/api/reviews/{review_id}/pointers")
def get_pointers(review_id: int) -> dict[str, Any]:
    specs = database.get_specs(review_id)
    if specs is None:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    old_doc, new_doc = specs
    return {
        "old": list_root_pointers(old_doc),
        "new": list_root_pointers(new_doc),
    }


@app.get("/api/reviews/{review_id}/tree")
def get_tree(
    review_id: int,
    side: str = "new",
    pointer: str = "",
    target: str = "",
) -> dict[str, Any]:
    if side not in ("old", "new"):
        raise HTTPException(status_code=400, detail="side 必须是 old 或 new")
    specs = database.get_specs(review_id)
    if specs is None:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    doc = specs[0] if side == "old" else specs[1]
    tree = build_ref_tree(doc, pointer)
    target_path: list[str] = []
    if target:
        target_path = expand_path_to_pointer(tree, target)
    tree["target_path"] = target_path
    tree["target_found"] = bool(target_path)
    return tree


# ---------------------------------------------------------------------- #
# 报告下载
# ---------------------------------------------------------------------- #
@app.get("/api/reviews/{review_id}/report")
def download_report(review_id: int, format: str = "markdown") -> Response:
    review = database.get_review(review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    review = _with_exemptions(review)

    safe_title = (review.get("title") or f"review-{review_id}")
    safe_title = "".join(c for c in safe_title if c.isalnum() or c in ("-", "_", " ", "."))
    safe_title = safe_title.strip().replace(" ", "_") or f"review-{review_id}"
    # HTTP 头仅支持 latin-1：ASCII 回退名 + RFC 5987 UTF-8 文件名（中文标题兼容）
    ascii_title = "".join(c for c in safe_title if c.isascii()) or f"review-{review_id}"

    def _disposition(ext: str) -> str:
        utf8_name = quote(f"openapi-review-{review_id}-{safe_title}.{ext}")
        return (
            f'attachment; filename="openapi-review-{review_id}-{ascii_title}.{ext}"; '
            f"filename*=UTF-8''{utf8_name}"
        )

    if format == "json":
        body = reports.build_json(review)
        return Response(
            content=body,
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": _disposition("json")},
        )

    body = reports.build_markdown(review)
    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": _disposition("md")},
    )


# ---------------------------------------------------------------------- #
# 最小复现包导出
# ---------------------------------------------------------------------- #
@app.get("/api/reviews/{review_id}/findings/{finding_id}/repro")
def export_repro(review_id: int, finding_id: int) -> Response:
    review = database.get_review(review_id, with_specs=True)
    if review is None:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    finding = repro.find_finding(review["findings"], finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="发现项不存在")
    try:
        zip_bytes, _manifest = repro.build_repro_package(review, finding)
    except repro.ReproError as exc:
        # 复核未通过：返回可读错误，不提供失真的包
        raise HTTPException(status_code=409, detail=str(exc))
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="openapi-repro-r{review_id}-f{finding_id}.zip"'
            )
        },
    )


@app.exception_handler(SpecParseError)
async def _parse_error_handler(_request, exc: SpecParseError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"message": str(exc)})


# ---------------------------------------------------------------------- #
# 静态前端（挂载在最后，避免覆盖 /api）
# ---------------------------------------------------------------------- #
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")
