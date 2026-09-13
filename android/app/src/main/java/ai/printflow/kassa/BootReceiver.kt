package ai.printflow.kassa

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Касса открывается сама после перезагрузки телефона (17.0.16).

 *  Утром магазин включает телефон вместе со светом, а кассир в потоке не
 *  вспоминает, что приложение надо поднять руками: продажи уходят в
 *  офлайн-очередь страницы, пока касса не открыта. Настройка живёт в тех же
 *  prefs, что и остальные («Открывать кассу при включении телефона»).
 *
 *  Ограничение системы, а не кода: на чистом Android 10+ фоновый запуск
 *  экрана разрешён не всегда — если телефон не открыл приложение, система
 *  может показать уведомление вместо окна. На кассовых телефонах с
 *  разрешением «показывать поверх других окон» и на большинстве прошивок
 *  магазинных устройств запуск проходит. Поэтому флаг можно выключить.
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action
        if (action != Intent.ACTION_BOOT_COMPLETED
            && action != Intent.ACTION_LOCKED_BOOT_COMPLETED
            && action != "android.intent.action.QUICKBOOT_POWERON"
        ) return
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        if (!prefs.getBoolean(KEY_BOOT, true)) return
        val launch = Intent(context, MainActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        // Не роняем приём широковещательного сообщения: если система не дала
        // открыть окно, телефон всё равно останется рабочим.
        runCatching { context.startActivity(launch) }
    }

    private companion object {
        /** Тот же файл настроек, что у MainActivity: «kassa_shell». */
        const val PREFS = "kassa_shell"
        const val KEY_BOOT = "boot_start"
    }
}
