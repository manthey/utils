FROM ubuntu:26.04

ARG PYTHON_VERSIONS="3.11 3.10 3.12 3.13 3.14"

# The CFLAGS is mainly so numcodecs compiles on python 3.14
ENV DEBIAN_FRONTEND=noninteractive \
    LANG=en_US.UTF-8 \
    PI_OFFLINE=1 \
    PI_SKIP_VERSION_CHECK=1 \
    PYENV_ROOT="/.pyenv" \
    CFLAGS="-std=gnu17 -march=native" \
    PATH="/.pyenv/bin:/.pyenv/shims:$PATH"

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      gir1.2-glib-2.0 \
      gpg \
      iso-codes \
      lsb-release \
      # as specified by \
      # https://github.com/pyenv/pyenv/wiki#suggested-build-environment \
      build-essential \
      cmake \
      curl \
      libffi-dev \
      liblzma-dev \
      libreadline-dev \
      libsqlite3-dev \
      tk-dev \
      wget \
      xz-utils \
      zlib1g-dev \
      # for curl \
      ca-certificates \
      # girder convenience \
      fuse3 \
      libfuse-dev \
      libfuse3-dev \
      libldap2-dev \
      libsasl2-dev \
      pandoc \
      # geojs convenience \
      imagemagick \
      mesa-utils \
      xvfb \
      # developer convenience \
      bsdmainutils \
      bzip2 \
      dirmngr \
      git \
      jq \
      less \
      locales \
      openssh-server \
      shellcheck \
      telnet \
      tmux \
      unzip \
      vim \
      # agent use \
      fd-find \
      ripgrep \
      # testing convenience \
      fonts-dejavu \
      libmagic-dev \
      # testing \
      docker-cli \
      lsof \
      memcached \
      openjdk-17-jdk \
      optipng \
      rabbitmq-server \
      redis \
      xmlstarlet \
      # shrink docker image \
      rdfind \
      && \
    curl -fsSL https://pgp.mongodb.com/server-8.0.asc | gpg -o /usr/share/keyrings/mongodb-server-8.0.gpg --dearmor && \
    # change to "resolute" for 26.04 \
    echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-8.0.gpg ] https://repo.mongodb.org/apt/ubuntu noble/mongodb-org/8.0 multiverse" | tee /etc/apt/sources.list.d/mongodb-org-8.0.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends mongodb-org && \
    locale-gen en_US.UTF-8 && \
    find /usr/share/X11/locale -mindepth 1 -maxdepth 1 ! -name 'en_US*' ! -name 'C' ! -name 'en' -type d -exec rm -rf {} + && \
    find /usr/share/i18n -mindepth 1 ! -name 'en_US*' ! -name 'C' -type f -exec rm -f {} + && \
    rm -rf /usr/bin/pebble && \
    curl -L https://github.com/pyenv/pyenv-installer/raw/master/bin/pyenv-installer | bash && \
    find / -xdev -name __pycache__ -type d -exec rm -r {} \+ && \
    rm -rf /etc/ssh/ssh_host* && \
    rm -rf /usr/share/vim/vim91/doc/* /usr/share/vim/vim91/tutor/* /usr/share/doc && \
    curl -sSL "https://github.com/universal-ctags/ctags-nightly-build/releases/download/$(curl -s https://api.github.com/repos/universal-ctags/ctags-nightly-build/releases/latest | grep '"tag_name"' | head -1 | cut -d '"' -f 4)/uctags-$(curl -s https://api.github.com/repos/universal-ctags/ctags-nightly-build/releases/latest | grep '"tag_name"' | head -1 | cut -d '"' -f 4 | cut -d '+' -f 1)-$(uname -m | sed 's/x86_64/linux-x86_64/;s/aarch64/linux-aarch64/').deb" -o /tmp/uctags.deb && \
    dpkg --force-architecture -i /tmp/uctags.deb && \
    rm /tmp/uctags.deb && \
    mkdir -p /usr/libexec/docker/cli-plugins && \
    BUILDX_LATEST=$(curl -s https://api.github.com/repos/docker/buildx/releases/latest | grep '"tag_name"' | cut -d '"' -f 4) && \
    curl -fsSL "https://github.com/docker/buildx/releases/download/${BUILDX_LATEST}/buildx-${BUILDX_LATEST}.linux-$(dpkg --print-architecture)" -o /usr/libexec/docker/cli-plugins/docker-buildx && \
    chmod +x /usr/libexec/docker/cli-plugins/docker-buildx && \
    apt-get clean && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* /var/cache/* && \
    rdfind -minsize 8192 -makehardlinks true -makeresultsfile false /usr && \
    rdfind -minsize 8192 -makehardlinks true -makeresultsfile false /var

RUN pyenv update && \
    pyenv install --list && \
    echo $PYTHON_VERSIONS | xargs -P `nproc` -n 1 pyenv install && \
    # ensure newest pip and setuptools for all python versions \
    echo $PYTHON_VERSIONS | xargs -n 1 bash -c 'pyenv global "${0}" && pip install -U setuptools pip' && \
    pyenv global $(pyenv versions --bare) && \
    find $PYENV_ROOT/versions -type d '(' -name '__pycache__' -o -name 'test' -o -name 'tests' ')' -exec rm -rfv '{}' + >/dev/null && \
    find $PYENV_ROOT/versions -type f '(' -name '*.py[co]' -o -name '*.exe' ')' -exec rm -fv '{}' + >/dev/null && \
    echo $PYTHON_VERSIONS | tr " " "\n" > $PYENV_ROOT/version && \
    find / -xdev -name __pycache__ -type d -exec rm -r {} \+ && \
    rm -rf /tmp/* /var/tmp/* /root/.cache/* && \
    find /.pyenv '(' -name '*.so' -o -name '*.a' -o -name '*.so.*' ')' -exec strip --strip-unneeded -p -D {} \; && \
    find /.pyenv -name 'libpython*.a' -delete && \
    chmod a+w /.pyenv/shims && \
    # This makes duplicate python library files hardlinks of each other \
    rdfind -minsize 8192 -makehardlinks true -makeresultsfile false /.pyenv

RUN for ver in $PYTHON_VERSIONS; do \
    pyenv local $ver && \
    python -m pip install --no-cache-dir -U pip && \
    python -m pip install --no-cache-dir tox wheel && \
    python -m pip install --no-cache-dir coverage && \
    pyenv local --unset; \
    done && \
    pyenv rehash && \
    find / -xdev -name __pycache__ -type d -exec rm -r {} \+ && \
    rm -rf /tmp/* /var/tmp/* && \
    rdfind -minsize 8192 -makehardlinks true -makeresultsfile false /.pyenv

# ENV PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64
RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.4/install.sh | bash && \
    . ~/.nvm/nvm.sh && \
    nvm install 22 && \
    nvm alias default 22 && \
    npm install -g npm@latest && \
    npx playwright install-deps && \
    npx playwright install chromium --with-deps && \
    npx playwright install firefox --with-deps && \
    npx playwright install --with-deps && \
    apt-get clean && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* /var/cache/* && \
    rdfind -minsize 8192 -makehardlinks true -makeresultsfile false /usr && \
    rdfind -minsize 8192 -makehardlinks true -makeresultsfile false /var

RUN usermod -aG rabbitmq ubuntu && \
    chmod -R 777 /var/lib/rabbitmq/mnesia && \
    chmod -R 777 /var/log/rabbitmq
COPY .vimrc /home/ubuntu/.vimrc
COPY cover.py /home/ubuntu/.local/bin/cover.py
RUN mkdir -p /run/sshd /home/ubuntu/.ssh && \
    chmod 700 /run/sshd /home/ubuntu/.ssh  && \
    sed -i 's/^#Port 22$/Port 2222/' /etc/ssh/sshd_config && \
    sed -i 's/^#PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config && \
    sed -i 's/^PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config && \
    /usr/bin/ssh-keygen -A
RUN chown -R ubuntu:ubuntu /home/ubuntu

# hadolint ignore=DL3066
USER ubuntu
WORKDIR /home/ubuntu
# hadolint ignore=SC2016
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/home/ubuntu/.local/bin:/home/ubuntu/.nvm/current:/home/ubuntu/.nvm:/home/ubuntu/.env:$PATH" \
    NVM_DIR="/home/ubuntu/.nvm"
RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.4/install.sh | bash && \
    . ~/.nvm/nvm.sh && \
    nvm install 22 && \
    nvm alias default 22 && \
    npm install -g npm@latest && \
    ln -s $(dirname $(which npm)) "$NVM_DIR/current"
RUN cd /tmp && npx playwright install
RUN uv venv --seed --no-project --python=python3.13 .env
RUN cat <<'EOF' > /home/ubuntu/.local/bin/start_services.sh
pkill redis-server
pkill mongod
pkill rabbitmq-server
pkill memcached
while ss -tuln | grep -q ":6379 "; do sleep 1; done
while ss -tuln | grep -q ":27017 "; do sleep 1; done
while ss -tuln | grep -q ":5672 "; do sleep 1; done
while ss -tuln | grep -q ":11211 "; do sleep 1; done
nohup redis-server --bind 0.0.0.0 >/tmp/redis.log 2>&1 &
rm -rf /tmp/db
mkdir -p /tmp/db
nohup mongod --noauth --bind_ip_all --dbpath=/tmp/db >/tmp/mongo.log 2>&1 &
nohup rabbitmq-server >/tmp/rabbitmq.log 2>&1 &
nohup memcached -d >/tmp/memcached.log 2>&1 &
EOF

RUN chmod a+x /home/ubuntu/.local/bin/start_services.sh
RUN uv tool install tox && \
    uv tool install pre-commit
RUN git config --global user.name "Container" && \
    git config --global user.email "container@example.com"
RUN uvx mini-swe-agent --help

RUN find /home/ubuntu -xdev -name mini_textbased.yaml -exec cp {} /home/ubuntu/.config/mini-swe-agent/mini.yaml \;
RUN cat <<'EOF' > /home/ubuntu/.config/mini-swe-agent/.env
MSWEA_CONFIGURED="true"
MSWEA_MODEL_NAME="ollama/qwen3.6:35b"
MSWEA_COST_TRACKING="ignore_errors"
MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT="10"
LITELLM_REQUEST_TIMEOUT="300000"
OLLAMA_API_BASE=http://host.docker.internal:11434
OPENAI_API_BASE=http://host.docker.internal:11434/v1
OPENAI_API_KEY=ollama
EOF

RUN cat <<'EOF' > /home/ubuntu/.local/bin/mswea.sh
#!/usr/bin/env bash
uvx --offline mini-swe-agent --model-class litellm_textbased -c ~/.config/mini-swe-agent/mini.yaml -c model.model_kwargs.timeout=300 -c environment.timeout=300 -c agent.max_consecutive_format_errors=7 -y -m openai/"$1" -t "$2" "${@:3}"
EOF

RUN chmod a+x /home/ubuntu/.local/bin/mswea.sh && \
    mswea.sh x x --help

RUN mkdir -p /home/ubuntu/.pi/agent && \
    mkdir -p /home/ubuntu/.pi/extensions && \
    npm install -g @earendil-works/pi-coding-agent

RUN cat <<'EOF' > /home/ubuntu/.pi/agent/global-guidelines.js
export default function addGuidelines(pi) {
  pi.on("before_agent_start", async (event) => {
    const customRule = "\n\n## Global Guidelines:\n" +
      "- Never use emojis, slang, or metaphors.\n" +
      "- Never claim code is verified unless you have actually run it.\n" +
      "- Always use up-to-date versions (e.g., python 3.10-3.14) when possible.\n" +
      "- If you are in a repo with a .pre-commit-config.yaml, pre-commit must be run and pass on all generated or altered code. You may not alter hooks or ignore rules without first getting approval.\n"
      "- When modifying existing code, prefer small changes to major refactors unless otherwise instructed";
    if (!event.systemPrompt) return event;
      return { ...event, systemPrompt: event.systemPrompt + customRule };
    });
}
EOF

RUN cat <<'EOF' > /home/ubuntu/.pi/agent/settings.json
{
  "defaultModel": "qwen3.6:35b",
  "defaultProjectTrust": "always",
  "defaultProvider": "ollama",
  "defaultThinkingLevel": "minimal",
  "enableInstallTelemetry": false,
  "followUpMode": "all",
  "hideThinkingBlock": true,
  "httpIdleTimeoutMs": 0,
  "outputPad": 0,
  "quietStartup": true,
  "extensions": [
    "~/.pi/agent/global-guidelines.js"
  ]
}
EOF

RUN cat <<'EOF' > /home/ubuntu/.pi/agent/local-providers.json
{
  "debug": false,
  "syncOnStartup": true,
  "addToScope": true,
  "providers": {
    "ollama": {
      "enabled": true,
      "baseUrl": "http://host.docker.internal:11434",
      "cleanupStale": false,
      "cacheTtlHours": 24
    }
  }
}
EOF

RUN cat <<'EOF' > /home/ubuntu/.pi/agent/models.json
{
  "providers": {
    "ollama": {
      "baseUrl": "http://host.docker.internal:11434/v1",
      "api": "openai-completions",
      "apiKey": "ollama",
      "compat": {
        "supportsDeveloperRole": false
      },
      "models": [
        { "id": "qwen3.6:35b" }
      ]
    }
  }
}
EOF

RUN cat <<'EOF' > /home/ubuntu/.pi/agent/compaction-continue.json
{
  "enabled": true,
  "appendSessionEntries": true,
  "log": true,
  "maxRecentEvents": 20
}
EOF

# RUN pi install npm:@kylebrodeur/pi-model-discovery
RUN pip install --no-cache-dir pyyaml && \
    pi install git:github.com/manthey/pi-model-discovery@dist && \
    pi install npm:@richardgill/pi-up-history && \
    pi install npm:@alexleekt/pi-bump && \
    pi install npm:@badliveware/pi-compaction-continue && \
    pi install npm:pi-loop-police && \
    true

RUN cat <<'EOF' > /home/ubuntu/.local/bin/pidev.sh
#!/usr/bin/env bash
pi --mode json --model "$1" "$2" "${@:3}" | jq -c 'select(.type != "message_update")'
EOF

RUN chmod a+x /home/ubuntu/.local/bin/pidev.sh && \
    pidev.sh x x --help

RUN cat <<'EOF' > /home/ubuntu/.local/bin/set_ollama.sh
#!/usr/bin/env bash
TARGET_URL="${1:-http://host.docker.internal:11434}"
sed -i 's|^\(.*_API_BASE=\)https\?://[^/]*|\1'"${TARGET_URL}"'|' /home/ubuntu/.config/mini-swe-agent/.env
sed -i 's|\("baseUrl": "\)https\?://[^/"]*|\1'"${TARGET_URL}"'|' /home/ubuntu/.pi/agent/local-providers.json
sed -i 's|\("baseUrl": "\)https\?://[^/"]*|\1'"${TARGET_URL}"'|' /home/ubuntu/.pi/agent/models.json
echo "${TARGET_URL}"
EOF

RUN chmod a+x /home/ubuntu/.local/bin/set_ollama.sh

RUN cat <<'EOF' >> /home/ubuntu/.bashrc
function truncate_current_directory () {
  echo -n "${PWD/#$HOME/\~}" | awk -F "/" '{
    if (length($0) > 32) {
    if (NF>4 && length($(NF-1)$NF) < 28) print $1 "/" $2 "/…/" $(NF-1) "/" $NF;
    else if (NF>3) print $1 "/" $2 "/…/" $NF;
    else print $1 "/…/" $NF; }
    else print $0;}'
}
function parse_git_branch () {
  git branch 2> /dev/null | sed -e '/^[^*]/d' -e 's/* \(.*\)/ (\1)/'
}
PS1='\h:$(truncate_current_directory)$(parse_git_branch)$ '
export LANG=en_US.UTF-8
export PI_OFFLINE=1
export PI_SKIP_VERSION_CHECK=1
export PYENV_ROOT="/.pyenv"
export CFLAGS="-std=gnu17 -march=native"
export PATH="$HOME/.local/bin:$HOME/.nvm/current:$HOME/.nvm:$HOME/.env:$HOME/.env:/.pyenv/bin:/.pyenv/shims:$PATH"
EOF

# USER root
# Run like `docker exec -t model_card_docker bash -c "cd some_repo && uvx mini-swe-agent --model-class litellm_textbased -c ~/.config/mini-swe-agent/mini.yaml -c model.model_kwargs.timeout=300 -c environment.local.timeout=300 -t \"some requires\" -m openai/{model} -y < /dev/null | tee /tmp/agent.log"`
# docker build --force-rm -t manthey/agent -f agent.Dockerfile .
