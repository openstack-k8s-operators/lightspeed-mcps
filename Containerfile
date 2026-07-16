# Use this base image so everything comes from RPM packages (including OSC plugins)
ARG BUILDER_IMAGE=quay.rdoproject.org/podified-master-centos10/openstack-openstackclient:current-tested
ARG BASE_IMAGE=quay.rdoproject.org/podified-master-centos10/openstack-openstackclient:current-tested

FROM $BUILDER_IMAGE AS builder

WORKDIR /app

# Install uv in the builder only; not needed in the final image.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency manifests and README first (build backend needs README.md).
# Dependency layer is reused when only application code changes.
COPY --chown=cloud-admin:cloud-admin pyproject.toml uv.lock README.md ./

# Using `--no-install-package` in `uv sync` for these 3 packages only prevents
# the top level package from installing, not their dependencies, installing
# incompatible versions of the dependencies.
RUN echo -e '\n[tool.uv]\nexclude-dependencies = [\n    "openstackclient",\n    "python-openstackclient",\n    "stevedore"\n]' >> pyproject.toml

# This should be safe because uv’s resolver is highly conservative and should
# keep every existing package pinned to its current locked version.
# Checked this with `uv lock --dry-run`
RUN uv lock

# Create the virtual environment enabling system's site-packages (RPM packages
# from base container) since `uv sync` doesn't support it.
RUN uv venv --system-site-packages /app/.venv

# Install dependencies but without the packages that come from the base image
RUN uv sync --frozen --no-dev --no-editable --no-install-project

# Copy application code and install the package into the venv.
COPY src/ src/
RUN uv sync --frozen --no-dev --no-editable

RUN curl -o oc.tar.gz https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable-4.18/openshift-client-linux.tar.gz && \
    tar xvf oc.tar.gz oc && \
    chmod +x oc && \
    rm oc.tar.gz

# Final stage: smaller image without uv or build tools.
FROM $BASE_IMAGE

LABEL com.redhat.component="rhos-ls-mcps" \
      name="openstack-lightspeed/rhos-mcps" \
      summary="MCP server providing OpenStack tools for RHOS-Lightspeed" \
      io.k8s.name="rhos-mcps" \
      io.k8s.description="MCP Tools for RHOS-Lightspeed" \
      io.openshift.tags="openstack,lightspeed,mcp" \
      org.label-schema.vcs-url="https://github.com/openstack-k8s-operators/lightspeed-mcps"

WORKDIR /app

# Copy the virtualenv (includes the installed package with --no-editable) and README.
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/oc /opt/app-root/bin/

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8080

USER 1001

ENTRYPOINT ["rhos-ls-mcps", "--ip", "0.0.0.0"]
