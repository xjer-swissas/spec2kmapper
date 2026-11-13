"""Persist each component before ACK; replace caches only for complete snapshots."""
from datetime import datetime, timezone, timedelta
import hashlib
import json

from sqlalchemy import text

from app.amos.csv_import import prepare_exports
from app.amos.extractor import AmosDataSyncer
from app.amos.xml_import import parse_data_source


class RejectedMessage(ValueError):
    pass


class BrokerInbox:
    def __init__(self, sessions, routing, client):
        self.sessions = sessions
        self.routing = routing
        self.client = client

    def __call__(self, envelope):
        digest = hashlib.sha256(envelope.to_bytes()).hexdigest()
        try:
            batch_id, snapshot_at = self.route(envelope)
            # Reject unknown senders before downloading their keys or decrypting bodies.
            with self.sessions() as db:
                known = db.execute(text('''SELECT schema_name FROM public.tenants
                    WHERE amoscentral_address=:sender AND amos_source='amoscentral' ''')
                    , {'sender': envelope.sender}).first()
                if not known:
                    raise RejectedMessage('Sender has no active tenant assignment')
                schema = known[0]
            message = self.client.codec.decode(envelope)
            kind, rows = parse_data_source(message.body, self.routing.data_source_ids)
            if kind == 'parts':
                # Conflicting master descriptions must be rejected before staging/ACK,
                # rather than making the final component retry an invalid snapshot forever.
                prepare_exports(rows, [], [])
        except (ValueError, RejectedMessage) as exc:
            # Store only an error category, never an XML body or credentials.
            self.reject(envelope, digest, type(exc).__name__)
            return
        schema_to_index = None
        try:
            with self.sessions() as db:
                with db.begin():
                    tenant = db.execute(text('''
                        SELECT id, schema_name, amos_source, last_sync_status, amoscentral_last_snapshot_at
                        FROM public.tenants WHERE schema_name=:schema AND amoscentral_address=:sender FOR UPDATE
                    '''), {'schema': schema, 'sender': envelope.sender}).mappings().first()
                    if not tenant or tenant['amos_source'] != 'amoscentral':
                        raise RejectedMessage('Tenant is not accepting AMOScentral exports')
                    if tenant['last_sync_status'] == 'RUNNING':
                        # A database/CSV operation currently owns the cache. Retry later.
                        raise RuntimeError('Tenant refresh is busy')
                    receipt = db.execute(text('SELECT digest FROM public.amoscentral_receipts WHERE message_id=:id'),
                                         {'id': envelope.message_id}).first()
                    if receipt:
                        if receipt[0] != digest:
                            raise RejectedMessage('Message ID reused with different content')
                        return
                    tenant_id = tenant['id']
                    self.expire_batches(db, tenant_id)
                    batch = db.execute(text('''
                        SELECT * FROM public.amoscentral_batches WHERE tenant_id=:tenant AND batch_id=:batch AND sender=:sender
                    '''), {'tenant': tenant_id, 'batch': batch_id, 'sender': envelope.sender}).mappings().first()
                    if batch is None:
                        pending = db.execute(text('''
                            SELECT count(*) FROM public.amoscentral_batches
                            WHERE tenant_id=:tenant AND status='PENDING'
                        '''), {'tenant': tenant_id}).scalar_one()
                        if pending >= self.routing.pending_batch_limit:
                            raise RejectedMessage('Too many incomplete snapshots')
                        batch = dict(status='PENDING', exports={}, digests={}, sender=envelope.sender,
                                     snapshot_at=snapshot_at)
                    if batch['sender'] != envelope.sender:
                        raise RejectedMessage('Sender changed within a snapshot')
                    component_digest = hashlib.sha256(message.body).hexdigest()
                    if kind in batch['digests'] and batch['digests'][kind] != component_digest:
                        raise RejectedMessage('Conflicting component for the same snapshot')
                    if batch['status'] != 'PENDING':
                        self.record_receipt(db, envelope, digest, tenant_id, 'IGNORED', 'Snapshot already closed')
                        return
                    exports, digests = dict(batch['exports']), dict(batch['digests'])
                    exports[kind], digests[kind] = rows, component_digest
                    # Default amc_send_time differs per component: earliest send orders snapshots.
                    snapshot_at = min(snapshot_at, batch['snapshot_at'])
                    last_snapshot = tenant['amoscentral_last_snapshot_at']
                    status = 'PENDING'
                    if len(exports) == 3:
                        if last_snapshot and snapshot_at <= last_snapshot:
                            status = 'SUPERSEDED'
                        else:
                            prepared = prepare_exports(exports['parts'], exports['configurations'], exports['documents'])
                            counts = AmosDataSyncer({}, schema, db).replace_cache(*prepared, commit=False)
                            db.execute(text('''
                                UPDATE public.tenants SET last_sync_status='SUCCESS',
                                    last_sync_time=CURRENT_TIMESTAMP, sync_started_at=CURRENT_TIMESTAMP,
                                    sync_error=NULL, sync_phase=:phase, amoscentral_last_snapshot_at=:snapshot
                                WHERE id=:id
                            '''), {'id': tenant_id, 'snapshot': snapshot_at,
                                   'phase': f'AMOScentral complete: {counts[0]} parts, {counts[1]} links, {counts[2]} positions, {counts[3]} documents'})
                            schema_to_index = schema
                            status = 'COMPLETE'
                    if status != 'PENDING':
                        exports = {}  # Retain replay guards, release staged customer payloads.
                    db.execute(text('''
                        INSERT INTO public.amoscentral_batches
                            (tenant_id, batch_id, sender, snapshot_at, status, exports, digests)
                        VALUES (:tenant, :batch, :sender, :snapshot, :status, CAST(:exports AS JSONB), CAST(:digests AS JSONB))
                        ON CONFLICT (tenant_id, sender, batch_id) DO UPDATE
                        SET snapshot_at=EXCLUDED.snapshot_at, status=EXCLUDED.status,
                            exports=EXCLUDED.exports, digests=EXCLUDED.digests
                    '''), {'tenant': tenant_id, 'batch': batch_id, 'sender': envelope.sender,
                           'snapshot': snapshot_at, 'status': status,
                           'exports': json.dumps(exports), 'digests': json.dumps(digests)})
                    self.record_receipt(db, envelope, digest, tenant_id, status)
        except RejectedMessage:
            self.reject(envelope, digest, 'RoutingOrSnapshotConflict')
            return
        if schema_to_index:
            # A failed index scheduling call must not cause a committed snapshot to be replayed.
            import logging
            try:
                from app.matching.catalog import schedule
                schedule(schema_to_index)
            except Exception:
                logging.getLogger(__name__).warning('AMOScentral snapshot committed; index scheduling failed')

    def route(self, envelope):
        headers = envelope.headers
        if (headers.get('amc_service') != self.routing.service or
                headers.get('amc_service_version') != self.routing.service_version or
                (self.routing.product and headers.get('amc_product') != self.routing.product)):
            raise RejectedMessage('Unexpected service, version, or product')
        encodings = [value.strip() for value in headers.get('amc_content_encoding', '').split(',')]
        if self.routing.require_encryption and 'ChaCha20_7539' not in encodings:
            raise RejectedMessage('Encrypted export required')
        if headers.get('amc_requested_ack') != 'technical':
            raise RejectedMessage('Technical acknowledgment must be requested for durable delivery')
        batch_id = headers.get(self.routing.batch_header, '')
        if not batch_id or len(batch_id) > 256:
            raise RejectedMessage('Missing snapshot correlation ID')
        try:
            sent_at = datetime.fromisoformat(headers[self.routing.snapshot_header].replace('Z', '+00:00'))
            if sent_at.tzinfo is None or sent_at > datetime.now(timezone.utc) + timedelta(minutes=5):
                raise ValueError()
        except (KeyError, ValueError):
            raise RejectedMessage('Missing or invalid snapshot timestamp') from None
        return batch_id, sent_at

    def expire_batches(self, db, tenant_id):
        db.execute(text('''
            UPDATE public.amoscentral_batches SET status='EXPIRED', exports='{}'::jsonb
            WHERE tenant_id=:tenant AND status='PENDING'
              AND created_at < CURRENT_TIMESTAMP - make_interval(hours => :hours)
        '''), {'tenant': tenant_id, 'hours': self.routing.pending_batch_hours})

    @staticmethod
    def record_receipt(db, message, digest, tenant_id, status, reason=None):
        db.execute(text('''
            INSERT INTO public.amoscentral_receipts (message_id, tenant_id, sender, digest, status, reason)
            VALUES (:id, :tenant, :sender, :digest, :status, :reason) ON CONFLICT (message_id) DO NOTHING
        '''), {'id': message.message_id, 'tenant': tenant_id, 'sender': message.sender,
               'digest': digest, 'status': status, 'reason': reason})

    def reject(self, message, digest, reason):
        with self.sessions() as db:
            with db.begin():
                self.record_receipt(db, message, digest, None, 'REJECTED', reason)
