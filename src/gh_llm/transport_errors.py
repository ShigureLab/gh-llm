from __future__ import annotations

TRANSPORT_ERROR_PATTERNS = (
    'post "https://api.github.com/graphql": eof',
    "eof",
    "timeout",
    "i/o timeout",
    "context deadline exceeded",
    "client.timeout exceeded",
    "request canceled",
    "tls handshake timeout",
    "remote error: tls",
    "connection reset",
    "connection reset by peer",
    "connection refused",
    "connection closed",
    "connection aborted",
    "broken pipe",
    "temporary failure",
    "temporarily unavailable",
    "network is unreachable",
    "server misbehaving",
    "stream error",
    "goaway",
    "proxyconnect",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "500 internal server error",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
)


def looks_like_transport_error(message: str) -> bool:
    lowered = message.lower()
    return any(pattern in lowered for pattern in TRANSPORT_ERROR_PATTERNS)
