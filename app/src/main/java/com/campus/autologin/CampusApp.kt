package com.campus.autologin

import android.app.Application
import com.campus.autologin.data.repository.AuthRepository

/**
 * Holds process-wide singletons (the repository builds the encrypted prefs
 * and the OkHttp client once).
 */
class CampusApp : Application() {

    val repository by lazy { AuthRepository(this) }

    override fun onCreate() {
        super.onCreate()
    }
}
