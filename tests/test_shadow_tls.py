from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from OpenSSL.crypto import X509

from pipmind.shadow.tls import configure_tls, validate_host


def certificate(sans):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "cn-only.example")])
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
    )
    if sans:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(name) for name in sans]), critical=False
        )
    return X509.from_cryptography(builder.sign(key, hashes.SHA256()))


@pytest.mark.parametrize(
    "sans,host,accepted",
    [
        (["*.database.windows.net"], b"synthetic.database.windows.net", True),
        (["*.database.windows.net"], b"nested.synthetic.database.windows.net", False),
        (["*.database.windows.net"], b"database.windows.net", False),
        (["sql.example"], b"sql.example", True),
        (["sql.example"], b"cn-only.example", False),
        ([], b"cn-only.example", False),
        (["sql.example"], b"other.example", False),
    ],
)
def test_real_x509_san_validation_on_current_pyopenssl(sans, host, accepted):
    # Name verification only: chain trust remains the TDS TLS context's job.
    assert validate_host(certificate(sans), host) is accepted


def test_driver_callback_keeps_peer_chain_validation():
    import pytds.tls

    configure_tls()
    assert pytds.tls.validate_host is validate_host
    assert pytds.tls.verify_cb(None, None, 0, 0, 1)
    assert not pytds.tls.verify_cb(None, None, 20, 0, 0)
    assert not validate_host(None, b"sql.example")


def test_trust_context_reuse_is_bounded_and_invalidated_by_ca_replacement(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from unittest.mock import Mock

    import pipmind.shadow.tls as tls

    trust = tmp_path / "trust.pem"
    trust.write_text("Synthetic trust store")
    factory = Mock(side_effect=lambda _: object())
    monkeypatch.setattr(tls, "_factory", factory)
    tls._context.cache_clear()
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            contexts = list(pool.map(lambda _: tls.shared_context(trust), range(30)))
        assert all(c is contexts[0] for c in contexts) and factory.call_count == 1
        trust.write_text("A replaced synthetic trust store")
        assert tls.shared_context(trust) is not contexts[0] and factory.call_count == 2
        for i in range(6):
            path = tmp_path / str(i)
            path.write_text("Synthetic store")
            tls.shared_context(path)
        assert tls._context.cache_info().currsize == 4
        missing = tmp_path / "missing.pem"
        with pytest.raises(FileNotFoundError):
            tls.shared_context(missing)
        factory.side_effect = ValueError("Synthetic invalid trust store")
        calls = factory.call_count
        for _ in range(2):
            with pytest.raises(ValueError, match="invalid trust"):
                tls.shared_context(trust)
        assert factory.call_count == calls + 2
    finally:
        tls._context.cache_clear()
