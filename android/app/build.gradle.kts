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
    buildTypes { release { isMinifyEnabled = false } }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")
    implementation("androidx.core:core-ktx:1.18.0")
}
