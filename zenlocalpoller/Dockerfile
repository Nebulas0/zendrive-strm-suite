FROM alpine:3.20

RUN apk add --no-cache ca-certificates wget tzdata

COPY entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh \
    && mkdir -p /config /state

ENV ZLP_CONFIG_DIR=/config
WORKDIR /config

VOLUME ["/config", "/state"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
