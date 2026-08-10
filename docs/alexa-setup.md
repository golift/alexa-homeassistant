# Alexa Smart Home → Home Assistant (nginx + optional mTLS)

This deploys the [official flow](https://www.home-assistant.io/integrations/alexa.smart_home/)
with **nginx** as the reverse proxy and an optional client certificate (mTLS)
between AWS Lambda and your nginx. No Envoy required.

```
Alexa → Lambda (us-east-1) → https://ha.example.com/api/alexa/smart_home (mTLS) → HA
Alexa app → https://ha.example.com/auth/authorize, /auth/token (no client cert) → HA
```

## Prerequisites

- Home Assistant reachable on `https://ha.example.com` (valid public cert, port 443).
- Amazon Developer account (same Amazon account as your Echo devices).
- AWS account.
- **Region:** North America → **us-east-1** (eu-west-1 for EU, us-west-2 for JP/AU).

## 1. Home Assistant

```yaml
# configuration.yaml
alexa:
  smart_home:
    # Optional: filter which entities Alexa can see.
    # filter:
    #   include_domains: [light, switch, fan]
```

Restart HA. Set Settings → System → Network → **External URL** to `https://ha.example.com`.

## 2. Alexa Developer Console

1. Create a **Smart Home** skill (“Provision your own”). Payload version **v3**.
2. Note the **Skill ID**.
3. Set **Default endpoint** to the Lambda ARN from step 4 (after deploy).

## 3. (Optional) mTLS client certificate

See [step-ca.md](step-ca.md). Short version:

```bash
step ca certificate alexa-lambda client.crt client.key \
  --not-after 2160h   # 90 days; renew with `step ca renew`
```

Put it in an SSM SecureString parameter (standard tier is free; typical
client cert+key JSON fits in 4KB). CloudFormation cannot create SecureString
parameters, so do this once via CLI before deploying:

```bash
jq -n --arg c "$(cat client.crt)" --arg k "$(cat client.key)" \
  '{client_crt:$c, client_key:$k}' > /tmp/mtls.json

aws ssm put-parameter --name "/alexa-ha/mtls-client" \
  --type SecureString --tier Standard \
  --value file:///tmp/mtls.json --region us-east-1
# pass this name as MtlsParamName
```

To rotate later: `aws ssm put-parameter ... --overwrite`.

Point nginx at your CA and use [nginx/homeassistant.conf](../nginx/homeassistant.conf).

## 4. Deploy CloudFormation

Package and upload the Lambda zip (or use GitHub Actions → `deploy`):

```bash
cd lambda && zip -r ../lambda.zip . -x 'requirements.txt' '*__pycache__*' '*.pyc' && cd ..

aws s3 mb s3://YOUR-artifacts-bucket --region us-east-1   # once
aws s3 cp lambda.zip s3://YOUR-artifacts-bucket/alexa-ha/lambda.zip

aws cloudformation deploy \
  --template-file cfn/template.yaml \
  --stack-name alexa-ha-smarthome \
  --region us-east-1 \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    BaseUrl=https://ha.example.com \
    AlexaSkillId=amzn1.ask.skill.xxxxxxxx \
    LogRetentionDays=90 \
    CodeS3Bucket=YOUR-artifacts-bucket \
    CodeS3Key=alexa-ha/lambda.zip \
    MtlsParamName=/alexa-ha/mtls-client
```

- Leave `AlexaSkillId` empty on the first deploy; set it later to lock the permission.
- Without mTLS, omit `MtlsParamName`.

## 5. Account linking (Developer Console)

- Authorization URI: `https://ha.example.com/auth/authorize`
- Access Token URI: `https://ha.example.com/auth/token`
- Client ID: `https://pitangui.amazon.com/` (US) — **trailing slash matters**
- Client Secret: anything (HA ignores it)
- Scheme: **Credentials in request body**
- Scope: `smart_home`

Then in the Alexa app → Skills → Your Skills → Dev → enable your skill and sign in.

## 6. Test

Alexa app: “Discover devices”, then “Alexa, turn on …”.

Lambda test event (with `DEBUG=True` and a long-lived token only for testing):

```json
{
  "directive": {
    "header": {
      "namespace": "Alexa.Discovery",
      "name": "Discover",
      "payloadVersion": "3",
      "messageId": "test-1"
    },
    "payload": {"scope": {"type": "BearerToken"}}
  }
}
```

Remove `DEBUG`/long-lived token afterward.
