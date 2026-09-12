# Build the normal Dockerfile as douyinsparkflow:local before this optional image.
FROM douyinsparkflow:local

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends x11vnc novnc websockify xauth \
    && rm -rf /var/lib/apt/lists/*

COPY docker/login.py docker/login-entrypoint.sh /app/docker/
RUN sed -i 's/\r$//' /app/docker/login-entrypoint.sh \
    && chmod +x /app/docker/login-entrypoint.sh
ENTRYPOINT ["/app/docker/login-entrypoint.sh"]
