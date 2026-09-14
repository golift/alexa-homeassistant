# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32
#
# cert-rotator: renews step-ca client certificates and publishes the Alexa
# client certificate to an AWS SSM SecureString parameter.
#
# The base is the AWS CLI image (Amazon Linux 2023), chosen because AWS ships
# an Amzn2023 build of aws_signing_helper and the image already carries jq.
# Every stage below only copies binaries into the final image, so buildx can
# produce arm64 images from an amd64 runner without emulation.

ARG AWS_CLI_VERSION=2.36.40@sha256:b6aeb95d19d7f5a8cae4eb814cb16739b6b2a4f2f46f427ada6a8c9a20d9881d
ARG STEP_CLI_VERSION=0.30.6@sha256:474768dd54700088e9480210eaf2c25e3041ed1e8302c7cf211725381cec9f5e
ARG SIGNING_HELPER_VERSION=1.8.4

FROM smallstep/step-cli:${STEP_CLI_VERSION} AS stepcli

FROM --platform=$BUILDPLATFORM alpine:3.22@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce AS fetch
ARG SIGNING_HELPER_VERSION
ARG TARGETARCH
RUN apk add --no-cache curl
WORKDIR /out
# URLs and checksums come from the IAM Roles Anywhere user guide:
# https://docs.aws.amazon.com/rolesanywhere/latest/userguide/credential-helper.html
RUN set -eux; \
    base="https://rolesanywhere.amazonaws.com/releases/${SIGNING_HELPER_VERSION}"; \
    case "${TARGETARCH}" in \
      amd64) url="${base}/X86_64/Linux/Amzn2023/aws_signing_helper"; \
             sha="b7568acd6e1517a4e1adaee68d52bfd6284a0e5305677166cd83d43a07c815c9" ;; \
      arm64) url="${base}/Aarch64/Linux/Amzn2023/aws_signing_helper"; \
             sha="581a57223b476637458fa4598169d1d71c1b5b796f08026b460ec3f41a2b3707" ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "${url}" -o aws_signing_helper; \
    echo "${sha}  aws_signing_helper" | sha256sum -c -; \
    chmod 0755 aws_signing_helper

ARG AWS_CLI_VERSION
FROM amazon/aws-cli:${AWS_CLI_VERSION}

COPY --from=stepcli /usr/local/bin/step /usr/local/bin/step
COPY --from=fetch /out/aws_signing_helper /usr/local/bin/aws_signing_helper
COPY --chmod=0755 rotate.sh /usr/local/bin/rotate.sh

# Certificates and keys are supplied by the operator on this volume.
VOLUME ["/certs"]

ENTRYPOINT ["/usr/local/bin/rotate.sh"]
