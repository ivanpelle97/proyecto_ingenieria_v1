#include <Arduino.h>
#include <WiFi.h>
#include <esp_system.h>

// -------------------- CONFIG --------------------
#define WIFI_SSID "WIFI EXPRESS 2.4"
#define WIFI_PASS "00416149152"

static const bool USE_CHANNEL_HINT = false;
static const uint8_t WIFI_AP_CHANNEL = 1;
static const bool USE_BSSID_LOCK = false;
static uint8_t WIFI_AP_BSSID[6] = {0x84, 0x01, 0x12, 0x89, 0x2F, 0x8C};
static const bool WIFI_DISABLE_SLEEP = true;

static const uint32_t CONNECT_TIMEOUT_MS = 15000;
static const uint32_t RETRY_BACKOFF_MS = 2500;
static const uint32_t STATUS_INTERVAL_MS = 5000;
// ------------------------------------------------

static bool got_ip = false;
static bool connect_in_progress = false;
static uint32_t connect_started_at = 0;
static uint32_t last_connect_attempt_at = 0;
static uint32_t last_status_at = 0;
static uint32_t connect_attempt = 0;

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

static const char* disconnect_reason_name(uint8_t reason) {
  switch (reason) {
    case WIFI_REASON_UNSPECIFIED: return "UNSPECIFIED";
    case WIFI_REASON_AUTH_EXPIRE: return "AUTH_EXPIRE";
    case WIFI_REASON_AUTH_LEAVE: return "AUTH_LEAVE";
    case WIFI_REASON_ASSOC_EXPIRE: return "ASSOC_EXPIRE";
    case WIFI_REASON_ASSOC_TOOMANY: return "ASSOC_TOOMANY";
    case WIFI_REASON_NOT_AUTHED: return "NOT_AUTHED";
    case WIFI_REASON_NOT_ASSOCED: return "NOT_ASSOCED";
    case WIFI_REASON_ASSOC_LEAVE: return "ASSOC_LEAVE";
    case WIFI_REASON_BEACON_TIMEOUT: return "BEACON_TIMEOUT";
    case WIFI_REASON_NO_AP_FOUND: return "NO_AP_FOUND";
    case WIFI_REASON_AUTH_FAIL: return "AUTH_FAIL";
    case WIFI_REASON_ASSOC_FAIL: return "ASSOC_FAIL";
    case WIFI_REASON_HANDSHAKE_TIMEOUT: return "HANDSHAKE_TIMEOUT";
    default: return "OTHER";
  }
}

static void print_banner() {
  Serial.println("=== WIFI CONNECT SMOKE TEST ===");
  Serial.printf("RESET reason=%d (%s)\n", (int)esp_reset_reason(), reset_reason_name(esp_reset_reason()));
  Serial.printf("SSID=%s\n", WIFI_SSID);
  Serial.printf("USE_CHANNEL_HINT=%s\n", USE_CHANNEL_HINT ? "true" : "false");
  Serial.printf("WIFI_AP_CHANNEL=%u\n", (unsigned)WIFI_AP_CHANNEL);
  Serial.printf("USE_BSSID_LOCK=%s\n", USE_BSSID_LOCK ? "true" : "false");
  if (USE_BSSID_LOCK) {
    Serial.printf(
      "WIFI_AP_BSSID=%02X:%02X:%02X:%02X:%02X:%02X\n",
      WIFI_AP_BSSID[0], WIFI_AP_BSSID[1], WIFI_AP_BSSID[2],
      WIFI_AP_BSSID[3], WIFI_AP_BSSID[4], WIFI_AP_BSSID[5]
    );
  }
  Serial.printf("WIFI_DISABLE_SLEEP=%s\n", WIFI_DISABLE_SLEEP ? "true" : "false");
}

static void start_connect_attempt() {
  connect_attempt++;
  connect_in_progress = true;
  got_ip = false;
  connect_started_at = millis();
  last_connect_attempt_at = connect_started_at;

  Serial.printf("[%lu] WIFI: intento %lu\n", (unsigned long)millis(), (unsigned long)connect_attempt);
  WiFi.disconnect(true, true);
  delay(150);

  if (USE_BSSID_LOCK) {
    Serial.printf(
      "[%lu] WIFI: begin(ssid, pass, canal=%u, bssid=%02X:%02X:%02X:%02X:%02X:%02X)\n",
      (unsigned long)millis(),
      (unsigned)WIFI_AP_CHANNEL,
      WIFI_AP_BSSID[0], WIFI_AP_BSSID[1], WIFI_AP_BSSID[2],
      WIFI_AP_BSSID[3], WIFI_AP_BSSID[4], WIFI_AP_BSSID[5]
    );
    WiFi.begin(WIFI_SSID, WIFI_PASS, WIFI_AP_CHANNEL, WIFI_AP_BSSID);
  } else if (USE_CHANNEL_HINT) {
    Serial.printf("[%lu] WIFI: begin(ssid, pass, canal=%u)\n", (unsigned long)millis(), (unsigned)WIFI_AP_CHANNEL);
    WiFi.begin(WIFI_SSID, WIFI_PASS, WIFI_AP_CHANNEL);
  } else {
    Serial.printf("[%lu] WIFI: begin(ssid, pass)\n", (unsigned long)millis());
    WiFi.begin(WIFI_SSID, WIFI_PASS);
  }
}

static void print_status() {
  wl_status_t status = WiFi.status();
  Serial.printf(
    "[%lu] STAT: status=%d connected=%s ip=%s rssi=%d canal=%d bssid=%s heap=%u\n",
    (unsigned long)millis(),
    (int)status,
    status == WL_CONNECTED ? "true" : "false",
    WiFi.localIP().toString().c_str(),
    status == WL_CONNECTED ? WiFi.RSSI() : 0,
    status == WL_CONNECTED ? WiFi.channel() : 0,
    status == WL_CONNECTED ? WiFi.BSSIDstr().c_str() : "-",
    (unsigned)ESP.getFreeHeap()
  );
}

static void on_wifi_event(WiFiEvent_t event, WiFiEventInfo_t info) {
  switch (event) {
    case ARDUINO_EVENT_WIFI_READY:
      Serial.printf("[%lu] EV: WIFI_READY\n", (unsigned long)millis());
      break;
    case ARDUINO_EVENT_WIFI_STA_START:
      Serial.printf("[%lu] EV: STA_START\n", (unsigned long)millis());
      break;
    case ARDUINO_EVENT_WIFI_STA_CONNECTED:
      Serial.printf("[%lu] EV: STA_CONNECTED ssid=%s canal=%d\n",
                    (unsigned long)millis(),
                    reinterpret_cast<const char*>(info.wifi_sta_connected.ssid),
                    info.wifi_sta_connected.channel);
      break;
    case ARDUINO_EVENT_WIFI_STA_GOT_IP:
      got_ip = true;
      connect_in_progress = false;
      Serial.printf("[%lu] EV: GOT_IP ip=%s gateway=%s subnet=%s\n",
                    (unsigned long)millis(),
                    IPAddress(info.got_ip.ip_info.ip.addr).toString().c_str(),
                    IPAddress(info.got_ip.ip_info.gw.addr).toString().c_str(),
                    IPAddress(info.got_ip.ip_info.netmask.addr).toString().c_str());
      break;
    case ARDUINO_EVENT_WIFI_STA_DISCONNECTED:
      got_ip = false;
      connect_in_progress = false;
      Serial.printf("[%lu] EV: STA_DISCONNECTED reason=%u (%s)\n",
                    (unsigned long)millis(),
                    (unsigned)info.wifi_sta_disconnected.reason,
                    disconnect_reason_name(info.wifi_sta_disconnected.reason));
      break;
    case ARDUINO_EVENT_WIFI_STA_LOST_IP:
      got_ip = false;
      Serial.printf("[%lu] EV: LOST_IP\n", (unsigned long)millis());
      break;
    case ARDUINO_EVENT_WIFI_SCAN_DONE:
      Serial.printf("[%lu] EV: SCAN_DONE\n", (unsigned long)millis());
      break;
    default:
      break;
  }
}

void setup() {
  Serial.begin(115200);
  delay(1200);
  print_banner();

  WiFi.mode(WIFI_STA);
  WiFi.persistent(false);
  if (WIFI_DISABLE_SLEEP) {
    WiFi.setSleep(false);
  }
  WiFi.onEvent(on_wifi_event);

  start_connect_attempt();
}

void loop() {
  uint32_t now = millis();

  if (connect_in_progress && !got_ip && (now - connect_started_at) >= CONNECT_TIMEOUT_MS) {
    connect_in_progress = false;
    Serial.printf("[%lu] WIFI: timeout sin IP\n", (unsigned long)now);
    WiFi.disconnect(true, true);
  }

  if (!got_ip && !connect_in_progress && (now - last_connect_attempt_at) >= RETRY_BACKOFF_MS) {
    start_connect_attempt();
  }

  if ((now - last_status_at) >= STATUS_INTERVAL_MS) {
    last_status_at = now;
    print_status();
  }

  delay(100);
}
