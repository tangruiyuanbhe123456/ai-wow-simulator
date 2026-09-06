"""V16 Guardian REST API."""
from __future__ import annotations
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from server.db import connect
from server.db.schema_v16 import ensure_v16_schema
from server.guardian import (
    open_threat, list_threats, get_threat, resolve_threat,
    scan_and_open_threats, detect_brute_force, detect_economic_anomaly,
)
from server.guardian.response import submit as resp_submit, list_for_threat, get as resp_get
from server.guardian.chest import award, open_chest, stats_for
from server.guardian.decision import accept, reject, resolve_threat as decision_resolve


router = APIRouter(prefix="/api/v1/guardian", tags=["v16-guardian"])


# --- Ensure schema on import -----------------------------------------------

def _ensure():
    c = connect()
    ensure_v16_schema(c)
    c.close()


_ensure()


# --- Pydantic models -------------------------------------------------------

class OpenThreatBody(BaseModel):
    category: str
    evidence: dict = {}
    severity: Optional[str] = None
    detected_by: str = "system"
    title: Optional[str] = None
    description: Optional[str] = None


class ResponseBody(BaseModel):
    guardian_pid: str
    analysis: str
    recommended_action: str
    evidence_refs: list = []
    confidence: int = 50


class DecisionBody(BaseModel):
    response_id: str
    operator_pid: str = "operator:human"
    notes: str = ""


class OpenChestBody(BaseModel):
    guardian_pid: str


# --- Endpoints --------------------------------------------------------------

@router.get("/threats")
def ep_list_threats(status: Optional[str] = Query(None),
                    limit: int = Query(50, ge=1, le=200)):
    return list_threats(connect(), status=status, limit=limit)


@router.post("/threats/open")
def ep_open_threat(body: OpenThreatBody):
    return open_threat(connect(), category=body.category, evidence=body.evidence,
                       severity=body.severity, detected_by=body.detected_by,
                       title=body.title, description=body.description)


@router.get("/threats/{tid}")
def ep_get_threat(tid: str):
    t = get_threat(connect(), tid)
    if not t:
        raise HTTPException(404, "not_found")
    t["responses"] = list_for_threat(connect(), tid)
    return t


@router.post("/scan")
def ep_scan():
    """Run all detectors, open any new threats."""
    return {"opened": scan_and_open_threats(connect())}


@router.post("/threats/{tid}/response")
def ep_submit_response(tid: str, body: ResponseBody):
    return resp_submit(
        connect(), threat_id=tid, guardian_pid=body.guardian_pid,
        analysis=body.analysis, recommended_action=body.recommended_action,
        evidence_refs=body.evidence_refs, confidence=body.confidence,
    )


@router.get("/responses/{rid}")
def ep_get_response(rid: str):
    r = resp_get(connect(), rid)
    if not r:
        raise HTTPException(404, "not_found")
    return r


@router.post("/responses/accept")
def ep_accept(body: DecisionBody):
    return accept(connect(), response_id=body.response_id,
                  operator_pid=body.operator_pid, notes=body.notes)


@router.post("/responses/reject")
def ep_reject(body: DecisionBody):
    return reject(connect(), response_id=body.response_id,
                  operator_pid=body.operator_pid, notes=body.notes)


@router.post("/threats/{tid}/resolve")
def ep_resolve_threat(tid: str, body: DecisionBody):
    """Resolve threat (no specific response)."""
    return decision_resolve(connect(), threat_id=tid,
                           operator_pid=body.operator_pid, notes=body.notes)


@router.post("/chests/{chest_id}/open")
def ep_open_chest(chest_id: str, body: OpenChestBody):
    return open_chest(connect(), chest_id, body.guardian_pid)


@router.get("/guardians/{pid}/stats")
def ep_guardian_stats(pid: str):
    return stats_for(connect(), pid)
