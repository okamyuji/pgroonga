# ビルドステージ
FROM postgres:17-alpine AS builder

ENV PGROONGA_VERSION=4.0.4 \
    GROONGA_VERSION=15.1.7 \
    MECAB_VERSION=0.996.12 \
    MECAB_IPADIC_VERSION=2.7.0-20070801

COPY alpine/build.sh /
RUN chmod +x /build.sh && \
  apk add --no-cache \
    apache-arrow-dev \
    build-base \
    clang19-dev \
    cmake \
    gettext-dev \
    linux-headers \
    llvm19 \
    lz4-dev \
    msgpack-c-dev \
    postgresql-dev \
    rapidjson-dev \
    ruby \
    samurai \
    wget \
    xsimd-dev \
    xxhash-dev \
    zlib-dev \
    zstd-dev && \
  /build.sh ${PGROONGA_VERSION} ${GROONGA_VERSION} ${MECAB_VERSION} ${MECAB_IPADIC_VERSION}

# 実行ステージ
FROM postgres:17-alpine

ENV PGROONGA_VERSION=4.0.4 \
    GROONGA_VERSION=15.1.7 \
    LD_LIBRARY_PATH=/usr/local/lib:$LD_LIBRARY_PATH

# ランタイム依存関係のインストール
RUN apk add --no-cache \
    libarrow \
    libgomp \
    libxxhash \
    msgpack-c \
    zlib \
    zstd

# ビルドステージからMeCab、Groonga、PGroongaをコピー
COPY --from=builder /usr/local/lib/libmecab* /usr/local/lib/
COPY --from=builder /usr/local/lib/libgroonga* /usr/local/lib/
COPY --from=builder /usr/local/lib/groonga /usr/local/lib/groonga
COPY --from=builder /usr/local/lib/postgresql/pgroonga* /usr/local/lib/postgresql/
COPY --from=builder /usr/local/lib/mecab /usr/local/lib/mecab
COPY --from=builder /usr/local/share/postgresql/extension/pgroonga* /usr/local/share/postgresql/extension/
COPY --from=builder /usr/local/bin/mecab* /usr/local/bin/
COPY --from=builder /usr/local/bin/groonga* /usr/local/bin/
COPY --from=builder /usr/local/bin/grn* /usr/local/bin/
COPY --from=builder /usr/local/etc/mecabrc /usr/local/etc/
