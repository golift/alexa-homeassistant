# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
#
# cert-rotator: renews step-ca client certificates and publishes the Alexa
# client certificate to an AWS SSM SecureString parameter.
#
# The base is the AWS CLI image (Amazon Linux 2023), chosen because AWS ships
# an Amzn2023 build of aws_signing_helper and the image already carries jq.
# Every stage below only copies binaries into the final image, so buildx can
# produce arm64 images from an amd64 runner without emulation.

ARG AWS_CLI_VERSION=2.38.5@sha256:c8156a0e74e13929a42d7e60f0d4e14e7d28d980cd21822505d4bff80e5460c1
ARG STEP_CLI_VERSION=0.31.0@sha256:107e8497646c0e7c06cc5b6fd6fa13b5d2001909df6d7da3706b8541ed42bddf
ARG SIGNING_HELPER_VERSION=1.8.4

FROM smallstep/step-cli:${STEP_CLI_VERSION} AS stepcli

FROM --platform=$BUILDPLATFORM alpine:3.24@sha256:be2656061b536c1883c4034790e21d0a09c5aaaf51968b89368a3c0371ea2c9a AS fetch
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
