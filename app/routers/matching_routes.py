import json
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.auth import get_current_user
from app.database import get_db
from app.matching.preferences import preferences
from app.matching.ranking import rrf_rank

router = APIRouter(prefix='/api/amos', tags=['Matching settings'])


def admin(user=Depends(get_current_user)):
    if not user.get('is_admin'):
        raise HTTPException(403, 'Only administrators can change matching settings or run evaluations')
    return user


class MatchingSettings(BaseModel):
    reranker: Literal['none', 'cross_encoder'] = 'none'
    ranking_mode: Literal['weighted', 'rrf'] = 'weighted'
    collect_confirmed_mappings: bool = False
    tuning_enabled: bool = False
    pn_weight: float = Field(0.25, ge=0, le=1)


@router.get('/matching-settings')
def get_settings(user=Depends(get_current_user), db: Session = Depends(get_db)):
    result = preferences(db, user['schema_name'])
    result['is_admin'] = bool(user.get('is_admin'))
    result['confirmed_count'] = db.execute(text('SELECT count(*) FROM public.confirmed_mappings WHERE tenant_id=:id'), {'id': user['tenant_id']}).scalar_one()
    return result


@router.put('/matching-settings')
def save_settings(req: MatchingSettings, user=Depends(admin), db: Session = Depends(get_db)):
    current = preferences(db, user['schema_name'])
    if req.pn_weight != current['pn_weight'] and not req.tuning_enabled:
        raise HTTPException(400, 'Enable evaluation and tuning before changing weights')
    db.execute(text('''UPDATE public.tenants SET ranking_mode=:mode, collect_confirmed_mappings=:collect,
        tuning_enabled=:tuning, pn_weight=:weight, reranker=:reranker WHERE id=:id'''),
        dict(mode=req.ranking_mode, collect=req.collect_confirmed_mappings, tuning=req.tuning_enabled,
             weight=req.pn_weight, reranker=req.reranker, id=user['tenant_id']))
    db.commit()
    return get_settings(user, db)


class Confirmation(BaseModel):
    session_id: str
    source_pn: str
    target_pn: str | None = None


@router.post('/confirmed-mappings')
def confirm_mapping(req: Confirmation, user=Depends(get_current_user), db: Session = Depends(get_db)):
    from app.routers.spec2500_routes import PARSED_SESSIONS
    if not preferences(db, user['schema_name'])['collect_confirmed_mappings']:
        raise HTTPException(403, 'Confirmed-mapping collection is disabled')
    session = PARSED_SESSIONS.get(req.session_id)
    if not session or session['schema'] != user['schema_name']:
        raise HTTPException(404, 'Upload session not found')
    candidates = session.get('review_candidates', {}).get(req.source_pn)
    if candidates is None:
        raise HTTPException(400, 'This part has not been analyzed in the session')
    if req.target_pn is not None:
        known = db.execute(text(f'SELECT part_number FROM "{user["schema_name"]}".amos_part_master WHERE part_number=:pn'), {'pn': req.target_pn}).scalar()
        if known is None:
            raise HTTPException(400, 'Select a part from the AMOS catalogue')
    db.execute(text('''INSERT INTO public.confirmed_mappings
        (tenant_id, root_pn, source_pn, source_description, target_pn, candidates, confirmed_by)
        VALUES (:id, :root, :source, :description, :target, CAST(:candidates AS JSONB), :actor)
        ON CONFLICT (tenant_id, root_pn, source_pn, source_description) DO UPDATE SET
        target_pn=EXCLUDED.target_pn, candidates=EXCLUDED.candidates,
        confirmed_by=EXCLUDED.confirmed_by, confirmed_at=CURRENT_TIMESTAMP'''),
        dict(id=user['tenant_id'], root=session['selected_root_pn'], source=req.source_pn,
             description=session['data']['pn_descriptions'].get(req.source_pn, ''), target=req.target_pn,
             candidates=json.dumps(candidates), actor=user['id']))
    db.commit()
    return {'message': 'Reviewed mapping saved' if req.target_pn else 'Reviewed no-match saved'}


class EvaluationRequest(BaseModel):
    pn_weight: float = Field(0.25, ge=0, le=1)


def weighted_rank(items, weight):
    ranked = []
    for m in items:
        desc = max(m.get('description_score') or 0, m.get('semantic_score') or 0)
        score = max(m['pn_score'], min(98, weight*m['pn_score'] + (1-weight)*desc))
        ranked.append(dict(m, evaluation_score=score))
    return sorted(ranked, key=lambda m: (m['match_type']=='EXACT', m['match_type']=='EXACT_NORMALIZED', m['evaluation_score']), reverse=True)


def evaluate(rows, weight):
    counts = {name: {'top1': 0, 'top5': 0} for name in ('weighted', 'rrf')}
    eligible = no_match = outside_pool = 0
    for target, candidates in rows:
        if target is None:
            no_match += 1
            continue
        if target not in {m['candidate'] for m in candidates}:
            outside_pool += 1
            continue
        eligible += 1
        for name, ranked in [('weighted', weighted_rank(candidates, weight)), ('rrf', rrf_rank(candidates))]:
            rank = [m['candidate'] for m in ranked].index(target)
            counts[name]['top1'] += rank == 0
            counts[name]['top5'] += rank < 5
    return dict(eligible=eligible, no_match=no_match, outside_pool=outside_pool, results=counts,
                note='Retrospective comparison on saved candidate pools (up to 50 suggestions), not a held-out accuracy estimate. No-match cases are counted separately; neither ranker decides interchangeability.')


@router.post('/evaluate-matching')
def evaluate_matching(req: EvaluationRequest, user=Depends(admin), db: Session = Depends(get_db)):
    if not preferences(db, user['schema_name'])['tuning_enabled']:
        raise HTTPException(403, 'Enable evaluation and tuning first')
    rows = db.execute(text('SELECT target_pn, candidates FROM public.confirmed_mappings WHERE tenant_id=:id ORDER BY confirmed_at DESC LIMIT 1000'), {'id': user['tenant_id']}).fetchall()
    return dict(evaluate(rows, req.pn_weight), sample_count=len(rows), pn_weight=req.pn_weight)
