#include <Arduino.h>
#include <esp_err.h>
#include <esp_event.h>
#include <esp_netif.h>
#include <esp_system.h>
#include <esp_timer.h>
#include <esp_wifi.h>

/*
 * TAG alternativo para inferencia online continua.
 *
 * Recomendado junto con:
 *   firmware/anchor_sniffer_inference_continuous/
 *
 * Uso recomendado:
 *   - Fijar el canal del router 2.4 GHz.
 *   - Ajustar WIFI_AP_CHANNEL al mismo canal fijo del AP.
 *   - Mantener este valor igual al usado por
 *     firmware/anchor_sniffer_inference_continuous/.
 *   - Usar este TAG durante inferencia online para aumentar la densidad
 *     de paquetes observables por todas las antenas ESP32.
 */

static const uint8_t WIFI_AP_CHANNEL = 1;
static const uint8_t TAG_CHANNEL = WIFI_AP_CHANNEL;
static const uint32_t TX_INTERVAL_MS = 90;
static const uint8_t BURST_PROBES_PER_CYCLE = 3;
static const uint32_t BURST_GAP_MS = 12;
static const char* TAG_SSID = "NEXA_TAG_INF";
static const int8_t TAG_TX_POWER_QDBM = 76;  // ~19 dBm

static const uint8_t TAG_MAC[6] = {0x02, 0x11, 0x22, 0x33, 0x44, 0x55};

static uint16_t seq = 0;

static void log_tag(const char* fmt, ...) {
  va_list args;
  va_start(args, fmt);
  Serial.print("[TAG-INF] ");
  Serial.vprintf(fmt, args);
  Serial.print("\n");
  Serial.flush();
  va_end(args);
}

static void fatal_wifi(const char* step, esp_err_t err) {
  log_tag("Fallo en %s: %s (%d)", step, esp_err_to_name(err), (int)err);
  delay(1500);
  ESP.restart();
}

static size_t build_probe_req(uint8_t* buf, size_t maxlen) {
  const uint8_t fc[2] = {0x40, 0x00};
  const uint8_t dur[2] = {0x00, 0x00};
  const uint8_t da[6] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};
  const uint8_t bssid[6] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};
  uint8_t sa[6];
  memcpy(sa, TAG_MAC, 6);

  uint16_t sc = (seq << 4);
  uint8_t sc_bytes[2] = {(uint8_t)(sc & 0xff), (uint8_t)((sc >> 8) & 0xff)};

  const uint8_t ssid_len = (uint8_t)min((size_t)32, strlen(TAG_SSID));
  const uint8_t rates[] = {0x82, 0x84, 0x8b, 0x96};

  size_t need = 24 + 2 + 2 + ssid_len + 2 + sizeof(rates);
  if (need > maxlen) return 0;

  size_t offset = 0;
  memcpy(buf + offset, fc, 2); offset += 2;
  memcpy(buf + offset, dur, 2); offset += 2;
  memcpy(buf + offset, da, 6); offset += 6;
  memcpy(buf + offset, sa, 6); offset += 6;
  memcpy(buf + offset, bssid, 6); offset += 6;
  memcpy(buf + offset, sc_bytes, 2); offset += 2;

  buf[offset++] = 0x00;
  buf[offset++] = ssid_len;
  memcpy(buf + offset, TAG_SSID, ssid_len); offset += ssid_len;

  buf[offset++] = 0x01;
  buf[offset++] = (uint8_t)sizeof(rates);
  memcpy(buf + offset, rates, sizeof(rates)); offset += sizeof(rates);

  return offset;
}

static void wifi_init_tx_channel() {
  esp_err_t err = esp_netif_init();
  if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) fatal_wifi("esp_netif_init", err);

  err = esp_event_loop_create_default();
  if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) fatal_wifi("esp_event_loop_create_default", err);

  wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
  err = esp_wifi_init(&cfg);
  if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) fatal_wifi("esp_wifi_init", err);

  err = esp_wifi_set_storage(WIFI_STORAGE_RAM);
  if (err != ESP_OK) fatal_wifi("esp_wifi_set_storage", err);

  err = esp_wifi_set_mode(WIFI_MODE_STA);
  if (err != ESP_OK) fatal_wifi("esp_wifi_set_mode", err);

  err = esp_wifi_set_ps(WIFI_PS_NONE);
  if (err != ESP_OK) fatal_wifi("esp_wifi_set_ps", err);

  err = esp_wifi_set_mac(WIFI_IF_STA, (uint8_t*)TAG_MAC);
  if (err != ESP_OK) fatal_wifi("esp_wifi_set_mac", err);

  err = esp_wifi_start();
  if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) fatal_wifi("esp_wifi_start", err);

  err = esp_wifi_set_max_tx_power(TAG_TX_POWER_QDBM);
  if (err != ESP_OK) fatal_wifi("esp_wifi_set_max_tx_power", err);

  err = esp_wifi_set_channel(TAG_CHANNEL, WIFI_SECOND_CHAN_NONE);
  if (err != ESP_OK) fatal_wifi("esp_wifi_set_channel", err);
}

static void send_probe() {
  uint8_t frame[128];
  size_t len = build_probe_req(frame, sizeof(frame));
  if (len == 0) return;

  esp_err_t tx_err = esp_wifi_80211_tx(WIFI_IF_STA, frame, len, false);
  if (tx_err != ESP_OK) {
    log_tag("Error TX: %s (%d)", esp_err_to_name(tx_err), (int)tx_err);
  } else if ((seq % 40) == 0) {
    log_tag("Probe TX seq=%u ch=%u", seq, TAG_CHANNEL);
  }
  seq++;
}

void setup() {
  Serial.begin(115200);
  delay(800);
  log_tag("Inicio probe request TX en canal fijo");
  log_tag("RESET reason=%d", (int)esp_reset_reason());

  wifi_init_tx_channel();

  uint8_t mac[6];
  esp_wifi_get_mac(WIFI_IF_STA, mac);
  Serial.print("[TAG-INF] MAC: ");
  for (int i = 0; i < 6; i++) {
    Serial.printf("%02X", mac[i]);
    if (i < 5) Serial.print(":");
  }
  Serial.println();
  Serial.flush();

  log_tag("Canal fijo AP/TAG: %u", TAG_CHANNEL);
  log_tag("Periodo: %lu ms", (unsigned long)TX_INTERVAL_MS);
  log_tag("Probes por ciclo: %u", (unsigned)BURST_PROBES_PER_CYCLE);
  log_tag("Separacion intra-ciclo: %lu ms", (unsigned long)BURST_GAP_MS);
  log_tag("TX power objetivo: %.2f dBm", TAG_TX_POWER_QDBM / 4.0f);
}

void loop() {
  static uint64_t last_tx_ms = 0;
  uint64_t now_ms = (uint64_t)(esp_timer_get_time() / 1000ULL);
  if (now_ms - last_tx_ms < TX_INTERVAL_MS) return;
  last_tx_ms = now_ms;
  for (uint8_t i = 0; i < BURST_PROBES_PER_CYCLE; i++) {
    send_probe();
    if (i + 1 < BURST_PROBES_PER_CYCLE) {
      delay(BURST_GAP_MS);
    }
  }
}
