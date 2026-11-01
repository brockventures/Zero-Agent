#pragma once
// ============================================================================
// GOTCHI V1 — QR Soft-AP Captive Portal Onboarding (T2.7, 192.168.4.1)
// ============================================================================

#include <Arduino.h>
#include <WebServer.h>

namespace gotchi_portal {

// Format standard WIFI:S:<ssid>;T:WPA;P:<pass>;; string for 200x200 e-paper QR code
String buildWifiQrPayload(const char* ap_ssid);

// Start 192.168.4.1 Soft-AP and register mobile HTML provisioning routes on WebServer
bool startSoftApPortal(WebServer& server, const char* ap_ssid = "GOTCHI-SETUP");

} // namespace gotchi_portal
