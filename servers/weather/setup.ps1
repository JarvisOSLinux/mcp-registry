# Setup for the Weather MCP server on Windows. CWD = install dir.
#
# The PowerShell counterpart of setup.sh, and the same defense in depth: the
# server is one stdlib-only file with no dependencies and no API key, so this
# proves the interpreter dmcp will spawn can load it before the first tool call.
$ErrorActionPreference = 'Stop'

# `python` is what the Windows transport spawns, so `python` is what gets
# checked -- testing python3 here would vouch for an interpreter this host
# never runs. Windows ships a `python.exe` App Execution Alias that is not an
# interpreter and exits 9009 when Python is absent, so a bare Get-Command is
# not enough to conclude anything.
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Error "python not found. Install Python 3.10+ from python.org or the Microsoft Store, and make sure it is on PATH."
    exit 1
}

& python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Error "Python >= 3.10 is required (the server annotates with PEP 604 unions, which earlier interpreters reject at import). Found: $(& python -c 'import sys; print(sys.version.split()[0])' 2>$null)"
    exit 1
}

# Import rather than merely parse, for the same reason as the POSIX script.
& python -c "import server; assert server.TOOLS" | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Error "server.py could not be imported by this interpreter."
    exit 1
}

Write-Output "OK: weather MCP server ready (Python standard library only, no dependencies installed)"
Write-Output "    Open-Meteo needs no API key. Tools require outbound HTTPS to api.open-meteo.com."
