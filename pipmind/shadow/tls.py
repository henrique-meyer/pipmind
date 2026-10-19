"""Hostname verification compatibility for pinned python-tds and pyOpenSSL 26.

python-tds 1.17.1 uses removed X509 extension APIs. Keep its verified TLS chain
and encrypted transport, replacing only that hostname callback with the public
service-identity/cryptography verifier. Do not fall back to CN or disable TLS.
"""

from functools import lru_cache
from pathlib import Path
from threading import RLock

_factory = None
_lock = RLock()


@lru_cache(maxsize=4)
def _context(path, modified, size, inode):
    return _factory(path)


def shared_context(cafile):
    # Trust stores are immutable after construction. Reuse their native CA
    # allocation, never SQL sessions, authentication or TLS connections. A CA
    # file replacement/change gets a new verified context; failures aren't cached.
    path = Path(cafile).resolve()
    stat = path.stat()
    with _lock:
        return _context(str(path), stat.st_mtime_ns, stat.st_size, stat.st_ino)


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

    global _factory
    with _lock:
        pytds.tls.validate_host = validate_host
        if pytds.tls.create_context is not shared_context:
            _factory = pytds.tls.create_context
            _context.cache_clear()
            pytds.tls.create_context = shared_context
