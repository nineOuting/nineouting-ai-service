#!/usr/bin/env bash
set -euo pipefail

# This script only copies a validated image. It never deploys or updates Helm.
test "${GITHUB_REPOSITORY:?}" = nineOuting/nineouting-ai-service
test "${GITHUB_REF:?}" = refs/heads/develop
[[ "${GITHUB_EVENT_NAME:?}" == push || "$GITHUB_EVENT_NAME" == workflow_dispatch ]] || { echo '::error::Invalid CI context or image digest'; exit 1; }
test "${EXPECTED_ACCOUNT:?}" = 443845329525
test "${AWS_REGION:?}" = ap-northeast-2
test "${SOURCE_IMAGE:?}" = ghcr.io/nineouting/nineouting-ai-service
test "${TARGET_IMAGE:?}" = 443845329525.dkr.ecr.ap-northeast-2.amazonaws.com/nineouting-dev-ai-service
[[ "${SOURCE_DIGEST:?}" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo '::error::Invalid CI context or image digest'; exit 1; }
[[ "${GITHUB_SHA:?}" =~ ^[0-9a-f]{40}$ ]] || { echo '::error::Invalid CI context or image digest'; exit 1; }
[[ "${GITHUB_RUN_ID:?}" =~ ^[0-9]+$ && "${GITHUB_RUN_ATTEMPT:?}" =~ ^[0-9]+$ ]] || { echo '::error::Invalid CI context or image digest'; exit 1; }

source="$SOURCE_IMAGE@$SOURCE_DIGEST"
tag="sha-${GITHUB_SHA}-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
target="$TARGET_IMAGE:$tag"
docker buildx imagetools inspect --raw "$source" > ghcr-index.json
amd64_digest=$(jq -er '
  [.manifests[] | select(.platform.os == "linux" and .platform.architecture == "amd64") | .digest]
  | if length == 1 then .[0] else error("Expected exactly one linux/amd64 image") end
' ghcr-index.json)
[[ "$amd64_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo '::error::Invalid CI context or image digest'; exit 1; }
docker buildx imagetools inspect --raw "$SOURCE_IMAGE@$amd64_digest" > ghcr-amd64-manifest.json

docker buildx imagetools create --prefer-index=false --tag "$target" "$SOURCE_IMAGE@$amd64_digest"
docker buildx imagetools inspect "$target" > ecr-manifest.txt
digest=$(awk '$1 == "Digest:" {print $2; exit}' ecr-manifest.txt)
[[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo '::error::Invalid CI context or image digest'; exit 1; }
docker buildx imagetools inspect --raw "$TARGET_IMAGE@$digest" > ecr-manifest-raw.json

source_config=$(jq -er '.config.digest' ghcr-amd64-manifest.json)
target_config=$(jq -er '.config.digest' ecr-manifest-raw.json)
[[ "$source_config" =~ ^sha256:[0-9a-f]{64}$ && "$target_config" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo '::error::Invalid CI context or image digest'; exit 1; }
if [[ "$source_config" != "$target_config" ]]; then
  echo '::error::GHCR/ECR image config digest mismatch'
  exit 1
fi
source_layers=$(jq -ce '[.layers[].digest]' ghcr-amd64-manifest.json)
target_layers=$(jq -ce '[.layers[].digest]' ecr-manifest-raw.json)
if [[ "$source_layers" != "$target_layers" ]]; then
  echo '::error::GHCR/ECR image layer mismatch'
  exit 1
fi
docker pull --platform linux/amd64 "$TARGET_IMAGE@$digest"
target_platform=$(docker image inspect "$TARGET_IMAGE@$digest" --format '{{.Os}}/{{.Architecture}}')
if [[ "$target_platform" != linux/amd64 ]]; then
  echo '::error::ECR image is not linux/amd64'
  exit 1
fi

image_uri="$TARGET_IMAGE@$digest"
printf 'image.repository=%s\nimage.digest=%s\nimage.tag=%s\nimage.uri=%s\n' \
  "$TARGET_IMAGE" "$digest" "$tag" "$image_uri" > image-result.txt
{
  echo "image_repository=$TARGET_IMAGE"
  echo "image_digest=$digest"
  echo "image_uri=$image_uri"
  echo "image_tag=$tag"
} >> "${GITHUB_OUTPUT:?}"
{
  echo '## DEV AI ECR Publish PASS'
  echo "- Source: $source"
  echo "- Source AMD64 digest: $amd64_digest"
  echo "- ECR image: $image_uri"
  echo "- Platform: $target_platform"
  echo '- Registry image config/layers: MATCH'
  echo '- EKS deployment / Argo CD Sync: NOT RUN'
} >> "${GITHUB_STEP_SUMMARY:?}"
