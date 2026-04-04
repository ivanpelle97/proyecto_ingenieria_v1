# Handoff Codex

Este archivo resume el estado tecnico y documental del proyecto para poder retomarlo en otra PC sin perder contexto.

## 1. Estado general

- Repositorio GitHub privado:
  - `https://github.com/ivanpelle97/proyecto_ingenieria_v1`
- Rama principal:
  - `main`
- Ultimo commit confirmado al momento de este handoff:
  - `301f91e` - `Add all 2026 source screenshots`

El proyecto quedo en una etapa avanzada y funcional:

- ya existe una cadena completa de:
  - firmware ESP32 para entrenamiento,
  - firmware ESP32 para inferencia continua,
  - firmware TAG viejo y nuevo,
  - servidor FastAPI,
  - UI Streamlit,
  - pipeline de dataset,
  - entrenamiento de modelos,
  - inferencia online,
  - documento final de entrega.

## 2. Documento final

Documento principal final generado:

- [PROYECTO_FINAL_CORREGIDO_MANUAL_v4_entrega_final.docx](./PROYECTO_FINAL_CORREGIDO_MANUAL_v4_entrega_final.docx)

Script que lo reconstruye:

- [scripts/build_final_delivery_report.py](./scripts/build_final_delivery_report.py)

Documento base utilizado para reconstruirlo:

- [PROYECTO_FINAL_CORREGIDO_MANUAL_v3_integrado.docx](./PROYECTO_FINAL_CORREGIDO_MANUAL_v3_integrado.docx)

Marco teorico de referencia:

- [PROYECTO DE INGENIERIA v3_MARCO_TEORICO.docx](./PROYECTO%20DE%20INGENIERIA%20v3_MARCO_TEORICO.docx)

El `v4` ya incluye:

- campañas `demo_s1` a `demo_s5`
- resultados reales y metricas
- cierre tecnico final
- explicacion detallada de firmware viejo y nuevo
- capturas de pantalla reales
- placeholders para fotos de campo

Para regenerarlo:

```powershell
python scripts/build_final_delivery_report.py
```

## 3. Carpetas de capturas y material visual

Capturas originales copiadas al repo:

- [doc_assets/source_screenshots](./doc_assets/source_screenshots)

Cantidad versionada:

- `107` capturas de pantalla originales del año `2026`

Figuras derivadas y contactos generados:

- [doc_assets/generated](./doc_assets/generated)

Si luego se agregan fotos de celular para incorporarlas al documento:

- crear o usar la carpeta:
  - [doc_assets/pending_phone_photos](./doc_assets/pending_phone_photos)

Luego volver a ejecutar:

```powershell
python scripts/build_final_delivery_report.py
```

## 4. Campañas reales ejecutadas

### 4.1 demo_s1 / train_01

- 3 antenas ESP32
- `samples_per_anchor = 20`
- 2 puntos completos
- `samples.csv`: `120` filas
- sin dataset consolidado ni metricas de modelo

Interpretacion:

- corrida piloto inicial para validar el pipeline extremo a extremo

### 4.2 demo_s2 / train_02

- 3 antenas ESP32
- `samples_per_anchor = 10`
- 18 puntos completos
- `samples.csv`: `540` filas
- `dataset.csv`: `18 x 58`
- mejor modelo:
  - `extratrees_500`
- metricas:
  - `MAE = 1.2625767240849362`
  - `P95 = 2.37493796944435`

Interpretacion:

- primera campaña completa utilizable
- mejor linea de base de error del proyecto

### 4.3 demo_s3 / train_03

- corrida de transicion
- sin puntos completos utiles
- sin dataset final

Interpretacion:

- utilizada para detectar problemas operativos antes de migrar a 4 antenas

### 4.4 demo_s4 / train_04

- 4 antenas ESP32
- `samples_per_anchor = 10`
- 18 puntos completos
- `samples.csv`: `720` filas
- `dataset.csv`: `18 x 75`
- mejor modelo:
  - `extratrees_500`
- metricas:
  - `MAE = 1.5124976536962778`
  - `P95 = 3.1848917611094056`

Interpretacion:

- primera campaña completa con 4 antenas
- sirvio para detectar problemas de firmware, consistencia de captura e inferencia

### 4.5 demo_s5 / train_05

- 4 antenas ESP32
- `samples_per_anchor = 50`
- 21 puntos completos
- `samples.csv`: `4200` filas
- `dataset.csv`: `21 x 235`
- mejor modelo:
  - `rf_300`
- metricas:
  - `MAE = 1.6699262015802194`
  - `P95 = 2.7558882415656787`
- estado final:
  - `ready`

Interpretacion:

- campaña final mas madura metodologicamente
- ya usa firmware nuevo de entrenamiento en canal fijo
- base principal para la inferencia final y el documento de entrega

## 5. Estado actual de demo_s5

Archivos principales:

- [runs/demo_s5/train_05/experiment.json](./runs/demo_s5/train_05/experiment.json)
- [runs/demo_s5/train_05/points.csv](./runs/demo_s5/train_05/points.csv)
- [runs/demo_s5/train_05/samples.csv](./runs/demo_s5/train_05/samples.csv)
- [runs/demo_s5/train_05/dataset.csv](./runs/demo_s5/train_05/dataset.csv)
- [runs/demo_s5/train_05/models/metrics.csv](./runs/demo_s5/train_05/models/metrics.csv)
- [runs/demo_s5/train_05/models/best_model_rf_300.joblib](./runs/demo_s5/train_05/models/best_model_rf_300.joblib)
- [runs/demo_s5/train_05/training_state.json](./runs/demo_s5/train_05/training_state.json)

Geometria de antenas en `demo_s5`:

- `A1 = (5.58, 0.0, 2.0)`
- `A2 = (5.58, 3.6, 2.0)`
- `A3 = (0.0, 0.5, 2.0)`
- `A4 = (0.0, 3.6, 2.0)`

Ultimo estado final observado de antenas en inferencia:

- `A1` ultimo lote: `40`
- `A2` ultimo lote: `38`
- `A3` ultimo lote: `33`
- `A4` ultimo lote: `42`

Esto se uso como evidencia de que la version estabilizada con 4 antenas ya estaba reportando de forma simultanea.

## 6. Firmware disponibles

### 6.1 Antena vieja de captura

- [firmware/anchor_sniffer/anchor_sniffer.ino](./firmware/anchor_sniffer/anchor_sniffer.ino)

Uso:

- firmware original de campañas iniciales

Caracteristicas:

- alterna captura promiscuo / asociacion Wi-Fi
- mas sensible a reconexiones y huecos temporales

### 6.2 Antena nueva para entrenamiento

- [firmware/anchor_sniffer_training_fixed_channel/anchor_sniffer_training_fixed_channel.ino](./firmware/anchor_sniffer_training_fixed_channel/anchor_sniffer_training_fixed_channel.ino)

Uso:

- campañas de entrenamiento por puntos

Caracteristicas:

- canal fijo del AP
- bloqueo por `BSSID`
- captura solo cuando `capture_active = true`
- pensada para construir datasets consistentes

### 6.3 Antena nueva para inferencia continua

- [firmware/anchor_sniffer_inference_continuous/anchor_sniffer_inference_continuous.ino](./firmware/anchor_sniffer_inference_continuous/anchor_sniffer_inference_continuous.ino)

Uso:

- inferencia online

Caracteristicas:

- asociacion continua al AP
- escucha en canal fijo
- filtrado `TARGET_ONLY`
- reintentos escalonados por antena:
  - `A1 = 0 ms`
  - `A2 = 700 ms`
  - `A3 = 1400 ms`
  - `A4 = 2100 ms`
- jitter de reconexion
- backoff adicional ante `AUTH_EXPIRE`

### 6.4 TAG viejo

- [firmware/tag_probe_tx/tag_probe_tx.ino](./firmware/tag_probe_tx/tag_probe_tx.ino)

Uso:

- campañas iniciales

Caracteristicas:

- barrido multicanal
- logica mas parecida a dispositivo movil generico

### 6.5 TAG nuevo final

- [firmware/tag_probe_tx_inference_fixed_channel/tag_probe_tx_inference_fixed_channel.ino](./firmware/tag_probe_tx_inference_fixed_channel/tag_probe_tx_inference_fixed_channel.ino)

Uso:

- entrenamiento final e inferencia final

Caracteristicas:

- canal fijo
- `TX_INTERVAL_MS = 90`
- `BURST_PROBES_PER_CYCLE = 3`
- `BURST_GAP_MS = 12`
- `TAG_TX_POWER_QDBM = 76`

Recomendacion metodologica:

- usar el mismo firmware del TAG en entrenamiento e inferencia

### 6.6 Firmwares de prueba

- [firmware/wifi_connect_smoke_test/wifi_connect_smoke_test.ino](./firmware/wifi_connect_smoke_test/wifi_connect_smoke_test.ino)
- [firmware/wifi_scan_smoke_test/wifi_scan_smoke_test.ino](./firmware/wifi_scan_smoke_test/wifi_scan_smoke_test.ino)

Uso:

- diagnostico de placas con problemas de asociacion o recepcion Wi-Fi

## 7. Software principal

Backend:

- [server/app.py](./server/app.py)

UI:

- [ui/app_streamlit.py](./ui/app_streamlit.py)

Pipeline:

- [pipeline/build_dataset.py](./pipeline/build_dataset.py)
- [pipeline/train.py](./pipeline/train.py)
- [pipeline/utils.py](./pipeline/utils.py)
- [pipeline/plot_results.py](./pipeline/plot_results.py)

## 8. Ajuste importante hecho en inferencia

Se agrego la posibilidad de no rellenar artificialmente con `-100` cuando faltan muestras reales en inferencia.

Archivos relevantes:

- [pipeline/utils.py](./pipeline/utils.py)
- [ui/app_streamlit.py](./ui/app_streamlit.py)

Concepto clave:

- `strict_full_window`
- `pad_missing = not strict_full_window`

Interpretacion:

- si se activa ventana estricta, la inferencia espera datos reales completos y no inventa RSSI faltantes

## 9. Modo solo inferencia en la UI

La UI ya quedo adaptada para reutilizar una campaña entrenada sin volver a medir puntos.

Archivo:

- [ui/app_streamlit.py](./ui/app_streamlit.py)

Uso previsto:

- activar una campaña ya entrenada
- dejar antenas en modo continuo
- usar `6. Inferencia online`

## 10. Router / Wi-Fi

Durante la estabilizacion final se concluyo que la red debe quedar asi:

- banda `2.4 GHz`
- canal fijo `1`
- `WPA2-PSK` con `AES`
- `Protected Management Frames = Off`
- `Band Steering = Off`
- filtro MAC deshabilitado
- idealmente ancho de banda `20 MHz`

Capturas de configuracion del router:

- [doc_assets/source_screenshots](./doc_assets/source_screenshots)
  - buscar las de fecha `2026-03-26`

## 11. Placas y programacion

Para placas ESP32 genericas nuevas tipo DevKit, la configuracion usada en Arduino IDE fue:

- `ESP32 Dev Module`

Esa decision tambien quedo reflejada visualmente en:

- `Captura de pantalla 2026-04-03 085858.png`

## 12. Git y capturas

Quedaron subidos:

- documento final `v4`
- script generador final
- figuras derivadas
- `107` capturas originales del año `2026`

Carpeta clave:

- [doc_assets/source_screenshots](./doc_assets/source_screenshots)

## 13. Proximos pasos sugeridos

Si se retoma este proyecto en otra PC, el orden recomendado es:

1. clonar el repo privado
2. abrir este archivo `HANDOFF_CODEX.md`
3. revisar [README.md](./README.md)
4. revisar [PROYECTO_FINAL_CORREGIDO_MANUAL_v4_entrega_final.docx](./PROYECTO_FINAL_CORREGIDO_MANUAL_v4_entrega_final.docx)
5. usar [scripts/build_final_delivery_report.py](./scripts/build_final_delivery_report.py) si hay que regenerar el documento
6. usar `demo_s5/train_05` como referencia principal de campaña final
7. si se vuelve a medir:
   - entrenamiento con `anchor_sniffer_training_fixed_channel`
   - inferencia con `anchor_sniffer_inference_continuous`
   - TAG con `tag_probe_tx_inference_fixed_channel`

## 14. Resumen corto para darle a otro Codex

Texto breve para copiar en otra sesion:

> Este repo ya tiene una version final bastante madura del sistema NEXA-IPS. La campaña final relevante es `runs/demo_s5/train_05`, con 4 antenas ESP32, 21 puntos, 50 muestras por antena, `dataset.csv` de `21 x 235` y mejor modelo `rf_300`. El documento final es `PROYECTO_FINAL_CORREGIDO_MANUAL_v4_entrega_final.docx` y se regenera con `scripts/build_final_delivery_report.py`. Los firmwares importantes son `firmware/anchor_sniffer_training_fixed_channel/anchor_sniffer_training_fixed_channel.ino`, `firmware/anchor_sniffer_inference_continuous/anchor_sniffer_inference_continuous.ino` y `firmware/tag_probe_tx_inference_fixed_channel/tag_probe_tx_inference_fixed_channel.ino`. Tambien quedaron subidas todas las capturas originales 2026 en `doc_assets/source_screenshots`. Continuar desde aqui.
