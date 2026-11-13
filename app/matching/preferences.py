from sqlalchemy import text


def preferences(db, schema):
    row = db.execute(text('''SELECT ranking_mode, collect_confirmed_mappings, tuning_enabled,
        pn_weight, reranker FROM public.tenants WHERE schema_name=:s'''), {'s': schema}).fetchone()
    return dict(ranking_mode=row[0], collect_confirmed_mappings=row[1], tuning_enabled=row[2], pn_weight=row[3], reranker=row[4])
