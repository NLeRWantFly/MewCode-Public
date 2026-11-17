const { createHash } = require("node:crypto");
const fs = require("node:fs/promises");
const path = require("node:path");
const { api } = require("@electron-forge/core");

const clientDir = path.resolve(__dirname, "..");
const platform = process.argv[2] || process.platform;
const arch = process.argv[3] || process.arch;
const finalOut = path.join(clientDir, "out");

function asciiWindowsBuildDir() {
  const publicDir = process.env.PUBLIC || `${process.env.SystemDrive || "C:"}\\Users\\Public`;
  const fingerprint = createHash("sha256").update(clientDir).digest("hex").slice(0, 12);
  return path.join(publicDir, "MewCodeForge", fingerprint);
}

async function main() {
  const needsAsciiWorkaround = platform === "win32" && /[^\x00-\x7f]/.test(finalOut);
  const buildOut = process.env.MEWCODE_FORGE_OUT_DIR || (
    needsAsciiWorkaround ? asciiWindowsBuildDir() : finalOut
  );
  const usesWorkaround = path.resolve(buildOut) !== path.resolve(finalOut);

  if (usesWorkaround) {
    console.log(`Using ASCII Forge output directory: ${buildOut}`);
    await fs.rm(buildOut, { recursive: true, force: true });
  }

  try {
    if (!usesWorkaround) {
      await api.make({
        dir: clientDir,
        platform,
        arch,
        outDir: finalOut,
        interactive: true,
      });
      return;
    }

    // Electron Packager handles Unicode paths correctly, while Squirrel's
    // bundled resource editor does not. Package locally, then run only the
    // maker against an ASCII copy of the unpacked application.
    await api.package({
      dir: clientDir,
      platform,
      arch,
      outDir: finalOut,
      interactive: true,
    });
    const suffix = `-${platform}-${arch}`;
    const packagedDirs = (await fs.readdir(finalOut, { withFileTypes: true }))
      .filter((entry) => entry.isDirectory() && entry.name.endsWith(suffix));
    if (packagedDirs.length === 0) {
      throw new Error(`Forge did not create a packaged application in ${finalOut}`);
    }
    for (const entry of packagedDirs) {
      const packagedPath = path.join(finalOut, entry.name);
      await fs.cp(
        packagedPath,
        path.join(buildOut, entry.name),
        { recursive: true },
      );
    }
    await api.make({
      dir: clientDir,
      platform,
      arch,
      outDir: buildOut,
      skipPackage: true,
      interactive: true,
    });
    await fs.rm(path.join(finalOut, "make"), { recursive: true, force: true });
    await fs.cp(path.join(buildOut, "make"), path.join(finalOut, "make"), {
      recursive: true,
    });
    console.log(`Artifacts copied to: ${finalOut}`);
  } finally {
    if (usesWorkaround) await fs.rm(buildOut, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
