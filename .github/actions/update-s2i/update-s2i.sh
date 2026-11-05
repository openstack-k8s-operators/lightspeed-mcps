#!/usr/bin/env bash
# Update S2I source pins and a uv-generated S2I requirements file.

set -euo pipefail

sources_file="${S2I_SOURCES_FILE:-.s2i/sources.txt}"
requirements_file="${S2I_REQUIREMENTS_FILE:-.s2i/requirements.lock}"
stream="${S2I_STREAM:-master}"
dependency_group="${S2I_DEPENDENCY_GROUP:-}"

usage() {
  echo "Usage: $0 {sources|requirements}" >&2
  exit 2
}

upper_constraints_record() {
  [ -f "$sources_file" ] || return 1
  awk -v stream="$stream" '
    $1 == stream && $2 == "upper-constraints" { print $3, $4, $5; exit }
  ' "$sources_file"
}

fetch_constraints() {
  local url="$1"
  local revision="$2"
  local checkout_dir
  checkout_dir="$(mktemp -d)"

  git init --quiet "$checkout_dir"
  git -C "$checkout_dir" remote add origin "$url"
  git -C "$checkout_dir" fetch --depth 1 origin "$revision"
  git -C "$checkout_dir" checkout --detach --quiet FETCH_HEAD
  cp "$checkout_dir/upper-constraints.txt" "$3"
  rm -rf "$checkout_dir"
}

update_sources() {
  local url branch current_revision latest_revision checkout_dir

  read -r url branch current_revision < <(upper_constraints_record) || {
    echo "No upper-constraints entry in $sources_file; nothing to update."
    return
  }

  checkout_dir="$(mktemp -d)"
  git clone --depth 1 --branch "$branch" "$url" "$checkout_dir"
  latest_revision="$(git -C "$checkout_dir" rev-parse HEAD)"
  rm -rf "$checkout_dir"

  if [ "$current_revision" = "$latest_revision" ]; then
    echo "Upper-constraints pin is already current: $current_revision"
    return
  fi

  awk -v stream="$stream" -v revision="$latest_revision" '
    BEGIN { OFS=" " }
    $1 == stream && $2 == "upper-constraints" { $5 = revision }
    { print }
  ' "$sources_file" > "$sources_file.tmp"
  mv "$sources_file.tmp" "$sources_file"
  echo "Updated upper-constraints pin: $current_revision -> $latest_revision"
}

update_requirements() {
  local url branch revision
  local args=(pyproject.toml --upgrade --generate-hashes --output-file "$requirements_file")

  if [ -n "$dependency_group" ]; then
    args+=(--group "$dependency_group")
  fi

  if read -r url branch revision < <(upper_constraints_record); then
    fetch_constraints "$url" "$revision" /tmp/upper-constraints.txt
    args+=(--constraint /tmp/upper-constraints.txt)
    echo "Using upper constraints at $revision ($branch)."
  else
    echo "No upper-constraints entry in $sources_file; compiling without constraints."
  fi

  uv pip compile "${args[@]}"
}

case "${1:-}" in
  sources)
    update_sources
    ;;
  requirements)
    update_requirements
    ;;
  *)
    usage
    ;;
esac
