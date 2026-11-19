import base64
import gzip
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives.asymmetric import ec

from amoscentral import (AmosCentralClient, ClientConfig, Message, MessageBuilder,
                         MessageCodec, Poller, ProtocolError, EncryptionError, create_private)
from amoscentral.crypto import public_der
from amoscentral.transport import Transport


def message(body=b'\x00binary\x04payload\xff'):
    return MessageBuilder().set_sender('com.example.sender').set_recipient('com.example.receiver').for_service(
        'com.example.export').as_content_type('application/xml').set_content(body).build()


class EnvelopeTests(unittest.TestCase):
    def test_opaque_expiry_and_long_metadata_are_not_rejected(self):
        value = message()
        value.headers['amc_expiry'] = '60000'
        value.headers['amc_custom_metadata'] = 'x' * 1000
        value.headers['amc_sender'] = 'Com.Example.' + 'x' * 80
        self.assertEqual(Message.from_bytes(value.to_bytes()), value)

    def test_export_data_source_service_round_trip(self):
        value = message()
        value.headers['amc_service'] = 'com.swiss-as.amos.aim.export_data_source'
        value.headers['amc_service_version'] = '1.0'
        self.assertEqual(Message.from_bytes(value.to_bytes()), value)

    def test_binary_envelope_and_colon_in_utf8_header(self):
        value = message()
        value.headers['amc_send_time'] = '2026-11-13T12:34:56Z'
        self.assertEqual(Message.from_bytes(value.to_bytes()), value)

    def test_rejects_ambiguous_headers_and_truncated_envelope(self):
        valid = message().to_bytes()
        for body in [b'no separator', b'x' * 64001 + b'\x04', b'\xff\x04',
                     b'amc_sender: com.example.attacker\n' + valid,
                     valid.replace(b'amc_service:', b'Amc_Service:'),
                     valid.replace(b'amc_service:', b'other_header:')]:
            with self.subTest(body=body[:60]), self.assertRaises(ProtocolError):
                Message.from_bytes(body)

    def test_builder_flags_stay_off_wire(self):
        builder = MessageBuilder().set_sender('com.example.sender').set_recipient('com.example.receiver').for_aim_service(' EXPORT ')
        built = builder.compress().never_encrypt().with_correlation_id('batch-1').set_content('Ä').build()
        self.assertEqual(built.headers['amc_service'], 'com.swiss-as.amos.aim.export')
        self.assertFalse(builder.encrypted)
        self.assertNotIn('to_compress', built.headers)
        self.assertEqual(Message.from_bytes(built.to_bytes()).body, 'Ä'.encode())


class CryptoTests(unittest.TestCase):
    def setUp(self):
        self.sender = ec.generate_private_key(ec.SECP256R1())
        self.receiver = ec.generate_private_key(ec.SECP256R1())
        self.encoder = MessageCodec([self.sender], lambda _: [self.receiver.public_key()])
        self.decoder = MessageCodec([self.receiver], lambda _: [self.sender.public_key()])

    def test_compression_encryption_and_history(self):
        original = message(b'payload' * 100)
        encoded = self.encoder.encode(original, compress=True)
        self.assertEqual(encoded.headers['amc_content_encoding'], 'ChaCha20_7539, gzip')
        self.assertEqual(self.decoder.decode(encoded).body, original.body)
        self.decoder.private_keys.insert(0, ec.generate_private_key(ec.SECP256R1()))
        self.assertEqual(self.decoder.decode(encoded).body, original.body)

    def test_tampering_never_returns_plaintext(self):
        encoded = self.encoder.encode(message())
        encoded.body = encoded.body[:-1] + bytes([encoded.body[-1] ^ 1])
        with self.assertRaises(EncryptionError):
            self.decoder.decode(encoded)

    def test_bounded_decompression_and_unknown_encoding(self):
        encoded = message(gzip.compress(b'x' * 1024))
        encoded.headers['amc_content_encoding'] = 'gzip'
        self.decoder.max_body_bytes = 100
        with self.assertRaises(ProtocolError):
            self.decoder.decode(encoded)
        encoded.headers['amc_content_encoding'] = 'gzip, ChaCha20_7539'
        with self.assertRaises(ProtocolError):
            self.decoder.decode(encoded)

    def test_key_creation_is_explicit_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as folder:
            key = Path(folder) / 'private.pem'
            create_private(key)
            with self.assertRaises(FileExistsError):
                create_private(key)
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)


class RestTests(unittest.TestCase):
    def setUp(self):
        self.config = ClientConfig('com.example.receiver', 'secret-password', 'oauth-client', 'secret-client')

    @staticmethod
    def response(status, data=b'', json_data=None):
        response = Mock(status_code=status)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = [data]
        response.json.return_value = json_data
        return response

    def test_password_grant_token_cache_and_401_reauthentication(self):
        session = Mock()
        session.post.side_effect = [self.response(200, json_data={'access_token': 'one', 'expires_in': 60}),
                                    self.response(200, json_data={'access_token': 'two', 'expires_in': 60})]
        session.request.side_effect = [self.response(204), self.response(401), self.response(200, b'payload')]
        transport = Transport(self.config, session)
        self.assertIsNone(transport.request('GET', 'api/messages/com.example.receiver'))
        self.assertEqual(transport.request('GET', 'api/messages/com.example.receiver'), b'payload')
        self.assertEqual(session.post.call_count, 2)
        self.assertEqual(session.post.call_args.kwargs['data']['username'], self.config.address)
        self.assertEqual(session.request.call_args.kwargs['headers']['Authorization'], 'Bearer two')
        self.assertFalse(session.request.call_args.kwargs['allow_redirects'])

    def test_send_is_not_retried_on_ambiguous_network_failure(self):
        import requests
        session = Mock()
        session.post.return_value = self.response(200, json_data={'access_token': 'one', 'expires_in': 60})
        session.request.side_effect = requests.Timeout('secret-password')
        with self.assertRaisesRegex(Exception, 'transport failed') as caught:
            Transport(self.config, session).request('POST', 'api/messages/com.example.sender', data=b'payload')
        self.assertNotIn('secret-password', str(caught.exception))
        session.request.assert_called_once()

    def test_missing_recipient_key_and_mismatch_are_not_rotated(self):
        with tempfile.TemporaryDirectory() as folder:
            self.config.state_dir = folder
            key = create_private(Path(folder) / 'private-key.pem')
            transport = Mock()
            transport.request.return_value = json.dumps({'publicKey': base64.b64encode(public_der(
                ec.generate_private_key(ec.SECP256R1()))).decode()}).encode()
            client = AmosCentralClient(self.config, transport=transport)
            with self.assertRaises(EncryptionError):
                client.connect()
            self.assertEqual(transport.request.call_count, 1)
            transport.request.return_value = None
            client.connect()
            self.assertTrue((Path(folder) / 'system-id').exists())

    def test_v3_peer_keys_ignore_revoked_and_legacy_certificate_path(self):
        with tempfile.TemporaryDirectory() as folder:
            self.config.state_dir = folder
            create_private(Path(folder) / 'private-key.pem')
            transport = Mock()
            key = ec.generate_private_key(ec.SECP256R1()).public_key()
            encoded = base64.b64encode(public_der(key)).decode()
            transport.request.return_value = json.dumps([
                {'status': 'REVOKED', 'publicKey': 'invalid'},
                {'status': 'GRACE', 'publicKey': encoded},
                {'status': 'ACTIVE', 'publicKey': encoded}]).encode()
            client = AmosCentralClient(self.config, transport=transport)
            self.assertEqual(len(client.peer_keys('com.example.sender')), 2)
            client.get_address_certificate('com.example.sender')
            self.assertEqual(transport.request.call_args.args, ('GET', 'api/addresses/com.example.sender/cert'))

    def test_ack_happens_only_after_handler_success(self):
        client = Mock()
        value = message()
        client.receive_message.return_value = value
        order = []
        def handler(_): order.append('commit')
        poller = Poller(client, handler)
        client.acknowledge.side_effect = lambda *a, **k: (order.append('ack'), poller.stop_event.set())
        poller.run()
        self.assertEqual(order, ['commit', 'ack'])
        client.acknowledge.reset_mock()
        poller = Poller(client, Mock(side_effect=RuntimeError('database unavailable')),
                        error_handler=lambda _: poller.stop_event.set())
        poller.run()
        client.acknowledge.assert_not_called()

    def test_rejected_message_is_retained_and_consumption_events_are_emitted(self):
        client = Mock()
        client.receive_message.return_value = message()
        events = []
        poller = Poller(client, lambda _: False,
                        error_handler=lambda _: poller.stop_event.set(),
                        event_handler=lambda m, status, detail: events.append(status))
        poller.run()
        client.acknowledge.assert_not_called()
        self.assertEqual(events, ['UNACKNOWLEDGED'])

    def test_ack_success_is_logged_after_handler_commit(self):
        client = Mock()
        client.receive_message.return_value = message()
        events = []
        poller = Poller(client, lambda _: events.append('COMMIT'),
                        event_handler=lambda m, status, detail: events.append(status))
        client.acknowledge.side_effect = lambda *a, **k: poller.stop_event.set()
        poller.run()
        self.assertEqual(events, ['COMMIT', 'ACKNOWLEDGED'])

    def test_management_bindings_encode_parameters_and_return_binary_downloads(self):
        with tempfile.TemporaryDirectory() as folder:
            self.config.state_dir = folder
            create_private(Path(folder) / 'private-key.pem')
            transport = Mock()
            client = AmosCentralClient(self.config, transport=transport)
            transport.request.return_value = b'[{"address":"com.example.sender"}]'
            self.assertEqual(client.call('findAllAddresses', search='sender', first=0, max=10)[0]['address'], 'com.example.sender')
            self.assertEqual(transport.request.call_args.kwargs['params'], {'search': 'sender', 'first': 0, 'max': 10})
            transport.request.return_value = b'<configuration/>'
            self.assertEqual(client.call('downloadConfigurationByProfileId', profileId='profile-1'), b'<configuration/>')
            with self.assertRaises(ProtocolError):
                client.call('findAllAddresses', unexpected='value')
            with self.assertRaises(ProtocolError):
                client.call('findPublicKeys')


class JavaFixtureTests(unittest.TestCase):
    def test_java_generated_ciphertext_decrypts_in_python(self):
        path = Path(__file__).parent / 'fixtures' / 'amoscentral-java.json'
        self.assertTrue(path.exists(), 'Generate the Java interoperability fixture before release')
        from cryptography.hazmat.primitives import serialization
        from amoscentral.crypto import public_key
        fixture = json.loads(path.read_text())
        private = serialization.load_der_private_key(base64.b64decode(fixture['receiver_private_pkcs8']), None)
        sender = public_key(base64.b64decode(fixture['sender_certificate']))
        codec = MessageCodec([private], lambda _: [sender])
        for name in ['encrypted', 'encrypted_gzip']:
            encoded = Message.from_bytes(base64.b64decode(fixture[name]))
            self.assertEqual(codec.decode(encoded).body, base64.b64decode(fixture['plaintext']))
