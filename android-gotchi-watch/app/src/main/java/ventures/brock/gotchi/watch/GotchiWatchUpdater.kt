package ventures.brock.gotchi.watch

import android.content.ContentProvider
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.database.Cursor
import android.database.MatrixCursor
import android.net.Uri
import android.os.ParcelFileDescriptor
import android.provider.OpenableColumns
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.io.File
import java.io.FileNotFoundException
import java.io.FileOutputStream
import java.util.concurrent.TimeUnit

/**
 * Built-in Wi-Fi Self-Updater & Live Remote Config Synchronizer for GOTCHI Pixel Watch.
 *
 * Capabilities:
 * 1. `syncRemoteConfig`: Fetches live sensitivity and silence thresholds from Host 2 gateway
 *    (`GET /api/gotchi/watch/config?node_id=...`) and hot-reloads:
 *    - `pinchDetector.minLinearAccelThreshold`
 *    - `pinchDetector.maxGyroThreshold`
 *    - `pinchDetector.requireDoubleImpulse`
 *    - `engine.config.pttSilenceMs`
 * 2. `downloadAndInstallApk`: Downloads latest release APK (`GET /api/gotchi/watch/apk.apk`),
 *    writes to the internal updates cache, and launches package installer via `Intent.ACTION_VIEW`
 *    with `FLAG_GRANT_READ_URI_PERMISSION` and `application/vnd.android.package-archive`.
 * 3. `FileProvider` / `GotchiFileProvider`: Standalone ContentProvider serving APK update
 *    streams directly to PackageInstaller without requiring static XML path resource maps.
 */
object GotchiWatchUpdater {

    private const val TAG = "GotchiWatchUpdater"
    private const val DEFAULT_TIMEOUT_SECONDS = 30L

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    private val httpClient: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(DEFAULT_TIMEOUT_SECONDS, TimeUnit.SECONDS)
            .readTimeout(60L, TimeUnit.SECONDS)
            .writeTimeout(DEFAULT_TIMEOUT_SECONDS, TimeUnit.SECONDS)
            .build()
    }

    /**
     * Retrieves the installed APK versionCode for this package.
     */
    fun getInstalledVersionCode(context: Context): Int {
        return try {
            context.packageManager.getPackageInfo(context.packageName, 0).longVersionCode.toInt()
        } catch (e: Exception) {
            Log.w(TAG, "Failed to get installed versionCode: ${e.message}")
            0
        }
    }

    /**
     * Synchronizes IMU sensitivity thresholds and silence timeouts with Host 2 Voice Gateway.
     */
    fun syncRemoteConfig(
        gatewayUrl: String,
        nodeId: String,
        pinchDetector: PinchGestureDetector,
        engine: GotchiWatchEngine,
        epaperView: GotchiEpaperView,
        context: Context? = null,
        onUpdateAvailable: ((Int) -> Unit)? = null,
        onComplete: ((Boolean) -> Unit)? = null
    ) {
        val safeBaseUrl = normalizeGatewayUrl(gatewayUrl)
        val configUrl = "${safeBaseUrl.trimEnd('/')}/api/gotchi/watch/config?node_id=${nodeId}"

        scope.launch(Dispatchers.IO) {
            try {
                val reqBuilder = Request.Builder()
                    .url(configUrl)
                    .get()

                val token = engine.config.bearerToken.trim()
                if (token.isNotEmpty()) {
                    reqBuilder.addHeader("Authorization", "Bearer $token")
                }

                val response = httpClient.newCall(reqBuilder.build()).execute()
                if (!response.isSuccessful) {
                    Log.w(TAG, "Remote config sync returned HTTP ${response.code}")
                    withContext(Dispatchers.Main) { onComplete?.invoke(false) }
                    return@launch
                }

                val rawBody = response.body?.string() ?: ""
                if (rawBody.isEmpty()) {
                    withContext(Dispatchers.Main) { onComplete?.invoke(false) }
                    return@launch
                }

                val json = JSONObject(rawBody)
                val minAccel = json.optDouble("min_linear_accel", pinchDetector.minLinearAccelThreshold.toDouble()).toFloat()
                val maxGyro = json.optDouble("max_gyro", pinchDetector.maxGyroThreshold.toDouble()).toFloat()
                val requireDouble = json.optBoolean("require_double_impulse", pinchDetector.requireDoubleImpulse)
                val pttSilence = json.optLong("ptt_silence_ms", engine.config.pttSilenceMs)
                val remoteVersion = json.optInt("version_code", 0)

                withContext(Dispatchers.Main) {
                    pinchDetector.minLinearAccelThreshold = minAccel
                    pinchDetector.maxGyroThreshold = maxGyro
                    pinchDetector.requireDoubleImpulse = requireDouble
                    engine.config.pttSilenceMs = pttSilence
                    Log.i(TAG, "Applied remote config: accel=$minAccel gyro=$maxGyro doublePinch=$requireDouble pttSilence=${pttSilence}ms")
                    if (context != null && remoteVersion > getInstalledVersionCode(context)) {
                        Log.i(TAG, "Newer remote APK version detected: $remoteVersion > ${getInstalledVersionCode(context)}")
                        onUpdateAvailable?.invoke(remoteVersion)
                    }
                    onComplete?.invoke(true)
                }
            } catch (e: Exception) {
                Log.w(TAG, "Failed to sync remote config: ${e.message}")
                withContext(Dispatchers.Main) { onComplete?.invoke(false) }
            }
        }
    }

    /**
     * Downloads APK update from gateway and hands off to Android PackageInstaller.
     */
    fun downloadAndInstallApk(
        context: Context,
        gatewayUrl: String,
        onStatus: ((String) -> Unit)? = null
    ) {
        val safeBaseUrl = normalizeGatewayUrl(gatewayUrl)
        val apkUrl = "${safeBaseUrl.trimEnd('/')}/api/gotchi/watch/apk.apk"

        scope.launch(Dispatchers.IO) {
            try {
                withContext(Dispatchers.Main) { onStatus?.invoke("CONNECTING...") }

                val request = Request.Builder()
                    .url(apkUrl)
                    .get()
                    .build()

                val response = httpClient.newCall(request).execute()
                if (!response.isSuccessful) {
                    val msg = "DOWNLOAD FAILED (${response.code})"
                    Log.e(TAG, msg)
                    withContext(Dispatchers.Main) { onStatus?.invoke(msg) }
                    return@launch
                }

                val responseBody = response.body
                if (responseBody == null) {
                    withContext(Dispatchers.Main) { onStatus?.invoke("EMPTY APK RESPONSE") }
                    return@launch
                }

                withContext(Dispatchers.Main) { onStatus?.invoke("DOWNLOADING APK...") }

                val baseDir = context.externalCacheDir ?: context.cacheDir
                val updateDir = File(baseDir, "updates")
                if (!updateDir.exists()) {
                    updateDir.mkdirs()
                }

                val apkFile = File(updateDir, "gotchi-watch.apk")
                if (apkFile.exists()) {
                    apkFile.delete()
                }

                responseBody.byteStream().use { input ->
                    FileOutputStream(apkFile).use { output ->
                        input.copyTo(output)
                    }
                }

                Log.i(TAG, "Downloaded ${apkFile.length()} bytes to ${apkFile.absolutePath}")
                withContext(Dispatchers.Main) {
                    onStatus?.invoke("LAUNCHING INSTALLER...")
                    launchPackageInstaller(context, apkFile)
                }
            } catch (e: Exception) {
                val err = "UPDATE ERROR: ${e.message}"
                Log.e(TAG, err, e)
                withContext(Dispatchers.Main) { onStatus?.invoke(err) }
            }
        }
    }

    private fun launchPackageInstaller(context: Context, apkFile: File) {
        val apkUri = FileProvider.getUriForFile(context, apkFile)
        val installIntent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(apkUri, "application/vnd.android.package-archive")
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        context.startActivity(installIntent)
    }

    private fun normalizeGatewayUrl(url: String): String {
        val trimmed = url.trim()
        if (trimmed.isEmpty()) {
            return GotchiWatchEngine.DEFAULT_GATEWAY_URL
        }
        return if (trimmed.contains(":8765")) {
            trimmed.replace(":8765", ":8769")
        } else {
            trimmed
        }
    }
}

/**
 * Custom FileProvider serving cached APK payloads to the Wear OS PackageInstaller.
 */
open class GotchiFileProvider : ContentProvider() {

    companion object {
        const val AUTHORITY_SUFFIX = ".fileprovider"

        fun getUriForFile(context: Context, file: File): Uri {
            val authority = "${context.packageName}$AUTHORITY_SUFFIX"
            return Uri.Builder()
                .scheme("content")
                .authority(authority)
                .encodedPath(Uri.encode(file.absolutePath))
                .build()
        }
    }

    override fun onCreate(): Boolean = true

    override fun getType(uri: Uri): String = "application/vnd.android.package-archive"

    override fun query(
        uri: Uri,
        projection: Array<out String>?,
        selection: String?,
        selectionArgs: Array<out String>?,
        sortOrder: String?
    ): Cursor {
        val file = getFileForUri(uri)
        val proj = projection ?: arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE)
        val cursor = MatrixCursor(proj, 1)
        val row = cursor.newRow()
        for (col in proj) {
            when (col) {
                OpenableColumns.DISPLAY_NAME -> row.add(file.name)
                OpenableColumns.SIZE -> row.add(file.length())
                else -> row.add(null)
            }
        }
        return cursor
    }

    override fun openFile(uri: Uri, mode: String): ParcelFileDescriptor? {
        val file = getFileForUri(uri)
        if (!file.exists()) {
            throw FileNotFoundException("APK file not found: ${file.absolutePath}")
        }
        return ParcelFileDescriptor.open(file, ParcelFileDescriptor.MODE_READ_ONLY)
    }

    override fun insert(uri: Uri, values: ContentValues?): Uri? = null
    override fun update(uri: Uri, values: ContentValues?, selection: String?, selectionArgs: Array<out String>?): Int = 0
    override fun delete(uri: Uri, selection: String?, selectionArgs: Array<out String>?): Int = 0

    private fun getFileForUri(uri: Uri): File {
        val rawPath = uri.encodedPath ?: ""
        val decoded = Uri.decode(rawPath)
        return File(decoded)
    }
}

/**
 * Standard FileProvider alias for manifest declaration.
 */
class FileProvider : GotchiFileProvider() {
    companion object {
        const val AUTHORITY_SUFFIX = GotchiFileProvider.AUTHORITY_SUFFIX

        fun getUriForFile(context: Context, file: File): Uri =
            GotchiFileProvider.getUriForFile(context, file)
    }
}
