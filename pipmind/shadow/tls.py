"""Hostname verification compatibility for pinned python-tds and pyOpenSSL 26.

python-tds 1.17.1 uses removed X509 extension APIs. Keep its verified TLS chain
and encrypted transport, replacing only that hostname callback with the public
service-identity/cryptography verifier. Do not fall back to CN or disable TLS.
"""


def validate_host(cert, name):
    from service_identity import CertificateError, VerificationError
    from service_identity.cryptography import verify_certificate_hostname

    try:
        verify_certificate_hostname(cert.to_cryptography(), name.decode("ascii"))
        return True
    except (CertificateError, VerificationError, AttributeError, UnicodeError, ValueError):
        return False


def configure_tls():
    import pytds.tls

    pytds.tls.validate_host = validate_host
