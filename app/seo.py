"""Server-rendered public metadata; deployment origins never come from request headers."""

import base64
import hashlib
import json
import re
from html import escape

TITLE = "PipMind — An AI Forex Trader with a Persistent Mind"
DESCRIPTION = (
    "A source-available noncommercial AI forex experiment with a persistent mind. Explore portable "
    "robot saves, owner guidance and Auto Learn. In development; no trading yet."
)


def public_site_url(value: str) -> str:
    """Accept only an HTTPS DNS origin, without credentials, port, path or parameters."""
    if not value:
        return ""
    label = r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    match = re.fullmatch(rf"https://({label}(?:\.{label})*\.[a-zA-Z]{{2,63}})/?", value)
    if not match or len(match[1]) > 253:
        raise ValueError("PUBLIC_SITE_URL must be an HTTPS DNS origin without a path or parameters")
    return f"https://{match[1].lower()}"


def structured_data(origin: str) -> str:
    data = {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": "PipMind",
        "url": origin + "/",
        "description": DESCRIPTION,
        "inLanguage": "en",
        "creator": {
            "@type": "Person",
            "name": "Henrique Meyer",
            "url": "https://www.hmeyer.com",
        },
        "sameAs": ["https://github.com/henrique-meyer/pipmind"],
    }
    return json.dumps(data, separators=(",", ":")).replace("<", "\\u003c")


def schema_csp_source(origin: str) -> str:
    digest = hashlib.sha256(structured_data(origin).encode()).digest()
    return "'sha256-" + base64.b64encode(digest).decode() + "'"


def metadata(origin: str) -> str:
    tags = [
        f"<title>{escape(TITLE)}</title>",
        f'<meta name="description" content="{escape(DESCRIPTION, quote=True)}">',
        f'<meta name="robots" content="{"index,follow" if origin else "noindex,nofollow"}">',
    ]
    if origin:
        image = origin + "/assets/logo.png"
        values = {
            "og:type": "website",
            "og:site_name": "PipMind",
            "og:locale": "en_US",
            "og:title": TITLE,
            "og:description": DESCRIPTION,
            "og:url": origin + "/",
            "og:image": image,
            "og:image:type": "image/png",
            "og:image:width": "1024",
            "og:image:height": "1024",
            "og:image:alt": "PipMind",
        }
        tags.append(f'<link rel="canonical" href="{origin}/">')
        tags.extend(
            f'<meta property="{key}" content="{escape(value, quote=True)}">'
            for key, value in values.items()
        )
        for name, value in {
            "twitter:card": "summary",
            "twitter:title": TITLE,
            "twitter:description": DESCRIPTION,
            "twitter:image": image,
            "twitter:image:alt": values["og:image:alt"],
        }.items():
            tags.append(f'<meta name="{name}" content="{escape(value, quote=True)}">')
        tags.append(f'<script type="application/ld+json">{structured_data(origin)}</script>')
    return "\n  ".join(tags)
