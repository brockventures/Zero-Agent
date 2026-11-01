#include "onboarding_portal.h"
#include <WiFi.h>
#include <Preferences.h>

namespace gotchi_portal {

static const char CAPTIVE_HTML[] PROGMEM = R"rawliteral(
<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GOTCHI V1 Setup</title>
<style>body{font-family:system-ui;background:#0f172a;color:#f8fafc;padding:24px;max-width:420px;margin:auto}
input,button{width:100%;padding:12px;margin:8px 0;border-radius:8px;border:1px solid #334155;box-sizing:border-box}
button{background:#38bdf8;color:#0f172a;font-weight:700;border:none;cursor:pointer}</style></head>
<body><h2>GOTCHI V1 Onboarding</h2>
<form method="POST" action="/provision">
<label>Wi-Fi SSID</label><input name="ssid" required placeholder="Home Wi-Fi SSID">
<label>Wi-Fi Password</label><input name="psk" type="password" placeholder="Wi-Fi Password">
<label>Voice Gateway URL</label><input name="hub" value="http://gotchi.local:8765">
<label>Node ID</label><input name="node_id" value="gotchi-v1-01">
<button type="submit">Save & Connect</button></form></body></html>
)rawliteral";

String buildWifiQrPayload(const char* ap_ssid) {
    return String("WIFI:S:") + ap_ssid + ";T:nopass;;";
}

bool startSoftApPortal(WebServer& server, const char* ap_ssid) {
    WiFi.mode(WIFI_AP_STA);
    IPAddress ip(192, 168, 4, 1);
    IPAddress gw(192, 168, 4, 1);
    IPAddress mask(255, 255, 255, 0);
    WiFi.softAPConfig(ip, gw, mask);
    bool ok = WiFi.softAP(ap_ssid);

    server.on("/", HTTP_GET, [&server]() {
        server.send_P(200, "text/html", CAPTIVE_HTML);
    });
    server.on("/provision", HTTP_POST, [&server]() {
        Preferences prefs;
        prefs.begin("gotchi", false);
        if (server.hasArg("ssid")) {
            String ssid = server.arg("ssid");
            ssid.trim();
            prefs.putString("wifi_ssid", ssid);
        }
        if (server.hasArg("psk"))  prefs.putString("wifi_psk", server.arg("psk"));
        if (server.hasArg("hub")) {
            String hub = server.arg("hub");
            hub.trim();
            prefs.putString("hub_url", hub);
        }
        if (server.hasArg("node_id")) {
            String nid = server.arg("node_id");
            nid.trim();
            prefs.putString("node_id", nid);
        }
        prefs.end();
        server.send(200, "text/plain", "Saved! Rebooting GOTCHI onto Wi-Fi...");
        delay(400);
        ESP.restart();
    });
    return ok;
}

} // namespace gotchi_portal
