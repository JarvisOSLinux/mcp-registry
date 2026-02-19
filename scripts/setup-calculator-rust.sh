#!/usr/bin/env bash
# Setup script for Calculator (Rust). CWD = install dir (project root with Cargo.toml).
set -e
cargo build --release
