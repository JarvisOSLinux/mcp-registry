# Setup scripts

These scripts are referenced by `setupScript` in `registry.json`. Discover downloads each script from the raw GitHub URL and runs it with `bash` in the server’s install directory (where `manifest.json` lives).

| Script | Server(s) | Purpose |
|--------|-----------|--------|
| `setup-calculator-ts.sh` | Calculator (TypeScript) | `npm install` + `npm run build` |
| `setup-calculator-py.sh` | Calculator (Python) | `pip install` (mcp + deps) |
| `setup-calculator-rust.sh` | Calculator (Rust) | `cargo build --release` |
| `setup-hello-config.sh` | Hello Config | `pip install` (hello_config) |
| `setup-remote.sh` | Hello World (SSE), Hello World (WebSocket) | Read `manifest.json` (user config), write `client-env.sh` for the local client |

All scripts assume **CWD = install directory** when run.
