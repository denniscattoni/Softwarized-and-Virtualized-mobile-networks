FROM ubuntu:20.04
LABEL authors="dennis"

ENV DEBIAN_FRONTEND=noninteractive

# Install runtime tools
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    nginx \
    iproute2 \
    net-tools \
    iperf3 \
    openssl \
 && rm -rf /var/lib/apt/lists/*

# Copy replicated content (edge cache)
COPY content/video.bin /var/www/html/video.bin

# Copy certificates
RUN mkdir -p /etc/nginx/certs
COPY certs/server.crt /etc/nginx/certs/server.crt
COPY certs/server.key /etc/nginx/certs/server.key

# Copy nginx configuration
COPY nginx/streaming.conf /etc/nginx/conf.d/streaming.conf

EXPOSE 8443

CMD ["/bin/bash"]
