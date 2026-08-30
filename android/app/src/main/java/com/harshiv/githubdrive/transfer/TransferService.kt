package com.harshiv.githubdrive.transfer

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import com.harshiv.githubdrive.MainActivity
import com.harshiv.githubdrive.R
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * Keeps the process alive and shows progress while transfers run.
 *
 * The work itself lives in [TransferManager]; this service only holds the foreground notification,
 * which is what stops Android from freezing a long upload the moment the app is backgrounded.
 */
class TransferService : Service() {

    private var ticker: Job? = null
    private val scope = CoroutineScope(Dispatchers.Main)

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForeground(NOTIFICATION_ID, buildNotification())
        if (ticker == null) {
            ticker = scope.launch {
                while (true) {
                    delay(1000L)
                    if (!TransferManager.hasActive) {
                        stopSelf()
                        break
                    }
                    notificationManager().notify(NOTIFICATION_ID, buildNotification())
                }
            }
        }
        return START_NOT_STICKY
    }

    /** Android 15 stops a dataSync service after 6 hours; leaving it running is an ANR. */
    override fun onTimeout(startId: Int, fgsType: Int) {
        ticker?.cancel()
        ticker = null
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf(startId)
    }

    override fun onDestroy() {
        ticker?.cancel()
        ticker = null
        super.onDestroy()
    }

    private fun buildNotification(): Notification {
        val open = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
            },
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val percent = TransferManager.activeFractionPercent()
        val builder = Notification.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(TransferManager.activeSummary())
            .setSmallIcon(android.R.drawable.stat_sys_upload)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(open)
            .setProgress(100, percent, percent <= 0)

        return builder.build()
    }

    private fun notificationManager(): NotificationManager =
        getSystemService(NOTIFICATION_SERVICE) as NotificationManager

    private fun createChannel() = ensureChannel(this)

    companion object {
        const val CHANNEL_ID = "gd_transfers"
        private const val NOTIFICATION_ID = 4210

        /** Idempotent, and shared with the gallery backup, which posts on the same channel. */
        fun ensureChannel(context: Context) {
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
            val channel = NotificationChannel(
                CHANNEL_ID,
                context.getString(R.string.transfer_channel_name),
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = context.getString(R.string.transfer_channel_desc)
                setShowBadge(false)
            }
            val manager = context.getSystemService(NOTIFICATION_SERVICE) as NotificationManager
            manager.createNotificationChannel(channel)
        }
    }
}
