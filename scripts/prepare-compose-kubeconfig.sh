#!/bin/sh
set -eu

source_path="${1:-}"
dest_path="${2:-}"
container_server="${3:-https://host.docker.internal:16443}"

if [ -z "$source_path" ] || [ -z "$dest_path" ]; then
  echo "usage: prepare-compose-kubeconfig.sh <source> <dest> [container-server]" >&2
  exit 2
fi

if [ ! -r "$source_path" ]; then
  echo "kubeconfig source is not readable: $source_path" >&2
  exit 1
fi

mkdir -p "$(dirname "$dest_path")"
umask 077

awk -v server="$container_server" '
  /^[[:space:]]*server:[[:space:]]*https:\/\/(127\.0\.0\.1|localhost):[0-9]+/ {
    sub(/https:\/\/(127\.0\.0\.1|localhost):[0-9]+/, server)
    print
    print "    tls-server-name: kubernetes"
    next
  }
  { print }
' "$source_path" > "$dest_path"

chmod 0640 "$dest_path"
