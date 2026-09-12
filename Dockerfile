FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends cron util-linux tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY . /app

RUN sed -i 's/\r$//' /app/docker/*.sh \
    && chmod +x /app/docker/*.sh

ENTRYPOINT ["/app/docker/entrypoint.sh"]
