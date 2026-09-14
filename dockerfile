FROM davidmonterocrespo/velxio:master

ARG WASI_SDK_VERSION=22

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl xz-utils ca-certificates && \
    curl -fL \
      "https://github.com/WebAssembly/wasi-sdk/releases/download/wasi-sdk-${WASI_SDK_VERSION}/wasi-sdk-${WASI_SDK_VERSION}.0-linux.tar.gz" \
      | tar -xz -C /opt && \
    mv "/opt/wasi-sdk-${WASI_SDK_VERSION}.0" /opt/wasi-sdk && \
    rm -rf /var/lib/apt/lists/*

ENV WASI_SDK=/opt/wasi-sdk

RUN test -x /opt/wasi-sdk/bin/clang && \
    /opt/wasi-sdk/bin/clang --version

RUN mkdir -p /app/sdk

COPY velxio-chip.h /app/sdk/velxio-chip.h

RUN test -f /app/sdk/velxio-chip.h
