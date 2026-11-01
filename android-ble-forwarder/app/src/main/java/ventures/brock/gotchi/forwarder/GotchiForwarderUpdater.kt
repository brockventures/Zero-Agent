package ventures.brock.gotchi.forwarder

import android.content.ContentProvider
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.database.Cursor
import android.database.MatrixCursor
import android.net.Uri
import android.os.Build
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
 * Built-in Wi-Fi Self-Updater & Remote Config Synchronizer for GOTCHI Phone BLE Forwarder.
 *
 * Capabilities:
 * 1. `checkRemoteUpdate`: Queries Host 2 Voice Gateway (`GET /api/gotchi/forwarder/config?node_id=...`)
 *    for newer APK version codes and triggers update prompts.
 * 2. `downloadAndInstallApk`: Downloads latest release APK (`GET /api/gotchi/forwarder/apk.apk`),
 *    writes to the internal updates cache, and launches package installer via `Intent.ACTION_VIEW`
 *    with `FLAG_GRANT_READ_URI_PERMISSION` and `application/vnd.android.package-archive`.
 * 3. `FileProvider` / `GotchiFileProvider`: Standalone ContentProvider serving APK update
 *    streams directly to PackageInstaller without requiring static XML path resource maps.
 */
object GotchiForwarderUpdater {

    private const val TAG = "GotchiForwarderUpdater"
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
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                context.packageManager.getPackageInfo(context.packageName, 0).longVersionCode.toInt()
            } else {
                @Suppress("DEPRECATION")
                context.packageManager.getPackageInfo(context.packageName, 0).versionCode
            }
        } catch (e: Exception) {
            Log.w(TAG, "Failed to get installed versionCode: ${e.message}")
            0
        }
    }

    /**
     * Checks Host 2 Voice Gateway for an updated forwarder APK version code.
     * Invokes onUpdateAvailable(remoteVersion) if remoteVersion > installedVersion.
     */
    fun checkRemoteUpdate(
        context: Context,
        onUpdateAvailable: ((remoteVersion: Int) -> Unit)? = null
    ) {
        checkRemoteUpdate(
            context = context,
            gatewayUrl = "",
            bearerToken = "",
            nodeId = "",
            onUpdateAvailable = onUpdateAvailable,
            onComplete = null
        )
    }

    /**
     * Checks Host 2 Voice Gateway for an updated forwarder APK version code with explicit parameters.
     */
    fun checkRemoteUpdate(
        context: Context,
        gatewayUrl: String = "",
        bearerToken: String = "",
        nodeId: String = "",
        onUpdateAvailable: ((remoteVersion: Int) -> Unit)? = null,
        onComplete: ((Boolean) -> Unit)? = null
    ) {
        val prefs = context.getSharedPreferences(GotchiBleRelayService.PREFS_NAME, Context.MODE_PRIVATE)
        val resolvedGatewayUrl = if (gatewayUrl.isNotBlank()) {
            gatewayUrl
        } else {
            prefs.getString(GotchiBleRelayService.KEY_GATEWAY_URL, GotchiBleRelayService.DEFAULT_GATEWAY_URL)
                ?: GotchiBleRelayService.DEFAULT_GATEWAY_URL
        }
        val resolvedToken = if (bearerToken.isNotBlank()) {
            bearerToken
        } else {
            prefs.getString(GotchiBleRelayService.KEY_BEARER_TOKEN, "") ?: ""
        }
        val resolvedNodeId = if (nodeId.isNotBlank()) {
            nodeId
        } else {
            prefs.getString(GotchiBleRelayService.KEY_NODE_ID, GotchiBleRelayService.DEFAULT_NODE_ID)
                ?: GotchiBleRelayService.DEFAULT_NODE_ID
        }

        val safeBaseUrl = normalizeGatewayUrl(resolvedGatewayUrl)
        val configUrl = "${safeBaseUrl.trimEnd('/')}/api/gotchi/forwarder/config?node_id=${resolvedNodeId}"

        scope.launch(Dispatchers.IO) {
            try {
                val reqBuilder = Request.Builder()
                    .url(configUrl)
                    .get()

                val token = resolvedToken.trim()
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
                val remoteVersion = json.optInt("version_code", 0)
                val installedVersion = getInstalledVersionCode(context)

                withContext(Dispatchers.Main) {
                    if (remoteVersion > installedVersion) {
                        Log.i(TAG, "Newer remote APK version detected: $remoteVersion > $installedVersion")
                        onUpdateAvailable?.invoke(remoteVersion)
                    }
                    onComplete?.invoke(true)
                }
            } catch (e: Exception) {
                Log.w(TAG, "Failed to check remote update: ${e.message}")
                withContext(Dispatchers.Main) { onComplete?.invoke(false) }
            }
        }
    }

    /**
     * Overload supporting gatewayUrl and nodeId as leading parameters.
     */
    fun checkRemoteUpdate(
        gatewayUrl: String,
        nodeId: String,
        context: Context? = null,
        bearerToken: String = "",
        onUpdateAvailable: ((Int) -> Unit)? = null,
        onComplete: ((Boolean) -> Unit)? = null
    ) {
        if (context == null) {
            onComplete?.invoke(false)
            return
        }
        checkRemoteUpdate(
            context = context,
            gatewayUrl = gatewayUrl,
            bearerToken = bearerToken,
            nodeId = nodeId,
            onUpdateAvailable = onUpdateAvailable,
            onComplete = onComplete
        )
    }

    /**
     * Downloads APK update from gateway and hands off to Android PackageInstaller.
     */
    fun downloadAndInstallApk(
        context: Context,
        gatewayUrl: String = "",
        onStatus: ((String) -> Unit)? = null
    ) {
        val prefs = context.getSharedPreferences(GotchiBleRelayService.PREFS_NAME, Context.MODE_PRIVATE)
        val resolvedGatewayUrl = if (gatewayUrl.isNotBlank()) {
            gatewayUrl
        } else {
            prefs.getString(GotchiBleRelayService.KEY_GATEWAY_URL, GotchiBleRelayService.DEFAULT_GATEWAY_URL)
                ?: GotchiBleRelayService.DEFAULT_GATEWAY_URL
        }
        val safeBaseUrl = normalizeGatewayUrl(resolvedGatewayUrl)
        val apkUrl = "${safeBaseUrl.trimEnd('/')}/api/gotchi/forwarder/apk.apk"

        scope.launch(Dispatchers.IO) {
            try {
                withContext(Dispatchers.Main) { onStatus?.invoke("CONNECTING...") }

                val bearerToken = prefs.getString(GotchiBleRelayService.KEY_BEARER_TOKEN, "") ?: ""
                val reqBuilder = Request.Builder()
                    .url(apkUrl)
                    .get()

                if (bearerToken.isNotBlank()) {
                    reqBuilder.addHeader("Authorization", "Bearer $bearerToken")
                }

                val response = httpClient.newCall(reqBuilder.build()).execute()
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

                val apkFile = File(updateDir, "gotchi-ble-forwarder.apk")
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
        val trimmed = url.trim().removeSuffix("/api/voice/interact").trimEnd('/')
        if (trimmed.isEmpty()) {
            return GotchiBleRelayService.DEFAULT_GATEWAY_URL
        }
        return trimmed
    }
}

/**
 * Custom FileProvider serving cached APK payloads to the PackageInstaller.
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
