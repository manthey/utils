FROM ubuntu:24.04

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
    chmod a+w /.pyenv/shims && \
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
    nvm install 22 && \
    nvm alias default 22 && \
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
    echo 'export PATH="$HOME/.local/bin:$HOME/.env:$PATH"' >> ~/.bashrc
ENV PATH="/home/ubuntu/.local/bin:/home/ubuntu/.nvm/current:/home/ubuntu/.nvm:/home/ubuntu/.env:$PATH" \
    NVM_DIR="/home/ubuntu/.nvm"
RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.4/install.sh | bash && \
    . ~/.nvm/nvm.sh && \
    nvm install 22 && \
    nvm alias default 22 && \
    npm install -g npm@latest && \
    ln -s $(dirname `which npm`) "$NVM_DIR/current"
RUN cd /tmp && npx playwright install
RUN uv venv --seed --no-project --python=python3.13 .env
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
RUN find /home/ubuntu -xdev -name mini_textbased.yaml -exec cp {} /home/ubuntu/.config/mini-swe-agent/mini.yaml \;
RUN cat <<'EOF' > /tmp/diff.txt
diff --git a/src/minisweagent/agents/default.py b/src/minisweagent/agents/default.py
index 75785585..2bc38592 100644
--- a/src/minisweagent/agents/default.py
+++ b/src/minisweagent/agents/default.py
@@ -12,7 +12,7 @@ from jinja2 import StrictUndefined, Template
 from pydantic import BaseModel

 from minisweagent import Environment, Model, __version__
-from minisweagent.exceptions import InterruptAgentFlow, LimitsExceeded, TimeExceeded
+from minisweagent.exceptions import InterruptAgentFlow, LimitsExceeded, TimeExceeded, FormatError
 from minisweagent.utils.serialize import recursive_merge


@@ -31,6 +31,7 @@ class AgentConfig(BaseModel):
     """Stop agent after this many seconds of wall-clock time. 0 means no limit."""
     output_path: Path | None = None
     """Save the trajectory to this path."""
+    format_error_limit: int = 10


 class DefaultAgent:
@@ -44,6 +45,7 @@ class DefaultAgent:
         self.logger = logging.getLogger("agent")
         self.cost = 0.0
         self.n_calls = 0
+        self.format_errors = 0
         self._start_time = time.time()

     def get_template_vars(self, **kwargs) -> dict:
@@ -110,7 +112,7 @@ class DefaultAgent:

     def query(self) -> dict:
         """Query the model and return model messages. Override to add hooks."""
-        if 0 < self.config.step_limit <= self.n_calls or 0 < self.config.cost_limit <= self.cost:
+        if 0 < self.config.step_limit <= self.n_calls or 0 < self.config.cost_limit <= self.cost or 0 < self.config.format_error_limit <= self.format_errors:
             raise LimitsExceeded(
                 {
                     "role": "exit",
@@ -127,7 +129,12 @@ class DefaultAgent:
                 }
             )
         self.n_calls += 1
-        message = self.model.query(self.messages)
+        try:
+            message = self.model.query(self.messages)
+        except FormatError:
+            self.format_errors += 1
+            raise
+        self.format_errors = 0
         self.cost += message.get("extra", {}).get("cost", 0.0)
         self.add_messages(message)
         return message
EOF

RUN find /home/ubuntu/. -name minisweagent -exec patch {}/agents/default.py /tmp/diff.txt \;
RUN cat <<'EOF' > /home/ubuntu/.config/mini-swe-agent/.env
MSWEA_CONFIGURED="true"
MSWEA_MODEL_NAME="ollama/qwen2.5-coder:14b"
MSWEA_COST_TRACKING="ignore_errors"
MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT="10"
MSWEA_AGENT_FORMAT_ERROR_LIMIT="7"
LITELLM_REQUEST_TIMEOUT="300000"
OLLAMA_API_BASE=http://host.docker.internal:11434
OPENAI_API_BASE=http://host.docker.internal:11434/v1
OPENAI_API_KEY=ollama
EOF

# USER root
# Run like `docker exec -t model_card_docker bash -c "cd some_repo && uvx mini-swe-agent --model-class litellm_textbased -c ~/.config/mini-swe-agent/mini.yaml -c model.model_kwargs.timeout=300 -c environment.local.timeout=300 -t \"some requires\" -m openai/{model} -y < /dev/null | tee /tmp/mswea.log"`
# docker build --force-rm -t manthey/mswea -f mswea.Dockerfile .
