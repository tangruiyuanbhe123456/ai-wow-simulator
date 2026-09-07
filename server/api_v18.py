"""V18 Guardian Council REST API.

Endpoints:
  GET  /api/v1/council/members                       — list council roster (optional ?tier=)
  GET  /api/v1/council/members/{pid}                 — single member detail
  POST /api/v1/council/sync/{pid}                    — re-sync tier/status from stats
  POST /api/v1/council/proposals                     — open new proposal
  GET  /api/v1/council/proposals                     — list (optional ?status=)
  GET  /api/v1/council/proposals/{proposal_id}       — detail + tally
  POST /api/v1/council/proposals/{proposal_id}/vote  — cast weighted vote
  POST /api/v1/council/fund/contribute               — platform adds QC (admin)
  POST /api/v1/council/fund/distribute               — split among elite members
  GET  /api/v1/council/fund                          — fund balance
"""
from __future__ import annotations
from typing import Optional, List

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from server.db import connect
from server.db.schema_v18 import ensure_v18_schema
from server.guardian.council import (
    sync_member, list_council, open_proposal, cast_vote,
    tally_proposal, list_proposals, compute_tier,
    contribute_to_fund, distribute_fund, get_fund,
)


router = APIRouter(prefix="/api/v1/council", tags=["v18-council"])


def _ensure():
    c = connect()
    ensure_v18_schema(c)
    c.close()


_ensure()


# --- Roster -----------------------------------------------------------------

@router.get("/members")
def council_members(tier: Optional[str] = None,
                    limit: int = Query(default=100, ge=1, le=500)):
    c = connect()
    try:
        return {"members": list_council(c, tier=tier, limit=limit)}
    finally:
        c.close()


@router.get("/members/{pid}")
def council_member_detail(pid: str):
    c = connect()
    try:
        cur = c.execute(
            """SELECT pid, tier, vote_weight, elected_at, budget_share,
                      proposals_made, votes_cast
               FROM guardian_council WHERE pid=?""",
            (pid,),
        )
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="not_in_council")
        return {
            "pid": r[0], "tier": r[1], "vote_weight": r[2],
            "elected_at": r[3], "budget_share": r[4] or 0,
            "proposals_made": r[5] or 0, "votes_cast": r[6] or 0,
        }
    finally:
        c.close()


@router.post("/sync/{pid}")
def council_sync(pid: str):
    """Bring council row in sync with current guardian_stats."""
    c = connect()
    try:
        return sync_member(c, pid)
    finally:
        c.close()


# --- Proposals --------------------------------------------------------------

class OpenProposalBody(BaseModel):
    proposer_pid: str
    proposal_type: str                # 'threat_disposition' | 'governance'
    title: str
    body: str = ""
    options: Optional[List[dict]] = None
    target_id: Optional[str] = None
    duration_hours: int = 72


@router.post("/proposals")
def proposals_open(req: OpenProposalBody):
    c = connect()
    try:
        return open_proposal(
            c,
            proposer_pid=req.proposer_pid,
            proposal_type=req.proposal_type,
            title=req.title,
            body=req.body,
            options=req.options,
            target_id=req.target_id,
            duration_hours=req.duration_hours,
        )
    finally:
        c.close()


@router.get("/proposals")
def proposals_list(status: Optional[str] = None,
                   limit: int = Query(default=50, ge=1, le=200)):
    c = connect()
    try:
        return {"proposals": list_proposals(c, status=status, limit=limit)}
    finally:
        c.close()


@router.get("/proposals/{proposal_id}")
def proposal_detail(proposal_id: str, tally: bool = True):
    c = connect()
    try:
        proposals = list_proposals(c, limit=500)
        match = next((p for p in proposals if p["proposal_id"] == proposal_id), None)
        if not match:
            raise HTTPException(status_code=404, detail="proposal_not_found")
        if tally:
            t = tally_proposal(c, proposal_id)
            match["tally"] = t.get("tally")
            match["total_weight"] = t.get("total_weight")
            match["winner"] = t.get("winner")
            match["current_status"] = t.get("status")
        return match
    finally:
        c.close()


class VoteBody(BaseModel):
    voter_pid: str
    vote_option: str
    rationale: str = ""


@router.post("/proposals/{proposal_id}/vote")
def proposal_vote(proposal_id: str, req: VoteBody):
    c = connect()
    try:
        return cast_vote(
            c,
            proposal_id=proposal_id,
            voter_pid=req.voter_pid,
            vote_option=req.vote_option,
            rationale=req.rationale,
        )
    finally:
        c.close()


# --- Fund -------------------------------------------------------------------

class ContributeBody(BaseModel):
    qc_amount: int
    reason: str = ""


@router.post("/fund/contribute")
def fund_contribute(req: ContributeBody):
    c = connect()
    try:
        return contribute_to_fund(c, qc_amount=req.qc_amount, reason=req.reason)
    finally:
        c.close()


@router.post("/fund/distribute")
def fund_distribute():
    c = connect()
    try:
        return distribute_fund(c)
    finally:
        c.close()


@router.get("/fund")
def fund_balance():
    c = connect()
    try:
        return get_fund(c)
    finally:
        c.close()
