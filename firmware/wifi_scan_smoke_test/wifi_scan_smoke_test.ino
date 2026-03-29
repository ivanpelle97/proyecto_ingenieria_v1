#include <Arduino.h>
#include <WiFi.h>
#include <esp_system.h>

// -------------------- CONFIG --------------------
#define WIFI_SSID "WIFI EXPRESS 2.4"
static const uint32_t SCAN_INTERVAL_MS = 8000;
// ------------------------------------------------

static uint32_t last_scan_at = 0;
static uint32_t scan_count = 0;

static const char* reset_reason_name(esp_reset_reason_t reason) {
  switch (reason) {
    case ESP_RST_UNKNOWN: return "UNKNOWN";
    case ESP_RST_POWERON: return "POWERON";
    case ESP_RST_EXT: return "EXT";
    case ESP_RST_SW: return "SW";
    case ESP_RST_PANIC: return "PANIC";
    case ESP_RST_INT_WDT: return "INT_WDT";
    case ESP_RST_TASK_WDT: return "TASK_WDT";
    case ESP_RST_WDT: return "WDT";
    case ESP_RST_DEEPSLEEP: return "DEEPSLEEP";
    case ESP_RST_BROWNOUT: return "BROWNOUT";
    case ESP_RST_SDIO: return "SDIO";
    default: return "OTHER";
  }
}

static const char* auth_name(wifi_auth_mode_t auth) {
  switch (auth) {
    case WIFI_AUTH_OPEN: return "OPEN";
    case WIFI_AUTH_WEP: return "WEP";
    case WIFI_AUTH_WPA_PSK: return "WPA_PSK";
    case WIFI_AUTH_WPA2_PSK: return "WPA2_PSK";
    case WIFI_AUTH_WPA_WPA2_PSK: return "WPA_WPA2_PSK";
    case WIFI_AUTH_WPA2_ENTERPRISE: return "WPA2_ENTERPRISE";
    case WIFI_AUTH_WPA3_PSK: return "WPA3_PSK";
    case WIFI_AUTH_WPA2_WPA3_PSK: return "WPA2_WPA3_PSK";
    case WIFI_AUTH_WAPI_PSK: return "WAPI_PSK";
    default: return "OTHER";
  }
}

static void print_banner() {
  Serial.println("=== WIFI SCAN SMOKE TEST ===");
  Serial.printf("RESET reason=%d (%s)\n", (int)esp_reset_reason(), reset_reason_name(esp_reset_reason()));
  Serial.printf("SSID objetivo=%s\n", WIFI_SSID);
  Serial.printf("Intervalo escaneo=%lu ms\n", (unsigned long)SCAN_INTERVAL_MS);
}

static void run_scan() {
  scan_count++;
  Serial.printf("\n[%lu] SCAN #%lu inicio\n", (unsigned long)millis(), (unsigned long)scan_count);

  WiFi.scanDelete();
  int found = WiFi.scanNetworks(false, true, false, 300, 0, nullptr);

  if (found < 0) {
    Serial.printf("[%lu] SCAN error=%d\n", (unsigned long)millis(), found);
    return;
  }

  Serial.printf("[%lu] SCAN redes detectadas=%d\n", (unsigned long)millis(), found);

  bool target_seen = false;
  for (int i = 0; i < found; ++i) {
    String ssid = WiFi.SSID(i);
    int32_t rssi = WiFi.RSSI(i);
    uint8_t channel = WiFi.channel(i);
    wifi_auth_mode_t auth = WiFi.encryptionType(i);
    String bssid = WiFi.BSSIDstr(i);

    Serial.printf(
      "  - %02d | ssid=%s | rssi=%ld | canal=%u | auth=%s | bssid=%s\n",
      i + 1,
      ssid.c_str(),
      (long)rssi,
      (unsigned)channel,
      auth_name(auth),
      bssid.c_str()
    );

    if (ssid == WIFI_SSID) {
      target_seen = true;
      Serial.printf("    -> OBJETIVO detectado: rssi=%ld canal=%u bssid=%s\n",
                    (long)rssi,
                    (unsigned)channel,
                    bssid.c_str());
    }
  }

  if (!target_seen) {
    Serial.println("    -> OBJETIVO no detectado en este escaneo");
  }

  WiFi.scanDelete();
}

void setup() {
  Serial.begin(115200);
  delay(1200);
  print_banner();

  WiFi.mode(WIFI_STA);
  WiFi.persistent(false);
  WiFi.setSleep(false);
  WiFi.disconnect(true, true);
  delay(200);

  run_scan();
  last_scan_at = millis();
}

void loop() {
  uint32_t now = millis();
  if ((now - last_scan_at) >= SCAN_INTERVAL_MS) {
    last_scan_at = now;
    run_scan();
  }
  delay(100);
}
