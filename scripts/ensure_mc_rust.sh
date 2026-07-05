#!/usr/bin/env bash
# Ensure the Rust Monte Carlo extension is available for ohmd (OHM-lqpk.4).
# This runs as ExecStartPre so the extension survives reboots without manual copy.
set -euo pipefail

REPO_DIR="/root/olympus/OHM"
SRC_SO="${REPO_DIR}/src/ohm/_mc_rust.so"
RUST_SO="${REPO_DIR}/rust/target/release/lib_mc_rust.so"
RUST_SRC="${REPO_DIR}/rust/src/lib.rs"

# Source cargo env if available (rustup installed non-interactively).
if [ -f "${HOME}/.cargo/env" ]; then
    . "${HOME}/.cargo/env"
fi

needs_build=false

if [ ! -f "${SRC_SO}" ]; then
    echo "ohm._mc_rust extension missing at ${SRC_SO}"
    needs_build=true
elif [ "${RUST_SRC}" -nt "${SRC_SO}" ]; then
    echo "Rust source newer than installed extension; rebuild needed"
    needs_build=true
fi

if [ "${needs_build}" = false ]; then
    echo "ohm._mc_rust extension up to date"
    exit 0
fi

if [ -f "${RUST_SO}" ] && [ "${RUST_SO}" -nt "${RUST_SRC}" ]; then
    echo "Copying existing release build to ${SRC_SO}"
    cp "${RUST_SO}" "${SRC_SO}"
    exit 0
fi

echo "Building Rust Monte Carlo extension..."
cd "${REPO_DIR}/rust"
cargo build --release
cp "${RUST_SO}" "${SRC_SO}"
echo "Build complete: ${SRC_SO}"
