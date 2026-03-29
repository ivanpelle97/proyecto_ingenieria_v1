/*
 * TAG ESP32 que emite Probe Requests en un perfil parecido a smartphone.
 *
 * Uso:
 *   - Ajustar BURST_ALL_CHANNELS para enviar en un solo canal o en varios
 *   - Ajustar TAG_CHANNEL o TAG_CHANNELS segun la estrategia deseada
 *   - Ajustar TX_CYCLE_INTERVAL_MS para enviar mas o menos seguido
 *   - Ajustar TAG_TX_POWER_QDBM para aproximar la potencia deseada
 *   - Flashear y observar por Serial la MAC efectiva del tag
 */

#include <WiFi.h>
#include <esp_timer.h>
#include <esp_wifi.h>

static const uint8_t TAG_CHANNEL = 1;
// Perfil "smartphone-like": un barrido multicanal menos agresivo que el modo de captura robusta.
static const bool BURST_ALL_CHANNELS = true;
static const uint8_t TAG_CHANNELS[] = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13};
static const uint32_t TX_CYCLE_INTERVAL_MS = 500;
static const uint32_t INTER_CHANNEL_GAP_MS = 18;
static const char* TAG_SSID = "NEXA_TAG";
// Espressif documenta que esp_wifi_set_max_tx_power usa unidades de 0.25 dBm.
// 44 => ~11 dBm, un nivel mas cercano a un emisor movil comun sin forzarlo al maximo.
static const int8_t TAG_TX_POWER_QDBM = 50;

// MAC local-admin fija para poder configurarla en el servidor.
static const uint8_t TAG_MAC[6] = {0x02, 0x11, 0x22, 0x33, 0x44, 0x55};

static uint16_t seq = 0;

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
  WiFi.mode(WIFI_STA);
  WiFi.disconnect(true, true);
  delay(100);

  wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
  ESP_ERROR_CHECK(esp_wifi_init(&cfg));
  ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
  ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
  ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, (uint8_t*)TAG_MAC));
  ESP_ERROR_CHECK(esp_wifi_start());
  ESP_ERROR_CHECK(esp_wifi_set_max_tx_power(TAG_TX_POWER_QDBM));
  ESP_ERROR_CHECK(esp_wifi_set_channel(TAG_CHANNEL, WIFI_SECOND_CHAN_NONE));
}

static void send_probe_on_channel(uint8_t channel) {
  esp_err_t ch_err = esp_wifi_set_channel(channel, WIFI_SECOND_CHAN_NONE);
  if (ch_err != ESP_OK) {
    Serial.printf("[TAG] Error set_channel(%u): %d\n", channel, (int)ch_err);
    return;
  }

  uint8_t frame[128];
  size_t len = build_probe_req(frame, sizeof(frame));
  if (len == 0) return;

  esp_err_t tx_err = esp_wifi_80211_tx(WIFI_IF_STA, frame, len, false);
  if (tx_err != ESP_OK) {
    Serial.printf("[TAG] Error TX ch=%u: %d\n", channel, (int)tx_err);
  } else if ((seq % 20) == 0) {
    Serial.printf("[TAG] Probe TX seq=%u ch=%u\n", seq, channel);
  }
  seq++;
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("[TAG] Inicio probe request TX");

  wifi_init_tx_channel();

  uint8_t mac[6];
  esp_wifi_get_mac(WIFI_IF_STA, mac);
  Serial.print("[TAG] MAC: ");
  for (int i = 0; i < 6; i++) {
    Serial.printf("%02X", mac[i]);
    if (i < 5) Serial.print(":");
  }
  Serial.println();

  if (BURST_ALL_CHANNELS) {
    Serial.printf("[TAG] Modo: smartphone-like multicanal (%u canales)\n", (unsigned)(sizeof(TAG_CHANNELS) / sizeof(TAG_CHANNELS[0])));
  } else {
    Serial.printf("[TAG] Modo: canal fijo %u\n", TAG_CHANNEL);
  }
  Serial.printf("[TAG] Periodo por ciclo: %lu ms\n", (unsigned long)TX_CYCLE_INTERVAL_MS);
  Serial.printf("[TAG] Gap entre canales: %lu ms\n", (unsigned long)INTER_CHANNEL_GAP_MS);
  Serial.printf("[TAG] TX power objetivo: %.2f dBm\n", TAG_TX_POWER_QDBM / 4.0f);
}

void loop() {
  static uint64_t last_tx_ms = 0;
  uint64_t now_ms = (uint64_t)(esp_timer_get_time() / 1000ULL);
  if (now_ms - last_tx_ms < TX_CYCLE_INTERVAL_MS) return;
  last_tx_ms = now_ms;

  if (BURST_ALL_CHANNELS) {
    const size_t num_channels = sizeof(TAG_CHANNELS) / sizeof(TAG_CHANNELS[0]);
    for (size_t idx = 0; idx < num_channels; idx++) {
      send_probe_on_channel(TAG_CHANNELS[idx]);
      delay(INTER_CHANNEL_GAP_MS);
    }
  } else {
    send_probe_on_channel(TAG_CHANNEL);
  }
}
