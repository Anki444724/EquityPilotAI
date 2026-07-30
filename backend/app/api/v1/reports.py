"""Report generation endpoints.

    POST   /reports/generate              build a report
    GET    /reports                       list reports
    GET    /reports/capabilities          engine self-description
    GET    /reports/statistics            corpus counters
    GET    /reports/jobs                  the generation queue
    GET    /reports/{id}                  one report, with its section index
    GET    /reports/{id}/download/{fmt}   the rendered artefact
    GET    /reports/{id}/preview          inline HTML preview
    GET    /reports/{id}/versions         every version of this report
    DELETE /reports/{id}                  remove a report

Literal paths precede `/{report_id}`: FastAPI matches in declaration order, so
`/reports/capabilities` would otherwise route to the detail handler and fail on
the integer coercion — the same trap Modules 7 and 8 hit.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.core.security import CurrentUser, get_current_user
from app.db.base import get_db
from app.domain.reports.blocks import (
    BlockKind, ChartKind, EvidenceSource, REPORT_TITLES, ReportType,
    SectionKey, Theme, narratives_for, sections_for,
)
from app.models.report import Report
from app.schemas.report import (
    ArtifactOut, CapabilitiesOut, GenerateRequest, GenerateResponse, JobOut,
    ReportDetailOut, ReportOut, ReportTypeOut, SectionOut, StatisticsOut,
)
from app.services.reports.renderers.base import (
    OutputFormat, registered_formats,
)
from app.services.reports.serialise import document_from_dict
from app.services.reports.service import ReportError, ReportService

router = APIRouter(tags=["reports"])


def _service(db: Session = Depends(get_db)) -> ReportService:
    return ReportService(db)


def _owned(service: ReportService, report_id: int, user: CurrentUser) -> Report:
    report = service.get(report_id)
    if report is None or report.owner_id != user.id:
        # 404 rather than 403: revealing that someone else's report exists is
        # itself a disclosure.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "report not found")
    return report


# ===========================================================================
@router.post(
    "/reports/generate", response_model=GenerateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Generate a research report",
)
def generate(
    payload: GenerateRequest,
    service: ReportService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> GenerateResponse:
    try:
        result = service.generate(
            payload.company_id, payload.report_type, owner_id=user.id,
            formats=payload.formats, theme=payload.theme,
            analyst=payload.analyst, portfolio_id=payload.portfolio_id,
            include_ai=payload.include_ai, use_cache=payload.use_cache,
        )
    except ReportError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

    report = result.report
    if result.cached:
        message = (
            "Returned an existing report — the inputs are unchanged since it "
            "was generated."
        )
    elif report.insufficient_count:
        message = (
            f"Generated version {report.version}. "
            f"{report.insufficient_count} of {report.section_count} sections "
            f"had insufficient evidence and say so explicitly."
        )
    else:
        message = f"Generated version {report.version}. All sections populated."

    return GenerateResponse(
        report=ReportOut.model_validate(report),
        cached=result.cached, timings=result.timings,
        errors=dict(service.errors), message=message,
    )


@router.get("/reports", response_model=list[ReportOut])
def list_reports(
    company_id: str | None = Query(default=None),
    report_type: ReportType | None = Query(default=None),
    include_superseded: bool = Query(default=True),
    limit: int = Query(default=50, ge=1, le=200),
    service: ReportService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> list[ReportOut]:
    return [
        ReportOut.model_validate(r)
        for r in service.list_reports(
            owner_id=user.id, company_id=company_id, report_type=report_type,
            include_superseded=include_superseded, limit=limit,
        )
    ]


@router.get("/reports/capabilities", response_model=CapabilitiesOut)
def capabilities(
    user: CurrentUser = Depends(get_current_user),
) -> CapabilitiesOut:
    """Self-description, derived from the registries rather than hard-coded."""
    return CapabilitiesOut(
        report_types=[
            ReportTypeOut(
                key=rt.value, label=REPORT_TITLES[rt],
                sections=[s.value for s in sections_for(rt)],
                narratives=list(narratives_for(rt)),
            )
            for rt in ReportType
        ],
        formats=[
            {
                "key": f.value, "media_type": f.media_type,
                "extension": f.extension,
            }
            for f in registered_formats()
        ],
        sections=[s.value for s in SectionKey],
        chart_kinds=[c.value for c in ChartKind],
        themes=[t.value for t in Theme],
        evidence_sources=[e.value for e in EvidenceSource],
        block_kinds=[b.value for b in BlockKind],
    )


@router.get("/reports/statistics", response_model=StatisticsOut)
def statistics(
    service: ReportService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> StatisticsOut:
    return StatisticsOut(**service.statistics(owner_id=user.id))


@router.get("/reports/jobs", response_model=list[JobOut])
def jobs(
    service: ReportService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> list[JobOut]:
    return [JobOut.model_validate(j) for j in service.jobs(owner_id=user.id)]


# ===========================================================================
@router.get("/reports/{report_id}", response_model=ReportDetailOut)
def get_report(
    report_id: int,
    include_document: bool = Query(default=False),
    service: ReportService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> ReportDetailOut:
    report = _owned(service, report_id, user)
    detail = ReportDetailOut.model_validate(report)
    # `from_attributes` populates `document` straight off the ORM column, so
    # the flag has to clear it explicitly. Left in, every list-adjacent call
    # would ship the entire block tree — several hundred kilobytes per report
    # that the caller did not ask for.
    detail.document = None

    if report.document:
        document = document_from_dict(report.document)
        detail.sections = [
            SectionOut(
                key=s.key.value, title=s.title, sufficient=s.sufficient,
                reason=s.reason, block_count=len(s.blocks),
                chart_count=len(s.charts()), table_count=len(s.tables()),
                evidence_count=len(s.evidence()), word_count=s.word_count(),
            )
            for s in document.ordered()
        ]
        if include_document:
            detail.document = report.document
    return detail


@router.get("/reports/{report_id}/versions", response_model=list[ReportOut])
def versions(
    report_id: int,
    service: ReportService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> list[ReportOut]:
    report = _owned(service, report_id, user)
    return [
        ReportOut.model_validate(r)
        for r in service.versions(
            report.company_id, ReportType(report.report_type)
        )
    ]


@router.get(
    "/reports/{report_id}/download/{fmt}",
    summary="Download a rendered artefact",
)
def download(
    report_id: int,
    fmt: OutputFormat,
    service: ReportService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> Response:
    _owned(service, report_id, user)
    artifact = service.artifact(report_id, fmt)
    if artifact is None:
        # Render it now from the stored block tree rather than refusing: the
        # report's content is fixed, so producing another view of it is safe.
        try:
            artifact = service.render_additional(report_id, fmt)
        except ReportError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))

    return Response(
        content=artifact.payload,
        media_type=artifact.media_type,
        headers={
            "Content-Disposition":
                f'attachment; filename="{artifact.filename}"',
            "Content-Length": str(artifact.size_bytes),
        },
    )


@router.get(
    "/reports/{report_id}/preview", response_class=HTMLResponse,
    summary="Inline HTML preview",
)
def preview(
    report_id: int,
    service: ReportService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> HTMLResponse:
    _owned(service, report_id, user)
    artifact = service.artifact(report_id, OutputFormat.HTML)
    if artifact is None:
        try:
            artifact = service.render_additional(report_id, OutputFormat.HTML)
        except ReportError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return HTMLResponse(content=artifact.payload.decode("utf-8"))


@router.delete(
    "/reports/{report_id}", status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_report(
    report_id: int,
    service: ReportService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> Response:
    _owned(service, report_id, user)
    try:
        service.delete(report_id)
    except ReportError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
