package ventures.brock.gotchi.forwarder

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build

/**
 * Automatically starts GotchiBleRelayService when the Android phone boots or updates the package.
 */
class BootCompletedReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED ||
            intent.action == Intent.ACTION_MY_PACKAGE_REPLACED
        ) {
            val prefs = context.getSharedPreferences(GotchiBleRelayService.PREFS_NAME, Context.MODE_PRIVATE)
            val relayEnabled = prefs.getBoolean(GotchiBleRelayService.KEY_RELAY_ENABLED, true)
            if (!relayEnabled) {
                return
            }
            val serviceIntent = Intent(context, GotchiBleRelayService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(serviceIntent)
            } else {
                context.startService(serviceIntent)
            }
        }
    }
}
