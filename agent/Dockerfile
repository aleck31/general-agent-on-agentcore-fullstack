# Agent container for Bedrock AgentCore Runtime (ARM64).
#
# Amazon Linux 2023 rather than a slim Debian image, and root rather than a dedicated
# user, for one reason: the per-user file mount. `mount.s3files` ships in
# amazon-efs-utils, which is packaged for AL2023, and mount(2) needs CAP_SYS_ADMIN, which
# means the process performing it must be root. The mount cannot be declared on the
# Runtime instead — filesystemConfigurations is Runtime-scoped and we need one Access
# Point per user. See .dev/adr/0007.
#
# The cost is real and worth stating: this image gives up the non-root execution the
# previous Debian-based one had. Nothing else here needs root. If it turns out that
# InvokeAgentRuntimeCommand executes as root independently of USER, and the watchdog can
# refresh a mount unprivileged, this should go back to a dedicated user.
FROM public.ecr.aws/amazonlinux/amazonlinux:2023

# amazon-efs-utils: provides /sbin/mount.s3files and amazon-efs-mount-watchdog (verified
# on amazon-efs-utils-3.3.1, aarch64).
#
# python3-botocore is for the SYSTEM python3, not for the agent: the mount helper's
# shebang is #!/usr/bin/python3 and it resolves the mount profile's credential_process
# through botocore, which AL2023 does not install with efs-utils (checked — importing it
# there fails otherwise, and the mount then reports no credentials).
#
# python3.11 is what AL2023 packages, and the agent's dependencies install cleanly on it.
RUN dnf install -y --setopt=install_weak_deps=False \
        amazon-efs-utils python3-botocore python3.11 python3.11-pip \
    && dnf clean all && rm -rf /var/cache/dnf

WORKDIR /app

# Install deps first for layer caching.
COPY requirements.txt /app/requirements.txt
RUN python3.11 -m pip install --no-cache-dir -r /app/requirements.txt

# `python3` must reach the interpreter that has the agent's dependencies: entrypoint.sh
# and cred_helper.py both use it, while efs-utils keeps using /usr/bin/python3 through its
# own absolute path.
RUN ln -sf /usr/bin/python3.11 /usr/local/bin/python3

COPY *.py /app/
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh /app/cred_helper.py

EXPOSE 8080

# Liveness: the same /ping the AgentCore platform probes. Python-only, so no curl needed.
HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --retries=3 \
  CMD ["python3", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/ping', timeout=2).status==200 else 1)"]

ENTRYPOINT ["/app/entrypoint.sh"]
