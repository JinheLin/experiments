#!/usr/bin/env bash
set -euo pipefail
# Only compile NumKong's x86 kernels that can run on the test host.
# GCC 11 cannot compile some optional targets for newer CPUs.
exec env -u http_proxy -u https_proxy -u all_proxy \
    -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY CARGO_HTTP_PROXY='' \
    RUSTFLAGS='' CARGO_ENCODED_RUSTFLAGS='' CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER=gcc \
    NK_TARGET_ICELAKE=0 NK_TARGET_GENOA=0 NK_TARGET_SAPPHIRE=0 NK_TARGET_SAPPHIREAMX=0 \
    NK_TARGET_GRANITEAMX=0 NK_TARGET_DIAMOND=0 NK_TARGET_TURIN=0 NK_TARGET_ALDER=0 NK_TARGET_SIERRA=0 \
    cargo build --locked --release "$@"
