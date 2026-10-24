plugins {
    id("com.android.application")
}

// The signing key is never stored in the repo. The build passes it in through
// environment variables (see .github/workflows/build-apk.yml).
fun env(name: String) = System.getenv(name) ?: error("$name is not set")

android {
    namespace = "uk.deskagent.mobile"
    compileSdk = 35

    defaultConfig {
        applicationId = "uk.deskagent.mobile"
        minSdk = 29
        targetSdk = 35
        versionCode = (System.getenv("GITHUB_RUN_NUMBER") ?: "1").toInt()
        versionName = "1.0." + (System.getenv("GITHUB_RUN_NUMBER") ?: "0")
        ndk { abiFilters += "arm64-v8a" }
    }

    signingConfigs {
        create("release") {
            storeFile = file(env("SIGNING_STORE_FILE"))
            storePassword = env("SIGNING_PASSWORD")
            keyAlias = "deskagent"
            keyPassword = env("SIGNING_PASSWORD")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("release")
        }
    }

    // The AI engine is a program, not a library. Android only lets apps run programs
    // from the native library folder, so it ships as lib*.so and must be unpacked there.
    packaging {
        jniLibs {
            useLegacyPackaging = true
            // A model packed in as libmodel_*.so is data, not code: don't try to strip it.
            keepDebugSymbols += "**/libmodel_*.so"
        }
    }

    // Release builds normally stop on lint warnings; this app is checked by its own tests instead.
    lint { checkReleaseBuilds = false }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}
