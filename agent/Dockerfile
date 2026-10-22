# Agent container for Bedrock AgentCore Runtime (ARM64).
#
# Amazon Linux 2023 because the agent's dependencies install cleanly on its python3.11.
# It used to be here for a stronger reason — `mount.s3files` from amazon-efs-utils, which
# needed root — and that reason is gone: the per-user mount now happens inside the Code
# Interpreter session, not in this container. So nothing here needs root, and it runs as a
# dedicated user.
FROM public.ecr.aws/amazonlinux/amazonlinux:2023

# python3.11 is what AL2023 packages. shadow-utils is for useradd: the minimal AL2023 image
# has none, and without it the USER line below fails the build with exit 127.
RUN dnf install -y --setopt=install_weak_deps=False python3.11 python3.11-pip shadow-utils \
    && dnf clean all && rm -rf /var/cache/dnf

WORKDIR /app

# Install deps first for layer caching.
COPY requirements.txt /app/requirements.txt
RUN python3.11 -m pip install --no-cache-dir -r /app/requirements.txt

# `python3` must reach the interpreter that has the agent's dependencies, which is what
# entrypoint.sh invokes.
RUN ln -sf /usr/bin/python3.11 /usr/local/bin/python3

COPY *.py /app/
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Least privilege. Nothing is written outside the interpreter session, so a read-only /app
# is enough; HOME is set because libraries fall back to writing caches there.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser
ENV HOME=/home/appuser

EXPOSE 8080

# Liveness: the same /ping the AgentCore platform probes. Python-only, so no curl needed.
HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --retries=3 \
  CMD ["python3", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/ping', timeout=2).status==200 else 1)"]

ENTRYPOINT ["/app/entrypoint.sh"]
