plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

val briocheVersionName: String = (findProperty("briocheVersion") as String?) ?: "1.2.0"
val briocheVersionCode: Int = briocheVersionName
    .split(".")
    .mapNotNull { it.toIntOrNull() }
    .let { parts ->
        val major = parts.getOrElse(0) { 1 }
        val minor = parts.getOrElse(1) { 0 }
        val patch = parts.getOrElse(2) { 0 }
        major * 10000 + minor * 100 + patch
    }

android {
    namespace = "com.brioche.android"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.brioche.android"
        minSdk = 26
        targetSdk = 34
        versionCode = briocheVersionCode
        versionName = briocheVersionName
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    signingConfigs {
        create("release") {
            val ksPath = System.getenv("BRIOCHE_ANDROID_KEYSTORE")
            if (!ksPath.isNullOrBlank() && file(ksPath).exists()) {
                storeFile = file(ksPath)
                storePassword = System.getenv("BRIOCHE_ANDROID_KEYSTORE_PASS") ?: "brioche123"
                keyAlias = System.getenv("BRIOCHE_ANDROID_KEY_ALIAS") ?: "brioche"
                keyPassword = System.getenv("BRIOCHE_ANDROID_KEY_PASS") ?: "brioche123"
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            val ksPath = System.getenv("BRIOCHE_ANDROID_KEYSTORE")
            signingConfig = if (!ksPath.isNullOrBlank() && file(ksPath).exists()) {
                signingConfigs.getByName("release")
            } else {
                signingConfigs.getByName("debug")
            }
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    testOptions {
        unitTests.isReturnDefaultValues = true
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("androidx.recyclerview:recyclerview:1.3.2")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
}
