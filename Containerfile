FROM registry.access.redhat.com/ubi9/python-312:latest AS builder

WORKDIR /app

# Install uv in the builder only; not needed in the final image.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency manifests and README first (build backend needs README.md).
# Dependency layer is reused when only application code changes.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-editable --no-install-project

# Copy application code and install the package into the venv.
COPY src/ src/
RUN uv sync --frozen --no-dev --no-editable

RUN curl -o oc.tar.gz https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable-4.18/openshift-client-linux.tar.gz && \
    tar xvf oc.tar.gz oc && \
    chmod +x oc && \
    rm oc.tar.gz

# Final stage: smaller image without uv or build tools.
FROM registry.access.redhat.com/ubi9/python-312:latest

LABEL com.redhat.component="rhos-ls-mcps" \
      name="openstack-lightspeed/rhos-mcps" \
      summary="MCP server providing OpenStack tools for RHOS-Lightspeed" \
      io.k8s.name="rhos-mcps" \
      io.k8s.description="MCP Tools for RHOS-Lightspeed" \
      io.openshift.tags="openstack,lightspeed,mcp" \
      org.label-schema.vcs-url="https://github.com/openstack-lightspeed/rhos-mcps"

WORKDIR /app

# Copy the virtualenv (includes the installed package with --no-editable) and README.
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/oc /opt/app-root/bin/

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8080

USER 1001

ENTRYPOINT ["rhos-ls-mcps", "--ip", "0.0.0.0"]
