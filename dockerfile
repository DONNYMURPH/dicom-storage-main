FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        dcmtk inotify-tools bash gawk findutils python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY . .
RUN chmod +x /app/*.sh

ENTRYPOINT ["/app/watch_dcm.sh"]