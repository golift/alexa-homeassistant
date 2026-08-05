# nginx configuration

See [`nginx/homeassistant.conf`](../nginx/homeassistant.conf).

Key points:

- `ssl_verify_client` is **server-level** (or `http`), never per-location.
- Use `ssl_verify_client optional`, then enforce per-path:

```nginx
if ($ssl_client_verify != SUCCESS) {
    return 403;
}
```

- **Do not require a client cert on:**
  - `= /auth/authorize`
  - `= /auth/token`
- **Require it on** `= /api/alexa/smart_home` (and the rest of the public UI if you want the whole public hostname locked down).

- Home Assistant needs websockets (`/api/websocket`) and standard
  `X-Forwarded-*` headers. On SWAG, `include /config/nginx/proxy.conf;` covers this.

## SWAG (LinuxServer.io) layout

Put the server block under `/config/nginx/site-confs/` and your CA under
`/config/nginx/pki/home_ca.crt`. Keep the public hostname on Cloudflare
(**Full (strict)** TLS mode) with a valid origin cert.

If you also expose `ha.home.example.com` (direct, no Cloudflare) or
`ha.lan.example.com`, do **not** require client certs on those names — use the
`map $host $require_mtls` pattern in the example file so only the public
Cloudflare hostname enforces mTLS.
