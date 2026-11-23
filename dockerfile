FROM debian:bookworm-slim

# samba + samba-vfs-modules : the SMB server and the full_audit module (access logs)
# dcmtk                     : dcmsend (sends studies to CTP)
# python3-pydicom           : reads patient name / ID for the upload log
# tzdata                    : so upload_log.csv shows local time (set TZ in the deployment)
RUN apt-get update && apt-get install -y --no-install-recommends \
        samba samba-vfs-modules smbclient \
        dcmtk python3 python3-pydicom tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && : > /etc/samba/smb.conf

# Every file that lands on the shares is owned by this account (samba "force user"),
# and the watcher container runs as this same uid, so it can move/delete uploads.
RUN groupadd -g 1000 dropbox \
    && useradd -u 1000 -g 1000 -M -d /nonexistent -s /usr/sbin/nologin dropbox

WORKDIR /app
COPY entrypoint.sh samba_server.py watcher.py ./
RUN chmod 755 /app/entrypoint.sh /app/samba_server.py /app/watcher.py

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["watcher"]
