from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from ..db import get_session
from ..models import Finding
from ..schemas import FindingPatchRequest


router = APIRouter(prefix="/api/findings", tags=["findings"])


@router.patch("/{finding_id}")
def patch_finding(
    finding_id: int,
    payload: FindingPatchRequest,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    finding = session.get(Finding, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    if payload.review_status is not None:
        finding.review_status = payload.review_status
    if payload.reviewer_notes is not None:
        finding.reviewer_notes = payload.reviewer_notes
    if payload.needs_review is not None:
        finding.needs_review = payload.needs_review
    finding.updated_at = datetime.now(timezone.utc)
    session.add(finding)
    session.commit()
    session.refresh(finding)
    return {
        "id": finding.id,
        "review_status": finding.review_status,
        "reviewer_notes": finding.reviewer_notes,
        "needs_review": finding.needs_review,
    }
