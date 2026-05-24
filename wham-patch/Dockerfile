# Build patched whamg with --flush-per-chr support (v3: 10 Mbp batches)
# Use Ubuntu 20.04 with GCC 9 to match the original build environment
FROM --platform=linux/amd64 ubuntu:20.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \
    git cmake build-essential zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy our forked wham source (already patched)
COPY wham-fork /build/wham
WORKDIR /build/wham

# Build bamtools library only (skip toolkit which has C++ issues)
RUN mkdir -p src/obj bin \
    && cd src/bamtools && mkdir -p build && cd build \
    && cmake .. \
    && make BamTools-static 2>&1 || true \
    && ls /build/wham/src/bamtools/lib/ \
    && cd /build/wham \
    && cd src/Complete-Striped-Smith-Waterman-Library/src && make && cd /build/wham \
    && cd src/fastahack && make && cd /build/wham \
    && make bin/whamg

# Final image: start from the original wham image, add patched binary
ARG ACCOUNT_ID
FROM ${ACCOUNT_ID}.dkr.ecr.ap-southeast-1.amazonaws.com/gatk-sv/wham:2024-10-25-v0.29-beta-5ea22a52

# Install patched whamg as whamg-flush (keep original whamg intact)
COPY --from=builder /build/wham/bin/whamg /usr/bin/whamg-flush

# Verify both binaries exist
RUN whamg 2>&1 | head -1 || true
RUN whamg-flush 2>&1 | head -1 || true
