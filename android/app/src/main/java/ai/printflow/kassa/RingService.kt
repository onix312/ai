package ai.printflow.kassa

import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.TimeUnit

/**
 * Фоновый слух кассы: читает `/api/stream`, даже когда экран погашен.
 *
 * Страница умеет звенить сама — но только пока WebView жив и экран включён.
 * На прилавке телефон обычно лежит экраном вниз или лежит в кармане, а
 * «платёж пришёл» должно быть слышно именно тогда. Фоновое соединение
 * обычного Android-кода система режет через несколько минут, поэтому нужен
 * foreground-сервис с постоянным уведомлением: такое соединение держат.
 *
 * Чего здесь сознательно нет:
 * - никаких денег: сервис не подтверждает платежи и не проводит продажи;
 * - никакой очереди офлайн-продаж — она живёт в WebView и выгружается
 *   страницей, потому что только у страницы есть доступ к localStorage;
 * - двойного звонка: пока активность на переднем плане, событием занимается
 *   страница, сервис только обновляет строчку в своём уведомлении.
 *
 * Сколько продаж ещё не доехало до сервера, страница сообщает через мостик
 * `window.PfApp.offline(n)` — так в кармане видно не только «деньги пришли»,
 * но и «N не проведено».
 */
class RingService : Service() {

    private var current: String = ""
    private var worker: Thread? = null
    @Volatile private var stopped = false
    @Volatile private var started = false
    private lateinit var ring: Ring

    // ---------------------------------------------------------- жизненный цикл
    override fun onCreate() {
        super.onCreate()
        ring = Ring(this).apply { ensureChannel() }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            shutdown()
            return START_NOT_STICKY
        }
        val base = Net.normalize(intent?.getStringExtra(EXTRA_BASE).orEmpty())
            .orEmpty()
        if (base.isBlank() || !backgroundRing()) {
            shutdown()
            return START_NOT_STICKY
        }
        if (!started) {
            started = true
            postNotification("Подключаюсь к потоку платежей…")
        }
        if (base != current) {
            current = base
            restart()
        } else if (worker?.isAlive != true) {
            start()
        }
        // START_STICKY: памяти не хватило и нас убили — система поднимет сама.
        // Intent при этом null, поэтому адрес в start() читаем из prefs.
        return START_STICKY
    }

    override fun onDestroy() {
        shutdown()
        super.onDestroy()
    }

    private fun shutdown() {
        stopped = true
        started = false
        worker?.let { runCatching { it.interrupt() } }
        worker = null
        runCatching { stopForeground(STOP_FOREGROUND_REMOVE) }
    }

    private fun restart() {
        stopped = true
        worker?.let { runCatching { it.interrupt() } }
        worker = null
        start()
    }

    private fun start() {
        if (current.isBlank()) {
            current = Net.normalize(getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .getString(KEY_URL, "").orEmpty()).orEmpty()
        }
        if (current.isBlank()) return
        stopped = false
        worker = Thread({ listen() }, "pf-ring").apply { isDaemon = true; start() }
    }

    private fun backgroundRing(): Boolean =
        getSharedPreferences(PREFS, Context.MODE_PRIVATE).getBoolean(KEY_RING_BG, true)

    // ---------------------------------------------------------------- слушатель
    /**
     * Поток читаем как есть: `event: <вид>`, `data: <json>`, пустая строка.
     * Сервер держит соединение живым ping'ом раз в 20 с, поэтому конец потока =
     * обрыв: переподключаемся с нарастающей паузой 3 → 30 с. Сервер мог уйти на
     * перезагрузку или сменить IP после перезарузки роутера — в обоих случаях
     * молчаливый retry лучше любого уведомления кассиру.
     */
    private fun listen() {
        val wake = runCatching {
            (getSystemService(Context.POWER_SERVICE) as? PowerManager)
                ?.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "kassa:ring")
        }.getOrNull()
        wake?.setReferenceCounted(false)
        var pause = 3_000L
        while (!stopped) {
            val base = current
            if (base.isBlank()) break
            var conn: HttpURLConnection? = null
            try {
                conn = (URL("$base/api/stream").openConnection() as? HttpURLConnection)?.apply {
                    connectTimeout = 6_000
                    readTimeout = 75_000          // три ping'а тишины — считаем, что связи нет
                    requestMethod = "GET"
                    setRequestProperty("Accept", "text/event-stream")
                    setRequestProperty("Cache-Control", "no-store")
                    useCaches = false
                }
                if (conn != null && conn.responseCode == 200) {
                    pause = 3_000L
                    note("Слушаю платежи · " + hostOf(base))
                    conn.inputStream.bufferedReader(Charsets.UTF_8).use { reader ->
                        var kind = ""
                        while (!stopped) {
                            val line = reader.readLine() ?: break
                            // Любая строка (даже ping) продлевает частичную
                            // блокировку: иначе на уснувшем телефоне событие
                            // доживёт только до утра.
                            runCatching { wake?.acquire(TimeUnit.MINUTES.toMillis(5)) }
                            when {
                                line.startsWith("event:") -> kind = line.substringAfter(':').trim()
                                line.startsWith("data:") -> handle(kind, line.substringAfter(':').trim())
                                else -> Unit
                            }
                        }
                    }
                } else {
                    note("Сервер не отвечает (" + (conn?.responseCode ?: 0) + ") — пробую снова")
                }
            } catch (_: Exception) {
                if (!stopped) note("Нет связи с сервером — переподключаюсь")
            } finally {
                runCatching { conn?.disconnect() }
            }
            if (stopped) break
            try {
                Thread.sleep(pause)
            } catch (_: InterruptedException) {
                break
            }
            pause = minOf(pause * 2, 30_000L)
        }
        runCatching { if (wake?.isHeld == true) wake.release() }
    }

    /** Одно денежное событие — один звонок. Остальные виды событий не трогаем. */
    private fun handle(kind: String, payload: String) {
        if (kind != "event" || payload.isEmpty()) return
        val row = try { JSONObject(payload) } catch (_: Exception) { return }
        val data = row.optJSONObject("data") ?: return
        val signal = data.optString("signal")
        val loud = signal == "client_claim" || signal == "bank_matched"
        if (!loud && signal != "money_in") return
        val key = (if (signal == "bank_matched") "r-" else "p-") +
            data.optString("payment_id").ifEmpty { data.optString("receipt_id") }
                .ifEmpty { row.optString("at") }
        if (!fresh(key)) return
        val amount = if (data.optDouble("amount", 0.0) > 0) " " + rub(data.getDouble("amount")) else ""
        val order = data.optString("order_number").let { if (it.isBlank()) "" else " заказ $it" }
        val text = when (signal) {
            "bank_matched" -> "Банк видит приход$amount — сверь и подтверди"
            "money_in" -> "Деньги в журнале$amount$order — можно выдавать"
            else -> "Клиент сообщил об оплате$amount$order — проверь поступление"
        }
        if (foreground) {
            // Страница на экране: звенит она, иначе было бы два звука на одно
            // событие. Здесь только строка состояния в уведомлении сервиса.
            note(text)
            return
        }
        val pending = pendingOffline
        ring.fire(if (loud) "loud" else "soft",
            if (pending > 0) "$text · не проведено: $pending" else text)
    }

    private fun hostOf(base: String): String = base.substringAfter("://").substringBefore('/')

    // ---------------------------------------------------------------- уведомление
    private fun buildNotification(text: String): Notification {
        val flags = PendingIntent.FLAG_UPDATE_CURRENT or
            (if (Build.VERSION.SDK_INT >= 23) PendingIntent.FLAG_IMMUTABLE else 0)
        val content = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), flags)
        val pending = pendingOffline
        val body = if (pending > 0) "$text · в очереди: $pending" else text
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, Ring.CHANNEL_SOFT)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }
        return builder
            .setContentTitle("Касса слушает платежи")
            .setContentText(body)
            // Та же иконка, что у звонка о платеже (Ring.kt): постоянное
            // уведомление «касса слушает» и звонок должны выглядеть одним
            // приложением, а не системным напоминанием.
            .setSmallIcon(R.drawable.ic_notify)
            .setOngoing(true)
            .setContentIntent(content)
            .build()
    }

    /** Первый раз — foreground (иначе Android убьёт сервис), дальше — только текст. */
    private fun postNotification(text: String) {
        val notification = buildNotification(text)
        runCatching {
            if (Build.VERSION.SDK_INT >= 34) {
                startForeground(NOTIFY_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
            } else {
                startForeground(NOTIFY_ID, notification)
            }
        }
    }

    private fun note(text: String) {
        if (!started) return
        runCatching {
            (getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager)
                ?.notify(NOTIFY_ID, buildNotification(text))
        }
    }

    // ---------------------------------------------------------------- дедупликация
    /**
     * SSE умеет отдать кадр повторно после переподключения, а START_STICKY —
     * поднять сервис с чистого листа. 200 последних id хватает на смену: больше
     * событий касса за день не видит, а памяти это копейки.
     */
    private fun fresh(key: String): Boolean = synchronized(seen) {
        if (seen.containsKey(key)) false
        else {
            seen[key] = Unit
            while (seen.size > SEEN_LIMIT) {
                val oldest = seen.keys.iterator()
                if (!oldest.hasNext()) break
                seen.remove(oldest.next())
            }
            true
        }
    }

    companion object {
        const val ACTION_STOP = "ai.printflow.kassa.STOP"
        private const val EXTRA_BASE = "base"
        private const val PREFS = "kassa_shell"
        private const val KEY_URL = "server_url"
        private const val KEY_RING_BG = "ring_background"
        private const val NOTIFY_ID = 1701
        private const val SEEN_LIMIT = 200

        /** Активность на переднем плане — звонок на странице, не здесь. */
        @Volatile @JvmStatic var foreground: Boolean = false

        /** Сколько офлайн-продаж ждут связи (страница знает точнее всех). */
        @Volatile @JvmStatic var pendingOffline: Int = 0

        private val seen = LinkedHashMap<String, Unit>()

        fun start(context: Context, base: String) {
            runCatching {
                context.startForegroundService(
                    Intent(context, RingService::class.java).putExtra(EXTRA_BASE, base))
            }
        }

        fun stop(context: Context) {
            runCatching {
                context.startService(Intent(context, RingService::class.java).setAction(ACTION_STOP))
            }
            runCatching { context.stopService(Intent(context, RingService::class.java)) }
        }

        fun offline(count: Int) {
            pendingOffline = if (count < 0) 0 else count
        }

        /** «1 250,5 ₽» — кассир читает деньги, а не научную нотацию. */
        private fun rub(value: Double): String {
            val cents = Math.round(value * 100)
            val whole = Math.abs(cents / 100)
            val frac = Math.abs(cents % 100)
            val grouped = whole.toString().reversed().chunked(3).joinToString(" ").reversed()
            val sign = if (cents < 0) "−" else ""
            val kopecks = if (frac > 0) "," + frac.toString().padStart(2, '0').trimEnd('0') else ""
            return sign + grouped + kopecks + " ₽"
        }
    }
}
