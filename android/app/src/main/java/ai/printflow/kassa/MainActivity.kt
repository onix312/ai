package ai.printflow.kassa

import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.graphics.Color
import android.net.Uri
import android.net.http.SslError
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.view.Gravity
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
    private var panel: LinearLayout? = null
    private var urlField: EditText? = null
    private var results: LinearLayout? = null
    private var failed = false
    private lateinit var ring: Ring

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
        val saved = prefs.getString(KEY_URL, "").orEmpty()
        if (saved.isBlank()) {
            showPanel("Куда ходить за кассой? Обычно http://192.168.1.x:8765")
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
                        showPanel("Сервер не отвечает. Проверьте Wi-Fi и что PrintFlow запущен — или выберите другой адрес.")
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
            runOnUiThread { showPanel("Адрес сервера в локальной сети") }
        }

        @JavascriptInterface
        fun appInfo(): String = JSONObject()
            .put("package", BuildConfig.APPLICATION_ID)
            .put("version", BuildConfig.VERSION_NAME)
            .put("version_code", BuildConfig.VERSION_CODE)
            .put("platform", "android")
            .toString()
    }

    // ---------------------------------------------------------- загрузка
    private fun load(base: String) {
        val root = Net.normalize(base) ?: return
        remember(root)
        web.loadUrl("$root$CASHIER_PATH")
    }

    private fun remember(url: String) {
        val root = Net.normalize(url) ?: return
        val current = prefs.getString(KEY_URL, "").orEmpty()
        if (current != root) {
            prefs.edit().putString(KEY_URL, root).apply()
            // Сервер сменился — слушать надо другой адрес, а не тот, где
            // касса «не отвечает» с вчерашнего вечера.
            syncRingService()
        }
    }

    private fun openExternally(url: String) {
        runCatching { startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url))) }
    }

    // ------------------------------------------------------- экран выбора
    private fun showPanel(hint: String) {
        if (panel != null) {
            results?.removeAllViews()
            urlField?.setText(prefs.getString(KEY_URL, "").orEmpty())
            return
        }
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(40, 56, 40, 40)
            setBackgroundColor(Color.parseColor("#0f1117"))
        }
        box.addView(TextView(this).apply {
            text = "Касса PrintFlow"
            setTextColor(Color.parseColor("#e5e7eb"))
            textSize = 24f
            gravity = Gravity.CENTER
            setPadding(0, 0, 0, 12)
        })
        box.addView(TextView(this).apply {
            text = hint
            setTextColor(Color.parseColor("#9ca3af"))
            textSize = 14f
            setPadding(0, 0, 0, 20)
        })
        val field = EditText(this).apply {
            hint = "http://192.168.1.20:8765"
            setText(prefs.getString(KEY_URL, "").orEmpty())
            setSingleLine()
            inputType = InputTypeHelper.uri()
            setTextColor(Color.parseColor("#e5e7eb"))
            setHintTextColor(Color.parseColor("#6b7280"))
        }
        urlField = field
        box.addView(field)
        box.addView(Button(this).apply {
            text = "Открыть кассу"
            setOnClickListener {
                val value = field.text.toString()
                if (Net.normalize(value) == null) {
                    field.error = "Нужен адрес вида http://192.168.1.20:8765"
                } else {
                    load(value)
                }
            }
        })
        val found = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        box.addView(Button(this).apply {
            text = "Найти сервер в сети"
            setOnClickListener {
                field.error = null
                results?.removeAllViews()
                note(found, "Ищу…")
                Thread {
                    val hits = Net.scan()
                    runOnUiThread {
                        found.removeAllViews()
                        if (hits.isEmpty()) {
                            note(found, "Ничего не нашёл. Запустите PrintFlow и введите адрес вручную.")
                        } else {
                            hits.forEach { (base, version) ->
                                found.addView(Button(this@MainActivity).apply {
                                    text = "$base   (v$version)"
                                    setOnClickListener {
                                        field.setText(base)
                                        load(base)
                                    }
                                })
                            }
                        }
                    }
                }.start()
            }
        })
        box.addView(ScrollView(this).apply {
            addView(found)
            setPadding(0, 16, 0, 8)
        })
        results = found
        box.addView(CheckBox(this).apply {
            isChecked = prefs.getBoolean(KEY_AWAKE, true)
            text = "Не гасить экран, пока открыта касса"
            setTextColor(Color.parseColor("#9ca3af"))
            setOnCheckedChangeListener { _, on ->
                prefs.edit().putBoolean(KEY_AWAKE, on).apply()
                applyKeepAwake()
            }
        })
        box.addView(CheckBox(this).apply {
            isChecked = prefs.getBoolean(KEY_RING_BG, true)
            text = "Звенеть о платежах, даже когда экран погашен"
            setTextColor(Color.parseColor("#9ca3af"))
            setOnCheckedChangeListener { _, on ->
                prefs.edit().putBoolean(KEY_RING_BG, on).apply()
                syncRingService()
            }
        })
        box.addView(Button(this).apply {
            text = "Экономия батареи: не ограничивать"
            setOnClickListener { askBattery(true) }
        })
        panel = box
        root.addView(box, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
    }

    private fun note(host: LinearLayout, value: String) {
        host.addView(TextView(this).apply {
            text = value
            setTextColor(Color.parseColor("#9ca3af"))
            textSize = 14f
        })
    }

    private fun hidePanel() {
        panel?.let { root.removeView(it) }
        panel = null
        urlField = null
        results = null
    }

    // --------------------------------------------------- мелкая полезность
    private fun applyKeepAwake() {
        if (prefs.getBoolean(KEY_AWAKE, true)) {
            window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        } else {
            window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
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
            runCatching { startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATIONS_SETTINGS)) }
        }
    }

    /** Обновка: APK лежит на том же сервере, что и касса, — тап и переустановка. */
    private fun checkForUpdate() {
        val base = prefs.getString(KEY_URL, "").orEmpty()
        if (base.isBlank()) return
        Thread {
            val json = Net.json(base, "/api/app/android?installed=${BuildConfig.VERSION_CODE}") ?: return@Thread
            if (json.optBoolean("update_available", false)) {
                val url = json.optString("url", "")
                val version = json.optString("version", "")
                if (url.isNotBlank()) {
                    runOnUiThread {
                        AlertDialog.Builder(this)
                            .setTitle("Доступна касса v$version")
                            .setMessage("Скачать и установить сейчас? Данные кассы и код кассира останутся на месте.")
                            .setPositiveButton("Скачать") { _, _ -> openExternally("$base$url") }
                            .setNegativeButton("Позже", null)
                            .show()
                    }
                }
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
                    0 -> showPanel("Адрес сервера в локальной сети")
                    1 -> moveTaskToBack(true)
                    else -> finish()
                }
            }
            .show()
    }

    override fun onDestroy() {
        runCatching { web.destroy() }
        super.onDestroy()
    }

    companion object {
        private const val KEY_URL = "server_url"
        private const val KEY_AWAKE = "keep_awake"
        private const val KEY_BATTERY_ASKED = "battery_asked"
        private const val KEY_RING_BG = "ring_background"
        private const val CASHIER_PATH = "/cashier.html"
    }
}

/** inputType для адреса: без автозамены и подсказок, с «/» и «:». */
object InputTypeHelper {
    fun uri(): Int = android.text.InputType.TYPE_CLASS_TEXT or
        android.text.InputType.TYPE_TEXT_VARIATION_URI or
        android.text.InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
}
