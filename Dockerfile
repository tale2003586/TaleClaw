ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_EXTRA_INDEX_URL=
ARG PIP_TRUSTED_HOST=
ARG PIP_DEFAULT_TIMEOUT=180
ARG PIP_RETRIES=10
ARG REQUIREMENTS_FILE=requirements-deploy.txt
ARG INSTALL_RAG_DEPS=0

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_EXTRA_INDEX_URL=${PIP_EXTRA_INDEX_URL} \
    PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST} \
    PIP_DEFAULT_TIMEOUT=${PIP_DEFAULT_TIMEOUT} \
    PIP_RETRIES=${PIP_RETRIES} \
    RAG_ENABLED=0 \
    HISTORY_VECTOR_ENABLED=0 \
    SECURITY_RAG_AUTO_CONTEXT_ENABLED=0 \
    SECURITY_RAG_PLUGIN_ENABLED=0 \
    USE_LOCAL_PROXY=0

WORKDIR /app

# Runtime tools used by coding agents and deployment diagnostics. Keep this
# list in the image so container recreation does not discard ad-hoc installs.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        file \
        git \
        jq \
        less \
        openssh-client \
        patch \
        procps \
        ripgrep \
        unzip \
    && rm -rf /var/lib/apt/lists/*

COPY requirements*.txt ./
RUN python -m pip install \
    --prefer-binary \
    --timeout "${PIP_DEFAULT_TIMEOUT}" \
    --retries "${PIP_RETRIES}" \
    -r "${REQUIREMENTS_FILE}" \
    && if [ "${INSTALL_RAG_DEPS}" = "1" ]; then \
        python -m pip install \
          --prefer-binary \
          --timeout "${PIP_DEFAULT_TIMEOUT}" \
          --retries "${PIP_RETRIES}" \
          -r requirements-rag.txt; \
    fi

COPY . .

EXPOSE 8000

CMD ["python", "web/server.py", "--host", "0.0.0.0", "--port", "8000"]
