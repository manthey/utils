FROM ubuntu:24.04

ARG NODE_VERSION=14
ARG PYTHON_VERSIONS="3.11 3.10 3.12 3.13 3.14"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=en_US.UTF-8 \
    PYENV_ROOT="/.pyenv" \
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
      fuse \
      libfuse2 \
      libldap2-dev \
      libsasl2-dev \
      # geojs convenience \
      imagemagick \
      # developer convenience \
      bzip2 \
      dirmngr \
      git \
      less \
      locales \
      vim \
      # testing convenience \
      fonts-dejavu \
      libmagic-dev \
      # testing \
      redis \
      rabbitmq-server \
      # tools \
      jq \
      shellcheck \
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
    curl -sSL "https://github.com/universal-ctags/ctags-nightly-build/releases/download/$(curl -s https://api.github.com/repos/universal-ctags/ctags-nightly-build/releases/latest | grep '"tag_name"' | head -1 | cut -d '"' -f 4)/uctags-$(curl -s https://api.github.com/repos/universal-ctags/ctags-nightly-build/releases/latest | grep '"tag_name"' | head -1 | cut -d '"' -f 4 | cut -d '+' -f 1)-linux-x86_64.deb" -o /tmp/uctags.deb && \
    dpkg -i /tmp/uctags.deb && \
    rm /tmp/uctags.deb && \
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
    # This makes duplicate python library files hardlinks of each other \
    rdfind -minsize 8192 -makehardlinks true -makeresultsfile false /.pyenv

RUN for ver in $PYTHON_VERSIONS; do \
    pyenv local $ver && \
    python -m pip install --no-cache-dir -U pip && \
    python -m pip install --no-cache-dir tox wheel && \
    pyenv local --unset; \
    done && \
    pyenv rehash && \
    find / -xdev -name __pycache__ -type d -exec rm -r {} \+ && \
    rm -rf /tmp/* /var/tmp/* && \
    rdfind -minsize 8192 -makehardlinks true -makeresultsfile false /.pyenv

# ENV PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64
RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.4/install.sh | bash && \
    . ~/.nvm/nvm.sh && \
    nvm install 20 && \
    nvm alias default 20 && \
    npm install -g npm@latest && \
    npx playwright install-deps && \
    npx playwright install chromium --with-deps && \
    npx playwright install --with-deps
RUN usermod -aG rabbitmq ubuntu && \
    chmod -R 777 /var/lib/rabbitmq/mnesia && \
    chmod -R 777 /var/log/rabbitmq
USER ubuntu
WORKDIR /home/ubuntu
# hadolint ignore=SC2016
RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
ENV PATH="/home/ubuntu/.local/bin:/home/ubuntu/.nvm/current:/home/ubuntu/.nvm/$PATH" \
    NVM_DIR="/home/ubuntu/.nvm"
RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.4/install.sh | bash && \
    . ~/.nvm/nvm.sh && \
    nvm install 20 && \
    nvm alias default 20 && \
    npm install -g npm@latest && \
    ln -s $(dirname `which npm`) "$NVM_DIR/current"
RUN cd /tmp && npx playwright install
RUN cat <<'EOF' > /home/ubuntu/.local/bin/start_services.sh
mkdir -p /tmp/db
nohup redis-server --bind 0.0.0.0 >/tmp/redis.log 2>&1 &
nohup mongod --noauth --bind_ip_all --dbpath=/tmp/db >/tmp/mongo.log 2>&1 &
nohup rabbitmq-server >/tmp/rabbitmq.log 2>&1 &
EOF

RUN chmod a+x /home/ubuntu/.local/bin/start_services.sh
RUN uv tool install tox && \
    uv tool install pre-commit
RUN git config --global user.name "Container" && \
    git config --global user.email "container@example.com"
RUN uvx mini-swe-agent --help
RUN cat <<'EOF' > /home/ubuntu/.config/mini-swe-agent/.env
MSWEA_CONFIGURED="true"
MSWEA_MODEL_NAME="ollama/qwen2.5-coder:14b"
MSWEA_COST_TRACKING="ignore_errors"
MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT="10"
OLLAMA_API_BASE=http://host.docker.internal:11434
OPENAI_API_BASE=http://host.docker.internal:11434/v1
OPENAI_API_KEY=ollama
EOF

# USER root
# docker build --force-rm -t manthey/mswea -f mswea.Dockerfile .
