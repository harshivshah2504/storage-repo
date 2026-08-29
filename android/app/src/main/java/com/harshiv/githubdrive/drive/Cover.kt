package com.harshiv.githubdrive.drive

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.media.ExifInterface
import android.net.Uri
import java.io.ByteArrayOutputStream
import kotlin.math.max
import kotlin.math.min

/**
 * Builds the `_cover.jpg` thumbnail an archive shows in the grid.
 *
 * The web app produces this three different ways already (browser canvas, Pillow, ffmpeg) and every
 * reader just fetches the asset and displays it, so the bytes are deliberately not part of the
 * interoperable format. What matters is: 480x480, centre-cropped, JPEG.
 */
object Cover {

    const val SIZE = 480
    private const val QUALITY = 80

    fun buildJpeg(context: Context, uri: Uri): ByteArray? = try {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        context.contentResolver.openInputStream(uri)?.use { BitmapFactory.decodeStream(it, null, bounds) }

        if (bounds.outWidth <= 0 || bounds.outHeight <= 0) {
            null
        } else {
            val options = BitmapFactory.Options().apply {
                inSampleSize = sampleSizeFor(bounds.outWidth, bounds.outHeight)
            }
            val decoded = context.contentResolver.openInputStream(uri)?.use {
                BitmapFactory.decodeStream(it, null, options)
            }
            decoded?.let { bitmap ->
                val oriented = applyExif(context, uri, bitmap)
                val square = centreCrop(oriented)
                val scaled = Bitmap.createScaledBitmap(square, SIZE, SIZE, true)
                val out = ByteArrayOutputStream()
                scaled.compress(Bitmap.CompressFormat.JPEG, QUALITY, out)
                if (scaled != square) square.recycle()
                if (oriented != bitmap) bitmap.recycle()
                out.toByteArray()
            }
        }
    } catch (e: Throwable) {
        // A cover is cosmetic; never fail an upload over it.
        null
    }

    private fun sampleSizeFor(width: Int, height: Int): Int {
        var sample = 1
        val target = SIZE * 2
        while (min(width, height) / (sample * 2) >= target) sample *= 2
        return sample
    }

    private fun centreCrop(bitmap: Bitmap): Bitmap {
        val side = min(bitmap.width, bitmap.height)
        val x = max(0, (bitmap.width - side) / 2)
        val y = max(0, (bitmap.height - side) / 2)
        if (side == bitmap.width && side == bitmap.height) return bitmap
        return Bitmap.createBitmap(bitmap, x, y, side, side)
    }

    private fun applyExif(context: Context, uri: Uri, bitmap: Bitmap): Bitmap = try {
        val orientation = context.contentResolver.openInputStream(uri)?.use { input ->
            ExifInterface(input).getAttributeInt(
                ExifInterface.TAG_ORIENTATION,
                ExifInterface.ORIENTATION_NORMAL
            )
        } ?: ExifInterface.ORIENTATION_NORMAL

        val degrees = when (orientation) {
            ExifInterface.ORIENTATION_ROTATE_90 -> 90f
            ExifInterface.ORIENTATION_ROTATE_180 -> 180f
            ExifInterface.ORIENTATION_ROTATE_270 -> 270f
            else -> 0f
        }
        if (degrees == 0f) {
            bitmap
        } else {
            val matrix = android.graphics.Matrix().apply { postRotate(degrees) }
            Bitmap.createBitmap(bitmap, 0, 0, bitmap.width, bitmap.height, matrix, true)
        }
    } catch (e: Throwable) {
        bitmap
    }
}
