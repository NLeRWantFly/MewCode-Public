# MewCode Desktop

The desktop client embeds the existing `frontend/static` renderer and starts the FastAPI backend as a loopback-only sidecar. Development uses the repository Python environment; packaged builds use the PyInstaller one-dir sidecar under `resources/backend`.

```powershell
cd client
npm install
npm start
```

Build the Python sidecar before packaging:

```powershell
cd client
npm run backend
npm run make
```

Install PyInstaller in the repository `.venv` (or set `MEWCODE_PYTHON`) before
running `npm run backend`; the build script discovers that interpreter on all
supported platforms. Packaged Windows builds expose `liteagent.exe` as the runnable desktop client.
Unsigned native maker commands are available as `make:win:x64`,
`make:mac:x64`, `make:mac:arm64`, and `make:linux:x64`; run each on its matching
host platform after building the sidecar with the same architecture. Signing
and notarization are enabled only when the corresponding environment variables
in `forge.config.cjs` are present. On Windows paths containing non-ASCII
characters, the maker transparently uses an ASCII staging directory and copies
the completed artifacts back to `client/out`.
