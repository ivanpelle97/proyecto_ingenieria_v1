#include <Arduino.h>
#include <ctype.h>
#include <stdio.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"

#include "esp_err.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "esp_wifi_types.h"

#include "ArduinoJson.h"
#include "HTTPClient.h"

/*
 * Firmware alternativo para inferencia online continua.
 *
 * Idea principal:
 *   - La antena ESP32 permanece asociada al AP todo el tiempo.
 *   - Se activa modo promiscuo sobre el mismo canal del AP.
 *   - Se envian mini-lotes periodicos al servidor sin abandonar la red.
 *
 * Recomendacion operativa:
 *   - Fijar el canal del router de 2.4 GHz.
 *   - Ajustar WIFI_AP_CHANNEL al canal fijo real del AP.
 *   - Hacer que el TAG emita sobre ese mismo canal.
 *   - Usar este firmware para inferencia online; conservar el firmware original
 *     para campanas de medicion si se desea mantener el barrido de canales.
 */

// -------------------- CONFIG --------------------
#define WIFI_SSID "WIFI EXPRESS 2.4"
#define WIFI_PASS "00416149152"
static const char *SERVER_BASE_URL = "http://192.168.0.20:8000";
static const char *ANCHOR_ID = "A1";
static const uint8_t WIFI_AP_CHANNEL = 1;
static const bool USE_BSSID_LOCK = true;
static uint8_t WIFI_AP_BSSID[6] = {0x84, 0x01, 0x12, 0x89, 0x2F, 0x8C};

#define LIVE_MODE_TARGET_ONLY 1
#define LIVE_MODE_MULTI_DEVICE 2
static const uint8_t LIVE_CAPTURE_MODE = LIVE_MODE_TARGET_ONLY;

#define MAX_PACKETS 256
#define FLUSH_PACKET_THRESHOLD 48
#define FLUSH_INTERVAL_MS 1500
#define CONFIG_REFRESH_MS 2500
#define STATUS_LOG_INTERVAL_MS 5000
#define WIFI_LOOP_DELAY_MS 80
#define WIFI_RECONNECT_SETTLE_MS 500
#define WIFI_RETRIES 8
#define STA_TIMEOUT_MS 8000
#define HTTP_TIMEOUT_MS 5000
#define RESTART_AFTER_WIFI_FAILURE_MS 1500
#define PROMISC_PAUSE_BEFORE_HTTP_MS 60
#define PROMISC_RESUME_AFTER_HTTP_MS 20
#define CONNECT_RETRY_JITTER_MS 450
#define CONNECT_RETRY_BACKOFF_MS 1800
#define AUTH_EXPIRE_EXTRA_BACKOFF_MS 2200
// ------------------------------------------------

typedef struct {
  uint32_t ts_us;
  uint8_t addr1[6];
  uint8_t addr2[6];
  uint8_t addr3[6];
  int8_t rssi;
  uint8_t channel;
} PacketData;

typedef struct {
  unsigned frame_ctrl : 16;
  unsigned duration_id : 16;
  uint8_t addr1[6];
  uint8_t addr2[6];
  uint8_t addr3[6];
  unsigned sequence_ctrl : 16;
} wifi_ieee80211_mac_hdr_t;

typedef struct {
  wifi_ieee80211_mac_hdr_t hdr;
  uint8_t payload[0];
} wifi_ieee80211_packet_t;

static PacketData captureBuffer[MAX_PACKETS];
static PacketData sendBuffer[MAX_PACKETS];
static volatile int captureCount = 0;
static portMUX_TYPE captureMux = portMUX_INITIALIZER_UNLOCKED;

static bool wifi_inited = false;
static bool promisc_enabled = false;
static esp_netif_t *sta_netif = nullptr;

static EventGroupHandle_t evg;
static const int GOT_IP_BIT = BIT0;

static volatile uint16_t last_disc_reason = 0;

static char session_id[64] = "";
static char campaign_id[64] = "";
static char target_mac[18] = "";
static uint8_t target_mac_bytes[6] = {0};
static bool config_enabled = false;
static bool capture_active = false;
static bool config_loaded_once = false;
static bool target_mac_valid = false;
static uint32_t samples_per_anchor = 0;

static void sniffer_cb(void *buff, wifi_promiscuous_pkt_type_t type);

static const char *wifi_reason_name(uint16_t reason) {
  switch (reason) {
    case WIFI_REASON_UNSPECIFIED:
      return "UNSPECIFIED";
    case WIFI_REASON_AUTH_EXPIRE:
      return "AUTH_EXPIRE";
    case WIFI_REASON_AUTH_LEAVE:
      return "AUTH_LEAVE";
    case WIFI_REASON_ASSOC_EXPIRE:
      return "ASSOC_EXPIRE";
    case WIFI_REASON_ASSOC_TOOMANY:
      return "ASSOC_TOOMANY";
    case WIFI_REASON_NOT_AUTHED:
      return "NOT_AUTHED";
    case WIFI_REASON_NOT_ASSOCED:
      return "NOT_ASSOCED";
    case WIFI_REASON_ASSOC_LEAVE:
      return "ASSOC_LEAVE";
    case WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT:
      return "4WAY_HANDSHAKE_TIMEOUT";
    case WIFI_REASON_GROUP_KEY_UPDATE_TIMEOUT:
      return "GROUP_KEY_UPDATE_TIMEOUT";
    case WIFI_REASON_IE_IN_4WAY_DIFFERS:
      return "IE_IN_4WAY_DIFFERS";
    case WIFI_REASON_NO_AP_FOUND:
      return "NO_AP_FOUND";
    case WIFI_REASON_AUTH_FAIL:
      return "AUTH_FAIL";
    case WIFI_REASON_CONNECTION_FAIL:
      return "CONNECTION_FAIL";
    case WIFI_REASON_HANDSHAKE_TIMEOUT:
      return "HANDSHAKE_TIMEOUT";
    default:
      return "OTHER";
  }
}

static const char *live_mode_name() {
  switch (LIVE_CAPTURE_MODE) {
    case LIVE_MODE_TARGET_ONLY:
      return "TARGET_ONLY";
    case LIVE_MODE_MULTI_DEVICE:
      return "MULTI_DEVICE";
    default:
      return "UNKNOWN";
  }
}

static bool live_mode_target_only() {
  return LIVE_CAPTURE_MODE == LIVE_MODE_TARGET_ONLY;
}

static int live_flush_packet_threshold() {
  return live_mode_target_only() ? FLUSH_PACKET_THRESHOLD : 20;
}

static uint32_t live_flush_interval_ms() {
  return live_mode_target_only() ? FLUSH_INTERVAL_MS : 700U;
}

static void logp(const char *fmt, ...) {
  va_list args;
  va_start(args, fmt);
  Serial.printf("[%lu] ", (unsigned long)millis());
  Serial.vprintf(fmt, args);
  Serial.print("\n");
  Serial.flush();
  va_end(args);
}

static uint32_t anchor_connect_slot_delay_ms() {
  if (strcmp(ANCHOR_ID, "A1") == 0) return 0;
  if (strcmp(ANCHOR_ID, "A2") == 0) return 700;
  if (strcmp(ANCHOR_ID, "A3") == 0) return 1400;
  if (strcmp(ANCHOR_ID, "A4") == 0) return 2100;
  return 0;
}

static uint32_t retry_jitter_ms() {
  return (uint32_t)(esp_random() % CONNECT_RETRY_JITTER_MS);
}

static bool parse_mac_string(const char *text, uint8_t out[6]) {
  if (!text || strlen(text) != 17) return false;
  unsigned int b[6];
  int scanned = sscanf(
    text,
    "%02x:%02x:%02x:%02x:%02x:%02x",
    &b[0],
    &b[1],
    &b[2],
    &b[3],
    &b[4],
    &b[5]
  );
  if (scanned != 6) return false;
  for (int i = 0; i < 6; i++) out[i] = (uint8_t)b[i];
  return true;
}

static inline bool mac_matches_target(const uint8_t addr[6]) {
  return target_mac_valid && memcmp(addr, target_mac_bytes, 6) == 0;
}

static bool wifi_has_ip() {
  return evg && ((xEventGroupGetBits(evg) & GOT_IP_BIT) != 0);
}

static void clear_capture_buffer() {
  portENTER_CRITICAL(&captureMux);
  captureCount = 0;
  portEXIT_CRITICAL(&captureMux);
}

static int snapshot_packets() {
  int n = 0;
  portENTER_CRITICAL(&captureMux);
  n = captureCount;
  if (n > 0) {
    memcpy(sendBuffer, captureBuffer, sizeof(PacketData) * n);
    captureCount = 0;
  }
  portEXIT_CRITICAL(&captureMux);
  return n;
}

static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data) {
  (void)arg;

  if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
    logp("EV: STA_START");
  }

  if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
    wifi_event_sta_disconnected_t *e = (wifi_event_sta_disconnected_t *)data;
    last_disc_reason = e ? e->reason : 0;
    promisc_enabled = false;
    xEventGroupClearBits(evg, GOT_IP_BIT);
    logp(
      "EV: STA_DISCONNECTED reason=%u (%s)",
      (unsigned)last_disc_reason,
      wifi_reason_name(last_disc_reason)
    );
  }

  if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
    ip_event_got_ip_t *e = (ip_event_got_ip_t *)data;
    xEventGroupSetBits(evg, GOT_IP_BIT);
    logp("EV: GOT_IP " IPSTR, IP2STR(&e->ip_info.ip));
  }
}

static void apply_sta_config() {
  wifi_config_t cfg = {};
  strncpy((char *)cfg.sta.ssid, WIFI_SSID, sizeof(cfg.sta.ssid) - 1);
  strncpy((char *)cfg.sta.password, WIFI_PASS, sizeof(cfg.sta.password) - 1);
  cfg.sta.threshold.authmode = WIFI_AUTH_OPEN;
  cfg.sta.pmf_cfg.capable = true;
  cfg.sta.pmf_cfg.required = false;
  cfg.sta.scan_method = WIFI_ALL_CHANNEL_SCAN;
  cfg.sta.sort_method = WIFI_CONNECT_AP_BY_SIGNAL;
  cfg.sta.channel = USE_BSSID_LOCK ? WIFI_AP_CHANNEL : 0;
  cfg.sta.bssid_set = USE_BSSID_LOCK ? 1 : 0;
  if (USE_BSSID_LOCK) {
    memcpy(cfg.sta.bssid, WIFI_AP_BSSID, sizeof(cfg.sta.bssid));
  }
  esp_wifi_set_config(WIFI_IF_STA, &cfg);
}

static void wifi_init_once() {
  if (wifi_inited) return;

  logp("wifi_init_once()...");

  esp_err_t err;
  err = esp_netif_init();
  if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) logp("esp_netif_init FAIL %s", esp_err_to_name(err));

  err = esp_event_loop_create_default();
  if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) logp("event_loop_create_default FAIL %s", esp_err_to_name(err));

  evg = xEventGroupCreate();
  sta_netif = esp_netif_create_default_wifi_sta();
  if (!sta_netif) {
    logp("WARN: esp_netif_create_default_wifi_sta() NULL");
  } else {
    esp_netif_set_hostname(sta_netif, ANCHOR_ID);
  }

  wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
  err = esp_wifi_init(&cfg);
  if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) logp("esp_wifi_init FAIL %s", esp_err_to_name(err));

  esp_wifi_set_storage(WIFI_STORAGE_RAM);

  esp_event_handler_instance_t h1, h2;
  err = esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL, &h1);
  if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) logp("register WIFI_EVENT FAIL %s", esp_err_to_name(err));
  err = esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL, &h2);
  if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) logp("register IP_EVENT FAIL %s", esp_err_to_name(err));

  esp_wifi_set_ps(WIFI_PS_NONE);
  esp_wifi_set_max_tx_power(40);
  esp_wifi_set_mode(WIFI_MODE_STA);
  apply_sta_config();

  err = esp_wifi_start();
  if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) logp("esp_wifi_start FAIL %s", esp_err_to_name(err));

  wifi_inited = true;
  logp("wifi_init_once DONE");
}

static bool wait_for_ip(uint32_t timeout_ms) {
  EventBits_t bits = xEventGroupWaitBits(
    evg,
    GOT_IP_BIT,
    pdFALSE,
    pdTRUE,
    pdMS_TO_TICKS(timeout_ms)
  );
  return (bits & GOT_IP_BIT) != 0;
}

static bool connect_sta_once() {
  last_disc_reason = 0;
  xEventGroupClearBits(evg, GOT_IP_BIT);
  esp_wifi_set_promiscuous(false);
  promisc_enabled = false;
  esp_wifi_disconnect();
  vTaskDelay(pdMS_TO_TICKS(200));
  apply_sta_config();

  if (USE_BSSID_LOCK) {
    logp(
      "WIFI: connect() target_channel=%u scan=all bssid=%02X:%02X:%02X:%02X:%02X:%02X",
      (unsigned)WIFI_AP_CHANNEL,
      WIFI_AP_BSSID[0],
      WIFI_AP_BSSID[1],
      WIFI_AP_BSSID[2],
      WIFI_AP_BSSID[3],
      WIFI_AP_BSSID[4],
      WIFI_AP_BSSID[5]
    );
  } else {
    logp("WIFI: connect() target_channel=%u scan=all", (unsigned)WIFI_AP_CHANNEL);
  }

  esp_err_t err = esp_wifi_connect();
  if (err != ESP_OK && err != ESP_ERR_WIFI_CONN) {
    logp("WIFI: esp_wifi_connect FAIL %s", esp_err_to_name(err));
  }

  if (!wait_for_ip(STA_TIMEOUT_MS)) {
    logp("WIFI: timeout sin IP (reason=%u)", (unsigned)last_disc_reason);
    return false;
  }
  return true;
}

static void restart_after_wifi_failure() {
  logp("WIFI: reinicio automatico en %ums", (unsigned)RESTART_AFTER_WIFI_FAILURE_MS);
  vTaskDelay(pdMS_TO_TICKS(RESTART_AFTER_WIFI_FAILURE_MS));
  esp_restart();
}

static void hard_reset_wifi_radio() {
  logp("WIFI: hard reset de radio");
  esp_wifi_set_promiscuous(false);
  promisc_enabled = false;
  xEventGroupClearBits(evg, GOT_IP_BIT);
  esp_wifi_disconnect();
  vTaskDelay(pdMS_TO_TICKS(150));
  esp_wifi_stop();
  vTaskDelay(pdMS_TO_TICKS(250));
  esp_wifi_set_mode(WIFI_MODE_STA);
  apply_sta_config();
  esp_err_t err = esp_wifi_start();
  if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
    logp("WIFI: esp_wifi_start tras hard reset FAIL %s", esp_err_to_name(err));
  }
  vTaskDelay(pdMS_TO_TICKS(300));
}

static void ensure_wifi_connected_or_restart() {
  if (wifi_has_ip()) return;

  for (int attempt = 1; attempt <= WIFI_RETRIES; attempt++) {
    uint32_t slot_delay = (attempt == 1) ? anchor_connect_slot_delay_ms() : 0;
    uint32_t jitter = retry_jitter_ms();
    if (slot_delay > 0 || jitter > 0) {
      logp("WIFI: espera previa slot=%ums jitter=%ums", (unsigned)slot_delay, (unsigned)jitter);
      vTaskDelay(pdMS_TO_TICKS(slot_delay + jitter));
    }

    logp("WIFI: reconnect attempt %d/%d", attempt, WIFI_RETRIES);
    if (connect_sta_once()) {
      vTaskDelay(pdMS_TO_TICKS(WIFI_RECONNECT_SETTLE_MS));
      return;
    }

    hard_reset_wifi_radio();

    uint32_t backoff = CONNECT_RETRY_BACKOFF_MS + retry_jitter_ms();
    if (last_disc_reason == WIFI_REASON_AUTH_EXPIRE) {
      backoff += AUTH_EXPIRE_EXTRA_BACKOFF_MS;
    }
    logp(
      "WIFI: backoff tras fallo=%ums reason=%u (%s)",
      (unsigned)backoff,
      (unsigned)last_disc_reason,
      wifi_reason_name(last_disc_reason)
    );
    vTaskDelay(pdMS_TO_TICKS(backoff));
  }

  restart_after_wifi_failure();
}

static void ensure_promiscuous_capture() {
  if (!wifi_has_ip()) return;

  wifi_promiscuous_filter_t filt = {};
  filt.filter_mask = WIFI_PROMIS_FILTER_MASK_MGMT;
  esp_wifi_set_promiscuous_filter(&filt);
  esp_wifi_set_promiscuous_rx_cb(&sniffer_cb);

  esp_err_t err = esp_wifi_set_promiscuous(true);
  if (err == ESP_OK) {
    uint8_t primary = 0;
    wifi_second_chan_t second = WIFI_SECOND_CHAN_NONE;
    esp_wifi_get_channel(&primary, &second);
    if (!promisc_enabled) {
      logp("sniffer: ON en canal AP=%u", (unsigned)primary);
    }
    promisc_enabled = true;
  } else {
    logp("sniffer: promisc(true) FAIL %s", esp_err_to_name(err));
  }
}

static void pause_promiscuous_capture() {
  if (!promisc_enabled) return;
  esp_wifi_set_promiscuous(false);
  promisc_enabled = false;
  vTaskDelay(pdMS_TO_TICKS(PROMISC_PAUSE_BEFORE_HTTP_MS));
}

static void resume_promiscuous_capture() {
  vTaskDelay(pdMS_TO_TICKS(PROMISC_RESUME_AFTER_HTTP_MS));
  ensure_promiscuous_capture();
}

static void sniffer_cb(void *buff, wifi_promiscuous_pkt_type_t type) {
  if (type != WIFI_PKT_MGMT) return;
  if (!config_enabled) return;

  const wifi_promiscuous_pkt_t *ppkt = (wifi_promiscuous_pkt_t *)buff;
  const wifi_ieee80211_packet_t *pkt = (wifi_ieee80211_packet_t *)ppkt->payload;

  if (target_mac_valid && (capture_active || live_mode_target_only())) {
    const bool matches_target =
      mac_matches_target(pkt->hdr.addr1) ||
      mac_matches_target(pkt->hdr.addr2) ||
      mac_matches_target(pkt->hdr.addr3);
    if (!matches_target) return;
  }

  PacketData d;
  d.ts_us = (uint32_t)esp_timer_get_time();
  memcpy(d.addr1, pkt->hdr.addr1, 6);
  memcpy(d.addr2, pkt->hdr.addr2, 6);
  memcpy(d.addr3, pkt->hdr.addr3, 6);
  d.rssi = (int8_t)ppkt->rx_ctrl.rssi;
  d.channel = (uint8_t)ppkt->rx_ctrl.channel;

  portENTER_CRITICAL_ISR(&captureMux);
  if (captureCount < MAX_PACKETS) {
    captureBuffer[captureCount++] = d;
  }
  portEXIT_CRITICAL_ISR(&captureMux);
}

static bool fetch_server_config() {
  if (!wifi_has_ip()) return false;

  pause_promiscuous_capture();

  HTTPClient http;
  String url = String(SERVER_BASE_URL) + "/api/anchors/" + ANCHOR_ID + "/config";
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.begin(url);
  int code = http.GET();
  if (code <= 0) {
    logp("CFG: GET error=%s", http.errorToString(code).c_str());
    http.end();
    resume_promiscuous_capture();
    return false;
  }

  String body = http.getString();
  http.end();
  resume_promiscuous_capture();

  DynamicJsonDocument doc(4096);
  DeserializationError err = deserializeJson(doc, body);
  if (err) {
    logp("CFG: JSON invalido: %s", err.c_str());
    return false;
  }

  config_enabled = doc["enabled"] | false;
  capture_active = doc["capture_active"] | false;
  samples_per_anchor = doc["samples_per_anchor"] | 0;

  const char *next_session = doc["session_id"] | "";
  const char *next_campaign = doc["campaign_id"] | "";
  const char *next_target_mac = doc["target_mac"] | "";

  snprintf(session_id, sizeof(session_id), "%s", next_session);
  snprintf(campaign_id, sizeof(campaign_id), "%s", next_campaign);
  snprintf(target_mac, sizeof(target_mac), "%s", next_target_mac);
  target_mac_valid = parse_mac_string(next_target_mac, target_mac_bytes);
  config_loaded_once = true;

  if (!config_enabled) {
    clear_capture_buffer();
  }

  logp(
    "CFG: enabled=%s session=%s campaign=%s capture_active=%s target=%s target_ok=%s mode=%s samples=%u",
    config_enabled ? "true" : "false",
    session_id,
    campaign_id,
    capture_active ? "true" : "false",
    target_mac,
    target_mac_valid ? "true" : "false",
    live_mode_name(),
    (unsigned)samples_per_anchor
  );
  return true;
}

static bool post_snapshot_json(int n) {
  if (n <= 0) return true;
  if (!config_enabled || strlen(session_id) == 0 || strlen(campaign_id) == 0) {
    logp("POST: omitido porque no hay sesion activa");
    return false;
  }

  pause_promiscuous_capture();

  const size_t cap =
    JSON_OBJECT_SIZE(4) +
    JSON_ARRAY_SIZE(n) +
    n * JSON_OBJECT_SIZE(6) +
    n * 3 * 18 +
    512;

  DynamicJsonDocument doc(cap);
  doc["session_id"] = session_id;
  doc["campaign_id"] = campaign_id;
  doc["anchor_id"] = ANCHOR_ID;
  JsonArray packets = doc.createNestedArray("packets");

  for (int i = 0; i < n; i++) {
    char a1[18], a2[18], a3[18];
    snprintf(a1, sizeof(a1), "%02x:%02x:%02x:%02x:%02x:%02x",
      sendBuffer[i].addr1[0], sendBuffer[i].addr1[1], sendBuffer[i].addr1[2],
      sendBuffer[i].addr1[3], sendBuffer[i].addr1[4], sendBuffer[i].addr1[5]);
    snprintf(a2, sizeof(a2), "%02x:%02x:%02x:%02x:%02x:%02x",
      sendBuffer[i].addr2[0], sendBuffer[i].addr2[1], sendBuffer[i].addr2[2],
      sendBuffer[i].addr2[3], sendBuffer[i].addr2[4], sendBuffer[i].addr2[5]);
    snprintf(a3, sizeof(a3), "%02x:%02x:%02x:%02x:%02x:%02x",
      sendBuffer[i].addr3[0], sendBuffer[i].addr3[1], sendBuffer[i].addr3[2],
      sendBuffer[i].addr3[3], sendBuffer[i].addr3[4], sendBuffer[i].addr3[5]);

    JsonObject o = packets.createNestedObject();
    o["ts_us"] = sendBuffer[i].ts_us;
    o["addr1"] = a1;
    o["addr2"] = a2;
    o["addr3"] = a3;
    o["rssi"] = sendBuffer[i].rssi;
    o["channel"] = sendBuffer[i].channel;
  }

  String payload;
  payload.reserve(cap);
  serializeJson(doc, payload);

  HTTPClient http;
  String url = String(SERVER_BASE_URL) + "/ingest";
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.begin(url);
  http.addHeader("Content-Type", "application/json");
  int code = http.POST((uint8_t *)payload.c_str(), payload.length());
  if (code > 0) {
    logp("POST: ok status=%d packets=%d capture_active=%s", code, n, capture_active ? "true" : "false");
  } else {
    logp("POST: error=%s", http.errorToString(code).c_str());
  }
  http.end();
  resume_promiscuous_capture();
  return code > 0;
}

void setup() {
  Serial.begin(115200);
  delay(1200);
  Serial.println("=== SKETCH START anchor_sniffer_inference_continuous ===");
  Serial.printf("RESET reason=%d\n", (int)esp_reset_reason());
  Serial.printf("CANAL AP FIJO=%u\n", (unsigned)WIFI_AP_CHANNEL);
  Serial.printf("USE_BSSID_LOCK=%s\n", USE_BSSID_LOCK ? "true" : "false");
  if (USE_BSSID_LOCK) {
    Serial.printf(
      "WIFI_AP_BSSID=%02X:%02X:%02X:%02X:%02X:%02X\n",
      WIFI_AP_BSSID[0],
      WIFI_AP_BSSID[1],
      WIFI_AP_BSSID[2],
      WIFI_AP_BSSID[3],
      WIFI_AP_BSSID[4],
      WIFI_AP_BSSID[5]
    );
  }
  Serial.printf("CONNECT_SLOT_DELAY_MS=%u\n", (unsigned)anchor_connect_slot_delay_ms());
  Serial.printf("MODO CAPTURA=%s\n", live_mode_name());
  Serial.flush();

  wifi_init_once();
  ensure_wifi_connected_or_restart();
  fetch_server_config();
  ensure_promiscuous_capture();
}

void loop() {
  static uint32_t last_cfg_ms = 0;
  static uint32_t last_flush_ms = 0;
  static uint32_t last_status_ms = 0;

  ensure_wifi_connected_or_restart();
  ensure_promiscuous_capture();

  const uint32_t now_ms = millis();

  if (now_ms - last_cfg_ms >= CONFIG_REFRESH_MS) {
    fetch_server_config();
    last_cfg_ms = now_ms;
  }

  int pending = 0;
  portENTER_CRITICAL(&captureMux);
  pending = captureCount;
  portEXIT_CRITICAL(&captureMux);

  const bool should_flush =
    pending >= live_flush_packet_threshold() ||
    (pending > 0 && (now_ms - last_flush_ms >= live_flush_interval_ms()));

  if (should_flush && wifi_has_ip()) {
    int n = snapshot_packets();
    if (n > 0) {
      post_snapshot_json(n);
    }
    last_flush_ms = now_ms;
  }

  if (now_ms - last_status_ms >= STATUS_LOG_INTERVAL_MS) {
    uint8_t primary = 0;
    wifi_second_chan_t second = WIFI_SECOND_CHAN_NONE;
    esp_wifi_get_channel(&primary, &second);
    logp(
      "STAT: wifi=%s promisc=%s channel=%u channel_cfg=%u enabled=%s capture_active=%s buffered=%d",
      wifi_has_ip() ? "up" : "down",
      promisc_enabled ? "on" : "off",
      (unsigned)primary,
      (unsigned)WIFI_AP_CHANNEL,
      config_enabled ? "true" : "false",
      capture_active ? "true" : "false",
      pending
    );
    last_status_ms = now_ms;
  }

  delay(WIFI_LOOP_DELAY_MS);
}
