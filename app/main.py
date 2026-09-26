"""FastAPI 入口：审查、历史、引用展开树、根因聚合、破坏项对账、报告下载、最小复现包导出。"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import database, groups, reconcile, reports, repro
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


def _version_of(doc: dict[str, Any]) -> str | None:
    info = doc.get("info")
    if isinstance(info, dict) and info.get("version"):
        return str(info["version"])
    return None


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

    review_id = database.create_review(
        title=title,
        old_filename=req.old_filename,
        new_filename=req.new_filename,
        old_version=_version_of(old_doc),
        new_version=_version_of(new_doc),
        summary=summary_data,
        findings=findings,
        old_doc=old_doc,
        new_doc=new_doc,
    )
    return {
        "id": review_id,
        "title": title,
        "old_version": _version_of(old_doc),
        "new_version": _version_of(new_doc),
        "summary": summary_data,
        "findings": findings,
    }


@app.get("/api/reviews")
def get_reviews() -> dict[str, Any]:
    return {"items": database.list_reviews()}


@app.get("/api/reviews/{review_id}")
def get_review(review_id: int) -> dict[str, Any]:
    review = database.get_review(review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    return review


@app.delete("/api/reviews/{review_id}", status_code=204)
def remove_review(review_id: int) -> Response:
    if not database.delete_review(review_id):
        raise HTTPException(status_code=404, detail="审查记录不存在")
    return Response(status_code=204)


# ---------------------------------------------------------------------- #
# 根因聚合（有向影响组）
# ---------------------------------------------------------------------- #
@app.get("/api/reviews/{review_id}/groups")
def get_review_groups(review_id: int) -> dict[str, Any]:
    review = database.get_review(review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="审查记录不存在")
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
