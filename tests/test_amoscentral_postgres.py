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
            conn.execute(text('TRUNCATE public.amoscentral_events RESTART IDENTITY'))
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
        self.routing = RoutingConfig(data_source_ids={k: k for k in EXPORTS})
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
                   .for_service('com.swiss-as.amos.aim.export_data_source').in_version('1.0').with_correlation_id(batch)
                   .add_property('amc_2k5mapper_product_header', {'parts':'part_definition','configurations':'configuration','documents':'documents'}[kind])
                   .add_property('amc_send_time', (when or self.when).isoformat()).set_content(body).build())
        return MessageCodec([self.senders[sender]], lambda _: [self.receiver.public_key()]).encode(message, compress=True)

    def value(self, statement, params=None):
        with self.engine.connect() as conn:
            return conn.execute(text(statement), params or {}).scalar_one()

    def test_each_export_updates_immediately_preserving_other_tables(self):
        self.inbox(self.message('documents'))
        self.assertEqual(self.value('SELECT part_number FROM tenant_amc_a.amos_part_master'), 'OLD')
        self.assertEqual(self.value('SELECT count(*) FROM tenant_amc_a.doc_part_of_pn_conf'), 1)
        self.inbox(self.message('parts'))
        self.assertEqual(self.value('SELECT count(*) FROM tenant_amc_a.amos_part_master'), 2)
        self.assertEqual(self.value('SELECT count(*) FROM tenant_amc_a.doc_part_of_pn_conf'), 1)
        self.inbox(self.message('configurations'))
        self.assertEqual(self.value("SELECT count(*) FROM tenant_amc_a.doc_part_of_pn_conf WHERE parent_pn='ROOT'"), 1)
        self.assertEqual(self.value('SELECT part_number FROM tenant_amc_b.amos_part_master'), 'OLD')
        self.assertEqual(self.value("SELECT count(*) FROM public.amoscentral_receipts WHERE status='IMPORTED'"), 3)
        self.assertEqual(self.schedule.call_count, 3)

    def test_plain_export_without_correlation_timestamp_or_ack_request(self):
        envelope = self.client.codec.decode(self.message('parts'))
        for key in ['amc_correlation_id', 'amc_send_time', 'amc_requested_ack']:
            envelope.headers.pop(key, None)
        self.inbox(envelope)
        self.assertEqual(self.value('SELECT count(*) FROM tenant_amc_a.amos_part_master'), 2)
        self.assertEqual(self.value('SELECT status FROM public.amoscentral_receipts'), 'IMPORTED')

    def test_redelivery_is_idempotent_and_repeated_export_replaces_previous(self):
        first = self.message('parts')
        self.inbox(first)
        self.inbox(first)
        self.assertEqual(self.value('SELECT count(*) FROM public.amoscentral_receipts'), 1)
        self.schedule.assert_called_once()
        self.inbox(self.message('parts', rows=[['NEW', 'Replacement']]))
        self.assertEqual(self.value('SELECT part_number FROM tenant_amc_a.amos_part_master'), 'NEW')

    def test_failed_write_rolls_back_and_redelivery_imports(self):
        envelope = self.message('parts')
        with patch.object(AmosDataSyncer, 'replace_cache', side_effect=RuntimeError('simulated failure')):
            with self.assertRaises(RuntimeError):
                self.inbox(envelope)
        self.assertEqual(self.value('SELECT part_number FROM tenant_amc_a.amos_part_master'), 'OLD')
        self.assertEqual(self.value('SELECT count(*) FROM public.amoscentral_receipts'), 0)
        self.inbox(envelope)
        self.assertEqual(self.value('SELECT status FROM public.amoscentral_receipts'), 'IMPORTED')

    def test_invalid_rows_have_detailed_reason_and_are_not_acknowledged(self):
        invalid = self.message('parts', rows=[['', 'Missing part number']])
        self.assertFalse(self.inbox(invalid))
        self.assertIn('row 2 requires part_number', self.value('SELECT reason FROM public.amoscentral_receipts'))
        self.assertEqual(self.value("SELECT count(*) FROM public.amoscentral_events WHERE status='REJECTED'"), 1)
        self.assertEqual(self.value('SELECT part_number FROM tenant_amc_a.amos_part_master'), 'OLD')

    def test_empty_parts_and_repeated_identifiers_are_accepted(self):
        self.inbox(self.message('parts', rows=[]))
        self.assertEqual(self.value('SELECT count(*) FROM tenant_amc_a.amos_part_master'), 0)
        self.inbox(self.message('parts', rows=[['001', 'First'], ['001', 'Last']]))
        self.assertEqual(self.value('SELECT description FROM tenant_amc_a.amos_part_master'), 'Last')

    def test_event_api_is_tenant_scoped_and_includes_diagnostics(self):
        from app.routers.amos_routes import get_sync_status
        self.inbox(self.message('parts', sender='a', rows=[['', 'Invalid']]))
        self.inbox(self.message('documents', sender='b'))
        with self.sessions() as db, patch('app.matching.catalog.status', return_value={}):
            result = get_sync_status({'tenant_id': 1, 'schema_name': 'tenant_amc_a'}, db)
        events = result['central']['events']
        self.assertEqual([e['status'] for e in events], ['REJECTED', 'RECEIVED'])
        self.assertIn('requires part_number', events[0]['detail'])
        self.assertEqual(events[0]['export_header_value'], 'part_definition')

    def test_unknown_sender_does_not_touch_cache_and_is_not_acknowledged(self):
        unknown = self.message('parts')
        unknown.headers['amc_sender'] = 'com.example.unknown'
        self.assertFalse(self.inbox(unknown))
        self.assertIn('no tenant', self.value('SELECT reason FROM public.amoscentral_receipts'))
        self.assertEqual(self.value('SELECT part_number FROM tenant_amc_a.amos_part_master'), 'OLD')
