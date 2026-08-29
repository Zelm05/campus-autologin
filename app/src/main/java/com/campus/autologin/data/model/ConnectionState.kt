package com.campus.autologin.data.model

/**
 * Coarse network state used by the UI and the background monitor.
 */
enum class ConnectionState {
    UNKNOWN,
    OFFLINE,    // no active network
    ONLINE,     // internet reachable, no login needed
    CAPTIVE     // behind a login / captive portal
}
