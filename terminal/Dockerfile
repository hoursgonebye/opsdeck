# Web terminal with Claude Code, for driving Ops Deck from a browser.
#
# Deliberately its own image and its own container: it shares nothing with
# the app except the docker network, has no docker socket, and never runs
# as root. If this container is compromised it can reach the Ops Deck API
# and its own workspace - not the host.

FROM node:20-slim

ARG TTYD_VERSION=1.7.7

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git jq less nano procps python3 ripgrep tini \
 && curl -fsSL -o /usr/local/bin/ttyd \
      "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.x86_64" \
 && chmod 0755 /usr/local/bin/ttyd \
 && ttyd --version \
 && npm install -g @anthropic-ai/claude-code \
 && npm cache clean --force \
 && apt-get clean && rm -rf /var/lib/apt/lists/*
# curl stays: the `ops` helper and every API example in CLAUDE.md use it.

# node:20-slim ships a uid-1000 'node' user. Everything runs as that.
# These are created owned by node so the named volumes mounted over them
# inherit the right ownership on first start.
RUN mkdir -p /workspace /home/node/.claude \
 && chown -R node:node /workspace /home/node

COPY --chown=node:node CLAUDE.md /workspace/CLAUDE.md
COPY --chown=node:node bashrc-extra.sh /etc/profile.d/opsdeck.sh
COPY bridge.py /opt/bridge.py
COPY start.sh /opt/start.sh
RUN chmod 0755 /opt/start.sh

USER node
WORKDIR /workspace
ENV SHELL=/bin/bash HOME=/home/node

# 7681 web terminal, 7682 chat bridge (bridge is network-internal only)
EXPOSE 7681 7682
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/opt/start.sh"]
