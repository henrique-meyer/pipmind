"""Import-only fork-server state; never construct credentials or connections."""

import gc

import pipmind.feeds.runtime  # noqa: F401

# Standalone SQLite feeds do not install the hosted SQL dependencies.
try:
    import azure.identity  # noqa: F401
    import OpenSSL  # noqa: F401
    import pytds.tls  # noqa: F401
    import requests  # noqa: F401
    import service_identity.cryptography  # noqa: F401
    from azure.core.pipeline.transport import RequestsTransport  # noqa: F401
except ImportError:
    pass

# Collection in children otherwise dirties inherited dependency object pages,
# defeating copy-on-write sharing as the workers warm up. New child objects
# remain collectible; no credential, client, thread or connection is inherited.
gc.freeze()
