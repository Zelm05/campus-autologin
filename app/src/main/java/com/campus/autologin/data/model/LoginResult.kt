package com.campus.autologin.data.model

/**
 * Result of a single login / logout attempt.
 */
sealed interface LoginResult {
    val message: String

    data class Success(
        override val message: String,
        val preview: String = ""
    ) : LoginResult

    data class Failure(
        override val message: String,
        val preview: String = ""
    ) : LoginResult

    data class Error(
        override val message: String,
        val cause: Throwable? = null
    ) : LoginResult
}
