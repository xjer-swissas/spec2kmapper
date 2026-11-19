from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, MagicMock

from app.amos.central_config import RoutingConfig
from app.amos.central_inbox import BrokerInbox, RejectedMessage


class ExportRoutingTests(unittest.TestCase):
    def test_each_event_records_exact_header_before_validation_and_at_ack(self):
        inbox = BrokerInbox(None, RoutingConfig(), None)
        db = Mock()
        for value in ['part_definition', 'configuration', 'documents', 'unexpected']:
            message = SimpleNamespace(message_id='message', sender='com.example.sender',
                                      headers={'amc_2k5mapper_product_header': value})
            for stage in ['RECEIVED', 'REJECTED', 'ACKNOWLEDGED']:
                inbox.write_event(db, message, stage)
                self.assertEqual(db.execute.call_args.args[1]['header_value'], value)

    def test_rejection_records_specific_reason(self):
        sessions = MagicMock()
        inbox = BrokerInbox(sessions, RoutingConfig(), None)
        envelope = Mock(headers={})
        envelope.to_bytes.return_value = b'envelope'
        inbox.event = Mock()
        inbox.record_receipt = Mock()
        self.assertFalse(inbox(envelope))
        self.assertIn('Unexpected amc_service', inbox.record_receipt.call_args.args[5])

    def export_headers(self):
        return {
            'amc_service': 'com.swiss-as.amos.aim.export_data_source',
            'amc_service_version': '1.0',
            'amc_content_encoding': 'ChaCha20_7539',
            'amc_correlation_id': 'snapshot-1',
            'amc_send_time': datetime.now(timezone.utc).isoformat(),
        }

    def test_ack_request_does_not_control_receiving(self):
        inbox = BrokerInbox(None, RoutingConfig(), None)
        for value in [None, 'none', 'technical', 'business']:
            headers = self.export_headers()
            if value is not None:
                headers['amc_requested_ack'] = value
            with self.subTest(value=value):
                inbox.route(SimpleNamespace(headers=headers))

    def test_no_encryption_correlation_timestamp_or_product_required(self):
        inbox = BrokerInbox(None, RoutingConfig(require_encryption=True, product='legacy'), None)
        headers = {'amc_service': 'com.swiss-as.amos.aim.export_data_source', 'amc_service_version': '1.0'}
        inbox.route(SimpleNamespace(headers=headers))

    def test_fixed_service_accepts_export_and_rejects_other_services(self):
        inbox = BrokerInbox(None, RoutingConfig(), None)
        headers = self.export_headers()
        envelope = SimpleNamespace(headers=headers)
        inbox.route(envelope)
        for service in ['com.example.export', '', 'export_data_source']:
            headers['amc_service'] = service
            with self.subTest(service=service), self.assertRaises(RejectedMessage):
                inbox.route(envelope)
