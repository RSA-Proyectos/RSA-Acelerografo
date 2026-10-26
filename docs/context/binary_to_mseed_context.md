---
proyecto: acelerografo
tipo: contexto_tecnico
archivo: scripts/operation/mseed/binary_to_mseed.py
temas: [mseed, conversion, gaps, livelock, rendimiento, obspy, numpy, decorrelacion_tiempo]
generado: 2026-10-26
---
# binary_to_mseed.py — Contexto para Agentes IA

> Motor de conversión sismológica de archivos binarios `.dat` (tramas de 2506 bytes del acelerógrafo) a formato MiniSEED (STEIM1) con dispersión vectorizada $O(N)$, preservación de fase, deduplicación de tramas y purga de anomalías de bus SPI.

**Ruta**: `scripts/operation/mseed/binary_to_mseed.py`  
**LOC**: 1044 | **Lenguaje**: Python 3 | **Dependencias**: `obspy`, `numpy`, `structured_logger`, `json`, `argparse`, `glob`, `re`, `datetime`  
**Proceso**: Ejecutado como script CLI (`1`, `2`, `3`, `4`) o importado por `procesar_y_subir_pendientes.py`  

---

## Arquitectura

```mermaid
graph TD
    subgraph Entrada
        DAT[".dat binarios<br>2506 B/trama"]
        CFG["Configuraciones JSON<br>dispositivo + mseed"]
        TMP["*.tmp (punteros a archivos)"]
    end

    subgraph "Procesamiento Core (binary_to_mseed.py)"
        ETB["extraer_tiempo_binario()"]
        LAB["leer_archivo_binario()<br>- Validación cabecera (año, mes, día, hora)<br>- Purga glitches SPI (0x04 0x04 0x04)<br>- Deduplicación de timestamps O(1)<br>- Filtro outliers cronológicos aislados<br>- Detección gaps y medianoche (23h->00h)"]
        DEC["Decodificación vectorial 20-bit<br>(3 canales X, Y, Z)"]
        OT["obtenerTraza() O(N)<br>- Preallocado directo de ceros<br>- Dispersión indexada de bloques de 250 muestras<br>- Preservación de fase y dimensión exacta npts"]
        SM["conversion_mseed_digital()<br>Stream(CH1, CH2, CH3)"]
    end

    subgraph Salida
        MSEED[".mseed (STEIM1, reclen=512B)"]
        LOG["mseed.log (StructuredLogger)"]
    end

    DAT --> LAB
    DAT --> ETB
    CFG --> LAB
    TMP -.-> LAB
    LAB --> DEC
    DEC --> OT
    ETB -.-> OT
    OT --> SM
    SM --> MSEED
    LAB --> LOG
    SM --> LOG
```

---

## Modos de Operación y CLI

Soporta ejecución posicional simple o flags extendidos:

| Modo | Argumento CLI | Flags alternativos | Entrada | Salida |
|---|---|---|---|---|
| **1** | `1` | `--continuous` | Lee línea 2 de `NombreArchivoRegistroContinuo.tmp` | `directorios.archivos_mseed` (o `--output-dir`) |
| **2** | `2` | `--event` | Lee línea 1 de `NombreArchivoEventoExtraido.tmp` | `directorios.eventos_extraidos` |
| **3** | `3 <archivo>` | `--file <archivo>` | Archivo binario individual | `directorios.archivos_mseed` (o `--output-dir`) |
| **4** | N/A | `--dir <directorio>` | Todos los `*.dat` del directorio | `directorios.archivos_mseed` (o `--output-dir`) |

### Parámetros opcionales CLI
* `--output-dir`, `-o`: Sobrescribe el directorio de destino configurado en JSON, permitiendo aislar salidas de prueba (ej. `/home/rsa/data/test-bin`) sin permisos de superusuario.

---

## Pipeline y Nivel de Optimización $O(N)$

### 1. Desempaquetado y Filtrado Vectorial en `leer_archivo_binario`
* Procesa chunks de 60 tramas ($150{,}360$ bytes).
* **Validación de cabecera**: Descarta tramas fuera de rango horario (`hora<=23`, `minuto<=59`, `segundo<=59`) o de calendario (`año`, `mes`, `día`).
* **Protección contra Glitch SPI (Fase 6 / H7)**: Purga tramas donde todos los bytes de tiempo son idénticos (`0x04 0x04 0x04 0x04 0x04 0x04`).
* **Deduplicación Vectorial**: `np.unique` sobre timestamps preserva la primera ocurrencia cronológica y rebanado tridimensional `(3, N_tramas, fsample)`.
* **Filtro de Coherencia Cronológica**: Purga tramas outlier intermedias ($|t_k - t_{k-1}| > 60$ s con retorno inmediato a continuidad circundante).
* **Acotamiento de Medianoche**: Suma modular $+86400$ s acotada estrictamente a la ventana limítrofe $t_{\text{ant}} \ge 86000$ (hora 23) y $t_{\text{sig}} \le 3600$ (hora 00), evitando falsos cruces diurnos de 24 horas.

### 2. Dispersión Vectorial en `obtenerTraza` (Solución H1, H2, H3)
* **Preallocado Directo**: `data_completo = np.zeros(npts_completo, dtype=np.int32)`.
* **Dispersión $O(N)$**: En lugar de `np.insert()` recursivo (livelock $O(M \cdot N)$), calcula directamente la posición de inserción de cada segundo presente `idx_destino = seg_relativo * fsample` y copia los bloques de 250 muestras.
* **Integridad de Fase**: Los datos registrados conservan su segundo absoluto en el MiniSEED; los segundos faltantes quedan rellenos con ceros continuos sin truncar la traza.

---

## Componentes y Funciones Clave

| Elemento | Firma / Tipo | Descripción |
|---|---|---|
| `leer_archivo_binario` | `(archivo, logger, usar_fecha) -> (datos, gaps, t_lectura, ts_ini, ts_fin, t_validado)` | Lee tramas, valida cabeceras, purga tramas corruptas, detecta gaps y retorna 6 valores estructurados. |
| `obtenerTraza` | `(nombre, num_ch, data, t_bin, gaps, cfg) -> obspy.Trace` | Genera traza ObsPy triaxial con header SEED, sample rate, npts exacto y ceros vectorizados en gaps. |
| `conversion_mseed_digital` | `(fileName, path, tiempo, datos, gaps, cfg, logger) -> None` | Construye el `Stream` con los 3 canales y exporta en formato STEIM1 (reclen 512B). |
| `procesar_archivo_individual` | `(bin_file, out_dir, codigo, cfg, logger, usar_fecha) -> (bool, msg, info)` | Orquesta la lectura, sincroniza el tiempo canónico validado y ejecuta la conversión. |
| `extraer_tiempo_binario` | `(archivo, usar_fecha) -> dict` | Interfaz pública requerida por `procesar_y_subir_pendientes.py` (extrae timestamp de primera trama). |
| `extraer_fecha_desde_nombre_archivo`| `(path) -> dict` | Extrae año, mes y día mediante regex del nombre `CODIGO_AAMMDD-HHMMSS.dat`. |

---

## Configuraciones y Entorno

* `PROJECT_LOCAL_ROOT`: Variable de entorno obligatoria (`/home/rsa/projects/acelerografo`).
* `configuracion_dispositivo.json`: Define rutas de directorios (`RC`, `ED`, `MSEED`).
* `configuracion_mseed.json`: Define metadatos SEED (`CODIGO`, `SENSOR`, `MUESTREO=250`, `RED=EC`, `CANAL=ZNE`, `CALIDAD=D`, `USAR_FECHA_FILENAME`).

---

## Rendimiento Validado (Raspberry Pi 3B+)

| Caso de Prueba | Entrada | Tiempo de Conversión | Duración MiniSEED | Muestras |
|---|---|---|---|---|
| **Archivo continuo nominal** | ~3600 tramas (8.7 MB) | **0.95 s** | 3599.996 s (1.00 h) | 900,000 |
| **Archivo con gaps masivos (CHA02)** | 100 tramas + 3500s gaps | **0.08 s** | 3599.996 s (1.00 h) | 900,000 |
| **Archivo interrumpido (apagón)** | 292 tramas (715 KB) | **0.50 s** | 291.996 s (~4.8 min) | 73,000 |
| **Glitch SPI (04:04:04 en hora 17)** | Trama patológica real | **0.95 s** | 3599.996 s (1.00 h) | 900,000 |

---

## Limitaciones Conocidas / Pendientes

1. **Desacople en archivos truncados de cola (H8/H9)**: Documentado en diagnóstico `2026-10-26`. Si un corte de luz interrumpe abruptamente la última trama física, `extraer_timestamp_completo_binario` devuelve `None` y omite las claves de fecha en `tiempo_binario_validado`.
2. **Sin compresión posterior**: Los archivos MiniSEED se graban con STEIM1 en bloques de 512 bytes sin compresión gzip adicional a nivel de archivo.
3. **Pérdida de tramas descartadas**: Las tramas con cabecera de tiempo corrupta se purgan; el segundo correspondiente se rellena con ceros continuos en ObsPy.
