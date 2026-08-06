# step-ca for the Alexa Lambda client cert

[Smallstep `step-ca`](https://smallstep.com/docs/step-ca/) is an easy home CA
with an official Docker image: [`smallstep/step-ca`](https://hub.docker.com/r/smallstep/step-ca/).

## unRAID (Community Applications)

Install using [`unraid/step-ca.xml`](../unraid/step-ca.xml):

1. unRAID → Apps → **Add Container Manually** (or add this repo as a template source).
2. Set appdata path and LAN port (default container port **9000**).
3. Set a strong `DOCKER_STEPCA_INIT_PASSWORD` (encrypts the CA keys on first boot).
4. **Do not** publish this container through Cloudflare / the public Internet.

First boot initializes the CA from the `DOCKER_STEPCA_INIT_*` variables.
Afterward, configuration lives in appdata — changing env vars does not re-init.

## Docker (any host)

```bash
docker volume create step-ca
docker run -d --name step-ca \
  -v step-ca:/home/step -p 9000:9000 \
  -e DOCKER_STEPCA_INIT_NAME="Home CA" \
  -e DOCKER_STEPCA_INIT_DNS_NAMES="step-ca.lan,step-ca,localhost" \
  -e DOCKER_STEPCA_INIT_PASSWORD='change-me' \
  -e DOCKER_STEPCA_INIT_REMOTE_MANAGEMENT=true \
  smallstep/step-ca
```

## Bootstrap the `step` CLI

```bash
brew install step    # or https://smallstep.com/docs/step-cli/installation

CA_FP=$(docker exec step-ca step certificate fingerprint /home/step/certs/root_ca.crt)
# --ca-url hostname must be one of DOCKER_STEPCA_INIT_DNS_NAMES above.
step ca bootstrap --ca-url https://step-ca.lan:9000 --fingerprint "$CA_FP"
```

## Issue the Alexa client cert

```bash
step ca certificate alexa-lambda client.crt client.key \
  --not-after 2160h     # 90 days

# Upload the JSON to SSM (see alexa-setup.md), then:
shred -u client.key     # or store safely; renewal below makes rotation easy
```

nginx trusts the CA with:

```nginx
ssl_client_certificate /path/to/home_ca.crt;   # root or intermediate
ssl_verify_client optional;
```

Copy `root_ca.crt` out of appdata (`certs/root_ca.crt`) into your nginx container.

## Renewal (mTLS client certs support `step ca renew`)

```bash
step ca renew client.crt client.key --ca-url https://step-ca.lan:9000 --force
# re-upload to the SSM parameter; Lambda picks it up on next cold start
```

Short-lived certs + renewal are the point of step-ca; avoid stuffing a 10-year
client key into AWS and forgetting it.
