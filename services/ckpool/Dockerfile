# Build ckpool-solo with BCH support.
#
# IMPORTANT: ckpool's upstream repo (Con Kolivas, Bitbucket) targets BTC.
# For BCH, use a fork that knows about CashAddr / BCH share validation.
# Common choices:
#   - https://github.com/cculianu/ckpool-solo  (Calin Culianu, BCHN dev)
#   - https://bitbucket.org/ckolivas/ckpool    (upstream; works for BCH if
#     you point it at a BCH node and use a legacy address)
#
# Verify the fork you pick still builds before publishing your store.

FROM debian:bookworm-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        autoconf \
        automake \
        libtool \
        pkg-config \
        libjansson-dev \
        libssl-dev \
        libzmq3-dev \
        ca-certificates \
        git \
    && rm -rf /var/lib/apt/lists/*

ARG CKPOOL_REPO=https://bitbucket.org/ckolivas/ckpool.git
ARG CKPOOL_REF=master

RUN git clone --depth=1 -b ${CKPOOL_REF} ${CKPOOL_REPO} /src \
    && cd /src \
    && ./autogen.sh \
    && ./configure --without-ckdb \
    && make -j"$(nproc)"

FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libjansson4 \
        libssl3 \
        libzmq5 \
        gettext-base \
        jq \
        curl \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /src/src/ckpool /usr/local/bin/ckpool

COPY ckpool.conf.template /etc/ckpool/ckpool.conf.template
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 4567

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
