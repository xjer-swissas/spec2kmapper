# AMOScentral Python client

Install independently with `pip install ./libs/amoscentral`, or build a wheel with
`python -m pip wheel --no-deps ./libs/amoscentral`. Python 3.11+ is required.
The library has no dependency on the mapper application, FastAPI, SQLAlchemy, or Java.

```python
from amoscentral import AmosCentralClient, ClientConfig, MessageBuilder, Poller

config = ClientConfig.from_file('/etc/spec2kmapper/amoscentral.toml')
with AmosCentralClient(config) as client:
    client.connect()
    outgoing = (MessageBuilder()
        .set_sender(config.address)
        .set_recipient('com.swiss-as.amos.customer.prod')
        .for_service('com.example.export.request')
        .with_correlation_id()
        .as_content_type('application/xml')
        .set_content('<request/>')
        .compress())
    client.send_message(outgoing)
```

For receiving, `client.receive_message()` decrypts a message but does not
acknowledge it. Call `client.acknowledge(message.message_id)` only after durable
processing. `Poller(client, handler)` performs that sequence, handles 204 empty
polls, and retries failures with bounded exponential backoff. Its handler receives
the encoded envelope so applications can check the sender before decrypting via
`client.codec.decode(envelope)`. `start()` and `stop()` manage its worker thread.
Configure senders to request `amc_requested_ack: technical` for durable delivery.

## Compatibility and evidence

The supplied amc-client 5.2.1, amc-message-builder 1.3.1, amc-rest-client 4.3.0,
and amc-polling 1.0.4 were decompiled with CFR 0.152 into temporary analysis files.
Their binaries and decompiled Java sources are not dependencies of this package.

| Behavior | Python implementation |
| --- | --- |
| Message model and fluent construction | `Message`, `MessageBuilder` |
| UTF-8 `name: value\n` headers, EOT `0x04`, binary body | `Message.to_bytes`, `Message.from_bytes` |
| OAuth password grant, bearer token reuse | `Transport` |
| Point-to-point ECDH P-256 | `MessageCodec` |
| Raw 32-byte shared secret; no HKDF or associated data | Same as the actual Java encryptor |
| ChaCha20-Poly1305; 12-byte Base64 `amc_nonce` | Same ciphertext layout as Java |
| Gzip, then encryption; decrypt, then decompress | `MessageCodec`, encoding `ChaCha20_7539, gzip` |
| Legacy X.509 certificate lookup/update | `api_version="legacy"`, certificate helpers |
| Current V3 SPKI public keys, ACTIVE/GRACE keys | `api_version="v3"` (default) |
| Historical local private keys | `historical_private_key_files` |
| Historical legacy peer certificates | Stored under external `state_dir/peers` |
| Explicitly supplied channel shared keys | `channel_key` / `channel_keys` codec arguments |
| Address, channel, subscription, namespace, catalogue and quota management | `client.call(operation_id, body=..., **parameters)`, 60 routes from the supplied V3 OpenAPI |
| Long polling, manual acknowledgment and shutdown | `Poller`, `acknowledge` |

The actual Java message builder and Bouncy Castle were executed using the harness
in `tests/interop/JavaMessageInterop.java`. Java-encrypted bytes were decrypted in
Python, and Python-encrypted bytes were decrypted by Java, both with and without
gzip, including Unicode and binary payloads. The committed fixture under
`tests/fixtures/amoscentral-java.json` contains **test-only** generated key material
and actual Java-generated ciphertext. The unit suite checks those fixtures.

The message-builder JAR used for these checks has SHA-256
`2a144679f2507d59775a47f5dac3c249dc239f599089014243b090bd416b520b`.

This is wire compatibility for the exercised messaging paths, not a claim that
every Java administrative API has identical signatures or behavior. The Java
client references generated REST dependencies that are absent from the bundle;
the supplied current OpenAPI defines the management bindings here. Legacy
administrative endpoints beyond certificate operations have not been verified.
Channel announcement/recovery messages require an application handler to install
the supplied shared keys; the mapper uses point-to-point messages only.

Intentional robustness differences: duplicate headers, invalid UTF-8, malformed
header separators, bad authentication tags and unknown encodings raise errors.
Header values containing a colon (timestamps, MIME parameters) parse correctly.
Decompression is bounded. Authentication and transport errors do not include
credentials or message bodies. This package does not reproduce Java parsing bugs
or return undecrypted bytes as successfully decoded content.

## Identity and configuration

Use the `[amoscentral]` section of `config/amoscentral.example.toml`. Keep the real
file and key directory outside the source tree and container image. The OAuth
client ID/secret come from your AMOScentral registration, in addition to the
address password. The library respects the normal Requests HTTPS proxy and CA
environment configuration; certificate verification remains enabled.

Create a P-256 identity explicitly with
`amoscentral init-key --config /etc/spec2kmapper/amoscentral.toml`.
It writes a PKCS#8 PEM private key with mode 0600 and refuses to overwrite it.
`amoscentral connect --config ...` checks the remote active key, uploads the local
public key only when there is no active key, and connects V3 runtime metadata.
A different registered key causes an error; it is never silently replaced.

Back up the state directory. `system-id` is stable across container replacements.
During deliberate rotation, retain old private keys in the external directory and
list them in `historical_private_key_files`. For legacy mode, register the matching
X.509 certificate using `update_certificate` before connecting. Existing Java
JKS/PKCS12 stores must be exported to PEM; they are not read directly.

For a management call, for example:

```python
addresses = client.call('findAllAddresses', search='customer', first=0, max=50)
public_keys = client.call('findPublicKeys', address='com.example.peer')
```

Operation names follow the supplied OpenAPI `operationId`. JSON responses return
Python values; binary/XML catalogue downloads return bytes. REST bridging is
excluded from this binding; normal message sending uses the encrypted client.
The route bindings are unit tested locally, but no live AMOScentral credentials
were provided, so production authentication, provisioning, and management calls
still require a connectivity test against the target environment.
