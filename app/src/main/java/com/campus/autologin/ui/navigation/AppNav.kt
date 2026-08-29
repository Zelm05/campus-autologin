package com.campus.autologin.ui.navigation

import androidx.compose.runtime.Composable
import androidx.navigation.NavHostController
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import com.campus.autologin.ui.screen.AboutScreen
import com.campus.autologin.ui.screen.MainScreen
import com.campus.autologin.ui.screen.SettingsScreen

@Composable
fun AppRoot(navController: NavHostController = rememberNavController()) {
    NavHost(navController = navController, startDestination = "main") {
        composable("main") { MainScreen(navController) }
        composable("settings") { SettingsScreen(navController) }
        composable("about") { AboutScreen(navController) }
    }
}
