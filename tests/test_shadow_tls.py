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
