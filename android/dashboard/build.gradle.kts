plugins {
    id("com.android.application")
}

// Desk Agent Dashboard: a small separate app that shows Desk Agent's Dashboard, chats and
// schedules. Android keeps each app's data private, so it reads them through Desk Agent's
// remote control (on this phone at 127.0.0.1, or another phone on the same Wi-Fi).
// Its page is Desk Agent's own remote.html, copied in by the build (see build-dashboard.yml).
// The signing key comes from the build, never from the repo.
fun env(name: String) = System.getenv(name) ?: error("$name is not set")

android {
    namespace = "uk.deskagent.dashboard"
    compileSdk = 35

    defaultConfig {
        applicationId = "uk.deskagent.dashboard"
        minSdk = 29
        targetSdk = 34
        versionCode = (System.getenv("GITHUB_RUN_NUMBER") ?: "1").toInt()
        versionName = "1.0." + (System.getenv("GITHUB_RUN_NUMBER") ?: "0")
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

    lint { checkReleaseBuilds = false }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}
