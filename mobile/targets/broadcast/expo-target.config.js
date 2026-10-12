/** @type {import('@bacons/apple-targets/app.plugin').ConfigFunction} */
module.exports = (config) => ({
  type: "broadcast-upload",
  name: "broadcast",
  displayName: "Asterisk",
  bundleIdentifier: ".broadcast",
  deploymentTarget: "17.0",
  frameworks: ["ReplayKit", "CoreMedia", "CoreVideo"],
  entitlements: {
    "com.apple.security.application-groups":
      config.ios.entitlements["com.apple.security.application-groups"],
  },
});
