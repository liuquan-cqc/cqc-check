#!/usr/bin/env sh
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
version=${1:-}

if [ -z "$version" ]; then
  echo "usage: $0 <version-tag>" >&2
  exit 2
fi

case "$version" in
  *[!A-Za-z0-9._-]*)
    echo "version tag contains unsupported characters" >&2
    exit 2
    ;;
esac

cd "$repo_dir"
test -z "$(git status --porcelain)" || {
  echo "working tree must be clean before image build" >&2
  exit 1
}

short_sha=$(git rev-parse --short=12 HEAD)
image_tag="${version}-${short_sha}"
docker build --pull=false \
  --label "org.opencontainers.image.revision=$(git rev-parse HEAD)" \
  --label "org.opencontainers.image.version=$version" \
  -t "cqc-ocr-api:$image_tag" \
  -t "cqc-ocr-worker:$image_tag" .

echo "built cqc-ocr-api:$image_tag"
echo "built cqc-ocr-worker:$image_tag"
