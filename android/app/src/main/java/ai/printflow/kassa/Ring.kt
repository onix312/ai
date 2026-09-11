package ai.printflow.kassa

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.media.AudioAttributes
import android.media.RingtoneManager
import android.net.Uri
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager

/**
 * «Звонок о платеже» на уровне системы.
 *
 * Страница умеет пищать сама (WebAudio) — но только пока экран включён и
 * WebView жив. Уведомление и вибрация от оболочки работают и когда телефон
 * лежит в кармане: кассир слышит, что деньги пришли, а не вспоминает через
 * десять минут. Никакой магии: канал + вибрация + системный звук.
 */
class Ring(private val context: Context) {

    private val manager: NotificationManager?
        get() = context.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager

    fun ensureChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val nm = manager ?: return
        val sound: Uri? = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION)
        val audio = Notification.AudioAttributes().apply {
            setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
            if (sound != null) setSound(sound)
        }
        nm.createNotificationChannel(NotificationChannel(
            CHANNEL_PAY, "Платежи на кассе", NotificationManager.IMPORTANCE_HIGH
        ).apply {
            description = "Подтверждение оплаты и поступления из банка"
            setSound(sound, audio)
            enableVibration(true)
        })
        nm.createNotificationChannel(NotificationChannel(
            CHANNEL_SOFT, "Касса: фон", NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = "Служебные сообщения оболочки: обновление, сервер, " +
                "фоновый слух платежей"
        })
    }

    /** kind = "loud" (нужен взгляд кассира) | "soft" (деньги уже в журнале). */
    fun fire(kind: String, text: String, title: String = "Касса NOZZA") {
        val loud = kind != "soft"
        vibrate(loud)
        val nm = manager ?: return
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val notification = Notification.Builder(context, if (loud) CHANNEL_PAY else CHANNEL_SOFT)
                .setContentTitle(title)
                .setContentText(text.ifBlank { "Платёж" })
                .setSmallIcon(android.R.drawable.ic_popup_reminder)
                .setAutoCancel(true)
                .build()
            nm.notify(NOTIFY_ID, notification)
        } else {
            @Suppress("DEPRECATION")
            val notification = Notification.Builder(context)
                .setContentTitle(title)
                .setContentText(text.ifBlank { "Платёж" })
                .setSmallIcon(android.R.drawable.ic_popup_reminder)
                .setTicker(text)
                .setAutoCancel(true)
                .build()
            @Suppress("DEPRECATION")
            nm.notify(NOTIFY_ID, notification)
        }
    }

    private fun vibrate(loud: Boolean) {
        val pattern = if (loud) longArrayOf(0, 170, 70, 170) else longArrayOf(0, 90)
        try {
            val vibrator = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                val vm = context.getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as? VibratorManager
                vm?.defaultVibrator
            } else {
                @Suppress("DEPRECATION")
                context.getSystemService(Context.VIBRATOR_SERVICE) as? Vibrator
            } ?: return
            if (!vibrator.hasVibrator()) return
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                vibrator.vibrate(VibrationEffect.createWaveform(pattern, -1))
            } else {
                @Suppress("DEPRECATION")
                vibrator.vibrate(pattern, -1)
            }
        } catch (_: Exception) {
            // вибрация не должна ломать кассу ни при каких обстоятельствах
        }
    }

    companion object {
        private const val CHANNEL_PAY = "printflow.payments"
        // Открыт намеренно: постоянным уведомлением его закрывает RingService.
        const val CHANNEL_SOFT = "printflow.status"
        private const val NOTIFY_ID = 1700
    }
}
