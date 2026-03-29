# NEXA-IPS - entrenamiento guiado por puntos con ESP32 y heatmap

Este proyecto quedo orientado a este flujo:

- 1 TAG ESP32 que emite Probe Requests con una MAC conocida.
- 3 o mas anclas ESP32 que escuchan en modo promiscuo y suben RSSI al servidor.
- Un servidor FastAPI que:
  - guarda los RAW,
  - publica la configuracion activa para cada ancla,
  - controla el punto de entrenamiento actual,
  - cuenta muestras por ancla hasta llegar a `10 RSSI` por punto.
- Una UI Streamlit que permite:
  - elegir cantidad de anclas,
  - cargar largo, ancho y alto del ambiente,
  - definir la posicion de cada ancla,
  - ingresar la MAC del dispositivo a ubicar,
  - iniciar la captura punto por punto,
  - construir el dataset,
  - entrenar el modelo,
  - ver la inferencia y el heatmap.

## 1. Que cambio respecto al PoC anterior

Antes el proyecto funcionaba como PoC manual por intervalos de tiempo.

Ahora el flujo principal es:

1. Crear una campana desde la UI.
2. Configurar cantidad de anclas y sus coordenadas.
3. Ingresar la MAC objetivo.
4. Cargar manualmente cada punto `(x, y, z)` del plano.
5. Esperar a que cada ancla junte `10 muestras` para ese punto.
6. Cerrar el punto y avanzar al siguiente.
7. Al final, generar `dataset.csv`, entrenar y usar el modelo con heatmap.

## 2. Archivos importantes

- `server/app.py`: backend FastAPI con orquestacion de la campana.
- `ui/app_streamlit.py`: consola operativa para setup, captura, training e inferencia.
- `pipeline/build_dataset.py`: arma un dataset fijo de `10 RSSI por ancla por punto`.
- `pipeline/train.py`: entrena y guarda el mejor modelo.
- `firmware/tag_probe_tx/tag_probe_tx.ino`: firmware del TAG ESP32.
- `firmware/anchor_sniffer/anchor_sniffer.ino`: firmware de cada ancla ESP32.

## 3. Preparacion del entorno en Windows / PowerShell

Desde la raiz del repo:

```powershell
python -m venv .venv
. .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 4. Levantar el servidor

Desde la raiz del repo:

```powershell
. .\.venv\Scripts\Activate.ps1
uvicorn server.app:app --host 0.0.0.0 --port 8000
```

Chequeo rapido:

```powershell
curl http://127.0.0.1:8000/health
```

## 5. Levantar la UI

En otra terminal:

```powershell
. .\.venv\Scripts\Activate.ps1
streamlit run ui/app_streamlit.py
```

La UI queda pensada para abrirse en:

- `Servidor FastAPI`: `http://127.0.0.1:8000`
- `Directorio runs`: `runs`

## 6. Flujo operativo completo

### 6.1 Crear la campana

En la UI:

1. Elegi `session_id` y `campaign_id`.
2. Ingresa la `MAC del dispositivo objetivo`.
3. Define `Cantidad de antenas`.
4. Carga `Largo`, `Ancho` y `Alto` del ambiente.
5. Presiona `Cargar esquinas por defecto` para ubicar las anclas a `2.0 m` de altura.
6. Si hace falta, edita manualmente la tabla de anclas.
7. Presiona `Guardar y activar campana`.

Al activarla, el servidor guarda:

- `runs/<session>/<campaign>/experiment.json`
- `runs/<session>/<campaign>/training_state.json`

### 6.2 Capturar cada punto

Para cada posicion del plano:

1. Ingresa `point_id`, `x`, `y` y `z`.
2. Presiona `Comenzar captura en este punto`.
3. Espera a que todas las anclas lleguen a `10/10`.
4. Presiona `Cerrar punto y habilitar el siguiente`.

Archivos que se generan:

- `runs/<session>/<campaign>/raw/<anchor>.jsonl`
- `runs/<session>/<campaign>/samples.csv`
- `runs/<session>/<campaign>/points.csv`

`samples.csv` contiene una fila por muestra capturada para el punto activo.

### 6.3 Construir dataset y entrenar

Desde la UI, al terminar de barrer el ambiente:

1. Presiona `Construir dataset.csv`
2. Presiona `Entrenar modelo`

Se generan:

- `runs/<session>/<campaign>/dataset.csv`
- `runs/<session>/<campaign>/models/metrics.csv`
- `runs/<session>/<campaign>/models/best_model_*.joblib`

Tambien se puede hacer por CLI:

```powershell
. .\.venv\Scripts\Activate.ps1
python pipeline/build_dataset.py --session demo_s1 --campaign train_01
python pipeline/train.py --dataset runs/demo_s1/train_01/dataset.csv --outdir runs/demo_s1/train_01/models
```

### 6.4 Inferencia y heatmap

Con el modelo ya entrenado, la UI usa las ultimas `10` muestras por ancla para estimar `(x, y)` y dibuja:

- plano del ambiente,
- posicion de las anclas,
- puntos de entrenamiento ya tomados,
- heatmap de predicciones recientes.

## 7. Firmware del TAG ESP32

Archivo:

- `firmware/tag_probe_tx/tag_probe_tx.ino`

Configurar:

- `TAG_CHANNEL`
- `TX_INTERVAL_MS`
- `TAG_MAC`

El firmware ya quedo ajustado para:

- emitir Probe Requests a baja frecuencia,
- imprimir la MAC por Serial al arrancar.

## 8. Firmware de las anclas ESP32

Archivo:

- `firmware/anchor_sniffer/anchor_sniffer.ino`

Configurar en cada placa:

- `WIFI_SSID`
- `WIFI_PASS`
- `SERVER_BASE_URL`
- `ANCHOR_ID` (`A1`, `A2`, `A3`, etc.)

Importante:

- ya no hace falta recompilar cada ancla cada vez que cambie la MAC objetivo o la campana;
- la ancla consulta eso al servidor en `/api/anchors/<anchor_id>/config`;
- el servidor decide si la muestra cuenta para el punto activo.

## 9. Recomendaciones practicas para tu caso de 3 anclas

Para una primera version estable:

1. Usa `3` anclas: `A1`, `A2`, `A3`.
2. Ubicalas por defecto en esquinas del ambiente a `2.0 m`.
3. Deja el TAG con un periodo de `750 ms` o `1000 ms`.
4. Barre puntos en una grilla regular del piso.
5. No cierres un punto hasta ver `10/10` en cada ancla.

## 10. Estado actual

Con los cambios hechos, lo que tenias originalmente no cubria bien el flujo que pedias.

Ahora el repo ya quedo preparado para:

- elegir cantidad de anclas desde la UI,
- configurar ambiente y posiciones,
- fijar la MAC a localizar,
- entrenar punto por punto con `10 RSSI por ancla`,
- construir dataset,
- entrenar modelo,
- visualizar inferencia con heatmap.

## 11. Validacion local hecha

Se valido la sintaxis Python con:

```powershell
python -m compileall server ui pipeline
```

No pude ejecutar FastAPI ni Streamlit en esta sesion porque en el entorno actual no estan instaladas las dependencias de runtime (`fastapi`, `streamlit`, etc.). Con `pip install -r requirements.txt` deberia quedar listo para correr.
