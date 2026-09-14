package ai.printflow.pult

import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.net.http.SslError
import android.os.Bundle
import android.view.Gravity
import android.view.KeyEvent
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
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
import android.widget.Toast
import java.util.Locale

/**
 * Оболочка пульта цеха: окно к странице `/pult` на сервере PrintFlow в LAN.
 *
 * Зачем приложение, если та же страница открывается в браузере телефона:
 *  • вкладку Android выгружает из памяти, когда её не хватает, — а телефон в
 *    цеху лежит у станка и должен быть готов сразу, без «введите адрес заново»;
 *  • нужен киоск: экран не гаснет, системные панели скрыты, случайный свайп не
 *    уводит оператора из пульта;
 *  • крупные кнопки страницы рассчитаны на палец в перчатке — оболочка не
 *    добавляет своего интерфейса поверх, кроме аварийного экрана связи.
 *
 * Что принципиально НЕ меняется: страница та же, что в браузере, состояние
 * (выбранный экран, режим киоска) живёт в localStorage WebView, а данные — в
 * базе PrintFlow. Приложение только окно: чтобы обновить пульт, APK пересобирать
 * не нужно.
 *
 * Наружу не уходит ничего: единственный адрес, к которому обращается оболочка, —
 * сервер владельца в его же сети.
 */
class MainActivity : Activity() {

    private lateinit var prefs: SharedPreferences
    private lateinit var root: FrameLayout
    private var web: WebView? = null
    private var panel: ScrollView? = null
    private var urlField: EditText? = null
    private var scanNote: TextView? = null
    private var failed = false
    private var lastBackAt = 0L

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = getSharedPreferences("pult_shell", Context.MODE_PRIVATE)
        root = FrameLayout(this)
        root.setBackgroundColor(bgColor())
        setContentView(root)
        applyKeepAwake()
        applyKiosk()
        val saved = prefs.getString(KEY_SERVER, null)
        if (saved.isNullOrBlank()) {
            showPanel(R.string.panel_hint_initial)
        } else {
            openPult(saved)
        }
    }

    // ------------------------------------------------------- открытие пульта

    /**
     * Сначала проверяем адрес, и только потом показываем WebView: белый экран
     * с «сайт недоступен» — худшее, что может увидеть человек у станка.
     */
    private fun openPult(raw: String) {
        val base = Net.normalize(raw)
        if (base == null) {
            showPanel(R.string.panel_hint_server)
            return
        }
        showBusy(getString(R.string.reconnecting))
        Thread {
            val version = Net.probe(base)
            runOnUiThread {
                if (version == null) {
                    showPanel(R.string.panel_hint_unreachable)
                } else {
                    prefs.edit().putString(KEY_SERVER, base).apply()
                    attachWebView(base)
                }
            }
        }.start()
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun attachWebView(base: String) {
        failed = false
        val view = web ?: WebView(this).also { web = it }
        val settings = view.settings
        settings.javaScriptEnabled = true
        settings.domStorageEnabled = true
        settings.databaseEnabled = true
        settings.cacheMode = WebSettings.LOAD_DEFAULT
        settings.loadWithOverviewMode = true
        settings.useWideViewPort = true
        settings.setSupportZoom(false)
        settings.builtInZoomControls = false
        settings.allowFileAccess = false
        settings.allowContentAccess = false
        view.webViewClient = object : WebViewClient() {
            override fun onPageStarted(view: WebView?, url: String?, favicon: android.graphics.Bitmap?) {
                failed = false
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                // Панель прячем только когда страница действительно открылась:
                // иначе «сервер не отвечает» сменится чёрным экраном без подсказки.
                if (failed) return
                hidePanelIfVisible()
            }

            override fun onReceivedError(view: WebView?, request: WebResourceRequest?,
                                         error: WebResourceError?) {
                // Только главная страница: не загрузившийся снимок камеры или
                // шрифт не должен вырывать оператора из пульта.
                if (request?.isForMainFrame != true) return
                failed = true
                val code = error?.errorCode ?: 0
                runOnUiThread {
                    // Ошибка HTTP (например, /pult не отдаётся старым коннектором)
                    // — это не «нет связи»: человеку нужен апдейт PrintFlow на ПК,
                    // а не поиск Wi-Fi. Подсказка должна быть другой.
                    if (code == WebViewClient.ERROR_HTTP_STATUS) {
                        showGone(R.string.page_missing)
                    } else {
                        showGone(R.string.conn_lost)
                    }
                }
            }

            override fun onReceivedSslError(view: WebView?, handler: SslErrorHandler?, error: SslError?) {
                // Свои сертификаты в LAN — норма (HTTPS без домена). Молча не
                // принимаем и молча не отклоняем: спрашиваем человека.
                if (handler == null) return
                AlertDialog.Builder(this@MainActivity)
                    .setTitle("Сертификат не из общего списка")
                    .setMessage("Для локальной сети это обычно самоподписанный сертификат " +
                        "вашего же коннектора. Продолжить?")
                    .setPositiveButton("Продолжить") { _, _ -> handler.proceed() }
                    .setNegativeButton("Назад") { _, _ -> handler.cancel() }
                    .show()
            }
        }
        view.webChromeClient = WebChromeClient()
        // Ссылки «наружу» (например, на документацию) открываем в браузере,
        // а не внутри пульта: окно должно оставаться окном цеха.
        view.setDownloadListener { url, _, _, _, _ -> openExternally(url) }
        root.removeAllViews()
        root.addView(view, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        panel = null
        view.loadUrl("$base$PULT_PATH")
        applyKeepAwake()
        applyKiosk()
        checkForUpdate()
    }

    private fun hidePanelIfVisible() {
        if (panel != null) {
            root.removeAllViews()
            web?.let { view ->
                root.addView(view, FrameLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
            }
            panel = null
        }
    }

    private fun openExternally(url: String) {
        runCatching { startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url))) }
    }

    // -------------------------------------------------- экран связи и адреса

    private fun showPanel(hintRes: Int) {
        val scroll = ScrollView(this)
        scroll.setBackgroundColor(bgColor())
        val box = LinearLayout(this)
        box.orientation = LinearLayout.VERTICAL
        val pad = dp(20)
        box.setPadding(pad, pad, pad, pad)
        scroll.addView(box, ViewGroup.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))

        box.addView(title(getString(R.string.panel_title), 26f, TEXT))
        box.addView(title(getString(R.string.panel_version_fmt, BuildConfig.VERSION_NAME), 13f, MUTED))
        box.addView(title(getString(hintRes), 15f, MUTED).apply {
            setPadding(0, dp(14), 0, dp(6))
        })

        val field = EditText(this)
        field.hint = getString(R.string.url_hint)
        field.setText(prefs.getString(KEY_SERVER, ""))
        field.setSingleLine(true)
        field.setTextColor(Color.parseColor(TEXT))
        field.setHintTextColor(Color.parseColor(HINT))
        field.setBackgroundResource(R.drawable.bg_edit_url)
        field.setPadding(dp(14), dp(14), dp(14), dp(14))
        urlField = field
        box.addView(field, LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))

        box.addView(button(R.string.btn_open, true) { openFromField() })
        box.addView(button(R.string.btn_find, false) { startScan() })

        scanNote = title("", 13f, MUTED).apply { setPadding(0, dp(6), 0, 0) }
        box.addView(scanNote)

        box.addView(check(R.string.cb_awake, KEY_AWAKE, true) { applyKeepAwake() })
        box.addView(check(R.string.cb_kiosk, KEY_KIOSK, false) { applyKiosk() })

        root.removeAllViews()
        root.addView(scroll, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        panel = scroll
    }

    /** Экран «связи нет»: без него офлайн выглядел бы как выключенное приложение. */
    private fun showGone(textRes: Int) {
        val box = LinearLayout(this)
        box.orientation = LinearLayout.VERTICAL
        box.gravity = Gravity.CENTER
        box.setBackgroundColor(bgColor())
        val pad = dp(24)
        box.setPadding(pad, pad, pad, pad)
        box.addView(title(getString(textRes), 20f, TEXT))
        box.addView(title(getString(R.string.reconnect_none), 14f, MUTED).apply {
            setPadding(0, dp(10), 0, dp(16))
        })
        box.addView(button(R.string.btn_retry, true) {
            val saved = prefs.getString(KEY_SERVER, null)
            if (saved.isNullOrBlank()) showPanel(R.string.panel_hint_initial) else openPult(saved)
        })
        box.addView(button(R.string.btn_change_server, false) { showPanel(R.string.panel_hint_server) })
        root.removeAllViews()
        root.addView(box, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        panel = null
    }

    private fun showBusy(text: String) {
        val box = LinearLayout(this)
        box.orientation = LinearLayout.VERTICAL
        box.gravity = Gravity.CENTER
        box.setBackgroundColor(bgColor())
        box.addView(title(text, 16f, MUTED))
        root.removeAllViews()
        root.addView(box, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        panel = null
    }

    private fun openFromField() {
        val raw = urlField?.text?.toString().orEmpty()
        if (Net.normalize(raw) == null) {
            Toast.makeText(this, getString(R.string.err_bad_url), Toast.LENGTH_LONG).show()
            return
        }
        openPult(raw)
    }

    /** Поиск сервера: оператор не должен знать, какой у компьютера IP. */
    private fun startScan() {
        val note = scanNote ?: return
        note.text = getString(R.string.scanning)
        Thread {
            val found = Net.scan(onFound = { count ->
                runOnUiThread { note.text = getString(R.string.scan_found, count) }
            })
            runOnUiThread {
                when {
                    found.isEmpty() -> note.text = getString(R.string.scan_none)
                    found.size == 1 -> {
                        urlField?.setText(found[0].first)
                        note.text = getString(R.string.reconnect_found, found[0].first)
                        openPult(found[0].first)
                    }
                    else -> {
                        // Несколько серверов: показываем списком — выбирает
                        // человек, а не «угадайка».
                        val names = found.map { "${it.first} · v${it.second}" }
                        AlertDialog.Builder(this@MainActivity)
                            .setTitle(R.string.btn_find)
                            .setItems(names.toTypedArray()) { _, which ->
                                val base = found[which].first
                                urlField?.setText(base)
                                openPult(base)
                            }
                            .show()
                    }
                }
            }
        }.start()
    }

    // ------------------------------------------------------------ киоск/экран

    private fun applyKeepAwake() {
        if (prefs.getBoolean(KEY_AWAKE, true)) {
            window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        } else {
            window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        }
    }

    /**
     * Киоск: прячем статусбар и панель навигации. Выход из режима — долгое
     * нажатие «назад» (см. onKeyDown): иначе оператор остался бы в киоске
     * без единой видимой кнопки.
     */
    private fun applyKiosk() {
        val decor = window.decorView
        decor.systemUiVisibility = if (prefs.getBoolean(KEY_KIOSK, false)) {
            (View.SYSTEM_UI_FLAG_FULLSCREEN
                or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                or View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                or View.SYSTEM_UI_FLAG_LAYOUT_STABLE)
        } else {
            View.SYSTEM_UI_FLAG_VISIBLE
        }
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        // Системные панели возвращаются сами после свайпа — возвращаем киоск.
        if (hasFocus) applyKiosk()
    }

    override fun onResume() {
        super.onResume()
        applyKeepAwake()
        applyKiosk()
        checkForUpdate()
    }

    /**
     * Проверяем именно APK пульта, а не кассы: сервер хранит два манифеста и
     * два префикса файлов. Проверка ограничена разом в шесть часов, чтобы
     * возвращение из системного диалога не создавало очередь одинаковых окон.
     * Установка остаётся подтверждаемой системным Android-установщиком.
     */
    private fun checkForUpdate() {
        val base = prefs.getString(KEY_SERVER, "").orEmpty()
        if (base.isBlank()) return
        val now = System.currentTimeMillis()
        if (now - prefs.getLong(KEY_UPDATE_CHECK, 0L) < UPDATE_INTERVAL_MS) return
        prefs.edit().putLong(KEY_UPDATE_CHECK, now).apply()
        Thread {
            val json = Net.json(base, "/api/app/android?app=pult&installed=${BuildConfig.VERSION_CODE}")
                ?: return@Thread
            if (!json.optBoolean("available", false)
                || !json.optBoolean("update_available", false)) return@Thread
            val url = json.optString("url", "")
            val file = json.optString("file", "")
            val bytes = json.optLong("size_bytes", 0L)
            val version = json.optString("version", "")
            val sha = json.optString("sha256", "")
            if (url.isBlank() || file.isBlank() || bytes <= 0L || sha.isBlank()) return@Thread
            val changes = json.optString("changelog", "")
            val size = String.format(Locale.ROOT, "%.1f", bytes / 1024.0 / 1024.0)
            val message = buildString {
                if (changes.isNotBlank()) append("Что нового:\n\n${changes.take(600)}\n\n")
                append("Файл: $file · $size МБ\n")
                append("SHA-256: ${sha.take(16)}…\n\nСкачать и установить сейчас?")
            }
            runOnUiThread {
                AlertDialog.Builder(this)
                    .setTitle("Доступен пульт v$version")
                    .setMessage(message)
                    .setPositiveButton("Скачать") { _, _ -> openExternally("$base$url") }
                    .setNegativeButton("Позже", null)
                    .show()
            }
        }.start()
    }

    // ----------------------------------------------------------- клавиатура

    override fun onKeyDown(keyCode: Int, event: KeyEvent?): Boolean {
        if (keyCode != KeyEvent.KEYCODE_BACK) return super.onKeyDown(keyCode, event)
        val view = web
        if (panel == null && view != null && view.canGoBack()) {
            view.goBack()
            return true
        }
        // Киоск выключается долгим нажатием: короткое «назад» в цеху — обычное
        // движение, и случайный выход из киоска не должен ломать рабочий режим.
        if (event?.isLongPress == true && prefs.getBoolean(KEY_KIOSK, false)) {
            prefs.edit().putBoolean(KEY_KIOSK, false).apply()
            applyKiosk()
            Toast.makeText(this, getString(R.string.kiosk_on), Toast.LENGTH_LONG).show()
            return true
        }
        val now = System.currentTimeMillis()
        if (now - lastBackAt < 2000) {
            finish()
        } else {
            lastBackAt = now
            Toast.makeText(this, getString(R.string.exit_hint), Toast.LENGTH_SHORT).show()
        }
        return true
    }

    // ---------------------------------------------------------- мелкая сборка

    private fun bgColor(): Int = Color.parseColor(BG)

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    private fun title(text: CharSequence, size: Float, color: String): TextView =
        TextView(this).apply {
            this.text = text
            textSize = size
            setTextColor(Color.parseColor(color))
        }

    private fun button(textRes: Int, accent: Boolean, onClick: () -> Unit): Button =
        Button(this).apply {
            text = getString(textRes)
            textSize = 16f
            isAllCaps = false
            setTextColor(Color.parseColor(if (accent) "#ffffff" else TEXT))
            background = GradientDrawable().apply {
                cornerRadius = dp(12).toFloat()
                setColor(Color.parseColor(if (accent) ACCENT else PANEL))
                setStroke(dp(1), Color.parseColor(LINE))
            }
            val lp = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(52))
            lp.topMargin = dp(10)
            layoutParams = lp
            setOnClickListener { onClick() }
        }

    private fun check(textRes: Int, key: String, default: Boolean, onChange: () -> Unit): CheckBox =
        CheckBox(this).apply {
            text = getString(textRes)
            textSize = 14f
            setTextColor(Color.parseColor(MUTED))
            isChecked = prefs.getBoolean(key, default)
            setOnCheckedChangeListener { _, value ->
                prefs.edit().putBoolean(key, value).apply()
                onChange()
            }
        }

    private companion object {
        const val KEY_SERVER = "server"
        const val KEY_AWAKE = "awake"
        const val KEY_KIOSK = "kiosk"
        const val KEY_UPDATE_CHECK = "update_check_at"
        const val UPDATE_INTERVAL_MS = 6L * 60L * 60L * 1000L
        const val PULT_PATH = "/pult"
        // Цвета тёмной темы страницы пульта — из site/assets/tokens.css
        // (html[data-theme="dark"]). Держим их здесь, чтобы на стыке «экран
        // связи приложения → страница» не было видно двух разных программ;
        // расхождение ловит connector/tests/test_android_pult.py.
        const val BG = "#0b0f19"
        const val PANEL = "#111827"
        const val LINE = "#1e293b"
        const val ACCENT = "#6366f1"
        const val TEXT = "#f8fafc"
        const val MUTED = "#8191aa"
        const val HINT = "#8191aa"
    }
}
