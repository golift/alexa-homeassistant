# alexa-homeassistant

Amazon Alexa Smart Home → Home Assistant through **nginx**, with optional
**mTLS** between AWS Lambda and your reverse proxy. CloudFormation deploy.
No Envoy, no Nabu Casa subscription, no long-lived HA token in production.

```
Alexa skill → Lambda → https://ha.example.com/api/alexa/smart_home (mTLS) → nginx → HA
Alexa app   → https://ha.example.com/auth/authorize + /auth/token (no client cert)
```

## Repo layout

| Path | What |
|------|------|
| [`lambda/`](lambda/) | Python proxy (`lambda_function.py`), dependencies |
| [`cfn/template.yaml`](cfn/template.yaml) | CloudFormation: Lambda, IAM, Alexa invoke permission |
| [`cfn/rotation.yaml`](cfn/rotation.yaml) | CloudFormation: IAM Roles Anywhere trust for automatic cert rotation |
| [`docker/`](docker/) | `cert-rotator` image: renews client certs and republishes them to SSM |
| [`nginx/homeassistant.conf`](nginx/homeassistant.conf) | mTLS + OAuth path exceptions |
| [`unraid/`](unraid/) | unRAID templates for [`smallstep/step-ca`](https://hub.docker.com/r/smallstep/step-ca/) and `cert-rotator` |
| [`docs/`](docs/) | [Alexa setup](docs/alexa-setup.md), [step-ca](docs/step-ca.md), [nginx](docs/nginx.md), [rotation](docs/rotation.md) |
| [`.github/workflows/`](.github/workflows/) | Build zip → S3 → `cloudformation deploy` (manual dispatch) |

## Quick start

1. **Home Assistant:** enable `alexa.smart_home`; set external URL to `https://ha.example.com`.
2. **Alexa Developer Console:** create Smart Home skill (payload v3); note Skill ID.
3. **mTLS (recommended):** run step-ca (see [`docs/step-ca.md`](docs/step-ca.md)), issue a client cert, store it in an SSM SecureString parameter.
4. **Deploy:** see [`docs/alexa-setup.md`](docs/alexa-setup.md) for the `aws cloudformation deploy` command, or run the `deploy` GitHub Action (set `AWS_ROLE_ARN`, `ARTIFACT_BUCKET`, `AWS_REGION`).
5. **Link account** in the Alexa app.
6. **Automate renewal:** run the [`cert-rotator`](docs/rotation.md) container so the client certificate never expires on you.

**Region:** North America → `us-east-1`.

## Cost

This stack is designed to run at ~$0/month: arm64 Lambda at 128MB, free SSM
standard-tier SecureString parameters instead of Secrets Manager, and a
configurable CloudWatch log retention (default 90 days).

## Why mTLS?

Without it, anyone on the Internet who can reach `ha.example.com` can attempt
Home Assistant logins. With mTLS, nginx refuses connections that do not present
a client certificate from your private CA — while Amazon’s OAuth callbacks
(`/auth/*`) stay reachable so account linking still works.

## Credits

- Lambda proxy pattern: [haaska](https://github.com/mike-grant/haaska) and the
  [Home Assistant Alexa docs](https://www.home-assistant.io/integrations/alexa.smart_home/).
- CA: [Smallstep step-ca](https://smallstep.com/docs/step-ca/).

## License

[MIT](LICENSE)
