"""
Alexa Smart Home skill Lambda — proxies directives to Home Assistant.

Optional mutual TLS (mTLS): when MTLS_CERT_PARAM is set, a client certificate
and key are loaded from an SSM SecureString parameter (JSON
{"client_crt": "...", "client_key": "..."}), written to /tmp once per container,
and presented to your reverse proxy (e.g. nginx).

Original pattern: https://github.com/mike-grant/haaska / alexa.smart_home docs.
mTLS adaptation for nginx (no Envoy required).
"""

import json
import logging
import os
import ssl
import tempfile
import urllib.error
import urllib.request
import uuid

import boto3

_logger = logging.getLogger("HomeAssistant-SmartHome")
_logger.setLevel(logging.DEBUG if os.environ.get("DEBUG") == "True" else logging.INFO)

_ssm = boto3.client("ssm")
_mtls_paths = {}
_ssl_context = None


def _error(err_type, message, directive=None):
    """Build a well-formed Alexa Smart Home ErrorResponse.

    Alexa requires event.header (namespace/name/payloadVersion/messageId) or it
    treats the response as malformed. correlationToken and endpointId are
    echoed from the directive when present so Alexa can match the error to
    its request.
    """
    header = {
        "namespace": "Alexa",
        "name": "ErrorResponse",
        "payloadVersion": "3",
        "messageId": str(uuid.uuid4()),
    }
    event = {"header": header, "payload": {"type": err_type, "message": message}}
    if directive:
        correlation_token = directive.get("header", {}).get("correlationToken")
        if correlation_token:
            header["correlationToken"] = correlation_token
        endpoint_id = directive.get("endpoint", {}).get("endpointId")
        if endpoint_id:
            event["endpoint"] = {"endpointId": endpoint_id}
    return {"event": event}


def _redact(obj):
    """Deep-copy obj with bearer token values masked, safe for logging."""
    if isinstance(obj, dict):
        return {k: "***" if k == "token" else _redact(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def _safe_text(data, limit=500):
    """Decode bytes for logging without raising; truncate to avoid log floods."""
    if isinstance(data, str):
        text = data
    else:
        text = data.decode("utf-8", errors="replace")
    if len(text) > limit:
        return text[:limit] + "...(truncated)"
    return text


def _mtls_cert_paths():
    """Return (cert_file, key_file) or (None, None) if mTLS is not configured."""
    param_name = os.environ.get("MTLS_CERT_PARAM")
    if not param_name:
        return None, None
    if _mtls_paths:
        return _mtls_paths["cert"], _mtls_paths["key"]

    payload = json.loads(
        _ssm.get_parameter(Name=param_name, WithDecryption=True)["Parameter"]["Value"]
    )
    if "client_crt" not in payload or "client_key" not in payload:
        raise ValueError(
            "Parameter {} must be JSON with client_crt and client_key keys".format(param_name)
        )
    cert = tempfile.NamedTemporaryFile(mode="w", suffix=".crt", delete=False, dir="/tmp")
    key = tempfile.NamedTemporaryFile(mode="w", suffix=".key", delete=False, dir="/tmp")
    cert.write(payload["client_crt"].replace("\\n", "\n"))
    key.write(payload["client_key"].replace("\\n", "\n"))
    cert.close()
    key.close()
    os.chmod(key.name, 0o600)
    _mtls_paths.update({"cert": cert.name, "key": key.name})
    _logger.info("Loaded mTLS client cert from SSM parameter: %s", param_name)
    return cert.name, key.name


def _ssl_ctx():
    """SSL context cached per container (includes client cert when mTLS is on)."""
    global _ssl_context
    if _ssl_context is None:
        try:
            ctx = ssl.create_default_context()
            cert_file, key_file = _mtls_cert_paths()
            if cert_file and key_file:
                ctx.load_cert_chain(certfile=cert_file, keyfile=key_file)
            _ssl_context = ctx
        except Exception as exc:
            # SSM access denied, bad JSON, missing keys, corrupt PEM, etc.
            # Re-raise as OSError so lambda_handler returns INTERNAL_ERROR.
            raise OSError("Failed to load mTLS client certificate: {}".format(exc)) from exc
    return _ssl_context


def _post_ha(url, token, body):
    """POST JSON to Home Assistant. Returns (status, body_bytes). Raises on network errors."""
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": "Bearer {}".format(token),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=10) as resp:
            return resp.getcode(), resp.read()
    except urllib.error.HTTPError as exc:
        # HTTPError is also a file-like response; read the body for logging.
        return exc.code, exc.read()


def lambda_handler(event, context):
    """Handle an incoming Alexa Smart Home directive."""

    _logger.debug("Event: %s", json.dumps(_redact(event)))

    base_url = os.environ.get("BASE_URL")
    if not base_url:
        raise ValueError("Set the BASE_URL environment variable (no trailing slash)")
    base_url = base_url.strip("/")

    directive = event.get("directive")
    if directive is None:
        return _error("INVALID_DIRECTIVE", "Malformatted request - missing directive.")
    if directive.get("header", {}).get("payloadVersion") != "3":
        return _error("INVALID_DIRECTIVE", "Only payloadVersion 3 is supported.", directive)

    scope = directive.get("endpoint", {}).get("scope")
    if scope is None:
        # Linking directive carries the token in payload.grantee
        scope = directive.get("payload", {}).get("grantee")
    if scope is None:
        # Discovery directive carries it in payload.scope
        scope = directive.get("payload", {}).get("scope")
    if scope is None:
        return _error("INVALID_DIRECTIVE", "Malformatted request - missing endpoint.scope.", directive)
    if scope.get("type") != "BearerToken":
        return _error("INVALID_DIRECTIVE", "Only BearerToken scope is supported.", directive)

    token = scope.get("token")
    if token is None and os.environ.get("DEBUG") == "True":
        # Only for local testing; do not rely on this in production.
        token = os.environ.get("LONG_LIVED_ACCESS_TOKEN")
    if token is None:
        _logger.error("Directive contains no bearer token; is account linking set up?")
        return _error(
            "INVALID_AUTHORIZATION_CREDENTIAL", "No bearer token in directive.", directive
        )

    try:
        status, body = _post_ha(
            "{}/api/alexa/smart_home".format(base_url),
            token,
            json.dumps(event).encode("utf-8"),
        )
    except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as exc:
        _logger.error("Upstream request to Home Assistant failed: %s", exc)
        return _error(
            "INTERNAL_ERROR",
            "Could not reach Home Assistant.",
            directive,
        )

    if status >= 400:
        # Log a safe, truncated body; never return it to Alexa (can be HTML /
        # leak internals).
        _logger.error("HA error %s: %s", status, _safe_text(body))
        if status in (401, 403):
            return _error(
                "INVALID_AUTHORIZATION_CREDENTIAL",
                "Home Assistant rejected the access token.",
                directive,
            )
        return _error(
            "INTERNAL_ERROR",
            "Home Assistant returned HTTP {}.".format(status),
            directive,
        )

    _logger.debug("Response: %s", _safe_text(body, limit=10000))
    try:
        # Parse the raw body; _safe_text truncates and must never touch the
        # payload itself (large Discover.Response bodies broke here once).
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        _logger.error("Home Assistant returned non-JSON body: %s", _safe_text(body))
        return _error(
            "INTERNAL_ERROR",
            "Home Assistant returned an invalid response.",
            directive,
        )
