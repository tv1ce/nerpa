package ru.tms.warehouse

import android.annotation.SuppressLint
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.view.View
import android.webkit.CookieManager
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.ProgressBar
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout

/**
 * Главный экран — полноэкранный WebView, открывающий мобильный интерфейс TMS.
 * Поддержка: pull-to-refresh, кнопка «назад», выбор файлов, экран ошибки
 * соединения со сменой адреса сервера, внешние ссылки (tel:/mailto:).
 */
class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private lateinit var swipe: SwipeRefreshLayout
    private lateinit var progress: ProgressBar
    private lateinit var errorView: View
    private var serverUrl: String = ""
    private var loadError = false

    // Загрузка файлов из <input type="file">
    private var filePathCallback: ValueCallback<Array<Uri>>? = null
    private val fileChooser =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
            val uris = WebChromeClient.FileChooserParams.parseResult(result.resultCode, result.data)
            filePathCallback?.onReceiveValue(uris)
            filePathCallback = null
        }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val prefs = getSharedPreferences(Prefs.NAME, MODE_PRIVATE)
        val saved = prefs.getString(Prefs.SERVER_URL, null)
        if (saved.isNullOrEmpty()) {
            startActivity(Intent(this, SetupActivity::class.java))
            finish()
            return
        }
        serverUrl = saved

        setContentView(R.layout.activity_main)
        webView = findViewById(R.id.webview)
        swipe = findViewById(R.id.swipe)
        progress = findViewById(R.id.progress)
        errorView = findViewById(R.id.errorView)

        configureWebView()

        swipe.setOnRefreshListener { webView.reload() }
        swipe.setColorSchemeResources(R.color.blue, R.color.orange)

        findViewById<Button>(R.id.btnRetry).setOnClickListener {
            errorView.visibility = View.GONE
            webView.visibility = View.VISIBLE
            webView.loadUrl(serverUrl + "/warehouse/")
        }
        findViewById<Button>(R.id.btnChangeServer).setOnClickListener {
            startActivity(Intent(this, SetupActivity::class.java))
            finish()
        }

        // Кнопка «назад» — навигация по истории WebView
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (webView.canGoBack()) webView.goBack() else finish()
            }
        })

        if (savedInstanceState == null) {
            webView.loadUrl("$serverUrl/warehouse/")
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun configureWebView() {
        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true            // localStorage для PWA-логики
            cacheMode = WebSettings.LOAD_DEFAULT
            useWideViewPort = true
            loadWithOverviewMode = true
            mediaPlaybackRequiresUserGesture = false
        }
        CookieManager.getInstance().setAcceptCookie(true)
        CookieManager.getInstance().setAcceptThirdPartyCookies(webView, true)

        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(
                view: WebView, request: WebResourceRequest
            ): Boolean {
                val url = request.url.toString()
                // Внешние схемы (звонок, почта) — отдаём системе
                if (url.startsWith("tel:") || url.startsWith("mailto:") ||
                    url.startsWith("geo:") || url.startsWith("whatsapp:")
                ) {
                    startActivity(Intent(Intent.ACTION_VIEW, request.url))
                    return true
                }
                // Свой сервер — внутри WebView; чужие http(s) — во внешний браузер
                return if (url.startsWith(serverUrl)) {
                    false
                } else if (url.startsWith("http")) {
                    startActivity(Intent(Intent.ACTION_VIEW, request.url))
                    true
                } else false
            }

            override fun onPageStarted(view: WebView?, url: String?, favicon: android.graphics.Bitmap?) {
                loadError = false
                progress.visibility = View.VISIBLE
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                progress.visibility = View.GONE
                swipe.isRefreshing = false
                if (!loadError) {
                    errorView.visibility = View.GONE
                    webView.visibility = View.VISIBLE
                }
            }

            override fun onReceivedError(
                view: WebView, request: WebResourceRequest, error: WebResourceError
            ) {
                // Ошибка только для главного фрейма (не для подресурсов)
                if (request.isForMainFrame) {
                    loadError = true
                    swipe.isRefreshing = false
                    progress.visibility = View.GONE
                    webView.visibility = View.GONE
                    errorView.visibility = View.VISIBLE
                }
            }
        }

        webView.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(
                webView: WebView,
                callback: ValueCallback<Array<Uri>>,
                params: FileChooserParams
            ): Boolean {
                filePathCallback?.onReceiveValue(null)
                filePathCallback = callback
                return try {
                    fileChooser.launch(params.createIntent())
                    true
                } catch (e: Exception) {
                    filePathCallback = null
                    false
                }
            }
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        webView.saveState(outState)
    }

    override fun onRestoreInstanceState(savedInstanceState: Bundle) {
        super.onRestoreInstanceState(savedInstanceState)
        webView.restoreState(savedInstanceState)
    }
}
