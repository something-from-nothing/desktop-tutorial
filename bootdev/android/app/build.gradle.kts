plugins {
    id("com.android.application")
}

// Boot.dev Dashboard for Android. The page (dashboard.html) and the boot.dev reader (reader.js)
// are shared with the Windows app, straight from ../../web. The signing key comes from the
// build (GitHub Actions cache or secrets), never from the repo.
fun env(name: String) = System.getenv(name) ?: error("$name is not set")

android {
    namespace = "dev.pete.bootdash"
    compileSdk = 35

    defaultConfig {
        applicationId = "dev.pete.bootdash"
        minSdk = 29
        targetSdk = 34
        versionCode = (System.getenv("GITHUB_RUN_NUMBER") ?: "1").toInt()
        versionName = "1.0." + (System.getenv("GITHUB_RUN_NUMBER") ?: "0")
    }

    sourceSets {
        getByName("main") {
            assets.srcDirs("../../web")
        }
    }

    signingConfigs {
        create("release") {
            storeFile = file(env("SIGNING_STORE_FILE"))
            storePassword = env("SIGNING_PASSWORD")
            keyAlias = "bootdash"
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

dependencies {
    implementation("androidx.webkit:webkit:1.12.1")
}
