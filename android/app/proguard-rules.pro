# R8 renames and strips the release build. Worth being clear about what that does and does not
# buy: an APK runs on someone else's device, so anyone determined can decompile it. Obfuscation
# raises the cost of reading the logic; it is not a secret-keeping mechanism. Nothing in this app
# is a secret anyway - the OAuth client id is public by design in the device flow, and the token
# lives in the Android Keystore on the phone that earned it.

-dontwarn okhttp3.**
-dontwarn okio.**
-keepattributes Signature

# WorkManager builds the backup worker by name through reflection. androidx.work ships a rule for
# ListenableWorker subclasses, but this one matters enough to pin explicitly: if R8 renames it,
# the nightly backup silently stops happening.
-keep class com.harshiv.githubdrive.transfer.AutoUpload$Worker {
    public <init>(android.content.Context, androidx.work.WorkerParameters);
}

# Line numbers are deliberately not kept. Crash traces from the release build are unreadable
# without the mapping file, which the build uploads alongside the APK.
