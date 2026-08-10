# alexa-homeassistant vs [haaska](https://github.com/mike-grant/haaska)

Both projects do the same core job: an AWS Lambda that forwards Alexa Smart
Home directives to Home Assistant’s `/api/alexa/smart_home` endpoint. This
project began from that [haaska](https://github.com/mike-grant/haaska) pattern
(and from the official
[Home Assistant Alexa docs](https://www.home-assistant.io/integrations/alexa.smart_home/)).
The differences are mostly about how the Lambda authenticates, how the stack is
deployed, and how the public HA endpoint is locked down.

## At a glance

| Concern | **haaska** | **alexa-homeassistant** (this repo) |
|---|---|---|
| Core behavior | POST the Alexa event to HA | Same |
| HA auth | Long-lived access token in `config.json` | Bearer token from Alexa account linking (per request) |
| Transport security | Optional client cert path in config; TLS verify toggle | Optional mTLS via SSM + nginx `ssl_verify_client` |
| Packaging | Zip / Docker / Makefile; `requests` dependency | Stdlib HTTP + runtime `boto3`; lean zip |
| Deploy | Manual Lambda / wiki-driven | CloudFormation + GitHub Actions → S3 |
| Proxy guidance | Generic (expose HA URL) | nginx snippets + unRAID templates |
| Cert lifecycle | Operator renews / re-uploads by hand | `cert-rotator` + IAM Roles Anywhere |
| Maturity / community | Long-lived (~570★, many forks, wiki) | Newer, narrower scope |
| Cost posture | Typical Lambda | Explicitly ~$0/month (SSM free tier, arm64, log retention) |

## What is the same

- Alexa Smart Home Skill API **payload version 3**.
- Lambda is a thin proxy: it does not invent device discovery or capability
  mapping — Home Assistant’s `alexa.smart_home` integration does that.
- You still need an Alexa Developer Console skill, a Lambda in the region
  Amazon requires (e.g. `us-east-1` for North America), and HA configured for
  Smart Home.

If discovery and voice control already work for you on haaska, the day-to-day
Alexa experience is essentially identical.

## Authentication model

### haaska

haaska ships a `config.json` (see the
[sample](https://github.com/mike-grant/haaska/blob/master/config/config.json.sample))
with a **long-lived Home Assistant token**. Every directive uses that token.
The wiki walkthrough is optimized around that model.

**Pros**

- Simple mental model: one secret, one config file in the Lambda package.
- Works without Alexa account linking if you wire the skill that way.
- Optional `ssl_client` / `ssl_verify` already exist for people who terminate
  TLS with client certificates.

**Cons**

- A long-lived token is a high-value secret sitting in the deployment artifact
  (or env). Rotation means rebuilding/redeploying the function.
- Anyone who can invoke the Lambda (or leak the package) gets standing HA API
  access at whatever privilege that token has.
- Home Assistant’s current Smart Home docs emphasize **OAuth account linking**
  (`/auth/authorize` + `/auth/token`) rather than baking a LLAT into Lambda.

### This repo

The Lambda takes the **Bearer token Alexa already obtained** during account
linking and forwards it on each request. There is no HA long-lived token in the
function configuration for normal operation.

**Pros**

- Credentials are user-scoped and refreshable through HA’s OAuth flow.
- Compromising the Lambda package does not by itself yield a standing HA token.
- Matches the flow HA documents for custom skills.

**Cons**

- Account linking must be set up correctly in the Alexa Developer Console
  (client ID quirks, redirect URLs, skill enablement under **Dev**).
- Debugging “401 Unauthorized” means chasing linking/token issues, not a single
  static secret you can paste into `config.json`.

## Exposing Home Assistant to the Internet

### haaska

haaska assumes you already have a reachable `url`. How you protect that URL is
mostly left to the operator and the wiki. Client certificates are supported in
the Python session (`ssl_client`), but there is no accompanying reverse-proxy
policy, CA bootstrap, or renewal story in the repo itself.

### This repo

The design center is: **keep HA off the open Internet except for what Alexa
needs**, and prove the Lambda’s identity with **mTLS** at nginx.

- `/api/alexa/smart_home` requires a client certificate from your private CA.
- `/auth/*` (and login-page assets) stay reachable for account linking.
- Everything else on the public name can stay LAN-only.
- Optional pieces: step-ca on unRAID, SSM SecureString for the client cert,
  CloudFormation for IAM Roles Anywhere, and a `cert-rotator` container so
  90-day certs do not become an annual outage.

**Pros**

- Stronger default threat model for a self-hosted HA with a public DNS name.
- No Envoy requirement; plain nginx / SWAG is enough.
- Automation for the boring, easy-to-forget renewal step.

**Cons**

- More moving parts (CA, DNS-only record so Cloudflare does not terminate TLS,
  Roles Anywhere, rotator).
- Overkill if HA is already only reachable via a trusted tunnel (Tailscale,
  Cloudflare Tunnel with tight rules, Nabu Casa, etc.).
- mTLS and Cloudflare orange-cloud are incompatible; the public name must be
  DNS-only (grey cloud).

## Operations and packaging

| Concern | haaska | This repo |
|---|---|---|
| Config | `config.json` in the zip | Env vars + optional SSM parameter |
| Dependencies | `requests` (pinned in requirements) | Stdlib `urllib` / `ssl`; `boto3` from the Lambda runtime |
| IaC | Not first-class | CloudFormation (`cfn/template.yaml`, `cfn/rotation.yaml`) |
| CI | Build/test workflows | Build zip, optional deploy, multi-arch GHCR image for rotator |
| Home-lab helpers | Wiki | unRAID templates, nginx example, rotation docs |

haaska wins on **familiarity and community documentation** (especially the
wiki). This repo wins if you want **reproducible AWS deploy + nginx mTLS +
automated cert rotation** in one place.

## When to prefer haaska

- You already run haaska and it is stable.
- You are fine with a long-lived HA token in the Lambda config.
- You expose HA through something that already satisfies your threat model
  (VPN-only, Nabu Casa, locked-down tunnel) and do not want a private CA.
- You want the larger existing community and wiki for skill setup edge cases.

## When to prefer this repo

- You want Alexa account linking (no LLAT in Lambda) as the default.
- Your HA hostname is on the public Internet (or soon will be) and you want
  nginx to demand a client certificate for Smart Home API calls.
- You prefer CloudFormation and GitHub Actions over hand-built zips.
- You want short-lived client certs with automatic renewal (step-ca +
  `cert-rotator`) instead of a forever cert in the Lambda package.
- You are on unRAID / SWAG and want templates that match that layout.

## Migration notes (haaska → this repo)

1. Keep the same Alexa skill; point the default endpoint at the new Lambda ARN
   after CloudFormation deploy.
2. Remove reliance on the haaska `bearer_token`; complete account linking in the
   Alexa app so directives carry a user token.
3. If you used haaska’s `ssl_client`, move that material into the SSM parameter
   format this Lambda expects (`client_crt` / `client_key`) and set
   `MtlsParamName` — or start fresh with step-ca.
4. Align nginx (or equivalent) with the path split: mTLS on
   `/api/alexa/smart_home`, open `/auth/*` for linking.
5. Run discovery again after cutover; entity filtering still lives in HA
   (`alexa.smart_home` filter), not in either Lambda.

## Summary

haaska is the established, minimal adapter: small Python, long-lived token,
flexible TLS knobs, big community. This project is a **security- and
ops-oriented packaging of the same proxy idea**: account-linking tokens, optional
nginx mTLS, CloudFormation, and automated certificate rotation. Choose haaska
for simplicity and incumbency; choose this repo when the public edge and
credential hygiene are the parts you care about hardening.
