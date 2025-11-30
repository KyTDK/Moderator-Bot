FROM pytorch/pytorch:2.5.1-cuda12.1-cudnn9-runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

RUN --mount=type=cache,target=/var/cache/apt \
    --mount=type=cache,target=/var/lib/apt/lists \
    apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc g++ make pkg-config \
    libopus0 libpng-dev zlib1g-dev \
    ffmpeg git curl ca-certificates openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Install a standalone Docker CLI so in-container updates can control the host daemon
ARG DOCKER_CLI_VERSION=27.2.0
RUN curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" \
    | tar --extract --gzip --strip-components=1 --directory=/usr/local/bin docker/docker \
    && chmod +x /usr/local/bin/docker

WORKDIR /app

COPY requirements.txt .

RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    python -m pip install --upgrade pip && \
    pip install --prefer-binary -r requirements.txt
    
COPY . .

CMD ["python", "bot.py"]
