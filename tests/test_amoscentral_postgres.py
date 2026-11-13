"""Integration tests use a disposable database, never the deployed app database."""
from datetime import datetime, timedelta, timezone
import os
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from amoscentral import MessageBuilder, MessageCodec
from app.amos.central_config import RoutingConfig
from app.amos.central_inbox import BrokerInbox
from app.amos.extractor import AmosDataSyncer
from app.amos.queries import EXPORTS
from app.database import init_master_tables, create_tenant_schema
from test_amoscentral_xml import export


@unittest.skipUnless(os.getenv('AMOSCENTRAL_TEST_DATABASE_URL'), 'Set an isolated AMOSCENTRAL_TEST_DATABASE_URL')
class BrokerPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(os.environ['AMOSCENTRAL_TEST_DATABASE_URL'])
        if not cls.engine.url.database.startswith('spec2kmapper_amc_test'):
            raise RuntimeError('Refusing to modify a database without the test prefix')
        cls.sessions = sessionmaker(bind=cls.engine)
        with patch('app.database.engine', cls.engine):
            init_master_tables()
            init_master_tables()  # Startup migrations must be idempotent.
            create_tenant_schema('tenant_amc_a')
            create_tenant_schema('tenant_amc_b')

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        with self.engine.begin() as conn:
            conn.execute(text('TRUNCATE public.tenants RESTART IDENTITY CASCADE'))
            for suffix in ['a', 'b']:
                conn.execute(text('''INSERT INTO public.tenants
                    (company_name, schema_name, amos_source, amoscentral_address)
                    VALUES (:company, :schema, 'amoscentral', :sender)'''),
                    {'company': suffix, 'schema': 'tenant_amc_' + suffix, 'sender': 'com.example.' + suffix})
                for table in ['amos_part_master', 'pn_part_of_pn_conf', 'fp_part_of_pn_conf', 'doc_part_of_pn_conf']:
                    conn.execute(text(f'TRUNCATE "tenant_amc_{suffix}".{table}'))
                conn.execute(text(f"INSERT INTO tenant_amc_{suffix}.amos_part_master VALUES ('OLD', 'Previous cache')"))
        self.receiver = ec.generate_private_key(ec.SECP256R1())
        self.senders = {name: ec.generate_private_key(ec.SECP256R1()) for name in ['a', 'b']}
        self.routing = RoutingConfig('com.example.export', {k: k for k in EXPORTS})
        class Client: pass
        self.client = Client()
        self.client.codec = MessageCodec([self.receiver], lambda sender: [self.senders[sender.rsplit('.', 1)[-1]].public_key()])
        self.inbox = BrokerInbox(self.sessions, self.routing, self.client)
        self.schedule = patch('app.matching.catalog.schedule').start()
        self.addCleanup(patch.stopall)
        self.when = datetime.now(timezone.utc) - timedelta(minutes=1)

    def message(self, kind, batch='snapshot-1', when=None, sender='a', rows=None):
        defaults = {'parts': [['ROOT', 'Pump'], ['CHILD', 'Child']],
                    'configurations': [['ROOT', 'FP1', 'Position', 'CHILD']], 'documents': [['CHILD', 'DOC1']]}
        body = export(kind, EXPORTS[kind]['columns'], defaults[kind] if rows is None else rows)
        message = (MessageBuilder().set_sender('com.example.' + sender).set_recipient('com.example.mapper')
                   .for_service('com.example.export').with_correlation_id(batch)
                   .add_property('amc_requested_ack', 'technical')
                   .add_property('amc_send_time', (when or self.when).isoformat()).set_content(body).build())
        return MessageCodec([self.senders[sender]], lambda _: [self.receiver.public_key()]).encode(message, compress=True)

    def value(self, statement, params=None):
        with self.engine.connect() as conn:
            return conn.execute(text(statement), params or {}).scalar_one()

    def complete(self, batch='snapshot-1', when=None, sender='a'):
        for kind in ['documents', 'parts', 'configurations']:
            self.inbox(self.message(kind, batch, when, sender))

    def test_partial_snapshot_survives_restart_and_updates_only_its_tenant(self):
        first = self.message('parts')
        self.inbox(first)
        self.assertEqual(self.value('SELECT part_number FROM tenant_amc_a.amos_part_master'), 'OLD')
        self.assertEqual(self.value('SELECT count(*) FROM public.amoscentral_receipts'), 1)
        self.inbox = BrokerInbox(self.sessions, self.routing, self.client)
        self.inbox(self.message('documents'))
        self.inbox(self.message('configurations'))
        self.assertEqual(self.value('SELECT count(*) FROM tenant_amc_a.amos_part_master'), 2)
        self.assertEqual(self.value('SELECT part_number FROM tenant_amc_b.amos_part_master'), 'OLD')
        self.assertEqual(self.value("SELECT count(*) FROM tenant_amc_a.doc_part_of_pn_conf WHERE parent_pn='ROOT'"), 1)
        self.assertEqual(self.value('SELECT status FROM public.amoscentral_batches'), 'COMPLETE')
        self.assertEqual(self.value('SELECT exports FROM public.amoscentral_batches'), {})
        self.schedule.assert_called_once_with('tenant_amc_a')
        self.inbox(first)  # ACK loss / redelivery after commit.
        self.assertEqual(self.value('SELECT count(*) FROM public.amoscentral_receipts'), 3)
        self.schedule.assert_called_once()

    def test_batches_do_not_mix_and_older_completed_snapshot_cannot_replace_newer(self):
        self.inbox(self.message('parts', 'old'))
        self.inbox(self.message('documents', 'new', self.when + timedelta(seconds=10)))
        self.assertEqual(self.value('SELECT part_number FROM tenant_amc_a.amos_part_master'), 'OLD')
        self.complete('new', self.when + timedelta(seconds=10))
        for kind in ['documents', 'configurations']:
            self.inbox(self.message(kind, 'old'))
        self.assertEqual(self.value("SELECT status FROM public.amoscentral_batches WHERE batch_id='old'"), 'SUPERSEDED')
        self.assertEqual(self.value('SELECT count(*) FROM tenant_amc_a.amos_part_master'), 2)
        self.schedule.assert_called_once()

    def test_failed_final_write_rolls_back_and_redelivery_completes(self):
        self.inbox(self.message('parts'))
        self.inbox(self.message('documents'))
        last = self.message('configurations')
        with patch.object(AmosDataSyncer, 'replace_cache', side_effect=RuntimeError('simulated failure')):
            with self.assertRaises(RuntimeError):
                self.inbox(last)
        self.assertEqual(self.value('SELECT part_number FROM tenant_amc_a.amos_part_master'), 'OLD')
        self.assertEqual(self.value('SELECT count(*) FROM public.amoscentral_receipts'), 2)
        self.assertEqual(len(self.value('SELECT exports FROM public.amoscentral_batches')), 2)
        self.inbox(last)
        self.assertEqual(self.value('SELECT status FROM public.amoscentral_batches'), 'COMPLETE')

    def test_unknown_sender_csv_mode_and_malformed_xml_are_durably_rejected(self):
        unknown = self.message('parts')
        unknown.headers['amc_sender'] = 'com.example.unknown'
        with patch.object(self.client.codec, 'decode', wraps=self.client.codec.decode) as decode:
            self.inbox(unknown)
            decode.assert_not_called()
        invalid = self.message('parts', rows=[['', 'Missing part number']])
        self.inbox(invalid)
        self.inbox(self.message('parts', rows=[['001', 'Pump'], ['001', 'Different description']]))
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE public.tenants SET amos_source='csv' WHERE schema_name='tenant_amc_a'"))
        self.inbox(self.message('documents'))
        self.assertEqual(self.value("SELECT count(*) FROM public.amoscentral_receipts WHERE status='REJECTED'"), 4)
        self.assertEqual(self.value('SELECT count(*) FROM public.amoscentral_batches'), 0)
        self.assertEqual(self.value('SELECT part_number FROM tenant_amc_a.amos_part_master'), 'OLD')

    def test_conflicting_component_never_changes_the_staged_snapshot(self):
        self.inbox(self.message('parts'))
        self.inbox(self.message('parts', rows=[['DIFFERENT', 'Conflicting snapshot']]))
        self.assertEqual(self.value("SELECT count(*) FROM public.amoscentral_receipts WHERE status='REJECTED'"), 1)
        self.inbox(self.message('configurations'))
        self.inbox(self.message('documents'))
        self.assertEqual(self.value("SELECT count(*) FROM tenant_amc_a.amos_part_master WHERE part_number='ROOT'"), 1)

    def test_expired_batch_and_pending_limits(self):
        self.inbox(self.message('parts', 'expired'))
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE public.amoscentral_batches SET created_at=CURRENT_TIMESTAMP-INTERVAL '8 days'"))
        from app.amos.scheduler import expire_broker_snapshots
        with patch('app.amos.central.receiver.routing', self.routing), patch('app.amos.scheduler.SessionLocal', self.sessions):
            expire_broker_snapshots()
        self.assertEqual(self.value('SELECT exports FROM public.amoscentral_batches'), {})
        self.routing.pending_batch_limit = 1
        self.inbox(self.message('parts', 'current'))
        self.assertEqual(self.value("SELECT status FROM public.amoscentral_batches WHERE batch_id='expired'"), 'EXPIRED')
        self.inbox(self.message('parts', 'too-many'))
        self.assertEqual(self.value('SELECT count(*) FROM public.amoscentral_batches'), 2)
        self.assertEqual(self.value("SELECT count(*) FROM public.amoscentral_receipts WHERE status='REJECTED'"), 1)

    def test_sender_addresses_are_unique(self):
        from sqlalchemy.exc import IntegrityError
        with self.assertRaises(IntegrityError), self.engine.begin() as conn:
            conn.execute(text("UPDATE public.tenants SET amoscentral_address='com.example.a' WHERE schema_name='tenant_amc_b'"))

    def test_status_exposes_only_this_tenants_pending_components(self):
        from app.routers.amos_routes import get_sync_status
        self.inbox(self.message('parts', sender='a'))
        self.inbox(self.message('documents', sender='b'))
        with self.sessions() as db, patch('app.matching.catalog.status', return_value={}):
            result = get_sync_status({'tenant_id': 1, 'schema_name': 'tenant_amc_a'}, db)
        self.assertEqual(result['central']['sender_address'], 'com.example.a')
        self.assertEqual(result['central']['snapshots'], [{'batch_id': 'snapshot-1', 'status': 'PENDING', 'received': ['parts']}])

    def test_switching_source_cancels_incomplete_broker_snapshot(self):
        from app.routers.amos_routes import update_amos_config, AmosConnectionConfig
        self.inbox(self.message('parts'))
        with self.sessions() as db:
            update_amos_config(AmosConnectionConfig(source='csv', amoscentral_address='com.example.a'),
                               {'tenant_id': 1, 'schema_name': 'tenant_amc_a', 'is_admin': True}, db)
        self.assertEqual(self.value('SELECT status FROM public.amoscentral_batches'), 'EXPIRED')
        self.inbox(self.message('documents'))
        self.assertEqual(self.value('SELECT part_number FROM tenant_amc_a.amos_part_master'), 'OLD')
