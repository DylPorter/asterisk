plugins {
    id("com.android.application")
}

android {
    namespace = "dev.tdporter.shortcheck"
    compileSdk = 37

    defaultConfig {
        applicationId = "dev.tdporter.shortcheck"
        minSdk = 26
        targetSdk = 36
        versionCode = 1
        versionName = "0.1"
        // Set at build time so the APK isn't pinned to one machine.
        buildConfigField("String", "SERVER", "\"http://100.82.117.61:8000\"")
    }

    buildFeatures { buildConfig = true }
    buildTypes {
        debug {
            // Emulator smoke test only: skip the per-UID match so any app's
            // media audio is captured. Off unless asked for explicitly
            // (./gradlew -PcaptureAllApps=true ...), even in debug, because a
            // debug build on a real phone should still only hear the 3 apps.
            val all = (project.findProperty("captureAllApps") as String?)?.toBoolean() ?: false
            buildConfigField("boolean", "CAPTURE_ALL_APPS", all.toString())
            // Logs the Instagram view tree so the unverified Reels heuristics
            // can be fixed against a real device. Never in release.
            buildConfigField("boolean", "DUMP_VIEW_TREE", "true")
        }
        release {
            isMinifyEnabled = false
            buildConfigField("boolean", "CAPTURE_ALL_APPS", "false")
            buildConfigField("boolean", "DUMP_VIEW_TREE", "false")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")
    implementation("androidx.core:core-ktx:1.18.0")
    testImplementation("junit:junit:4.13.2")
}
