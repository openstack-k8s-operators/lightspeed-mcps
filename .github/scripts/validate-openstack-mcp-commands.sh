#!/usr/bin/env bash
set -euo pipefail

MCP_IMAGE="${1:?Error: Target MCP image URL must be provided as argument 1}"
REF_IMAGE="${2:?Error: Reference image URL must be provided as argument 2}"

extract_commands() {
  awk '
    /^Commands:$/ { commands=1 }
    commands { print }
    END {
      if (!commands) {
        print "Commands section not found" > "/dev/stderr"
        exit 1
      }
    }
  '
}

echo "Target MCP image: ${MCP_IMAGE}"
echo "Reference image:  ${REF_IMAGE}"

# Set PYTHONHASHSEED=0 in both containers because OpenStack iterates over
# a frozenset of authentication plugins when generating help. Python hash
# randomization can otherwise change their order and the wrapping of help
# text across runs, causing a diff even when the commands are identical.
echo "Extracting command sections..."
podman run --rm -e PYTHONHASHSEED=0 --entrypoint openstack "${MCP_IMAGE}" --help | extract_commands > mcp_commands.txt
podman run --rm -e PYTHONHASHSEED=0 --entrypoint openstack "${REF_IMAGE}" --help | extract_commands > ref_commands.txt

echo "Comparing outputs..."
if ! diff -u --color=always mcp_commands.txt ref_commands.txt > diff.patch; then
  echo "::error::Command differences detected between images!"
  cat diff.patch
  exit 1
fi

echo "Commands match successfully."
