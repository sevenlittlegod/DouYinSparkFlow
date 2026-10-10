FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends cron util-linux tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r /tmp/requirements.txt

COPY portal/requirements.txt /tmp/portal-requirements.txt
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r /tmp/portal-requirements.txt

COPY . /app

RUN sed -i 's/\r$//' /app/docker/*.sh \
    && chmod +x /app/docker/*.sh \
    && chmod -R a+rX /app/account_worker /app/core /app/utils /app/portal /app/docker /app/tests

ENTRYPOINT ["/app/docker/entrypoint.sh"]
