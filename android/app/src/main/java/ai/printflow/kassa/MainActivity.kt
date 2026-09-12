package ai.printflow.kassa

import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.content.res.Configuration
import android.graphics.Color
import android.net.Uri
import android.net.http.SslError
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.text.Spannable
import android.text.SpannableString
import android.text.style.ForegroundColorSpan
import android.view.KeyEvent
import android.view.ViewGroup
import android.view.WindowManager
import android.webkit.JavascriptInterface
import android.webkit.SslErrorHandler
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import org.json.JSONObject

/**
 * Оболочка мобильной кассы: WebView + адрес сервера + системный звонок.
 *
 * Почему вообще существует: браузер в LAN умеет то же самое, но вкладку
 * Android выгружает из памяти, когда её не хватает, — и кассир утром снова
 * вводит код, а «невидимая смена» прерывается. Приложению это не грозит:
 * свой процесс, иконка, экран не гаснет посреди очереди, и звонок уходит
 * уведомлением, даже когда телефон в кармане.
 *
 * Что принципиально НЕ меняется: страница та же, что в браузере, состояние
 * (код кассира, корзина, отметки) живёт в localStorage WebView и в базе
 * PrintFlow. Приложение — только окно: обновлять кассу можно, не пересобирая
 * APK (пересборка нужна лишь когда меняется само окно).
 */
class MainActivity : Activity() {

    private lateinit var prefs: SharedPreferences
    private lateinit var root: FrameLayout
    private lateinit var web: WebView
    private var panel: ScrollView? = null
    private var urlField: EditText? = null
    private var results: LinearLayout? = null
    private var hintView: TextView? = null
    private var panelHintRes: Int = R.string.panel_hint_server
    private var foundServers: List<Pair<String, String>> = emptyList()
    private var scanNote: TextView? = null
    private var failed = false
    private lateinit var ring: Ring
    // 17.0.13: канал «касса ↔ ПК» восстанавливается сам. Сторож проверяет
    // сохранённый адрес с нарастающей паузой (2→4→8→15→30 с), а если адрес
    // умер вместе с DHCP — ищет коннектор в своей /24 и переключается, когда
    // нашёлся ровно один. Кассир при этом ничего не вводит.
    private var watch: Thread? = null
    private val watchStop = java.util.concurrent.atomic.AtomicBoolean(false)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = getSharedPreferences("kassa_shell", Context.MODE_PRIVATE)
        ring = Ring(this).apply { ensureChannel() }
        askNotificationPermission()

        root = FrameLayout(this)
        web = WebView(this).apply {
            setBackgroundColor(Color.parseColor("#0f1117"))
            isVerticalScrollBarEnabled = false
        }
        configureWebView()
        root.addView(web, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        setContentView(root)

        applyKeepAwake()
        applySecure()
        val saved = prefs.getString(KEY_URL, "").orEmpty()
        if (saved.isBlank()) {
            showPanel(R.string.panel_hint_initial)
        } else {
            load(saved)
            checkForUpdate()
            maybeAskBattery()
        }
    }

    /**
     * Фоновый слух платежей (RingService) включаем сами, а не «когда повезёт»:
     * Android 12+ запрещает поднимать foreground-сервис из фона, поэтому
     * момент старта — активность. onResume отпускаем сервис звенеть, onPause
     * — забираем право на звук себе, иначе на одно событие было бы два сигнала.
     */
    override fun onResume() {
        super.onResume()
        RingService.foreground = true
        syncRingService()
    }

    override fun onPause() {
        RingService.foreground = false
        super.onPause()
    }

    /**
     * Поворот/плотность/масштаб шрифта: activity не пересоздаётся (см. configChanges
     * в манифесте), чтобы WebView не перезагружал страницу и не терял корзину
     * кассира посреди продажи. WebView при этом сам перекладывает страницу под
     * новый размер окна (onSizeChanged → CSS reflow, состояние в localStorage
     * цело). А вот панель выбора сервера собрана из ресурсов — её пересобираем
     * вручную, иначе dimens из values-land/values-sw600dp не применились бы.
     */
    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        if (panel != null) rebuildPanel()
    }

    /** Пересборка панели без потери введённого адреса и найденных серверов. */
    private fun rebuildPanel() {
        val hint = panelHintRes
        val url = urlField?.text?.toString().orEmpty()
        hidePanel()
        showPanel(hint)
        urlField?.setText(url)
        renderFoundServers()
    }

    private fun syncRingService() {
        val base = prefs.getString(KEY_URL, "").orEmpty()
        if (base.isBlank() || !prefs.getBoolean(KEY_RING_BG, true)) {
            RingService.stop(this)
        } else {
            RingService.start(this, base)
        }
    }

    // ------------------------------------------------------------- WebView
    @SuppressLint("SetJavaScriptEnabled")
    private fun configureWebView() {
        val settings = web.settings
        settings.javaScriptEnabled = true
        settings.domStorageEnabled = true
        settings.databaseEnabled = true
        settings.cacheMode = WebSettings.LOAD_DEFAULT
        settings.loadWithOverviewMode = true
        settings.useWideViewPort = true
        settings.setSupportZoom(false)
        settings.builtInZoomControls = false
        settings.javaScriptCanOpenWindowsAutomatically = true
        settings.mediaPlaybackRequiresUserGesture = false   // чтобы «пик» playable без касания
        settings.allowFileAccess = false
        settings.allowContentAccess = false
        web.addJavascriptInterface(Bridge(), "PfApp")
        web.webViewClient = object : WebViewClient() {
            override fun onPageStarted(view: WebView?, url: String?, favicon: android.graphics.Bitmap?) {
                failed = false
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                // Панель прячем только когда страница действительно открылась:
                // иначе «сервер не отвечает» сменится чёрным экраном без подсказки.
                if (failed) return
                if (url != null && view?.canGoBack() != true) remember(url)
                hidePanel()
            }

            override fun onReceivedError(view: WebView?, request: WebResourceRequest?, error: WebResourceError?) {
                // Только главная страница: картинки и шрифты не должны вырывать
                // кассира из продажи.
                if (request?.isForMainFrame == true) {
                    failed = true
                    runOnUiThread {
                        showPanel(R.string.panel_hint_unreachable)
                        startWatch()      // дальше касса поднимется сама
                    }
                }
            }

            override fun onReceivedSslError(view: WebView?, handler: SslErrorHandler?, error: SslError?) {
                // Свои сертификаты в LAN — норма (HTTPS без домена). Молча не
                // принимаем и молча не reject'им: спрашиваем человека.
                if (handler == null) return
                AlertDialog.Builder(this@MainActivity)
                    .setTitle("Сертификат не из общего списка")
                    .setMessage("Для локальной сети это обычно самоподписанный сертификат вашего же " +
                        "коннектора. Продолжить?")
                    .setPositiveButton("Продолжить") { _, _ -> handler.proceed() }
                    .setNegativeButton("Назад") { _, _ -> handler.cancel() }
                    .show()
            }
        }
        web.webChromeClient = WebChromeClient()
        web.setDownloadListener { url, _, _, _, _ -> openExternally(url) }
        if (BuildConfig.DEBUG) WebView.setWebContentsDebuggingEnabled(true)
    }

    // --------------------------------------------------------- мостик JS
    inner class Bridge {
        /** kind: "loud" | "soft" — страница знает, нужен ли взгляд кассира. */
        /** Имя метода — контракт со страницей (`window.PfApp.ring`). */
        @JavascriptInterface
        fun ring(kind: String, text: String) {
            this@MainActivity.ring.fire(kind, text)
        }

        /**
         * Сколько наличных продаж ждут связи в офлайн-очереди страницы.
         * Сервис не лезет в localStorage — он только показывает число в
         * уведомлении, чтобы «деньги пришли» и «N не проведено» звучали
         * одним сообщением, а не двумя.
         */
        @JavascriptInterface
        fun offline(count: Int) {
            RingService.offline(count)
            runOnUiThread { syncRingService() }
        }

        @JavascriptInterface
        fun openSettings() {
            runOnUiThread { showPanel(R.string.panel_hint_server) }
        }

        /**
         * Копия офлайн-очереди в памяти оболочки.
         *
         * Адрес сервера — это origin страницы, и у каждого IP он свой. Роутер
         * выдал ПК новый адрес, касса переподключилась на него — и localStorage
         * старого адреса странице уже не виден: очередь наличных продаж
         * осталась бы в никуда. Копия в prefs переживает и смену адреса, и
         * чистку хранилища WebView. Повторная отправка безопасна: номер
         * продажи (request_id) тот же, сервер вторую не запишет.
         */
        @JavascriptInterface
        fun queueSave(json: String?) {
            prefs.edit().putString(KEY_QUEUE, (json ?: "").take(QUEUE_LIMIT)).apply()
        }

        /** Что сохранили в прошлый раз; пусто — страница живёт своей памятью. */
        @JavascriptInterface
        fun queueLoad(): String = prefs.getString(KEY_QUEUE, "").orEmpty()

        @JavascriptInterface
        fun appInfo(): String = JSONObject()
            .put("package", BuildConfig.APPLICATION_ID)
            .put("version", BuildConfig.VERSION_NAME)
            .put("version_code", BuildConfig.VERSION_CODE)
            .put("platform", "android")
            .toString()
    }

    // ------------------------------------------- самовосстановление канала
    /**
     * Сторож связи: пока экран выбора сервера открыт (значит, касса не
     * открылась), проверяем адрес и возвращаем кассу в строй без кассира.
     *
     * Порядок ровно такой, как решил раунд вопросов: сначала тот же адрес
     * (роутер обычно не меняет его посреди смены), потом — перескан своей /24.
     * Автопереключение делаем только если нашёлся РОВНО ОДИН коннектор: две
     * кассы в одной сети — это уже не «угадаем», а «спросим владельца».
     */
    private fun startWatch() {
        if (watch?.isAlive == true) return
        watchStop.set(false)
        watch = Thread {
            var attempt = 0
            var scanned = false
            while (!watchStop.get() && !isFinishing) {
                val base = prefs.getString(KEY_URL, "").orEmpty()
                if (base.isBlank()) return@Thread
                val version = Net.probe(base, timeoutMs = 2500)
                if (version != null) {
                    prefs.edit().putString(KEY_LAST_OK, base).apply()
                    runOnUiThread { load(base) }
                    return@Thread
                }
                attempt += 1
                // Шаг 2: помним адрес, который недавно отвечал. Если роутер
                // выдал ПК новый IP, коннектор живёт именно там — и это
                // быстрее и точнее перескана всей /24.
                if (attempt == 3) {
                    val lastOk = prefs.getString(KEY_LAST_OK, "").orEmpty()
                    if (lastOk.isNotBlank() && lastOk != base &&
                        Net.probe(lastOk, timeoutMs = 2500) != null) {
                        runOnUiThread {
                            toast(getString(R.string.reconnect_found, lastOk))
                            load(lastOk)
                        }
                        return@Thread
                    }
                }
                // Шаг 3: ~30 с мёртвого адреса — повод поискать коннектор
                // заново: именно так выглядит «роутер выдал другой IP»,
                // если прошлый адрес тоже молчит.
                if (attempt >= 6 && !scanned) {
                    scanned = true
                    val hits = Net.scan(timeoutMs = 700)
                    val fresh = hits.map { it.first }.filter { it != base }
                    if (hits.size == 1) {
                        val only = hits[0].first
                        prefs.edit().putString(KEY_LAST_OK, only).apply()
                        runOnUiThread {
                            toast(getString(R.string.reconnect_found, only))
                            load(only)
                        }
                        return@Thread
                    }
                    if (fresh.isNotEmpty()) {
                        runOnUiThread {
                            foundServers = hits
                            renderFoundServers()
                            hintView?.text = getString(R.string.reconnect_none)
                        }
                    }
                }
                val pause = when {
                    attempt <= 1 -> 2000L
                    attempt <= 3 -> 4000L
                    attempt <= 5 -> 8000L
                    attempt <= 8 -> 15000L
                    else -> 30000L
                }
                try {
                    Thread.sleep(pause)
                } catch (_: InterruptedException) {
                    return@Thread
                }
            }
        }.also { it.start() }
    }

    private fun stopWatch() {
        watchStop.set(true)
        watch?.interrupt()
        watch = null
    }

    /** Кнопка «Переподключиться»: тот же адрес, потом перескан. Без ввода. */
    private fun reconnect() {
        val base = prefs.getString(KEY_URL, "").orEmpty()
        if (base.isBlank()) {
            showPanel(R.string.panel_hint_initial)
            return
        }
        hintView?.text = getString(R.string.reconnecting)
        Thread {
            val version = if (Net.probe(base, timeoutMs = 2500) != null) base
            else Net.scan(timeoutMs = 700).takeIf { it.size == 1 }?.get(0)?.first
            if (version != null) {
                prefs.edit().putString(KEY_LAST_OK, version).apply()
                runOnUiThread { load(version) }
            } else {
                runOnUiThread { startWatch() }
            }
        }.start()
    }

    private fun toast(text: String) {
        android.widget.Toast.makeText(this, text, android.widget.Toast.LENGTH_SHORT).show()
    }

    // ---------------------------------------------------------- загрузка
    private fun load(base: String) {
        val root = Net.normalize(base) ?: return
        remember(root)
        web.loadUrl("$root$CASHIER_PATH")
    }

    private fun remember(url: String) {
        val root = Net.normalize(url) ?: return
        markAlive(root)
        val current = prefs.getString(KEY_URL, "").orEmpty()
        if (current != root) {
            prefs.edit().putString(KEY_URL, root).apply()
            // Сервер сменился — слушать надо другой адрес, а не тот, где
            // касса «не отвечает» с вчерашнего вечера.
            syncRingService()
        }
    }

    /**
     * Запомнить адрес как «последний живой». Проверка идёт в фоне: onPageFinished
     * вызывается на UI-потоке, и ждать там сеть нельзя (панель/касса замерли бы).
     */
    private fun markAlive(root: String) {
        Thread {
            if (Net.probe(root, timeoutMs = 1500) != null) {
                prefs.edit().putString(KEY_LAST_OK, root).apply()
            }
        }.start()
    }

    private fun openExternally(url: String) {
        runCatching { startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url))) }
    }

    // ------------------------------------------------------- экран выбора
    /**
     * Панель выбора сервера. Разметка — `res/layout/panel_server.xml`, размеры —
     * `dimens.xml` во всех папках `res/values…` (dp), шрифты — sp. В коде остались
     * только те размеры, которых нет в разметке (кнопки найденных серверов
     * создаются динамически) — их берём через getDimensionPixelSize, а не пикселями.
     */
    private fun showPanel(hintRes: Int) {
        panelHintRes = hintRes
        if (panel != null) {
            urlField?.setText(prefs.getString(KEY_URL, "").orEmpty())
            hintView?.setText(hintRes)
            renderFoundServers()
            return
        }
        val scroll = layoutInflater.inflate(R.layout.panel_server, root, false) as ScrollView
        val hintView = scroll.findViewById(R.id.panelHint) as TextView
        hintView.text = getString(hintRes)
        this.hintView = hintView
        val field = scroll.findViewById(R.id.urlField) as EditText
        field.setText(prefs.getString(KEY_URL, "").orEmpty())
        urlField = field
        val found = scroll.findViewById(R.id.results) as LinearLayout
        results = found
        (scroll.findViewById(R.id.btnOpen) as Button).setOnClickListener { openFromField() }
        (scroll.findViewById(R.id.btnFind) as Button).setOnClickListener { scanServers() }
        val cbAwake = scroll.findViewById(R.id.cbAwake) as CheckBox
        cbAwake.isChecked = prefs.getBoolean(KEY_AWAKE, true)
        cbAwake.setOnCheckedChangeListener { _, on ->
            prefs.edit().putBoolean(KEY_AWAKE, on).apply()
            applyKeepAwake()
        }
        val cbRing = scroll.findViewById(R.id.cbRing) as CheckBox
        cbRing.isChecked = prefs.getBoolean(KEY_RING_BG, true)
        cbRing.setOnCheckedChangeListener { _, on ->
            prefs.edit().putBoolean(KEY_RING_BG, on).apply()
            syncRingService()
        }
        val cbSecure = scroll.findViewById(R.id.cbSecure) as CheckBox
        cbSecure.isChecked = prefs.getBoolean(KEY_SECURE, false)
        cbSecure.setOnCheckedChangeListener { _, on ->
            prefs.edit().putBoolean(KEY_SECURE, on).apply()
            applySecure()
        }
        val cbBoot = scroll.findViewById(R.id.cbBoot) as CheckBox
        cbBoot.isChecked = prefs.getBoolean(KEY_BOOT, true)
        cbBoot.setOnCheckedChangeListener { _, on ->
            prefs.edit().putBoolean(KEY_BOOT, on).apply()
        }
        (scroll.findViewById(R.id.btnReconnect) as Button).setOnClickListener { reconnect() }
        (scroll.findViewById(R.id.btnUpdate) as Button).setOnClickListener { checkForUpdate(manual = true) }
        (scroll.findViewById(R.id.btnBattery) as Button).setOnClickListener { askBattery(true) }
        (scroll.findViewById(R.id.panelVersion) as? TextView)?.text =
            getString(R.string.panel_version_fmt, BuildConfig.VERSION_NAME)
        panel = scroll
        root.addView(scroll, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
    }

    private fun openFromField() {
        val field = urlField ?: return
        val value = field.text.toString()
        if (Net.normalize(value) == null) {
            field.error = getString(R.string.err_bad_url)
        } else {
            load(value)
        }
    }

    private fun scanServers() {
        urlField?.error = null
        val host = results ?: return
        host.removeAllViews()
        scanNote = note(host, getString(R.string.scanning))
        Thread {
            val hits = Net.scan(onFound = { n -> runOnUiThread { updateScanNote(n) } })
            runOnUiThread {
                foundServers = hits
                renderFoundServers()
            }
        }.start()
    }

    private fun updateScanNote(n: Int) {
        scanNote?.text = getString(R.string.scan_found, n)
    }

    private fun renderFoundServers() {
        val host = results ?: return
        host.removeAllViews()
        scanNote = null
        if (foundServers.isEmpty()) {
            note(host, getString(R.string.scan_none))
            return
        }
        foundServers.forEach { (base, version) ->
            host.addView(Button(this).apply {
                val label = SpannableString("●  $base   (v$version)")
                label.setSpan(ForegroundColorSpan(Color.parseColor("#10b981")), 0, 1,
                    Spannable.SPAN_EXCLUSIVE_EXCLUSIVE)
                text = label
                setTextColor(Color.parseColor("#9ca3af"))
                minHeight = resources.getDimensionPixelSize(R.dimen.panel_control_min_height)
                setOnClickListener {
                    urlField?.setText(base)
                    load(base)
                }
            })
        }
    }

    private fun note(host: LinearLayout, value: String): TextView {
        val tv = TextView(this).apply {
            text = value
            setTextColor(Color.parseColor("#9ca3af"))
            textSize = 14f
        }
        host.addView(tv)
        return tv
    }

    private fun hidePanel() {
        stopWatch()
        panel?.let { root.removeView(it) }
        panel = null
        urlField = null
        results = null
        hintView = null
        scanNote = null
    }

    // --------------------------------------------------- мелкая полезность
    private fun applyKeepAwake() {
        if (prefs.getBoolean(KEY_AWAKE, true)) {
            window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        } else {
            window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        }
    }

    /** Снимки экрана и миниатюра в списке задач (17.0.16).

     *  Флаг работает на живом окне: после переключения система перерисовывает
     *  его уже с защитой. На некоторых прошивках miniature в «недавних»
     *  обновляется только после возврата к приложению — это ограничение
     *  системы, а не ошибки здесь. */
    private fun applySecure() {
        if (prefs.getBoolean(KEY_SECURE, false)) {
            window.setFlags(WindowManager.LayoutParams.FLAG_SECURE,
                WindowManager.LayoutParams.FLAG_SECURE)
        } else {
            window.clearFlags(WindowManager.LayoutParams.FLAG_SECURE)
        }
    }

    private fun askNotificationPermission() {
        if (Build.VERSION.SDK_INT < 33) return
        runCatching {
            requestPermissions(arrayOf("android.permission.POST_NOTIFICATIONS"), 1)
        }
    }

    private fun maybeAskBattery() {
        if (prefs.getBoolean(KEY_BATTERY_ASKED, false)) return
        prefs.edit().putBoolean(KEY_BATTERY_ASKED, true).apply()
        if (isIgnoringBattery()) return
        AlertDialog.Builder(this)
            .setTitle("Чтобы звонок не терялся")
            .setMessage("Android экономит батарею и может «усыплять» фоновые соединения: " +
                "телефон перестанет звенеть о платежах, когда экран погашен. " +
                "Для кассы полезнее снять ограничение — на расход это влияет в пределах пары процентов в смену.")
            .setPositiveButton("Настроить") { _, _ -> askBattery(true) }
            .setNegativeButton("Не надо", null)
            .show()
    }

    private fun isIgnoringBattery(): Boolean {
        if (Build.VERSION.SDK_INT < 23) return true
        val pm = getSystemService(Context.POWER_SERVICE) as? PowerManager ?: return true
        return runCatching { pm.isIgnoringBatteryOptimizations(packageName) }.getOrDefault(true)
    }

    private fun askBattery(showToast: Boolean) {
        if (Build.VERSION.SDK_INT < 23) return
        val intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
            Uri.parse("package:$packageName"))
        if (!runCatching { startActivity(intent) }.isSuccess && showToast) {
            runCatching { startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)) }
        }
    }

    /**
     * Обновка: APK лежит на том же сервере, что и касса, — тап и переустановка.
     *
     * 17.0.13: перед установкой показываем «что нового» (changelog из
     * version.json), имя файла и размер, а не только номер версии. Сборку
     * без имени файла или размера не предлагаем вовсе: ставить «что-то»
     * вслепую на кассу нельзя.
     *
     * `manual` — нажата кнопка «Проверить обновление»: тогда честно говорим
     * и «обновлений нет», и «сборки на сервере нет». При старте молчим.
     */
    private fun checkForUpdate(manual: Boolean = false) {
        val base = prefs.getString(KEY_URL, "").orEmpty()
        if (base.isBlank()) {
            if (manual) runOnUiThread { toast(getString(R.string.panel_hint_server)) }
            return
        }
        Thread {
            val json = Net.json(base, "/api/app/android?installed=${BuildConfig.VERSION_CODE}")
            if (json == null) {
                if (manual) runOnUiThread { toast(getString(R.string.reconnect_none)) }
                return@Thread
            }
            val available = json.optBoolean("available", false)
            val update = json.optBoolean("update_available", false)
            if (!available) {
                if (manual) runOnUiThread { toast(getString(R.string.update_missing)) }
                return@Thread
            }
            if (!update) {
                if (manual) runOnUiThread {
                    toast(getString(R.string.update_none, BuildConfig.VERSION_NAME))
                }
                return@Thread
            }
            // Отказ от этой сборки запоминаем: при следующем запуске молчим.
            // Кнопка «Проверить обновление» показывает диалог всегда — иначе
            // кассир, однажды нажавший «Позже», не смог бы обновиться сам.
            val offeredCode = json.optInt("version_code", 0)
            if (!manual && offeredCode > 0
                && offeredCode <= prefs.getInt(KEY_UPDATE_SKIPPED, 0)) {
                return@Thread
            }
            val url = json.optString("url", "")
            val version = json.optString("version", "")
            val file = json.optString("file", "")
            val sizeMb = json.optDouble("size_mb", 0.0)
            val bytes = json.optLong("size_bytes", 0L)
            val changes = json.optString("changelog", "")
            val sha = json.optString("sha256", "")
            if (url.isBlank() || file.isBlank() || (bytes <= 0L && sizeMb <= 0.0)) {
                if (manual) runOnUiThread { toast(getString(R.string.update_broken)) }
                return@Thread
            }
            val size = if (sizeMb > 0.0) String.format(java.util.Locale.ROOT, "%.1f", sizeMb)
                       else String.format(java.util.Locale.ROOT, "%.1f", bytes / 1024.0 / 1024.0)
            val message = buildString {
                if (changes.isNotBlank()) {
                    append(getString(R.string.update_note, changes.take(600)))
                    append("\n\n")
                }
                append(getString(R.string.update_ready, file, size))
                if (sha.isNotBlank()) {
                    append("\n")
                    append(getString(R.string.update_integrity, sha.take(16)))
                }
            }
            runOnUiThread {
                AlertDialog.Builder(this)
                    .setTitle(getString(R.string.update_title, version))
                    .setMessage(message)
                    .setPositiveButton("Скачать") { _, _ -> openExternally("$base$url") }
                    .setNegativeButton("Позже") { _, _ ->
                        if (offeredCode > 0) {
                            prefs.edit().putInt(KEY_UPDATE_SKIPPED, offeredCode).apply()
                        }
                    }
                    .show()
            }
        }.start()
    }

    // ----------------------------------------------------------- клавиатура
    override fun onKeyDown(keyCode: Int, event: KeyEvent?): Boolean {
        if (keyCode == KeyEvent.KEYCODE_BACK) {
            when {
                panel != null -> hidePanel()
                web.canGoBack() -> web.goBack()
                else -> confirmExit()
            }
            return true
        }
        return super.onKeyDown(keyCode, event)
    }

    /** На кассе «назад» — частая случайность: не выходим молча, предлагаем сменить сервер. */
    private fun confirmExit() {
        AlertDialog.Builder(this)
            .setItems(arrayOf("Сменить сервер…", "Свернуть", "Выйти")) { _, which ->
                when (which) {
                    0 -> showPanel(R.string.panel_hint_server)
                    1 -> moveTaskToBack(true)
                    else -> finish()
                }
            }
            .show()
    }

    override fun onDestroy() {
        stopWatch()
        runCatching { web.destroy() }
        super.onDestroy()
    }

    companion object {
        private const val KEY_URL = "server_url"
        // Последний адрес, который реально ответил: при смене IP роутером
        // касса пробует его первым, а не «вспоминает» вчерашний мёртвый.
        private const val KEY_LAST_OK = "server_url_last_ok"
        private const val KEY_AWAKE = "keep_awake"
        // Экран кассы не должен утекать в снимки и в миниатюру списка задач:
        // на прилавке телефон лежит экраном вверх, а в корзине — суммы и
        // телефоны покупателей. По умолчанию выключено: кассир решает сам.
        private const val KEY_SECURE = "flag_secure"
        // Поднимать кассу после перезагрузки телефона (см. BootReceiver).
        private const val KEY_BOOT = "boot_start"
        private const val KEY_BATTERY_ASKED = "battery_asked"
        // Версия, от которой кассир уже отказался кнопкой «Позже»: при старте
        // больше не спрашиваем, иначе диалог «Скачать» всплывал на каждом
        // запуске, пока на сервере лежит сборка новее установленной.
        private const val KEY_UPDATE_SKIPPED = "update_skipped_code"
        private const val KEY_RING_BG = "ring_background"
        // Копия очереди из страницы: страховка от смены адреса сервера.
        private const val KEY_QUEUE = "offline_queue_backup"
        private const val QUEUE_LIMIT = 96 * 1024
        private const val CASHIER_PATH = "/cashier.html"
    }
}
