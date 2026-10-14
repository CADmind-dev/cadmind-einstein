# CADmind einstein: one image for the validator and the reference miner.
#
#   docker build -t cadmind-einstein .
#   docker run --rm cadmind-einstein cadmind-einstein --version
#
# The reference miner runs an agent CLI of your choice; the base image contains none. Install yours
# at build time with AGENT_INSTALL (a shell command run as root), for example a Node.js-based CLI:
#
#   docker build -t cadmind-einstein-miner \
#     --build-arg AGENT_INSTALL="apt-get update && apt-get install -y --no-install-recommends nodejs npm && npm install -g <your-agent-cli>" .
#
# See deploy/docker/compose.yml for running both with their volumes.
FROM python:3.12-slim

ARG AGENT_INSTALL=""
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN if [ -n "$AGENT_INSTALL" ]; then sh -c "$AGENT_INSTALL" && rm -rf /var/lib/apt/lists/*; fi

WORKDIR /opt/cadmind-einstein
COPY pyproject.toml README.md LICENSE ./
COPY cadmind_einstein ./cadmind_einstein
RUN pip install . && cadmind-einstein --version

# An unprivileged user; wallets, keys and state are mounted (see deploy/docker/compose.yml).
RUN useradd --create-home --uid 10001 einstein
USER einstein
WORKDIR /home/einstein

EXPOSE 8793
CMD ["cadmind-einstein", "--help"]
