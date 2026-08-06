"""
Alexa Smart Home skill Lambda — proxies directives to Home Assistant.

Optional mutual TLS (mTLS): when MTLS_CERT_SECRET is set, a client certificate
and key are loaded from AWS Secrets Manager (JSON {"client_crt": "...", "client_key": "..."}),
written to /tmp once per container, and presented to your reverse proxy (e.g. nginx).

Original pattern: https://github.com/mike-grant/haaska / alexa.smart_home docs.
mTLS adaptation for nginx (no Envoy required).
"""

import json
import logging
import os
import ssl
import tempfile
import urllib3

import boto3

_logger = logging.getLogger("HomeAssistant-SmartHome")
_logger.setLevel(logging.DEBUG if os.environ.get("DEBUG") == "True" else logging.INFO)

_secrets = boto3.client("secretsmanager")
_mtls_paths = {}


def _mtls_cert_paths():
    """Return (cert_file, key_file) or (None, None) if mTLS is not configured."""
    secret_id = os.environ.get("MTLS_CERT_SECRET")
    if not secret_id:
        return None, None
    if _mtls_paths:
        return _mtls_paths["cert"], _mtls_paths["key"]

    payload = json.loads(_secrets.get_secret_value(SecretId=secret_id)["SecretString"])
    cert = tempfile.NamedTemporaryFile(mode="w", suffix=".crt", delete=False, dir="/tmp")
    key = tempfile.NamedTemporaryFile(mode="w", suffix=".key", delete=False, dir="/tmp")
    cert.write(payload["client_crt"].replace("\\n", "\n"))
    key.write(payload["client_key"].replace("\\n", "\n"))
    cert.close()
    key.close()
    os.chmod(key.name, 0o600)
    _mtls_paths.update({"cert": cert.name, "key": key.name})
    _logger.info("Loaded mTLS client cert from Secrets Manager: %s", secret_id)
    return cert.name, key.name


def _http_manager():
    cert_file, key_file = _mtls_cert_paths()
    kwargs = {
        "ca_certs": ssl.get_default_verify_paths().cafile,
        "cert_reqs": "CERT_REQUIRED",
        "timeout": urllib3.Timeout(connect=2.0, read=10.0),
    }
    if cert_file and key_file:
        kwargs["cert_file"] = cert_file
        kwargs["key_file"] = key_file
    return urllib3.PoolManager(**kwargs)


def lambda_handler(event, context):
    """Handle an incoming Alexa Smart Home directive."""

    _logger.debug("Event: %s", event)

    base_url = os.environ.get("BASE_URL")
    assert base_url is not None, "Set BASE_URL environment variable (no trailing slash)"
    base_url = base_url.strip("/")

    directive = event.get("directive")
    assert directive is not None, "Malformatted request - missing directive"
    assert directive.get("header", {}).get("payloadVersion") == "3", "Only payloadVersion == 3"

    scope = directive.get("endpoint", {}).get("scope")
    if scope is None:
        # Linking directive carries the token in payload.grantee
        scope = directive.get("payload", {}).get("grantee")
    if scope is None:
        # Discovery directive carries it in payload.scope
        scope = directive.get("payload", {}).get("scope")
    assert scope is not None, "Malformatted request - missing endpoint.scope"
    assert scope.get("type") == "BearerToken", "Only BearerToken is supported"

    token = scope.get("token")
    if token is None and os.environ.get("DEBUG") == "True":
        # Only for local testing; do not rely on this in production.
        token = os.environ.get("LONG_LIVED_ACCESS_TOKEN")
    if token is None:
        _logger.error("Directive contains no bearer token; is account linking set up?")
        return {
            "event": {
                "payload": {
                    "type": "INVALID_AUTHORIZATION_CREDENTIAL",
                    "message": "No bearer token in directive.",
                }
            }
        }

    response = _http_manager().request(
        "POST",
        "{}/api/alexa/smart_home".format(base_url),
        headers={
            "Authorization": "Bearer {}".format(token),
            "Content-Type": "application/json",
        },
        body=json.dumps(event).encode("utf-8"),
    )

    if response.status >= 400:
        _logger.error("HA error %s: %s", response.status, response.data.decode("utf-8"))
        return {
            "event": {
                "payload": {
                    "type": "INVALID_AUTHORIZATION_CREDENTIAL"
                    if response.status in (401, 403)
                    else "INTERNAL_ERROR",
                    "message": response.data.decode("utf-8"),
                }
            }
        }

    _logger.debug("Response: %s", response.data.decode("utf-8"))
    return json.loads(response.data.decode("utf-8"))
