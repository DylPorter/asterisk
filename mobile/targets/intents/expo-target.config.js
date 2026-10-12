/** @type {import('@bacons/apple-targets/app.plugin').ConfigFunction} */
module.exports = (config) => ({
  type: "app-intent",
  name: "intents",
  displayName: "Asterisk Intents",
  bundleIdentifier: ".intents",
  // apple-targets pins app-intent targets to 17.0 regardless of this field;
  // the iOS 26 Visual Intelligence code is gated with @available instead.
  frameworks: ["AppIntents", "SwiftUI"],
  entitlements: {
    "com.apple.security.application-groups":
      config.ios.entitlements["com.apple.security.application-groups"],
  },
});
