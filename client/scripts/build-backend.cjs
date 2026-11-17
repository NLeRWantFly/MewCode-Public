const { existsSync } = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const clientDir = path.resolve(__dirname, "..");
const repoDir = path.resolve(clientDir, "..");
const venvPython = process.platform === "win32"
  ? path.join(repoDir, ".venv", "Scripts", "python.exe")
  : path.join(repoDir, ".venv", "bin", "python");

const candidates = [
  process.env.MEWCODE_PYTHON,
  existsSync(venvPython) ? venvPython : undefined,
  process.platform === "win32" ? "python" : "python3",
].filter(Boolean);

const args = [
  "-m",
  "PyInstaller",
  path.join(clientDir, "mewcode-backend.spec"),
  "--distpath",
  path.join(clientDir, "dist"),
  "--workpath",
  path.join(clientDir, "build", "backend"),
  "--noconfirm",
];

let lastError;
for (const python of candidates) {
  const result = spawnSync(python, args, {
    cwd: clientDir,
    stdio: "inherit",
    windowsHide: true,
  });
  if (!result.error && result.status === 0) {
    process.exit(0);
  }
  if (result.error?.code !== "ENOENT") {
    process.exit(result.status ?? 1);
  }
  lastError = result.error;
}

console.error(
  "Unable to find Python with PyInstaller. Create .venv, install pyinstaller, " +
  "or set MEWCODE_PYTHON to a Python executable.",
);
if (lastError) console.error(lastError.message);
process.exit(1);
