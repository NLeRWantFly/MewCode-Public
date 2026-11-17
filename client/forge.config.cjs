const path = require("node:path");

module.exports = {
  packagerConfig: {
    asar: true,
    name: "MewCode",
    executableName: "liteagent",
    electronZipDir: process.env.MEWCODE_ELECTRON_ZIP_DIR || undefined,
    // Keep local build artifacts and runtime state out of the application ASAR.
    // The freshly built backend is copied separately via extraResource below.
    ignore: [
      /^\/(?:out(?:-[^/]+)?|build|dist|\.mewcode)(?:\/|$)/,
    ],
    extraResource: [
      path.join(__dirname, "dist", "backend"),
    ],
    osxSign: process.env.MEWCODE_OSX_SIGN_IDENTITY
      ? { identity: process.env.MEWCODE_OSX_SIGN_IDENTITY }
      : undefined,
    osxNotarize: process.env.MEWCODE_APPLE_ID
      ? {
          appleId: process.env.MEWCODE_APPLE_ID,
          appleIdPassword: process.env.MEWCODE_APPLE_PASSWORD,
          teamId: process.env.MEWCODE_APPLE_TEAM_ID,
        }
      : undefined,
  },
  rebuildConfig: {},
  makers: [
    {
      name: "@electron-forge/maker-squirrel",
      config: { name: "liteagent", authors: "MewCode" },
      platforms: ["win32"],
    },
    { name: "@electron-forge/maker-zip", platforms: ["darwin"] },
    { name: "@electron-forge/maker-deb", config: {}, platforms: ["linux"] },
    { name: "@electron-forge/maker-rpm", config: {}, platforms: ["linux"] },
  ],
};
