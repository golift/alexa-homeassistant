#!/usr/bin/env bash
#
# Renew step-ca client certificates and publish the Alexa client certificate to
# an AWS SSM SecureString parameter.
#
# Two certificates are managed:
#   * the Alexa client cert, which the Lambda presents to nginx (published)
#   * the rotator's own cert, used to obtain AWS credentials via IAM Roles
#     Anywhere (never leaves this host)
#
# Both are renewed with `step ca renew`, which authenticates over mTLS using the
# certificate being renewed. No CA password is needed, and no long-lived AWS
# credential has to exist anywhere.

set -euo pipefail

CA_URL="${CA_URL:-}"
CA_ROOT="${CA_ROOT:-/certs/root_ca.crt}"
CLIENT_CERT="${CLIENT_CERT:-/certs/alexa-lambda.crt}"
CLIENT_KEY="${CLIENT_KEY:-/certs/alexa-lambda.key}"
SSM_PARAMETER="${SSM_PARAMETER:-}"
# Fingerprint of the certificate SSM last accepted. Renewal and publication are
# tracked separately so a failed upload is retried rather than forgotten.
STATE_FILE="${STATE_FILE:-/certs/.published-fingerprint}"
RENEW_BEFORE="${RENEW_BEFORE:-720h}"
INTERVAL="${INTERVAL:-12h}"
ONESHOT="${ONESHOT:-false}"

# IAM Roles Anywhere. Leave TRUST_ANCHOR_ARN empty to fall back to whatever
# credentials the AWS CLI finds normally (env vars, mounted ~/.aws, etc).
AUTH_CERT="${AUTH_CERT:-/certs/rotator.crt}"
AUTH_KEY="${AUTH_KEY:-/certs/rotator.key}"
TRUST_ANCHOR_ARN="${TRUST_ANCHOR_ARN:-}"
PROFILE_ARN="${PROFILE_ARN:-}"
ROLE_ARN="${ROLE_ARN:-}"

NOTIFIARR_APIKEY="${NOTIFIARR_APIKEY:-}"
NOTIFIARR_URL="${NOTIFIARR_URL:-https://notifiarr.com/api/v1/notification/passthrough}"

log() {
    printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

# Fires a Notifiarr passthrough notification when NOTIFIARR_APIKEY is set.
# Never fails the run: a missed notification must not break a good rotation.
notify() {
    local level="$1" title="$2" body="$3"
    log "[${level}] ${title}: ${body}"
    [[ -n "${NOTIFIARR_APIKEY}" ]] || return 0

    local color payload
    case "${level}" in
        error) color="FF0000" ;;
        *)     color="00FF00" ;;
    esac
    payload=$(jq -n --arg t "${title}" --arg b "${body}" --arg c "${color}" \
        '{notification: {update: false, name: "cert-rotator", event: "0"},
          discord: {color: $c,
                    text: {title: $t, description: $b},
                    ids: {channel: 0}}}')
    if ! curl -fsS -m 15 -X POST "${NOTIFIARR_URL}" \
        -H "X-API-Key: ${NOTIFIARR_APIKEY}" \
        -H 'Content-Type: application/json' \
        -d "${payload}" >/dev/null 2>&1; then
        log "warning: Notifiarr notification failed (continuing)"
    fi
}

die() {
    notify error "Certificate rotation failed" "$*"
    exit 1
}

fingerprint() {
    step certificate fingerprint "$1"
}

published_fingerprint() {
    [[ -r "${STATE_FILE}" ]] && cat "${STATE_FILE}" || true
}

expires() {
    step certificate inspect --format json "$1" | jq -r '.validity.end'
}

# Renews one certificate in place. step exits 0 and leaves the file untouched
# when the certificate is not yet inside the renewal window, so this is safe to
# run as often as you like.
renew() {
    local cert="$1" key="$2" label="$3"
    [[ -r "${cert}" && -r "${key}" ]] || die "missing ${label} certificate or key (${cert}, ${key})"

    local before after
    before=$(fingerprint "${cert}")
    if ! step ca renew --force --expires-in "${RENEW_BEFORE}" \
        --ca-url "${CA_URL}" --root "${CA_ROOT}" "${cert}" "${key}"; then
        die "step ca renew failed for ${label}"
    fi
    after=$(fingerprint "${cert}")

    [[ "${before}" != "${after}" ]]
}

publish() {
    local fp="$1" tmp
    tmp=$(mktemp /tmp/ssm-XXXXXX.json)
    # Build the API request in a file so the PEMs never appear in argv.
    if ! jq -n --arg name "${SSM_PARAMETER}" \
        --rawfile crt "${CLIENT_CERT}" --rawfile key "${CLIENT_KEY}" \
        '{Name: $name, Type: "SecureString", Overwrite: true,
          Value: ({client_crt: $crt, client_key: $key} | tostring)}' >"${tmp}"; then
        rm -f "${tmp}"
        die "could not build the SSM request payload"
    fi

    local version
    if ! version=$(aws ssm put-parameter --cli-input-json "file://${tmp}" \
        --query Version --output text 2>&1); then
        rm -f "${tmp}"
        die "aws ssm put-parameter failed: ${version}"
    fi
    rm -f "${tmp}"

    # Only recorded once SSM has accepted the upload, so any earlier failure
    # leaves the certificate queued for the next pass.
    printf '%s\n' "${fp}" >"${STATE_FILE}"

    notify info "Alexa client certificate published" \
        "${SSM_PARAMETER} is now version ${version}, valid until $(expires "${CLIENT_CERT}")."
}

# Writes an AWS CLI profile that shells out to aws_signing_helper, which trades
# the rotator certificate for short-lived credentials.
configure_roles_anywhere() {
    [[ -n "${TRUST_ANCHOR_ARN}" ]] || return 0
    [[ -n "${PROFILE_ARN}" && -n "${ROLE_ARN}" ]] || die "PROFILE_ARN and ROLE_ARN are required with TRUST_ANCHOR_ARN"
    [[ -r "${AUTH_CERT}" && -r "${AUTH_KEY}" ]] || die "missing rotator certificate or key (${AUTH_CERT}, ${AUTH_KEY})"

    mkdir -p "${HOME}/.aws"
    cat >"${HOME}/.aws/config" <<EOF
[default]
credential_process = /usr/local/bin/aws_signing_helper credential-process --certificate ${AUTH_CERT} --private-key ${AUTH_KEY} --trust-anchor-arn ${TRUST_ANCHOR_ARN} --profile-arn ${PROFILE_ARN} --role-arn ${ROLE_ARN}
EOF
}

run_once() {
    # The rotator's own credentials come first: if this certificate ever lapses,
    # the container loses its access to AWS and needs manual re-bootstrapping.
    if [[ -n "${TRUST_ANCHOR_ARN}" ]]; then
        if renew "${AUTH_CERT}" "${AUTH_KEY}" "rotator"; then
            log "rotator certificate renewed, valid until $(expires "${AUTH_CERT}")"
        else
            log "rotator certificate still valid until $(expires "${AUTH_CERT}")"
        fi
    fi

    if renew "${CLIENT_CERT}" "${CLIENT_KEY}" "Alexa client"; then
        log "Alexa client certificate renewed, valid until $(expires "${CLIENT_CERT}")"
    else
        log "Alexa client certificate still valid until $(expires "${CLIENT_CERT}")"
    fi

    # Compare against what SSM last accepted rather than against what this pass
    # renewed. A renewal whose upload failed is still pending, and a first run
    # publishes once to guarantee SSM and disk agree.
    local current
    current=$(fingerprint "${CLIENT_CERT}")
    if [[ "${current}" == "$(published_fingerprint)" ]]; then
        log "${SSM_PARAMETER} already holds this certificate, nothing to publish"
        return
    fi

    log "publishing the Alexa client certificate to ${SSM_PARAMETER}"
    publish "${current}"
}

main() {
    [[ -n "${CA_URL}" ]] || die "CA_URL is required (for example https://step-ca.lan:9000)"
    [[ -n "${SSM_PARAMETER}" ]] || die "SSM_PARAMETER is required (for example /alexa-ha/mtls-client)"
    [[ -r "${CA_ROOT}" ]] || die "cannot read the CA root at ${CA_ROOT}"

    configure_roles_anywhere

    if [[ "${ONESHOT}" == "true" ]]; then
        run_once
        return
    fi

    log "starting: checking every ${INTERVAL}, renewing within ${RENEW_BEFORE} of expiry"
    while true; do
        # The subshell contains die()'s exit so a failed check never kills the
        # container; it has already notified, and the next pass retries.
        ( run_once ) || log "check failed; retrying in ${INTERVAL}"
        sleep "${INTERVAL}"
    done
}

main "$@"
