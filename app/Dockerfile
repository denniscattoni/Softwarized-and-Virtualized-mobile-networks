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
 && rm -rf /var/lib/apt/lists/*

# Copy replicated content (edge cache)
COPY content/video.bin /var/www/html/video.bin

# Copy nginx configuration
COPY nginx/streaming.conf /etc/nginx/conf.d/streaming.conf

EXPOSE 8080

CMD ["/bin/bash"]
