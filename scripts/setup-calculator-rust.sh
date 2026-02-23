#!/usr/bin/env bash
# Setup script for Calculator (Rust). CWD = install dir (project root with Cargo.toml).
set -e
command -v cargo >/dev/null 2>&1 || { echo "cargo not found. Install Rust (e.g. pacman -S rust, apt install rustc cargo, or rustup) before running setup." >&2; exit 1; }
cargo build --release
