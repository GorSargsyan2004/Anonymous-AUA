FROM alpine:3.20

RUN apk add --no-cache tor && \
    mkdir -p /var/lib/tor/auahub_drop && \
    chown -R tor:tor /var/lib/tor && \
    chmod 700 /var/lib/tor /var/lib/tor/auahub_drop

COPY torrc /etc/tor/torrc.base
COPY entrypoint.sh /entrypoint.sh
RUN chmod 644 /etc/tor/torrc.base && chmod +x /entrypoint.sh

USER tor

ENTRYPOINT ["/entrypoint.sh"]
