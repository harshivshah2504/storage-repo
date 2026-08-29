package com.harshiv.githubdrive

import android.app.Application
import com.harshiv.githubdrive.core.Prefs

class GdApp : Application() {
    val prefs: Prefs by lazy { Prefs(this) }
}
