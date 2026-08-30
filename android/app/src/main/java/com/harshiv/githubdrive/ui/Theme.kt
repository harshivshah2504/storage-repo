package com.harshiv.githubdrive.ui

import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color

/**
 * A near-monochrome palette, dark whatever the phone is set to.
 *
 * Ink is the accent, inverted: white type, white buttons with black labels, so the one coloured
 * thing on any screen is the person's own content. Dynamic colour is deliberately not used -
 * letting the wallpaper repaint the app was what put grey cards behind black text.
 */
private val DarkColors = darkColorScheme(
    primary = Color(0xFFF2F4F8),
    onPrimary = Color(0xFF14161A),
    primaryContainer = Color(0xFFF2F4F8),
    onPrimaryContainer = Color(0xFF14161A),
    secondary = Color(0xFF9BA3AF),
    onSecondary = Color(0xFF14161A),
    background = Color(0xFF0B0D10),
    onBackground = Color(0xFFE9ECF1),
    surface = Color(0xFF14171C),
    onSurface = Color(0xFFE9ECF1),
    surfaceVariant = Color(0xFF1E222A),
    onSurfaceVariant = Color(0xFF9BA3AF),
    outline = Color(0xFF2A2F39),
    outlineVariant = Color(0xFF232830),
    error = Color(0xFFF2B8B5),
    onError = Color(0xFF14161A)
)

/**
 * The Surface is not decoration.
 *
 * MaterialTheme sets the colour scheme but not `LocalContentColor` - Surface is what does that -
 * so any Text that does not name a colour of its own falls back to Compose's default black. On a
 * screen built from a bare Column, that is invisible type. Wrapping here fixes it for every screen
 * at once rather than one forgotten Text at a time.
 */
@Composable
fun MemVaultTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = DarkColors) {
        Surface(
            modifier = Modifier.fillMaxSize(),
            color = MaterialTheme.colorScheme.background,
            contentColor = MaterialTheme.colorScheme.onBackground,
            content = content
        )
    }
}
