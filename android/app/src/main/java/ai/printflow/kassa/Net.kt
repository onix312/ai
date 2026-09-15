package ai.printflow.kassa

import org.json.JSONObject
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.HttpURLConnection
import java.net.Inet4Address
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.NetworkInterface
import java.net.Socket
import java.net.URL
import java.util.Locale
import java.util.concurrent.Callable
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * «Жив ли сервер» и «где он в сети».
 *
 * Адрес сервера в LAN — единственное, что в кассе реально способно всё сломать:
 * роутер выдал ноутбуку новый IP, и телефон «не видит кассу». Порядок действий
 * здесь выстроен от быстрого к медленному:
 *
 *  1. [probe] — короткая проверка сохранённого адреса (1,6 с, кассир не ждёт);
 *  2. [discover] — спросить «кто здесь?» по UDP (порт 8765): сервер PrintFlow
 *     отвечает адресом, портом и версией за десятые доли секунды. Именно так
 *     касса находит сервер, когда адрес сменился, а кассир ничего не вводил;
 *  3. [scan] — перебор своей /24, когда автопоиск запрещён (гостевая Wi-Fi-сеть
 *     с изоляцией клиентов, широковещание режет фаервол). Перебор идёт по всем
 *     сетевым интерфейсам и всем портам сразу: два порта из четырёх раньше не
 *     проверялись вовсе — до них не доживал общий таймаут.
 *
 * Чего здесь больше нет: единственного «своего» интерфейса. На телефоне с
 * мобильной сетью и Wi-Fi перебор уходил в GSM-подсеть оператора, где сервера
 * нет и быть не может, — поэтому интерфейсы сортируются (Wi-Fi первым), а
 * мобильные (rmnet/ccmni/pdp/wwan) из перебора исключаются.
 */
object Net {

    /**
     * Порты, по которым ищем сервер: 8765 — порт PrintFlow по умолчанию (он же
     * в подсказке на экране выбора сервера), остальные — установки, где порт
     * меняли при запуске или в автозапуске.
     */
    val PORTS = intArrayOf(8765, 8080, 8766, 8790)

    /** UDP-порт автопоиска: на него сервер отвечает «я тут». */
    const val DISCOVERY_PORT = 8765

    private const val CALL = "PRINTFLOW?"
    private const val PROTO = 1
    private const val APP = "printflow"
    /** TCP-пробник порта: на своей сети открытый порт отвечает мгновенно. */
    private const val CONNECT_TIMEOUT_MS = 260
    /** Потоков на перебор: 254 адреса × 4 порта должны укладываться в секунды. */
    private const val SWEEP_THREADS = 96
    /** Сколько подсетей обходим: телефон обычно в одной, вторая — запас. */
    private const val MAX_SUBNETS = 2

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

    /** IPv4 активного интерфейса (Wi-Fi первым) — от него считаем /24. */
    fun localV4(): String? {
        return try {
            val interfaces = NetworkInterface.getNetworkInterfaces()
            while (interfaces != null && interfaces.hasMoreElements()) {
                val net = interfaces.nextElement()
                if (!net.isUp || net.isLoopback || cellular(net.name)) continue
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
     * Префиксы подсетей этого телефона: «192.168.1» для 192.168.1.37.
     *
     * Wi-Fi и кабель — раньше мобильной сети и VPN: сервер PrintFlow стоит в
     * цехе или магазине, а не у оператора связи. Мобильные интерфейсы из
     * перебора исключаются целиком: обход подсети сотового оператора — это
     * 254 гарантированных таймаута и «сервер не найден».
     */
    fun subnets(): List<String> {
        val wifi = ArrayList<String>()
        val cable = ArrayList<String>()
        val other = ArrayList<String>()
        try {
            val interfaces = NetworkInterface.getNetworkInterfaces() ?: return emptyList()
            while (interfaces.hasMoreElements()) {
                val net = interfaces.nextElement()
                if (!net.isUp || net.isLoopback) continue
                val name = net.name.lowercase(Locale.ROOT)
                if (cellular(name)) continue
                for (address in net.interfaceAddresses) {
                    val addr = address.address
                    if (addr !is Inet4Address || addr.isLoopbackAddress || addr.isLinkLocalAddress) {
                        continue
                    }
                    val host = addr.hostAddress ?: continue
                    val parts = host.split(".")
                    if (parts.size != 4) continue
                    val prefix = parts[0] + "." + parts[1] + "." + parts[2]
                    if (prefix in wifi || prefix in cable || prefix in other) continue
                    when {
                        name.startsWith("wlan") || name.startsWith("ap") -> wifi.add(prefix)
                        name.startsWith("eth") || name.startsWith("en") || name.startsWith("usb") ->
                            cable.add(prefix)
                        else -> other.add(prefix)
                    }
                }
            }
        } catch (_: Exception) {
            return emptyList()
        }
        val ordered = ArrayList<String>()
        for (prefix in wifi + cable + other) {
            if (ordered.size >= MAX_SUBNETS) break
            ordered.add(prefix)
        }
        return ordered
    }

    private fun cellular(name: String): Boolean {
        val lower = name.lowercase(Locale.ROOT)
        for (marker in arrayOf("rmnet", "ccmni", "pdp", "wwan", "clat", "v4-rmnet")) {
            if (lower.startsWith(marker) || lower.contains(marker)) return true
        }
        return false
    }

    /** Широковещательные адреса: «вообще все» и по каждой своей подсети. */
    private fun broadcastTargets(): List<String> {
        val targets = ArrayList<String>()
        targets.add("255.255.255.255")
        for (prefix in subnets()) {
            val guest = "$prefix.255"
            if (guest !in targets) targets.add(guest)
        }
        // Подсеть телефона могла не попасть в [subnets] (только мобильная сеть) —
        // тогда спрашиваем хотя бы по адресу самого устройства.
        val local = localV4()
        if (local != null) {
            val guest = local.substringBeforeLast(".", "") + ".255"
            if (guest !in targets) targets.add(guest)
        }
        return targets
    }

    /**
     * Автопоиск сервера по UDP: «кто здесь?» → адрес, порт, версия.
     *
     * Ничего не вводить и не перебирать: сервер PrintFlow отвечает на запрос
     * сам, если он запущен и виден в сети. Пустой ответ — не ошибка: значит
     * широковещание в этой сети закрыто (гостевая Wi-Fi-сеть, изоляция
     * клиентов) и надо идти перебором адресов.
     */
    fun discover(timeoutMs: Int = 1200): List<Pair<String, String>> {
        val found = LinkedHashMap<String, String>()
        var socket: DatagramSocket? = null
        try {
            socket = openDatagram()
            val datagram = socket ?: return emptyList()
            datagram.broadcast = true
            datagram.soTimeout = 300
            val request = CALL.toByteArray(Charsets.US_ASCII)
            for (target in broadcastTargets()) {
                try {
                    val packet = DatagramPacket(request, request.size,
                        InetAddress.getByName(target), DISCOVERY_PORT)
                    datagram.send(packet)
                } catch (_: Exception) {
                    continue
                }
            }
            val deadline = System.currentTimeMillis() + timeoutMs
            val buffer = ByteArray(1024)
            while (System.currentTimeMillis() < deadline) {
                val packet = DatagramPacket(buffer, buffer.size)
                try {
                    datagram.receive(packet)
                } catch (_: Exception) {
                    continue
                }
                val base = beacon(packet) ?: continue
                found[base.first] = base.second
            }
        } catch (_: Exception) {
            // автопоиск — удобство, а не условие работы: молчим и идём перебором
        } finally {
            runCatching { socket?.close() }
        }
        return found.entries.map { it.key to it.value }
    }

    /**
     * UDP-сокет для автопоиска. Пытаемся занять порт маяка (8765) — тогда
     * слышны и вещательные датаграммы сервера; не вышло — берём любой
     * свободный: ответ на запрос придёт на него же.
     */
    private fun openDatagram(): DatagramSocket? {
        return try {
            DatagramSocket(DISCOVERY_PORT)
        } catch (_: Exception) {
            try {
                DatagramSocket(0)
            } catch (_: Exception) {
                null
            }
        }
    }

    /**
     * Автопоиск с проверкой: ответившие маяком адреса подтверждаем `/api/health`.
     *
     * Маяк сам по себе доказывает только то, что в сети кто-то говорит на нашем
     * протоколе. Касса подключается к живому серверу, поэтому перед показом
     * «нашёл» адрес проверяется тем же запросом, что и раньше.
     */
    fun discoverServers(timeoutMs: Int = 1200, confirmMs: Int = 1500): List<Pair<String, String>> {
        val verified = ArrayList<Pair<String, String>>()
        for (hit in discover(timeoutMs)) {
            val version = probe(hit.first, timeoutMs = confirmMs) ?: continue
            verified.add(hit.first to version)
        }
        return verified
    }

    /** Разбор ответа маяка. Всё, что не наш протокол, отбрасывается молча. */
    private fun beacon(packet: DatagramPacket): Pair<String, String>? {
        val json = try {
            JSONObject(String(packet.data, 0, packet.length, Charsets.UTF_8))
        } catch (_: Exception) {
            return null
        }
        if (json.optString("app") != APP || json.optInt("proto", 0) != PROTO) return null
        val port = json.optInt("port", 0)
        if (port < 1 || port > 65535) return null
        val reported = json.optJSONArray("lan")
        var host = packet.address?.hostAddress.orEmpty()
        if (reported != null && reported.length() > 0) {
            val first = reported.optString(0, "")
            if (first.isNotBlank()) host = first
        }
        if (host.isBlank() || host.startsWith("127.") || host.startsWith("169.254.")) {
            host = packet.address?.hostAddress.orEmpty()
        }
        if (host.isBlank()) return null
        val version = json.optString("version", "")
        return "http://$host:$port" to version.ifEmpty { "ok" }
    }

    /**
     * Обход своей сети: «base → version» для всех, где откликнулся PrintFlow.
     *
     * Два такта вместо одного перебора. Сначала TCP-пробник по всем адресам и
     * всем портам сразу (по 260 мс на порт — на своей сети порт отвечает
     * мгновенно) — так находятся открытые порты. Затем на них проверяется
     * `/api/health`: в список попадают только настоящие серверы PrintFlow, а не
     * любые открытые порты в сети.
     *
     * Общий бюджет считается от числа адресов, а не берётся «на глаз»: в
     * прошлой версии он был фиксированным (4,2 с) и покрывал едва треть одного
     * порта — 8080 и 8790 не проверялись никогда, отсюда «не может найти
     * сервер». Потолок остался: кассир не должен ждать бесконечно.
     */
    fun scan(ports: IntArray = PORTS, timeoutMs: Int = 700,
             onFound: ((Int) -> Unit)? = null): List<Pair<String, String>> {
        val found = ConcurrentHashMap<String, String>()
        // 1. Автопоиск: если сервер ответил маяком, перебор уже не нужен.
        for (hit in discoverServers()) {
            found[hit.first] = hit.second
            onFound?.invoke(found.size)
        }
        val prefixes = subnets()
        if (prefixes.isEmpty()) {
            return sorted(found)
        }
        // 2. Перебор: порты перебираем в порядке PORTS для каждого адреса, а не
        // «все адреса по одному порту» — иначе на медленном порту бюджет
        // заканчивался бы снова, как только один порт перестал отвечать.
        val targets = ArrayList<Pair<String, Int>>()
        for (prefix in prefixes) {
            for (host in 1..254) {
                val ip = "$prefix.$host"
                for (port in ports) targets.add(ip to port)
            }
        }
        val open = ConcurrentHashMap.newKeySet<String>()
        val pool = Executors.newFixedThreadPool(SWEEP_THREADS)
        try {
            val jobs: List<Callable<Unit>> = targets.map { target ->
                object : Callable<Unit> {
                    override fun call() {
                        if (openPort(target.first, target.second)) {
                            open.add("http://${target.first}:${target.second}")
                        }
                    }
                }
            }
            runCatching { pool.invokeAll(jobs, budgetMs(targets.size), TimeUnit.MILLISECONDS) }
        } finally {
            pool.shutdownNow()
        }
        // 3. Проверка: открытый порт ≠ PrintFlow.
        for (base in open) {
            if (found.containsKey(base)) continue
            val version = probe(base, timeoutMs = timeoutMs) ?: continue
            found[base] = version
            onFound?.invoke(found.size)
        }
        return sorted(found)
    }

    /**
     * Сколько ждать перебор целиком: по числу адресов и потоков, с запасом.
     *
     * Запас нужен на розыск DNS/маршрута и на медленный Wi-Fi; потолок — на
     * «кассир стоит у прилавка»: дольше 12 секунд ждать нечего, лучше показать
     * список найденного и кнопку «Переподключиться».
     */
    private fun budgetMs(targets: Int): Long {
        val waves = (targets + SWEEP_THREADS - 1) / SWEEP_THREADS
        val estimate = waves.toLong() * CONNECT_TIMEOUT_MS.toLong() * 2L
        return estimate.coerceIn(2_000L, 12_000L)
    }

    /** Открыт ли порт: TCP-соединение без обмена данными, 260 мс на попытку. */
    private fun openPort(host: String, port: Int): Boolean {
        val socket = Socket()
        return try {
            socket.connect(InetSocketAddress(host, port), CONNECT_TIMEOUT_MS)
            true
        } catch (_: Exception) {
            false
        } finally {
            runCatching { socket.close() }
        }
    }

    private fun sorted(found: Map<String, String>): List<Pair<String, String>> =
        found.entries.sortedWith(compareBy({ portOf(it.key) }, { netKey(it.key) }))
            .map { it.key to it.value }

    private fun portOf(base: String): Int =
        base.substringAfterLast(':').substringBefore('/').toIntOrNull() ?: 0

    /** Сортировка адреса по числам октетов: 192.168.1.9 раньше 192.168.1.10. */
    private fun netKey(base: String): String {
        val host = base.substringAfter("//").substringBeforeLast(':')
        return host.split('.').joinToString(".") { (it.toIntOrNull() ?: 0).toString().padStart(3, '0') }
    }
}
