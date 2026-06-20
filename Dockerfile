ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_EXTRA_INDEX_URL=
ARG PIP_TRUSTED_HOST=
ARG PIP_DEFAULT_TIMEOUT=180
ARG PIP_RETRIES=10

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_EXTRA_INDEX_URL=${PIP_EXTRA_INDEX_URL} \
    PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST} \
    PIP_DEFAULT_TIMEOUT=${PIP_DEFAULT_TIMEOUT} \
    PIP_RETRIES=${PIP_RETRIES} \
    USE_LOCAL_PROXY=0

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install \
    --prefer-binary \
    --timeout "${PIP_DEFAULT_TIMEOUT}" \
    --retries "${PIP_RETRIES}" \
    -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["python", "web/server.py", "--host", "0.0.0.0", "--port", "8000"]
