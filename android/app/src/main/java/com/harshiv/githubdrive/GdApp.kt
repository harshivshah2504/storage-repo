package com.harshiv.githubdrive

import android.app.Application
import com.harshiv.githubdrive.core.Prefs
import com.harshiv.githubdrive.transfer.AutoUpload

class GdApp : Application() {
    val prefs: Prefs by lazy { Prefs(this) }

    override fun onCreate() {
        super.onCreate()
        // Re-registers the gallery backup after a reboot or an update, and cancels it if the
        // setting was turned off while the app was not running.
        AutoUpload.sync(this)
    }
}
