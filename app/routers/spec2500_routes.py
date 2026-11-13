import io
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Response
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.database import get_db
from app.auth import get_current_user
from app.spec2500.parser import Spec2500Parser
from app.spec2500.transformer import Spec2500Transformer
from app.matching.fuzzy import RapidFuzzMatcher
from app.matching.parts import PartMatcher, encoder
from app.matching.preferences import preferences
from app.matching.reranker import reranker_warning
from pydantic import BaseModel
from typing import Dict, List, Any

router = APIRouter(prefix="/api/spec2500", tags=["Spec2500 Processing"])
matcher = RapidFuzzMatcher()
part_matcher = PartMatcher()

# In-memory session analysis store (or use Redis/DB table)
PARSED_SESSIONS: Dict[str, Any] = {}

class MatchCandidatesRequest(BaseModel):
    root_pn: str

class TransformRequest(BaseModel):
    session_id: str
    pn_mapping: Dict[str, str]
    fp_mapping: Dict[str, str]
    doc_mapping: Dict[str, str]

@router.post("/upload")
async def upload_spec2500(file: UploadFile = File(...), user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    content = await file.read()
    try:
        parser = Spec2500Parser(content)
        data = parser.extract_assembly_and_elements()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid Spec2500 XML: {str(e)}")

    s = user["schema_name"]
    prefs = preferences(db, s)
    # Get all master parts for root matching
    master_parts = db.execute(text(f'SELECT part_number, description FROM "{s}".amos_part_master')).fetchall()

    configured_roots = {r[0] for r in db.execute(text(f'''
        SELECT parent_pn FROM "{s}".pn_part_of_pn_conf
        UNION SELECT parent_pn FROM "{s}".fp_part_of_pn_conf
        UNION SELECT parent_pn FROM "{s}".doc_part_of_pn_conf
    ''')).fetchall()}
    clean_root = matcher.clean_str(data['root_pn'])
    root_pool = [(pn, description) for pn, description in master_parts
                 if pn in configured_roots or (clean_root and matcher.clean_str(pn) == clean_root)]
    # Avoid embedding hundreds of thousands of unrelated master descriptions
    # when looking for an asset. The global picker remains unrestricted.
    root_matches = part_matcher.match_many(
        [data['root_pn']], root_pool or master_parts,
        data['pn_descriptions'] if root_pool else {}, top_k=10,
        ranking_mode=prefs['ranking_mode'], pn_weight=prefs['pn_weight'], reranker=prefs['reranker'],
    )[data['root_pn']]

    import uuid
    session_id = str(uuid.uuid4())
    PARSED_SESSIONS[session_id] = {
        "raw_xml": content,
        "data": data,
        "schema": s
    }

    return {
        "session_id": session_id,
        "detected_root_pn": data["root_pn"],
        "root_description": data['pn_descriptions'].get(data['root_pn'], ''),
        "root_matches": root_matches,
        "matching_warning": reranker_warning([root_matches]),
        "total_sub_pns": len(set(data["sub_pns"]) - {data['root_pn']}),
        "total_fps": len(data["fp_codes"]),
        "total_docs": len(data["doc_numbers"])
    }

@router.post("/candidates/{session_id}")
def get_candidates_for_root(session_id: str, req: MatchCandidatesRequest, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    session = PARSED_SESSIONS.get(session_id)
    if not session or session['schema'] != user['schema_name']:
        raise HTTPException(status_code=404, detail="Session expired or not found")

    s = user["schema_name"]
    prefs = preferences(db, s)
    root_pn = req.root_pn
    master_parts = dict(db.execute(text(f'SELECT part_number, description FROM "{s}".amos_part_master')).fetchall())
    if root_pn not in master_parts:
        raise HTTPException(400, 'Choose a root part number from the AMOS cache')

    # 1. Fetch valid sub-PNs for this selected root PN from tenant DB
    sub_pns_db = [r[0] for r in db.execute(
        text(f'SELECT child_pn FROM "{s}".pn_part_of_pn_conf WHERE parent_pn = :p'), {"p": root_pn}
    ).fetchall()]

    # 2. Fetch valid FPs
    fps_db = [r[0] for r in db.execute(
        text(f'SELECT fp_code FROM "{s}".fp_part_of_pn_conf WHERE parent_pn = :p'), {"p": root_pn}
    ).fetchall()]

    # 3. Fetch valid Docs
    docs_db = [r[0] for r in db.execute(
        text(f'SELECT docno FROM "{s}".doc_part_of_pn_conf WHERE parent_pn = :p'), {"p": root_pn}
    ).fetchall()]

    # Run ranked fuzzy matching
    spec_data = session["data"]

    # Keep the root in the graph for export, but not in the subpart review list.
    subparts = [pn for pn in spec_data['sub_pns'] if pn != spec_data['root_pn']]
    pn_matches = part_matcher.match_many(
        subparts, [(pn, master_parts.get(pn, '')) for pn in sub_pns_db],
        spec_data['pn_descriptions'], top_k=50,
        ranking_mode=prefs['ranking_mode'], pn_weight=prefs['pn_weight'], reranker=prefs['reranker'],
    )
    fp_matches = {fp: matcher.match_item(fp, fps_db, top_k=5) for fp in spec_data["fp_codes"]}
    doc_matches = {doc: matcher.match_item(doc, docs_db, top_k=5) for doc in spec_data["doc_numbers"]}

    session["selected_root_pn"] = root_pn
    session['review_candidates'] = pn_matches

    return {
        "pn_matches": pn_matches,
        "collect_confirmed_mappings": prefs['collect_confirmed_mappings'],
        "pn_descriptions": spec_data['pn_descriptions'],
        "matching_warning": " ".join(filter(None, [encoder.warning, reranker_warning(pn_matches.values())])) or None,
        "fp_matches": fp_matches,
        "doc_matches": doc_matches,
        "available_pns_pool": sub_pns_db,
        "available_fps_pool": fps_db,
        "available_docs_pool": docs_db,
        "pn_children_map": spec_data["pn_children_map"],
        "configuration_warning": (
            f'No AMOS subpart configuration is available for root "{root_pn}". '
            'Check the selected root and refresh the AMOS data. '
            'Part numbers cannot be compared without configured candidates.'
            if not sub_pns_db else None
        )
    }

@router.post("/export")
def export_transformed_spec2500(req: TransformRequest, user: dict = Depends(get_current_user)):
    session = PARSED_SESSIONS.get(req.session_id)
    if not session or session['schema'] != user['schema_name']:
        raise HTTPException(status_code=404, detail="Session expired")

    selected_root_pn = session.get("selected_root_pn")
    if not selected_root_pn:
        raise HTTPException(status_code=400, detail="Confirm the root part number before exporting")

    pn_mapping = dict(req.pn_mapping)
    # Apply the confirmed root everywhere, including higher-assembly references.
    # A subpart selection must not overwrite or exclude the root asset.
    original_root_pn = session["data"]["root_pn"]
    if original_root_pn:
        pn_mapping[original_root_pn] = selected_root_pn

    transformer = Spec2500Transformer(session["raw_xml"])
    output_xml = transformer.transform(
        pn_mapping=pn_mapping,
        fp_mapping=req.fp_mapping,
        doc_mapping=req.doc_mapping,
        pn_children_map=session["data"]["pn_children_map"]
    )

    return Response(
        content=output_xml,
        media_type="application/xml",
        headers={"Content-Disposition": "attachment; filename=transformed_spec2500.xml"}
    )
