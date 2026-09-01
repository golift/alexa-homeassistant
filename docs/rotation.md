# Automatic certificate rotation

The Alexa Lambda authenticates to nginx with a client certificate from your own
CA. Short-lived certificates are the point of running step-ca — but a 90-day
certificate you renew by hand is a 90-day outage waiting for the one time you
forget. This rotates it for you.

```
cert-rotator  --renews-->  step-ca            (mTLS, no CA password)
cert-rotator  --writes-->  SSM SecureString   (IAM Roles Anywhere, no AWS key)
Lambda        --reads--->  SSM SecureString   (on cold start)
```

Nothing else has to change when the certificate rotates: nginx validates
against the CA chain, which stays the same, and the Lambda re-reads the
parameter whenever a new execution environment starts. Renewing 30 days ahead
of expiry means every warm Lambda container has long since been recycled before
the old certificate dies.

## How it authenticates to AWS

[IAM Roles Anywhere](https://docs.aws.amazon.com/rolesanywhere/latest/userguide/introduction.html)
lets the container trade an X.509 certificate for temporary AWS credentials, so
no access key is stored on your server. The container presents a certificate
issued by the same CA, and AWS returns credentials that last an hour and are
allowed to write exactly one SSM parameter.

The role is pinned two ways: the request must come through your trust anchor,
and the certificate's common name must match exactly. Another certificate from
the same CA cannot assume the role.

The rotator renews its own certificate on the same schedule, so it never locks
itself out. The service is free — only AWS Private CA costs money, and you are
using your own CA instead.

## 1. Create the AWS trust

[`cfn/rotation.yaml`](../cfn/rotation.yaml) creates the trust anchor, the role
and the profile.

```bash
# step-ca signs leaf certificates with its intermediate, so that is the CA to
# trust. Copy it out of the step-ca container:
docker exec step-ca cat /home/step/certs/intermediate_ca.crt > home_ca.pem

aws cloudformation deploy \
  --region us-east-1 \
  --stack-name alexa-ha-cert-rotation \
  --template-file cfn/rotation.yaml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
      "CaCertificatePem=$(cat home_ca.pem)" \
      RotatorCertificateCN=cert-rotator \
      MtlsParamName=/alexa-ha/mtls-client

aws cloudformation describe-stacks --region us-east-1 \
  --stack-name alexa-ha-cert-rotation \
  --query 'Stacks[0].Outputs' --output table
```

Keep the three ARNs it prints; the container needs them.

## 2. Issue the rotator's certificate

Its common name must equal `RotatorCertificateCN` above, or AWS will refuse the
credentials.

```bash
docker exec step-ca step ca certificate cert-rotator \
  /home/step/rotator.crt /home/step/rotator.key \
  --provisioner admin --provisioner-password-file /home/step/secrets/password \
  --not-after 2160h \
  --ca-url https://localhost:9000 --root /home/step/certs/root_ca.crt
```

## 3. Lay out the certificates

Collect everything in one directory, which the container mounts at `/certs`:

| File | What |
|------|------|
| `root_ca.crt` | CA root, used to verify the step-ca server itself |
| `alexa-lambda.crt` / `.key` | the certificate published to SSM |
| `rotator.crt` / `.key` | the container's own AWS identity |

Keys should be `chmod 600`. This directory is the only copy — the rotator
writes renewed certificates back into it.

## 4. Run it

```bash
docker run -d --name cert-rotator --restart unless-stopped \
  -v /mnt/user/appdata/cert-rotator/certs:/certs \
  -e CA_URL=https://192.168.1.10:9000 \
  -e SSM_PARAMETER=/alexa-ha/mtls-client \
  -e AWS_REGION=us-east-1 \
  -e TRUST_ANCHOR_ARN=arn:aws:rolesanywhere:...:trust-anchor/... \
  -e PROFILE_ARN=arn:aws:rolesanywhere:...:profile/... \
  -e ROLE_ARN=arn:aws:iam::...:role/... \
  ghcr.io/golift/alexa-homeassistant/cert-rotator:latest
```

The hostname in `CA_URL` must be one of the CA's configured DNS names (or an IP
in its SAN list), or TLS verification fails.

On unRAID, use [`unraid/cert-rotator.xml`](../unraid/cert-rotator.xml) instead
of typing that out.

### Settings

| Variable | Default | What |
|----------|---------|------|
| `CA_URL` | — | step-ca URL, required |
| `SSM_PARAMETER` | — | parameter to write, required |
| `CA_ROOT` | `/certs/root_ca.crt` | CA root used to verify step-ca |
| `CLIENT_CERT` / `CLIENT_KEY` | `/certs/alexa-lambda.*` | certificate published to SSM |
| `AUTH_CERT` / `AUTH_KEY` | `/certs/rotator.*` | certificate used for AWS |
| `STATE_FILE` | `/certs/.published-fingerprint` | fingerprint SSM last accepted |
| `RENEW_BEFORE` | `720h` | renew inside this much of expiry |
| `INTERVAL` | `12h` | sleep between checks |
| `ONESHOT` | `false` | check once and exit, for cron |
| `NOTIFIARR_APIKEY` | — | optional notification on rotation and failure |

Leave `TRUST_ANCHOR_ARN` empty to skip Roles Anywhere and use ordinary AWS
credentials (environment variables, or a mounted `~/.aws`) instead.

`SSM_PARAMETER` must be the same parameter the Lambda reads (`MtlsParamName` in
the Lambda stack). Writing creates the parameter if it does not exist, so a
typo here produces a container that reports successful rotations forever while
the Lambda keeps reading a parameter nobody updates.

### Publishing is separate from renewing

The container records the fingerprint of whatever SSM last accepted in
`STATE_FILE`, and uploads whenever the certificate on disk differs from it.
Renewal and publication therefore fail independently: if a certificate renews
but the upload fails, the next pass retries the upload instead of assuming the
new certificate was delivered. It also means the first run uploads once, so
that SSM and disk are known to agree.

### Running from cron instead

Set `ONESHOT=true` and run `docker run --rm ...` from cron or, on unRAID, from
the User Scripts plugin. The container exits non-zero on failure, so the
calling script can raise a host-native alert.

## Testing it

Checks are no-ops until the certificate is inside the renewal window, so force
one by asking for a window wider than the certificate's remaining life:

```bash
# expires-in must be less than the certificate's total lifetime (2160h for a
# 90-day cert), but larger than the remaining life, or step refuses the request.
docker run --rm -e RENEW_BEFORE=2100h -e ONESHOT=true ... cert-rotator:latest
```

Then confirm the parameter version incremented and the Lambda still works:

```bash
aws ssm get-parameter --name /alexa-ha/mtls-client --query 'Parameter.Version'
```

## When it breaks

The failure that matters is the rotator's own certificate expiring while the
container is stopped — it can no longer authenticate to AWS, and no longer
renew itself. Recovery is to issue a fresh rotator certificate (step 2) and
start the container again; nothing in AWS needs to change, because the trust is
in the CA, not in any individual certificate.

This is why the container renews its own certificate before the Alexa one, and
why it is worth pointing `NOTIFIARR_APIKEY` at something you actually read.
