package ai.printflow.kassa

import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.Inet4Address
import java.net.NetworkInterface
import java.net.URL
import java.util.concurrent.Callable
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * «Жив ли сервер» и «где он в сети».
 *
 * Адрес сервера в LAN — единственное, что в кассе реально способно всё сломать:
 * роутер выдал ноутбуку новый IP, и телефон «не видит кассу». Поэтому здесь две
 * вещи: короткая проверка сохранённого адреса (1,6 с — кассир не должен ждать)
 * и поиск по своей /24 (48 сокетов, по 0,7 с на адрес, общий потолок ~4 с).
 */
object Net {

    /** version из `/api/health`; null — сервер не ответил или это не PrintFlow. */
    fun probe(base: String, timeoutMs: Int = 1600): String? {
        val root = normalize(base) ?: return null
        val body = get("$root/api/health", timeoutMs) ?: return null
        return try {
            JSONObject(body).optString("version", "").ifEmpty { "ok" }
        } catch (_: Exception) {
            null
        }
    }

    /** Ответ JSON-маршрута (`/api/app/android`) или null. */
    fun json(base: String, path: String, timeoutMs: Int = 2000): JSONObject? {
        val root = normalize(base) ?: return null
        val body = get("$root$path", timeoutMs) ?: return null
        return try {
            JSONObject(body)
        } catch (_: Exception) {
            null
        }
    }

    /** http(s)://host:port — без хвоста пути: адрес сервера храним «корнем». */
    fun normalize(raw: String): String? {
        var value = raw.trim()
        if (value.isEmpty()) return null
        if (!value.contains("://")) value = "http://$value"
        val schemeEnd = value.indexOf("://")
        if (schemeEnd < 0) return null
        val host = value.substringAfter("://").substringBefore('/')
        if (host.isBlank() || host.any { it.isWhitespace() }) return null
        val slash = value.indexOf('/', schemeEnd + 3)
        return if (slash < 0) value.trimEnd('/') else value.substring(0, slash)
    }

    private fun get(url: String, timeoutMs: Int): String? {
        val conn = try {
            (URL(url).openConnection() as? HttpURLConnection)?.also {
                it.connectTimeout = timeoutMs
                it.readTimeout = timeoutMs
                it.requestMethod = "GET"
                it.setRequestProperty("Accept", "application/json")
                it.useCaches = false
                it.instanceFollowRedirects = true
            }
        } catch (_: Exception) {
            return null
        } ?: return null
        return try {
            if (conn.responseCode != 200) return null
            conn.inputStream.bufferedReader(Charsets.UTF_8).use { it.readText() }
        } catch (_: Exception) {
            null
        } finally {
            runCatching { conn.disconnect() }
        }
    }

    /** IPv4 активного интерфейса (обычно wlan0) — от него считаем /24. */
    fun localV4(): String? {
        return try {
            val interfaces = NetworkInterface.getNetworkInterfaces()
            while (interfaces != null && interfaces.hasMoreElements()) {
                val net = interfaces.nextElement()
                if (!net.isUp || net.isLoopback) continue
                val addresses = net.inetAddresses
                while (addresses.hasMoreElements()) {
                    val addr = addresses.nextElement()
                    if (addr is Inet4Address && !addr.isLoopbackAddress && !addr.isLinkLocalAddress) {
                        val host = addr.hostAddress ?: continue
                        if (host.count { it == '.' } == 3) return host
                    }
                }
            }
            null
        } catch (_: Exception) {
            null
        }
    }

    /**
     * Обход своей /24: «base → version» для всех, где откликнулся PrintFlow.
     * Порты — типичные для коннектора: находят и установку с другим портом.
     */
    fun scan(ports: IntArray = intArrayOf(8765, 8766, 8080), timeoutMs: Int = 700,
             onFound: ((Int) -> Unit)? = null): List<Pair<String, String>> {
        val local = localV4() ?: return emptyList()
        val head = local.substringBeforeLast(".", "")
        if (head.isEmpty()) return emptyList()
        val targets = ArrayList<String>()
        for (port in ports) {
            for (host in 1..254) targets.add("http://$head.$host:$port")
        }
        val found = ConcurrentHashMap<String, String>()
        val pool = Executors.newFixedThreadPool(48)
        return try {
            // invokeAll принимает только Callable — с Runnable компилятор
            // отказывает по типу. Оборачиваем задачу явным объектом: ни вывода
            // типов, ни SAM-неоднозначности, поведение прежнее — ждём все
            // адреса, но не дольше общего потолка.
            val jobs: List<Callable<Unit>> = targets.map { base ->
                object : Callable<Unit> {
                    override fun call(): Unit {
                        probe(base, timeoutMs)?.let { version ->
                            found[base] = version
                            onFound?.invoke(found.size)
                        }
                    }
                }
            }
            runCatching { pool.invokeAll(jobs, timeoutMs * 6L, TimeUnit.MILLISECONDS) }
            found.entries.sortedWith(compareBy({ portOf(it.key) }, { netKey(it.key) }))
                .map { it.key to it.value }
        } finally {
            pool.shutdownNow()
        }
    }

    private fun portOf(base: String): Int =
        base.substringAfterLast(':').substringBefore('/').toIntOrNull() ?: 0

    /** Сортировка адреса по числам октетов: 192.168.1.9 раньше 192.168.1.10. */
    private fun netKey(base: String): String {
        val host = base.substringAfter("//").substringBeforeLast(':')
        return host.split('.').joinToString(".") { (it.toIntOrNull() ?: 0).toString().padStart(3, '0') }
    }
}
