# Setup scripts

These scripts are referenced by `setupScript` in `registry.json`. Discover downloads each script from the raw GitHub URL and runs it with `bash` in the server’s install directory (where `manifest.json` lives).

## Prerequisites (install before running setup)

| Runtime       | Required for                    | Install (examples) |
|---------------|----------------------------------|--------------------|
| Python 3.10+  | calculator-py, hello-config, setup-remote | Arch: `pacman -S python` · Debian/Ubuntu: `apt install python3 python3-venv` · Fedora: `dnf install python3` |
| Node.js + npm | calculator-ts                   | Arch: `pacman -S nodejs npm` · Debian/Ubuntu: `apt install nodejs npm` · Fedora: `dnf install nodejs npm` |
| Rust + cargo  | calculator-rust                 | Arch: `pacman -S rust` · Or use [rustup](https://rustup.rs) on any distro |

Python scripts create a venv (`.venv/`) in the install dir to avoid PEP 668 externally-managed-environment. The registry transport uses `.venv/bin/python3` for Python servers.

| Script | Server(s) | Purpose |
|--------|-----------|--------|
| `setup-calculator-ts.sh` | Calculator (TypeScript) | `npm install` + `npm run build` |
| `setup-calculator-py.sh` | Calculator (Python) | Create venv, `pip install` (mcp + deps) |
| `setup-calculator-rust.sh` | Calculator (Rust) | `cargo build --release` |
| `setup-hello-config.sh` | Hello Config | Create venv, `pip install` (hello_config) |
| `setup-remote.sh` | Hello World (SSE), Hello World (WebSocket) | Read `manifest.json` (user config), write `client-env.sh` for the local client |

All scripts assume **CWD = install directory** when run.
