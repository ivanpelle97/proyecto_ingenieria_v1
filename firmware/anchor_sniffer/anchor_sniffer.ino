#include <Arduino.h>
#include <ctype.h>
#include <string.h>
#include <stdio.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"

#include "esp_system.h"
#include "esp_err.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "esp_wifi_types.h"
#include "esp_timer.h"

#include "lwip/ip4_addr.h"
#include "lwip/inet.h"

#include "ArduinoJson.h"
#include "HTTPClient.h"
#include "esp_http_client.h"

// -------------------- CONFIG --------------------
#define WIFI_SSID   "WIFI EXPRESS 2.4"
#define WIFI_PASS   "00416149152"
static const char* SERVER_BASE_URL = "http://192.168.0.18:8000";
static const char* ANCHOR_ID = "A1";

#define MAX_PACKETS                   500
#define WIFI_CHANNEL_MAX              13
#define CHANNEL_HOP_INTERVAL_MS       400
#define SNIFF_DURATION_MS             12000
#define POST_COOLDOWN_MS              3000
#define STA_SLOT_DELAY_MS             700
#define CYCLE_JITTER_MS               250
#define RESTART_AFTER_STA_FAILURE_MS  1500

#define STA_TIMEOUT_MS                20000

#define STA_RETRIES                   3
// ------------------------------------------------

typedef struct {
  uint32_t ts_us;
  uint8_t addr1[6];
  uint8_t addr2[6];
  uint8_t addr3[6];
  int8_t  rssi;
  uint8_t channel;
} PacketData;

typedef struct {
  unsigned frame_ctrl:16;
  unsigned duration_id:16;
  uint8_t addr1[6];
  uint8_t addr2[6];
  uint8_t addr3[6];
  unsigned sequence_ctrl:16;
} wifi_ieee80211_mac_hdr_t;

typedef struct {
  wifi_ieee80211_mac_hdr_t hdr;
  uint8_t payload[0];
} wifi_ieee80211_packet_t;

static PacketData packetBuffer[MAX_PACKETS];
static volatile int packetCount = 0;

static bool wifi_inited = false;
static esp_netif_t* sta_netif = nullptr;

static EventGroupHandle_t evg;
static const int GOT_IP_BIT = BIT0;

static volatile uint16_t last_disc_reason = 0;
static uint16_t ssid_chan_hits[WIFI_CHANNEL_MAX + 1] = {0};
static uint8_t last_ap_channel_guess = 0;

static char session_id[64] = "";
static char campaign_id[64] = "";
static char target_mac[18] = "";
static uint8_t target_mac_bytes[6] = {0};
static bool config_enabled = false;
static bool capture_active = false;
static bool config_loaded_once = false;
static bool target_mac_valid = false;
static uint32_t samples_per_anchor = 0;

static void logp(const char* fmt, ...) {
  va_list args;
  va_start(args, fmt);
  Serial.printf("[%lu] ", (unsigned long)millis());
  Serial.vprintf(fmt, args);
  Serial.print("\n");
  Serial.flush();
  va_end(args);
}

static inline void addPacket(const PacketData &d) {
  int idx = packetCount;
  if (idx < MAX_PACKETS) {
    packetBuffer[idx] = d;
    packetCount = idx + 1;
  }
}

static bool parse_mac_string(const char* text, uint8_t out[6]) {
  if (!text || strlen(text) != 17) return false;
  unsigned int b[6];
  int scanned = sscanf(
    text,
    "%02x:%02x:%02x:%02x:%02x:%02x",
    &b[0], &b[1], &b[2], &b[3], &b[4], &b[5]
  );
  if (scanned != 6) return false;
  for (int i = 0; i < 6; i++) out[i] = (uint8_t)b[i];
  return true;
}

static inline bool mac_matches_target(const uint8_t addr[6]) {
  return target_mac_valid && memcmp(addr, target_mac_bytes, 6) == 0;
}

static uint32_t anchor_slot_delay_ms() {
  const size_t len = strlen(ANCHOR_ID);
  if (len > 0 && isdigit((unsigned char)ANCHOR_ID[len - 1])) {
    int slot = ANCHOR_ID[len - 1] - '1';
    if (slot < 0) slot = 0;
    return (uint32_t)slot * STA_SLOT_DELAY_MS;
  }
  return 0;
}

static uint32_t cycle_jitter_ms() {
  return (uint32_t)(esp_random() % (CYCLE_JITTER_MS + 1));
}

static inline uint8_t wifi_fc_type(uint16_t fc) {
  return (fc >> 2) & 0x3;
}

static inline uint8_t wifi_fc_subtype(uint16_t fc) {
  return (fc >> 4) & 0xF;
}

static void sniff_try_learn_ap_channel(const wifi_promiscuous_pkt_t* ppkt) {
  const wifi_ieee80211_packet_t *ipkt = (wifi_ieee80211_packet_t *)ppkt->payload;
  const uint16_t fc = ipkt->hdr.frame_ctrl;
  const uint8_t type = wifi_fc_type(fc);
  const uint8_t st = wifi_fc_subtype(fc);

  if (type != 0) return;
  if (!(st == 8 || st == 5)) return;

  const uint8_t* p = (const uint8_t*)ppkt->payload;
  const int hdr_len = 24;
  const int fixed_len = 12;
  const uint8_t* ies = p + hdr_len + fixed_len;
  int ies_len = ppkt->rx_ctrl.sig_len - hdr_len - fixed_len;
  if (ies_len <= 0) return;

  const char* target = WIFI_SSID;
  const int target_len = (int)strlen(target);

  int i = 0;
  while (i + 2 <= ies_len) {
    uint8_t id = ies[i];
    uint8_t len = ies[i + 1];
    if (i + 2 + len > ies_len) break;

    if (id == 0) {
      if (len == target_len && memcmp(&ies[i + 2], target, target_len) == 0) {
        uint8_t ch = (uint8_t)ppkt->rx_ctrl.channel;
        if (ch >= 1 && ch <= WIFI_CHANNEL_MAX) {
          ssid_chan_hits[ch]++;
        }
        return;
      }
    }
    i += 2 + len;
  }
}

static void wifi_event_handler(void* arg, esp_event_base_t base, int32_t id, void* data) {
  (void)arg;

  if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
    logp("EV: STA_START");
  }

  if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
    wifi_event_sta_disconnected_t* e = (wifi_event_sta_disconnected_t*)data;
    last_disc_reason = e ? e->reason : 0;
    logp("EV: STA_DISCONNECTED reason=%u", (unsigned)last_disc_reason);
    xEventGroupClearBits(evg, GOT_IP_BIT);
  }

  if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
    ip_event_got_ip_t* e = (ip_event_got_ip_t*)data;
    logp("EV: GOT_IP " IPSTR, IP2STR(&e->ip_info.ip));
    xEventGroupSetBits(evg, GOT_IP_BIT);
  }
}

static void wifi_init_once() {
  if (wifi_inited) return;

  logp("wifi_init_once()...");

  esp_err_t e;

  e = esp_netif_init();
  if (e != ESP_OK && e != ESP_ERR_INVALID_STATE) logp("esp_netif_init FAIL %s", esp_err_to_name(e));

  e = esp_event_loop_create_default();
  if (e != ESP_OK && e != ESP_ERR_INVALID_STATE) logp("event_loop_create_default FAIL %s", esp_err_to_name(e));

  evg = xEventGroupCreate();

  sta_netif = esp_netif_create_default_wifi_sta();
  if (!sta_netif) logp("WARN: esp_netif_create_default_wifi_sta() NULL");

  wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
  e = esp_wifi_init(&cfg);
  if (e != ESP_OK && e != ESP_ERR_INVALID_STATE) logp("esp_wifi_init FAIL %s", esp_err_to_name(e));

  esp_wifi_set_storage(WIFI_STORAGE_RAM);

  esp_event_handler_instance_t h1, h2;
  e = esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL, &h1);
  if (e != ESP_OK && e != ESP_ERR_INVALID_STATE) logp("register WIFI_EVENT FAIL %s", esp_err_to_name(e));

  e = esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL, &h2);
  if (e != ESP_OK && e != ESP_ERR_INVALID_STATE) logp("register IP_EVENT FAIL %s", esp_err_to_name(e));

  esp_wifi_set_ps(WIFI_PS_NONE);
  esp_wifi_set_max_tx_power(40);

  e = esp_wifi_start();
  if (e != ESP_OK && e != ESP_ERR_INVALID_STATE) logp("esp_wifi_start FAIL %s", esp_err_to_name(e));

  wifi_inited = true;
  logp("wifi_init_once DONE");
}

static void sniffer_cb(void* buff, wifi_promiscuous_pkt_type_t type) {
  if (type != WIFI_PKT_MGMT) return;

  const wifi_promiscuous_pkt_t *ppkt = (wifi_promiscuous_pkt_t *)buff;

  sniff_try_learn_ap_channel(ppkt);

  const wifi_ieee80211_packet_t *pkt = (wifi_ieee80211_packet_t *)ppkt->payload;
  if (capture_active && target_mac_valid) {
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
  addPacket(d);
}

static void reset_ssid_channel_stats() {
  memset(ssid_chan_hits, 0, sizeof(ssid_chan_hits));
  last_ap_channel_guess = 0;
}

static uint8_t pick_best_ap_channel() {
  uint16_t best = 0;
  uint8_t best_ch = 0;
  for (uint8_t ch = 1; ch <= WIFI_CHANNEL_MAX; ch++) {
    if (ssid_chan_hits[ch] > best) {
      best = ssid_chan_hits[ch];
      best_ch = ch;
    }
  }
  return best_ch;
}

static bool enter_sniffer_mode() {
  wifi_init_once();

  reset_ssid_channel_stats();
  packetCount = 0;

  esp_wifi_set_promiscuous(false);

  esp_err_t e = esp_wifi_set_mode(WIFI_MODE_NULL);
  if (e != ESP_OK) {
    logp("sniffer: set_mode(NULL) FAIL %s", esp_err_to_name(e));
    return false;
  }

  wifi_promiscuous_filter_t filt = {};
  filt.filter_mask = WIFI_PROMIS_FILTER_MASK_MGMT;
  esp_wifi_set_promiscuous_filter(&filt);
  esp_wifi_set_promiscuous_rx_cb(&sniffer_cb);

  e = esp_wifi_set_promiscuous(true);
  if (e != ESP_OK) {
    logp("sniffer: promisc(true) FAIL %s", esp_err_to_name(e));
    return false;
  }

  logp("sniffer: ON");
  return true;
}

static void exit_sniffer_mode() {
  esp_err_t e = esp_wifi_set_promiscuous(false);
  logp("sniffer: OFF (%s)", esp_err_to_name(e));
}

static void sta_set_config(uint8_t ap_channel_hint, bool use_channel_hint, bool fast_scan) {
  wifi_config_t cfg = {};
  strncpy((char*)cfg.sta.ssid, WIFI_SSID, sizeof(cfg.sta.ssid) - 1);
  strncpy((char*)cfg.sta.password, WIFI_PASS, sizeof(cfg.sta.password) - 1);

  cfg.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
  cfg.sta.pmf_cfg.capable = false;
  cfg.sta.pmf_cfg.required = false;
  cfg.sta.scan_method = fast_scan ? WIFI_FAST_SCAN : WIFI_ALL_CHANNEL_SCAN;
  cfg.sta.sort_method = WIFI_CONNECT_AP_BY_SIGNAL;

  if (use_channel_hint && ap_channel_hint >= 1 && ap_channel_hint <= WIFI_CHANNEL_MAX) {
    cfg.sta.channel = ap_channel_hint;
  } else {
    cfg.sta.channel = 0;
  }

  esp_wifi_set_config(WIFI_IF_STA, &cfg);
}

static bool enter_sta_and_wait_ip(uint8_t ap_channel_hint, bool use_channel_hint, bool fast_scan) {
  wifi_init_once();

  exit_sniffer_mode();

  last_disc_reason = 0;
  xEventGroupClearBits(evg, GOT_IP_BIT);

  esp_err_t e = esp_wifi_set_mode(WIFI_MODE_STA);
  if (e != ESP_OK) {
    logp("sta: set_mode(STA) FAIL %s", esp_err_to_name(e));
    return false;
  }

  sta_set_config(ap_channel_hint, use_channel_hint, fast_scan);

  if (use_channel_hint && ap_channel_hint >= 1 && ap_channel_hint <= WIFI_CHANNEL_MAX) {
    esp_wifi_set_channel(ap_channel_hint, WIFI_SECOND_CHAN_NONE);
    vTaskDelay(pdMS_TO_TICKS(300));
  }

  esp_wifi_disconnect();
  vTaskDelay(pdMS_TO_TICKS(200));

  logp(
    "sta: connect() (ch_hint=%u use_hint=%s scan=%s)",
    (unsigned)ap_channel_hint,
    use_channel_hint ? "true" : "false",
    fast_scan ? "fast" : "all"
  );
  e = esp_wifi_connect();
  if (e != ESP_OK && e != ESP_ERR_WIFI_CONN) logp("sta: esp_wifi_connect %s", esp_err_to_name(e));

  EventBits_t bits = xEventGroupWaitBits(
      evg, GOT_IP_BIT, pdFALSE, pdTRUE, pdMS_TO_TICKS(STA_TIMEOUT_MS));

  if (bits & GOT_IP_BIT) {
    if (sta_netif) {
      esp_netif_ip_info_t ip;
      if (esp_netif_get_ip_info(sta_netif, &ip) == ESP_OK) {
        logp("sta: IP=" IPSTR " GW=" IPSTR " MASK=" IPSTR,
             IP2STR(&ip.ip), IP2STR(&ip.gw), IP2STR(&ip.netmask));
      }
    }
    return true;
  }

  logp("sta: TIMEOUT no IP (reason=%u)", (unsigned)last_disc_reason);
  return false;
}

static void sta_disconnect() {
  esp_err_t e = esp_wifi_disconnect();
  logp("sta: disconnect (%s)", esp_err_to_name(e));
}

static bool fetch_server_config() {
  HTTPClient http;
  String url = String(SERVER_BASE_URL) + "/api/anchors/" + ANCHOR_ID + "/config";
  logp("CFG: GET %s", url.c_str());
  http.begin(url);
  int code = http.GET();
  if (code <= 0) {
    logp("CFG: GET error=%s", http.errorToString(code).c_str());
    http.end();
    return false;
  }

  String body = http.getString();
  http.end();

  DynamicJsonDocument doc(4096);
  DeserializationError err = deserializeJson(doc, body);
  if (err) {
    logp("CFG: JSON invalido: %s", err.c_str());
    return false;
  }

  config_enabled = doc["enabled"] | false;
  capture_active = doc["capture_active"] | false;
  samples_per_anchor = doc["samples_per_anchor"] | 0;

  const char* next_session = doc["session_id"] | "";
  const char* next_campaign = doc["campaign_id"] | "";
  const char* next_target_mac = doc["target_mac"] | "";

  snprintf(session_id, sizeof(session_id), "%s", next_session);
  snprintf(campaign_id, sizeof(campaign_id), "%s", next_campaign);
  snprintf(target_mac, sizeof(target_mac), "%s", next_target_mac);
  target_mac_valid = parse_mac_string(next_target_mac, target_mac_bytes);

  config_loaded_once = true;
  logp(
    "CFG: enabled=%s session=%s campaign=%s target=%s target_ok=%s capture_active=%s samples=%u",
    config_enabled ? "true" : "false",
    session_id,
    campaign_id,
    target_mac,
    target_mac_valid ? "true" : "false",
    capture_active ? "true" : "false",
    (unsigned)samples_per_anchor
  );
  return true;
}

static esp_err_t http_post_json(const char* url, const char* payload, int payload_len, int* out_status) {
  esp_http_client_config_t config = {};
  config.url = url;
  config.method = HTTP_METHOD_POST;
  config.timeout_ms = 7000;

  esp_http_client_handle_t client = esp_http_client_init(&config);
  if (!client) return ESP_FAIL;

  esp_http_client_set_header(client, "Content-Type", "application/json");
  esp_http_client_set_post_field(client, payload, payload_len);

  esp_err_t err = esp_http_client_perform(client);
  if (err == ESP_OK) {
    int status = esp_http_client_get_status_code(client);
    if (out_status) *out_status = status;
  }

  esp_http_client_cleanup(client);
  return err;
}

static void send_buffer_as_json() {
  int n = packetCount;
  if (n <= 0) {
    logp("POST: buffer vacio");
    return;
  }
  if (!config_enabled || strlen(session_id) == 0 || strlen(campaign_id) == 0) {
    logp("POST: skip porque no hay campana activa para %s", ANCHOR_ID);
    return;
  }

  const size_t CAP =
      JSON_OBJECT_SIZE(4) +
      JSON_ARRAY_SIZE(n) +
      n * JSON_OBJECT_SIZE(6) +
      n * 3 * 18 +
      512;

  DynamicJsonDocument doc(CAP);
  doc["session_id"] = session_id;
  doc["campaign_id"] = campaign_id;
  doc["anchor_id"] = ANCHOR_ID;
  JsonArray arr = doc.createNestedArray("packets");

  for (int i = 0; i < n; ++i) {
    char a1[18], a2[18], a3[18];
    snprintf(a1, 18, "%02x:%02x:%02x:%02x:%02x:%02x", packetBuffer[i].addr1[0], packetBuffer[i].addr1[1], packetBuffer[i].addr1[2], packetBuffer[i].addr1[3], packetBuffer[i].addr1[4], packetBuffer[i].addr1[5]);
    snprintf(a2, 18, "%02x:%02x:%02x:%02x:%02x:%02x", packetBuffer[i].addr2[0], packetBuffer[i].addr2[1], packetBuffer[i].addr2[2], packetBuffer[i].addr2[3], packetBuffer[i].addr2[4], packetBuffer[i].addr2[5]);
    snprintf(a3, 18, "%02x:%02x:%02x:%02x:%02x:%02x", packetBuffer[i].addr3[0], packetBuffer[i].addr3[1], packetBuffer[i].addr3[2], packetBuffer[i].addr3[3], packetBuffer[i].addr3[4], packetBuffer[i].addr3[5]);

    JsonObject o = arr.createNestedObject();
    o["ts_us"] = packetBuffer[i].ts_us;
    o["addr1"] = a1;
    o["addr2"] = a2;
    o["addr3"] = a3;
    o["rssi"] = packetBuffer[i].rssi;
    o["channel"] = packetBuffer[i].channel;
  }

  String payload;
  payload.reserve(CAP);
  serializeJson(doc, payload);

  String post_url = String(SERVER_BASE_URL) + "/ingest";
  logp(
    "POST: url=%s len=%d packets=%d capture_active=%s",
    post_url.c_str(),
    (int)payload.length(),
    n,
    capture_active ? "true" : "false"
  );

  int status = -1;
  esp_err_t err = http_post_json(post_url.c_str(), payload.c_str(), payload.length(), &status);
  if (err == ESP_OK) {
    logp("POST: OK status=%d", status);
    packetCount = 0;
  } else {
    logp("POST: FAIL %s", esp_err_to_name(err));
  }
}

static void main_task(void* pv) {
  (void)pv;

  logp("main_task START");
  vTaskDelay(pdMS_TO_TICKS(1500));

  uint8_t ch = 1;

  while (true) {
    logp("CYCLE start heap_free=%u config_loaded=%s", (unsigned)ESP.getFreeHeap(), config_loaded_once ? "true" : "false");

    if (!enter_sniffer_mode()) {
      logp("sniffer: no pudo iniciar");
      vTaskDelay(pdMS_TO_TICKS(1000));
      continue;
    }

    int elapsed = 0;
    while (elapsed < SNIFF_DURATION_MS) {
      esp_wifi_set_channel(ch, WIFI_SECOND_CHAN_NONE);
      ch = (ch % WIFI_CHANNEL_MAX) + 1;

      vTaskDelay(pdMS_TO_TICKS(CHANNEL_HOP_INTERVAL_MS));
      elapsed += CHANNEL_HOP_INTERVAL_MS;

      logp("sniff elapsed=%dms packets=%d", elapsed, packetCount);
      if (packetCount >= MAX_PACKETS) {
        logp("sniff: buffer lleno, se corta el barrido antes");
        break;
      }
    }

    last_ap_channel_guess = pick_best_ap_channel();
    logp(
      "AP channel guess=%u (hits=%u)",
      (unsigned)last_ap_channel_guess,
      (unsigned)(last_ap_channel_guess ? ssid_chan_hits[last_ap_channel_guess] : 0)
    );

    const uint32_t slot_delay_ms = anchor_slot_delay_ms();
    if (slot_delay_ms > 0) {
      logp("sta: slot delay=%ums para desfasar reconexion", (unsigned)slot_delay_ms);
      vTaskDelay(pdMS_TO_TICKS(slot_delay_ms));
    }

    bool sta_ok = false;
    for (int i = 1; i <= STA_RETRIES; i++) {
      bool use_channel_hint = (i == 1 && last_ap_channel_guess >= 1 && last_ap_channel_guess <= WIFI_CHANNEL_MAX);
      bool fast_scan = use_channel_hint;
      logp(
        "sta: attempt %d/%d (use_hint=%s)",
        i,
        STA_RETRIES,
        use_channel_hint ? "true" : "false"
      );
      if (enter_sta_and_wait_ip(last_ap_channel_guess, use_channel_hint, fast_scan)) {
        sta_ok = true;
        break;
      }
      sta_disconnect();
      vTaskDelay(pdMS_TO_TICKS(500));
    }

    if (sta_ok) {
      bool cfg_ok = fetch_server_config();
      if (cfg_ok) send_buffer_as_json();
      else logp("CFG: no se pudo obtener config -> skip POST");
    } else {
      logp("sta: NO CONNECT -> skip POST");
      logp("sta: reinicio automatico en %ums", (unsigned)RESTART_AFTER_STA_FAILURE_MS);
      vTaskDelay(pdMS_TO_TICKS(RESTART_AFTER_STA_FAILURE_MS));
      esp_restart();
    }

    sta_disconnect();

    const uint32_t cooldown_ms = POST_COOLDOWN_MS + cycle_jitter_ms();
    logp("CYCLE end cooldown=%ums", (unsigned)cooldown_ms);
    vTaskDelay(pdMS_TO_TICKS(cooldown_ms));
  }
}

void setup() {
  Serial.begin(115200);
  delay(1200);
  Serial.println("=== SKETCH START anchor_sniffer legacy-compatible ===");
  Serial.printf("RESET reason=%d\n", (int)esp_reset_reason());
  Serial.flush();

  xTaskCreatePinnedToCore(main_task, "main_task", 16384, nullptr, 2, nullptr, 1);
}

void loop() {
  delay(1000);
}
