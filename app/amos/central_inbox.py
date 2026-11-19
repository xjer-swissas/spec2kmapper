"""Import each export independently and commit before acknowledging delivery."""
from collections import defaultdict
import hashlib
import logging

from sqlalchemy import text
from amoscentral.errors import ProtocolError
from app.amos.csv_import import prepare_exports
from app.amos.extractor import AmosDataSyncer
from app.amos.xml_import import parse_data_source
from app.amos.central_config import EXPORT_SERVICE

logger = logging.getLogger('uvicorn.error.amoscentral')


class RejectedMessage(ValueError):
    pass


class BrokerInbox:
    def __init__(self, sessions, routing, client):
        self.sessions = sessions
        self.routing = routing
        self.client = client

    def event(self, message, status, detail=None, kind=None):
        with self.sessions() as db, db.begin():
            self.write_event(db, message, status, detail, kind)
        logger.info('AMOScentral message %s: %s%s', message.message_id if message else '(unparsed)',
                    status, ': ' + detail if detail else '')

    def write_event(self, db, message, status, detail=None, kind=None):
        # Keep the sender's exact export-type value on every stage, including ACKs
        # and failures that occur before export routing.
        header_value = message.headers.get(self.routing.export_type_header) if message and self.routing.export_type_header else None
        db.execute(text('''INSERT INTO public.amoscentral_events
            (message_id, sender, status, detail, export_kind, export_header_value)
            VALUES (:id, :sender, :status, :detail, :kind, :header_value)'''),
            dict(id=message.message_id if message else None,
                 sender=message.sender if message else None, status=status,
                 detail=detail, kind=kind, header_value=header_value))

    def __call__(self, envelope):
        digest = hashlib.sha256(envelope.to_bytes()).hexdigest()
        tenant_id = None
        kind = None
        self.event(envelope, 'RECEIVED',
                   f"Service {envelope.headers.get('amc_service', '(missing)')}, version {envelope.headers.get('amc_service_version', '(missing)')}")
        try:
            self.route(envelope)
            kind = self.routing.export_kind(envelope.headers)
            with self.sessions() as db:
                tenant = db.execute(text('''SELECT id, schema_name FROM public.tenants
                    WHERE amoscentral_address=:sender AND amos_source='amoscentral' '''),
                    {'sender': envelope.sender}).first()
                if not tenant:
                    raise RejectedMessage('Sender has no tenant accepting AMOScentral exports; assign the sender in Connection settings')
                tenant_id, schema = tenant
            message = self.client.codec.decode(envelope)
            kind, rows = parse_data_source(message.body, self.routing.data_source_ids, export_kind=kind)
            self.event(envelope, 'VALIDATED', f'{len(rows)} rows validated', kind)
            with self.sessions() as db, db.begin():
                tenant = db.execute(text('''SELECT id, last_sync_status FROM public.tenants
                    WHERE id=:id AND amoscentral_address=:sender AND amos_source='amoscentral' FOR UPDATE'''),
                    dict(id=tenant_id, sender=envelope.sender)).first()
                if not tenant:
                    raise RejectedMessage('Tenant is no longer accepting messages from this sender')
                if tenant[1] == 'RUNNING':
                    raise RuntimeError('Tenant refresh is busy; delivery will be retried')
                receipt = db.execute(text('SELECT digest, status FROM public.amoscentral_receipts WHERE message_id=:id'),
                                     {'id': envelope.message_id}).first()
                if receipt and receipt[0] == digest and receipt[1] == 'IMPORTED':
                    return
                self.apply_export(db, schema, kind, rows)
                db.execute(text('''UPDATE public.tenants SET last_sync_status='SUCCESS',
                    last_sync_time=CURRENT_TIMESTAMP, sync_error=NULL, sync_phase=:phase WHERE id=:id'''),
                    dict(id=tenant_id, phase=f'AMOScentral imported {kind}: {len(rows)} rows'))
                self.record_receipt(db, envelope, digest, tenant_id, 'IMPORTED', f'{kind}: {len(rows)} rows')
                self.write_event(db, envelope, 'IMPORTED', f'{len(rows)} rows committed', kind)
            logger.info('AMOScentral message %s: IMPORTED %s (%s rows)', envelope.message_id, kind, len(rows))
        except (ValueError, ProtocolError) as exc:
            reason = str(exc)
            with self.sessions() as db, db.begin():
                self.record_receipt(db, envelope, digest, tenant_id, 'REJECTED', reason)
            self.event(envelope, 'REJECTED', reason, kind)
            return False  # Preserve invalid deliveries for inspection/correction.
        try:
            from app.matching.catalog import schedule
            schedule(schema)
        except Exception:
            logger.warning('AMOScentral import committed; search index scheduling failed')

    @staticmethod
    def apply_export(db, schema, kind, rows):
        # Schema comes from the tenant registry, never the message.
        parts = list(db.execute(text(f'SELECT part_number, description FROM "{schema}".amos_part_master')))
        children, fps, docs = defaultdict(set), defaultdict(dict), defaultdict(set)
        for parent, child in db.execute(text(f'SELECT parent_pn, child_pn FROM "{schema}".pn_part_of_pn_conf WHERE is_direct=1')):
            children[parent].add(child)
        for parent, code, description in db.execute(text(f'SELECT parent_pn, fp_code, fp_description FROM "{schema}".fp_part_of_pn_conf WHERE is_direct=1')):
            fps[parent][code] = description
        for parent, doc in db.execute(text(f'SELECT parent_pn, docno FROM "{schema}".doc_part_of_pn_conf WHERE is_direct=1')):
            docs[parent].add(doc)
        if kind == 'parts':
            parts = list({row['part_number']: row['description'] for row in rows}.items())
        elif kind == 'configurations':
            _, children, fps, _ = prepare_exports([], rows, [])
        elif kind == 'documents':
            _, _, _, docs = prepare_exports([], [], rows)
        else:
            raise RejectedMessage('Unknown export type')
        return AmosDataSyncer({}, schema, db).replace_cache(parts, children, fps, docs, commit=False)

    def route(self, envelope):
        headers = envelope.headers
        if headers.get('amc_service', '').casefold() != EXPORT_SERVICE.casefold():
            raise RejectedMessage(f"Unexpected amc_service {headers.get('amc_service', '(missing)')}; expected {EXPORT_SERVICE}")
        if headers.get('amc_service_version') != self.routing.service_version:
            raise RejectedMessage(f"Unexpected amc_service_version {headers.get('amc_service_version', '(missing)')}; expected {self.routing.service_version}")

    @staticmethod
    def record_receipt(db, message, digest, tenant_id, status, reason=None):
        db.execute(text('''INSERT INTO public.amoscentral_receipts
            (message_id, tenant_id, sender, digest, status, reason)
            VALUES (:id, :tenant, :sender, :digest, :status, :reason)
            ON CONFLICT (message_id) DO UPDATE SET tenant_id=EXCLUDED.tenant_id,
                digest=EXCLUDED.digest, status=EXCLUDED.status, reason=EXCLUDED.reason'''),
            dict(id=message.message_id, tenant=tenant_id, sender=message.sender,
                 digest=digest, status=status, reason=reason))
