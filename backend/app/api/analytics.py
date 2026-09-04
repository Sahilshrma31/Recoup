"""Dashboard metrics (design §19, §27, §28)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ExperimentResult
from ..schemas import OverviewOut, PerformanceOut, PipelineItem
from ..services import analytics_service
from ..services.runtime import get_runtime

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/overview", response_model=OverviewOut)
def overview(db: Session = Depends(get_db)):
    return analytics_service.overview(db)


@router.get("/pipeline", response_model=list[PipelineItem])
def pipeline(db: Session = Depends(get_db)):
    return analytics_service.pipeline(db)


@router.get("/performance", response_model=PerformanceOut)
def performance(db: Session = Depends(get_db)):
    return analytics_service.performance(db)


@router.get("/failures")
def failures(db: Session = Depends(get_db)):
    return analytics_service.failure_breakdown(db)


@router.get("/agent")
def agent_stats(db: Session = Depends(get_db)):
    """How the AI and the policy engine are actually behaving."""
    stats = analytics_service.ai_decision_stats(db)
    try:
        stats["runtime"] = get_runtime().status()
    except RuntimeError:
        stats["runtime"] = {"running": False}
    return stats


@router.get("/experiment")
def experiment(db: Session = Depends(get_db)):
    """Latest measured baseline-vs-agent run, or null if none has been run."""
    row = db.execute(
        select(ExperimentResult).order_by(ExperimentResult.id.desc()).limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    return {"id": row.id, "label": row.label, "created_at": row.created_at, **row.payload}
