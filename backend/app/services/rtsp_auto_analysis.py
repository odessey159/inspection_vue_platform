"""Fire-and-forget provider_yolo analysis when RTSP streams connect via the watchdog."""

from __future__ import annotations

import logging
import threading

from sqlmodel import Session, select

from ..db import engine
from ..models import Project
from ..settings import RTSP_WATCH_AUTO_ANALYSIS_MODE, VISION_MODEL, resolve_vision_model
from .analysis import run_analysis
from .analysis_summary import read_analysis_summary
from .rtsp_recorder import is_rtsp_project, resolve_storage_key_for_rtsp_url

logger = logging.getLogger(__name__)

_inflight_project_ids: set[int] = set()
_inflight_lock = threading.Lock()


def rtsp_auto_analysis_mode() -> str | None:
    """Return batch auto-analysis mode, or None when the live monitor LLM path owns the stream."""
    from ..settings import RTSP_YOLO_MONITOR_LLM_ENABLED

    # Prefer the continuous monitor→LLM path when it is enabled.
    if RTSP_YOLO_MONITOR_LLM_ENABLED:
        return None

    mode = RTSP_WATCH_AUTO_ANALYSIS_MODE.strip().lower()
    if mode == "provider_yolo":
        return mode
    return None


def rtsp_auto_analysis_settings_payload() -> dict[str, object]:
    mode = rtsp_auto_analysis_mode()
    return {
        "enabled": mode == "provider_yolo",
        "mode": mode,
        "default_model": VISION_MODEL,
    }


def schedule_auto_analysis_for_rtsp_url(rtsp_url: str) -> None:
    """Queue background analysis for all RTSP projects matching this stream storage key."""
    mode = rtsp_auto_analysis_mode()
    if mode is None:
        return

    storage_key = resolve_storage_key_for_rtsp_url(rtsp_url)
    thread = threading.Thread(
        target=_run_auto_analysis_for_storage_key,
        args=(storage_key, rtsp_url, mode),
        name=f"rtsp-auto-analysis-{storage_key}",
        daemon=True,
    )
    thread.start()


def schedule_auto_analysis_for_project(project_id: int) -> None:
    mode = rtsp_auto_analysis_mode()
    if mode is None:
        return

    thread = threading.Thread(
        target=_run_auto_analysis_for_project,
        args=(project_id, mode),
        name=f"rtsp-auto-analysis-project-{project_id}",
        daemon=True,
    )
    thread.start()


def list_rtsp_projects_for_storage_key(session: Session, storage_key: str) -> list[Project]:
    matches: list[Project] = []
    for project in session.exec(select(Project)).all():
        if not is_rtsp_project(project):
            continue
        try:
            project_key = resolve_storage_key_for_rtsp_url(project.bag_dir.strip())
        except ValueError:
            continue
        if project_key == storage_key:
            matches.append(project)
    return matches


def project_accepts_auto_analysis(project: Project, mode: str) -> bool:
    if mode != "provider_yolo":
        return False
    if project.status in {"indexing", "provider_analyzing"}:
        return False
    if not project.scene_path:
        return False

    summary = read_analysis_summary(project)
    preferred_mode = str(summary.get("analysis_mode") or "").strip().lower()
    if preferred_mode and preferred_mode not in {"provider_yolo"}:
        return False
    return True


def _resolve_auto_analysis_model(project: Project) -> str | None:
    summary = read_analysis_summary(project)
    model_override = str(summary.get("analysis_model") or "").strip() or None
    return resolve_vision_model(model_override)


def _mark_inflight(project_id: int) -> bool:
    with _inflight_lock:
        if project_id in _inflight_project_ids:
            return False
        _inflight_project_ids.add(project_id)
        return True


def _clear_inflight(project_id: int) -> None:
    with _inflight_lock:
        _inflight_project_ids.discard(project_id)


def _run_auto_analysis_for_storage_key(storage_key: str, rtsp_url: str, mode: str) -> None:
    with Session(engine) as session:
        projects = list_rtsp_projects_for_storage_key(session, storage_key)
        if not projects:
            logger.info(
                "RTSP auto-analysis skipped for %s: no imported RTSP project matches this stream",
                storage_key,
            )
            return

        for project in projects:
            if project.id is None:
                continue
            _run_auto_analysis_for_project(project.id, mode, session=session, rtsp_url=rtsp_url)


def _run_auto_analysis_for_project(
    project_id: int,
    mode: str,
    *,
    session: Session | None = None,
    rtsp_url: str | None = None,
) -> None:
    """Run analysis once per project, skipping duplicates via _inflight_project_ids."""
    owns_session = session is None
    active_session = session or Session(engine)
    try:
        if not _mark_inflight(project_id):
            logger.info("RTSP auto-analysis skipped for project %s: already running", project_id)
            return

        project = active_session.get(Project, project_id)
        if project is None or not is_rtsp_project(project):
            return
        if not project_accepts_auto_analysis(project, mode):
            logger.info(
                "RTSP auto-analysis skipped for project %s (%s): status=%s",
                project_id,
                project.name,
                project.status,
            )
            return

        model = _resolve_auto_analysis_model(project)
        logger.info(
            "RTSP live auto-analysis starting for project %s (%s) mode=%s model=%s url=%s",
            project_id,
            project.name,
            mode,
            model,
            rtsp_url or project.bag_dir,
        )
        run_analysis(active_session, project, mode=mode, model=model)
        logger.info("RTSP auto-analysis finished for project %s status=%s", project_id, project.status)
    except Exception:
        logger.exception("RTSP auto-analysis failed for project %s", project_id)
    finally:
        _clear_inflight(project_id)
        if owns_session:
            active_session.close()
