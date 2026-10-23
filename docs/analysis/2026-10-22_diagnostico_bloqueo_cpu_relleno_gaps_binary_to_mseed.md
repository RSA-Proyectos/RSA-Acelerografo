---
proyecto: RSA-Acelerografo
tipo: diagnostico_tecnico
resolucion: pendiente
temas: [mseed, conversion, binary_to_mseed, rendimiento_cpu, livelock, corrupcion_fase, gaps_temporales]
fecha: 2026-10-22
---

# Diagnóstico Técnico Profundo: Bloqueo de CPU (Livelock), Corrupción de Fase y Desincronización Temporal en Conversión de Binarios con Gaps (`binary_to_mseed.py`)

**Fecha**: 2026-10-22  
**Proyecto / Repositorio**: `RSA-Acelerografo`  
**Componente(s) afectado(s)**: [`scripts/operation/mseed/binary_to_mseed.py`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/mseed/binary_to_mseed.py) ([`obtenerTraza`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/mseed/binary_to_mseed.py#L404-L464), [`leer_archivo_binario`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/mseed/binary_to_mseed.py#L99-L187)), [`scripts/operation/drive/procesar_y_subir_pendientes.py`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/drive/procesar_y_subir_pendientes.py)  
**Estado**: Diagnosticado en profundidad (mitigado provisionalmente mediante aislamiento en cuarentena)  
**Severidad**: Crítica  

---

## 1. Resumen Ejecutivo

Durante el despliegue del proceso masivo de conversión offline en la estación acelerográfica `CHA02` (`procesarpendientes --convert-only --all`), el flujo de ejecución colapsó en el primer archivo de la cola (`CHA2_261001-000000.dat`), manteniendo la CPU al 100% de manera indefinida sin emitir trazas de error ni avanzar en la conversión. La interrupción manual (`SIGINT`) expuso que el hilo de ejecución se encontraba atrapado en la función [`obtenerTraza`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/mseed/binary_to_mseed.py#L404-L464) de [`binary_to_mseed.py`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/mseed/binary_to_mseed.py), dentro de un bucle iterativo que invoca repetidamente `np.insert()` para intercalar bloques de ceros por cada segundo faltante (*gap* temporal).

Un análisis estático y dinámico minucioso revela que el problema no se limita a un cuello de botella de rendimiento, sino que involucra **dos fallas críticas simultáneas**:
1. **Livelock algorítmico y saturación de bus de memoria ($O(M \cdot N)$)**: `np.insert` no opera *in-place*. En un archivo con miles de segundos faltantes, el sistema ejecuta miles de reasignaciones completas de arreglos de cientos de miles de elementos en memoria RAM. En los procesadores ARM de recursos limitados (Raspberry Pi 3/4 con 0.9 GB de RAM), esto fuerza una copia redundante de más de 37 GB de datos en RAM para un único archivo de una hora, colapsando la caché y bloqueando el núcleo de ejecución.
2. **Defecto de lógica, corrupción de fase y desincronización de forma de onda**: El código pre-aloca el arreglo final con ceros al final (`data_completo[:len(data)] = data`) y luego inserta ceros dinámicamente mediante `np.insert()`. Esto provoca una **inflación dimensional errónea** (el arreglo crece al doble de los ceros requeridos) y un **desfase progresivo de índices**: las inserciones se realizan en posiciones temporales incorrectas, rebanando y fragmentando señales sismológicas reales continuas y generando archivos MiniSEED gravemente corruptos con timestamps artificiales desalineados.

Adicionalmente, se detectan vulnerabilidades temporales en [`leer_archivo_binario`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/mseed/binary_to_mseed.py#L99-L187) ante cruces de medianoche ($23:59:59 \rightarrow 00:00:00$) y saltos por sincronización de reloj GPS/RTC.

---

## 2. Estado Actual

| Componente / Módulo | Estado Operativo | Observaciones Técnicas |
|---|---|---|
| [`binary_to_mseed.py`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/mseed/binary_to_mseed.py) (Archivos sin gaps) | ✅ Operativo | Convierte nominalmente en $< 3$ segundos cuando `segundos_faltantes is None`. |
| [`binary_to_mseed.py:obtenerTraza`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/mseed/binary_to_mseed.py#L404-L464) (Archivos con gaps) | ❌ Inoperativo / Corruptor | Livelock de CPU en bucle `np.insert()`, inflación indebida de `npts` y fragmentación espuria de la señal. |
| [`binary_to_mseed.py:leer_archivo_binario`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/mseed/binary_to_mseed.py#L99-L187) | ⚠️ Degradado | No maneja cruce de medianoche (`dif_segundos < 0`), no detecta gaps iniciales/finales (head/tail gaps) ni timestamps duplicados. |
| [`procesar_y_subir_pendientes.py`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/drive/procesar_y_subir_pendientes.py) | ⚠️ Bloqueado en cascada | El flujo general opera bien, pero queda retenido indefinidamente al procesar cualquier día con gaps debido al bloqueo en el subproceso de conversión. |
| Suite de pruebas de `mseed` | ❌ Inexistente | No existe un archivo `test_binary_to_mseed.py` que valide algorítmicamente la integridad sismológica y el manejo de gaps. |
| Archivos `CHA02` del día 2026-10-01 | ⚠️ En Cuarentena | 24 archivos `.dat` aislados en directorio de cuarentena para permitir el procesamiento del resto del backlog (días 02 al 21). |

---

## 3. Evidencia y Análisis

### 3.1. Recursos Analizados
* **Código fuente**:
  - [`scripts/operation/mseed/binary_to_mseed.py`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/mseed/binary_to_mseed.py) (Líneas 99–187, 404–464)
  - [`scripts/operation/core/frame_decoder.py`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/core/frame_decoder.py) (Constantes y especificación de tramas binarias de 2506 bytes)
  - [`scripts/operation/drive/procesar_y_subir_pendientes.py`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/drive/procesar_y_subir_pendientes.py) (Líneas 298–315)
* **Configuración del sistema**:
  - [`configuration/configuracion_mseed.json.template`](file:///home/rsa/git/montajes/acelerografo-DEV00/configuration/configuracion_mseed.json.template) (`"MUESTREO(20)": 250`, 250 Hz nominales)

### 3.2. Traza de Interrupción en Producción (`CHA02`)
```text
rsa@CHA02:~/data/registro-continuo $ procesarpendientes --convert-only --all
[FASE 1] Conversión Local de Archivos Binarios a miniSEED:
  Archivo(s) activo(s) en grabación protegidos: CHA2_261021-210000.dat
  [CONVIRTIENDO] CHA2_261001-000000.dat ──> CHA2_20261001_000000.mseed ...^CTraceback (most recent call last):
  File "/home/rsa/projects/acelerografo/scripts/drive/procesar_y_subir_pendientes.py", line 828, in <module>
    main()
  File "/home/rsa/projects/acelerografo/scripts/drive/procesar_y_subir_pendientes.py", line 740, in main
    res_conv = convertir_archivos_pendientes(
  File "/home/rsa/projects/acelerografo/scripts/drive/procesar_y_subir_pendientes.py", line 298, in convertir_archivos_pendientes
    exito, mensaje, info = procesar_archivo_individual(
  File "/home/rsa/projects/acelerografo/scripts/mseed/binary_to_mseed.py", line 497, in procesar_archivo_individual
    conversion_mseed_digital(nombre_archivo_mseed, path_archivo_salida, tiempo_binario, datos_archivo_binario, segundos_faltantes, config_mseed, logger)
  File "/home/rsa/projects/acelerografo/scripts/mseed/binary_to_mseed.py", line 391, in conversion_mseed_digital
    trazaCH1 = obtenerTraza(nombre, 1, datos_archivo_binario[0], tiempo_binario, segundos_faltantes, parametros_mseed)
  File "/home/rsa/projects/acelerografo/scripts/mseed/binary_to_mseed.py", line 457, in obtenerTraza
    data_completo = np.insert(data_completo, indice_muestra_faltante, lista_ceros)
  File "/home/rsa/projects/acelerografo/.venv/lib/python3.9/site-packages/numpy/lib/function_base.py", line 4755, in insert
    return _insert(arr, mask, values, axis)
KeyboardInterrupt
```

### 3.3. Inspección del Segmento Defectuoso en `obtenerTraza`
```python
# binary_to_mseed.py: Líneas 445-458
if segundos_faltantes is not None:
    segundo_inicio = (horas * 3600) + (minutos * 60) + segundos
    muestras_por_segundo = fsample
    lista_ceros = np.zeros(muestras_por_segundo, dtype=np.int32)
    npts_completo = len(data) + int(len(segundos_faltantes) * muestras_por_segundo)
    data_completo = np.zeros(npts_completo, dtype=np.int32)
    data_completo[:len(data)] = data
    stats['npts'] = npts_completo

    for segundo_faltante in segundos_faltantes:
        tiempo_muestra_faltante = int(segundo_faltante - segundo_inicio)
        indice_muestra_faltante = tiempo_muestra_faltante * muestras_por_segundo
        data_completo = np.insert(data_completo, indice_muestra_faltante, lista_ceros)

    traza = Trace(data=data_completo, header=stats)
```

---

## 4. Hallazgos y Causa Raíz

### Hallazgo 1: Livelock por Reasignación Cuadrática en Memoria RAM ($O(M \cdot N)$)
* **Descripción técnica**: La función `np.insert(arr, obj, values)` **no modifica el arreglo en sitio**. Aloca un búfer C completamente nuevo en el heap de tamaño `len(arr) + len(values)`, ejecuta dos operaciones `memcpy()` para los segmentos anterior y posterior al índice de inserción, copia los nuevos valores y libera el búfer anterior.
* **Modelado matemático del colapso en Raspberry Pi**:
  - Sea $N_{\text{datos}}$ el número de muestras continuas presentes (ej. 1,100 segundos grabados a 250 Hz = $275,000$ muestras).
  - Sea $M$ el número de segundos faltantes (ej. 2,500 segundos ausentes en una hora con desconexiones intermitentes).
  - En cada iteración $k \in [1, M]$, el tamaño del arreglo es aproximadamente:
    $$S_k = N_{\text{datos}} + k \cdot f_{\text{sample}} + M \cdot f_{\text{sample}}$$
    Con un promedio de $\sim 700,000$ enteros de 32 bits ($2.8 \text{ MB}$) por arreglo.
  - Para 3 canales independientes (`trazaCH1`, `trazaCH2`, `trazaCH3`), el volumen total de memoria copiada en el bucle es:
    $$\text{Memoria Copiada} = 3 \times \sum_{k=1}^{M} S_k \cdot 4 \text{ bytes} \approx 3 \times M \times \bar{S} \times 4 \text{ bytes} \approx 3 \times 2500 \times 2.8 \text{ MB} \approx \mathbf{21.0 \text{ GB a } 37.5 \text{ GB}}$$
* **Impacto en Hardware ARM**:
  - La Raspberry Pi cuenta con un bus de memoria LPDDR2/LPDDR4 compartido entre CPU y GPU con ancho de banda efectivo muy reducido ante alocaciones fragmentadas.
  - El tamaño de los arreglos ($> 2.5 \text{ MB}$) desborda con creces la memoria caché L2 del SoC ARM BCM2837/BCM2711 (512 KB a 1 MB). Cada inserción provoca *cache-thrashing* total y viajes obligatorios a DRAM.
  - En un solo hilo, copiar $> 20 \text{ GB}$ en bloques de 2.8 MB en una Pi 3B toma entre **15 y 35 minutos de uso continuo de CPU al 100% por archivo**, lo que aparenta un congelamiento permanente (*livelock*).

---

### Hallazgo 2: Corrupción Severa de Fase y Fragmentación Espuria de la Señal Sísmica
* **Descripción técnica**: El algoritmo actual asume falsamente que el arreglo sobre el que opera `np.insert` ya se encuentra alineado temporalmente.
* **Mecanismo de la corrupción**:
  1. En la línea 451 se ejecuta:
     ```python
     data_completo[:len(data)] = data
     ```
     En este punto, **todas las muestras válidas se encuentran compactadas consecutivamente al inicio** (índices `0` a `len(data)-1`), sin separación temporal entre paquetes.
  2. Luego, en el bucle:
     ```python
     tiempo_muestra_faltante = int(segundo_faltante - segundo_inicio)
     indice_muestra_faltante = tiempo_muestra_faltante * muestras_por_segundo
     data_completo = np.insert(data_completo, indice_muestra_faltante, lista_ceros)
     ```
     `indice_muestra_faltante` calcula un offset absoluto desde el inicio de la hora. **Pero en `data_completo`, la posición calculada NO corresponde a un vacío temporal**, sino que apunta directamente a datos reales válidos que estaban compactados allí.
  3. Al ejecutar `np.insert()`, la señal real se **rebana arbitrariamente**, empujando la mitad de un evento sismológico hacia la derecha e intercalando 250 ceros en medio de la onda.
  4. Peor aún: cada inserción previa $k-1$ expande el arreglo hacia la derecha en 250 muestras. Cuando se procesa el siguiente segundo faltante, el índice absoluto cae aún más desfasado respecto a los datos reales ya desplazados.
* **Impacto sismológico**: La señal resultante no representa la realidad física. Las fases sísmicas (ondas P y S) quedan despedazadas, desfasadas y mezcladas con ceros intermitentes en marcas de tiempo erróneas.

---

### Hallazgo 3: Hipertrofia Dimensional y Metadatos Incoherentes en ObsPy
* **Descripción técnica**: Se produce una doble suma de muestras en el tamaño final del arreglo.
  - En la línea 450 se dimensiona:
    $$\text{npts\_completo} = \text{len}(data) + M \cdot f_{\text{sample}}$$
    Y se inicializa con ceros (`np.zeros(npts_completo)`).
  - En la línea 451 se asignan los datos válidos al principio, dejando ya los $M \cdot f_{\text{sample}}$ ceros restantes al final del arreglo.
  - Posteriormente, el bucle ejecuta $M$ llamadas a `np.insert(..., lista_ceros)`, insertando **otros** $M \cdot f_{\text{sample}}$ ceros en el cuerpo del arreglo.
  - **Resultado**: El arreglo final tiene una longitud de:
    $$\text{Longitud Real} = \text{len}(data) + 2 \cdot M \cdot f_{\text{sample}} > \text{npts\_completo}$$
  - En la línea 452 se fijó `stats['npts'] = npts_completo`. Al instanciar `Trace(data=data_completo, header=stats)`, ObsPy sobreescribe `trace.stats.npts` con `len(data_completo)`.
  - Como `trace.stats.endtime` se calcula como `starttime + (npts - 1) / fsample`, el archivo miniSEED resultante registra una duración artificialmente extendida (ejemplo: un archivo horario con 1,800 s faltantes terminaría registrando una duración de **1 hora y 30 minutos** en lugar de 1 hora exacta).

---

### Hallazgo 4: Fragilidad y Casos de Borde en `leer_archivo_binario`
Al analizar [`leer_archivo_binario`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/mseed/binary_to_mseed.py#L99-L187), se identifican tres anomalías no contempladas:

1. **Cruce de Medianoche ($23:59:59 \rightarrow 00:00:00$)**:
   - `tiempos.append(h * 3600 + m * 60 + s)`.
   - Si un archivo binario contiene tramas antes y después de las 00:00:00 UTC (ej. grabación retrasada o archivo que cruza medianoche):
     - `tiempos_np[k] = 86399`, `tiempos_np[k+1] = 0`.
     - `dif_segundos = 0 - 86399 = -86399`.
     - `dif_segundos > 1` evalúa a `False`, omitiendo la detección de gaps.
     - En `obtenerTraza`, `segundo_faltante - segundo_inicio` produce índices negativos que corrompen el indexado en NumPy.
2. **Gaps en Cabecera y Cola (Head / Tail Gaps)**:
   - `np.diff(tiempos_np)` solo analiza las diferencias entre tramas efectivamente grabadas en el archivo.
   - Si la estación se reinició y el archivo comenzó a grabar a las `00:05:00` en lugar de las `00:00:00`, no se detectan los primeros 300 segundos faltantes. La traza inicia en `00:05:00`, lo cual es válido como traza corta, pero inconsistente si los procesos posteriores asumen archivos estrictamente horarios.
3. **Desincronizaciones de Reloj (GPS lock / RTC jitter)**:
   - Si una sincronización de GPS corrige el reloj hacia atrás o produce tramas duplicadas con el mismo segundo, `dif_segundos <= 0`.
   - El código actual no deduplica tramas con el mismo timestamp de segundo ni maneja timestamps monótonamente no crecientes.

---

### Hallazgo 5: Carencia de Suite de Pruebas Unitaria en el Subsistema MiniSEED
* A diferencia de los subsistemas `drive` ([`test_procesar_y_subir_pendientes.py`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/drive/test_procesar_y_subir_pendientes.py)) y `export` ([`test_empaquetar_archivos.py`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/export/test_empaquetar_archivos.py)), el conversor central [`binary_to_mseed.py`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/mseed/binary_to_mseed.py) no posee pruebas automatizadas.
* Este vacío permitió que una implementación deficiente de inserción de gaps pasara a producción sin ser detectada en DEV00.

---

### Hallazgo 6: Desacople entre `extraer_tiempo_binario` y `leer_archivo_binario` ante Primera Trama Inválida
*Identificado en auditoría independiente (2026-10-23)*

* **Descripción técnica**: La función [`extraer_tiempo_binario`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/mseed/binary_to_mseed.py#L190-L242) lee la **primera trama** del archivo binario (bytes 2503-2505) y extrae hora, minuto y segundo **sin aplicar ninguna validación de rango**. En contraste, [`leer_archivo_binario`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/mseed/binary_to_mseed.py#L99-L187) aplica una máscara estricta `(horas <= 23) & (minutos <= 59) & (segundos <= 59)` (Línea 123) y **descarta** las tramas inválidas de la lista de datos y tiempos.
* **Mecanismo de la inconsistencia**:
  1. Si la primera trama del archivo binario contiene bytes de hora corruptos (ej. `h=255, m=255, s=255` por un glitch de SPI), `extraer_tiempo_binario` genera `n_segundo = 255*3600 + 255*60 + 255 = 934,455` y asigna `hora=255`, `minuto=255`, `segundo=255` al diccionario `tiempo_binario`.
  2. `leer_archivo_binario` descarta esa misma trama (la máscara la filtra), y su `tiempos_np[0]` corresponde a la **segunda** trama válida del archivo.
  3. En [`obtenerTraza`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/mseed/binary_to_mseed.py#L404-L464), `segundo_inicio` se calcula desde `tiempo_binario` (la primera trama no validada, Línea 446), mientras que `segundos_faltantes` proviene de `tiempos_np` (que excluyó la trama inválida). El desfase entre ambas referencias produce cálculos de `indice_muestra_faltante` incorrectos.
  4. Adicionalmente, `UTCDateTime(anio, mes, dia, 255, 255, 255, ...)` en la Línea 441 lanzará una excepción no controlada de ObsPy, abortando la conversión de forma abrupta sin mensaje de diagnóstico claro.
* **Impacto**: Este escenario es poco frecuente pero no improbable en condiciones de interferencia electromagnética o reinicio brusco del dsPIC. Cuando ocurre, el resultado es una excepción opaca o, si la trama es inválida pero aún dentro de rango (ej. `h=0, m=0, s=0` cuando la grabación inició a las 14:00), un desfase silencioso de todos los índices de gaps.

---

## 5. Evaluación de Riesgo

| # | Escenario de Riesgo | Probabilidad | Impacto | Mitigación Requerida |
|---|---|---|---|---|
| **R1** | **Saturación de almacenamiento y caída de adquisición SPI**: El livelock en `binary_to_mseed.py` impide que `procesarpendientes` complete su ciclo. Los binarios `.dat` no se purgan (`--purge-dat`), colmando la MicroSD y provocando la caída del servicio C en tiempo real (`registro_continuo_4.5.0.c`). | Alta | Crítico | Refactorizar el algoritmo a complejidad $O(N)$ garantizando conversión en $< 2$ s. |
| **R2** | **Degradación y estrés térmico del hardware en campo**: CPU al 100% durante horas en estaciones remotas sin ventilación activa eleva la temperatura del SoC ARM sobre los 75°C, activando *thermal throttling*. | Alta | Alto | Eliminar bucles de copia en RAM; liberar CPU inmediatamente tras la conversión. |
| **R3** | **Contaminación de la red sismológica institucional**: Si un archivo con gaps se llega a convertir con el código actual, la forma de onda queda fragmentada y desfasada. Ingestar estos MiniSEED distorsiona los cálculos de aceleración pico (PGA) y detección de sismos. | Media | Crítico | Reemplazar `np.insert` por mapeo directo indexado exacto. |
| **R4** | **Fallo por memoria agotada (OOM Killer)**: En archivos con pérdida masiva de datos (ej. más de 3,000 s faltantes), la inflación dimensional al doble de ceros combinada con buffers temporales de `np.insert` puede superar la RAM libre y ser cancelado por OOM. | Media | Alto | Pre-alocación fija y única de dimensiones exactas. |
| **R5** | **Excepción opaca o desfase silencioso por primera trama corrupta**: Si la primera trama del binario tiene bytes de hora inválidos, `extraer_tiempo_binario` la usa sin validar mientras `leer_archivo_binario` la descarta. Esto produce un `UTCDateTime` inválido (crash) o un desacople del `segundo_inicio` de referencia (corrupción silenciosa de índices). | Baja | Alto | Unificar la fuente de timestamp de referencia con la primera trama válida filtrada por `leer_archivo_binario`. |

---

## 6. Opciones y Decisiones de Arquitectura

Para resolver la conversión ante la presencia de gaps, se evalúan dos enfoques arquitectónicos:

```mermaid
graph TD
    A["Archivo Binario .dat con Gaps"] --> B{"Estrategia de Conversión"}
    B -->|Opción A: Vectorización O(N)| C["Arreglo Continuo con Relleno de Ceros Exacto"]
    C --> D["1 Traza MiniSEED por Canal (3600s / npts exacto)"]
    D --> E["100% Compatible con Scripts Actuales de RSA"]
    
    B -->|Opción B: Estándar ObsPy/FDSN| F["División en Trazas Segmentadas (Sin Ceros)"]
    F --> G["Stream con N Trazas independientes por canal"]
    G --> H["MiniSEED Estándar con Encabezados de Gap"]
```

---

### Decisión 1: Estrategia de Representación de Gaps en MiniSEED

#### Opción A: Mapeo Vectorizado Directo por Ranuras Temporales ($O(N)$ Slicing) con Relleno de Ceros
Consiste en mantener una traza única continua por canal de duración fija o acotada, pero eliminando totalmente `np.insert()`. Se pre-aloca un arreglo de ceros del tamaño exacto y se copian los bloques de muestras válidas en sus posiciones cronológicas calculadas directamente:
* **Algoritmo**:
  1. Identificar el rango temporal total: `inicio_seg` a `fin_seg`.
  2. Alocar arreglo único de ceros: `data_completo = np.zeros(total_muestras, dtype=np.int32)`.
  3. Para cada trama válida $i$ con timestamp de segundo $t_i$:
     $$\text{idx\_inicio} = (t_i - t_{\text{inicio}}) \times f_{\text{sample}}$$
     $$\text{idx\_fin} = \text{idx\_inicio} + f_{\text{sample}}$$
     $$\text{data\_completo}[\text{idx\_inicio}:\text{idx\_fin}] = \text{muestras\_trama}_i$$
* **Ventajas**:
  - Complejidad estrictamente lineal **$O(N)$**. Tiempo de ejecución: **$< 0.15$ segundos** para un archivo completo de 1 hora, independientemente de la cantidad de gaps.
  - Cero reasignaciones de memoria; uso estricto y predecible de RAM ($\sim 3.6 \text{ MB}$ por traza).
  - Preserva 100% de compatibilidad con las herramientas y flujos actuales de la RSA que esperan 1 traza por canal en cada archivo horario.
  - Conserva la fase sismológica intacta: los ceros se ubican exactamente en los segundos donde no hubo datos, sin desplazar las señales reales.
* **Desventajas**:
  - Los escalones bruscos hacia cero pueden introducir discontinuidades espectrales artificiales si se aplican filtros FFT sin tapers.

---

#### Opción B: Segmentación Canónica en Múltiples Trazas (`obspy.Stream`)
Consiste en apegarse al estándar sismológico internacional (FDSN / SEED). Si existen gaps, no se inventan ceros artificiales. Se crean múltiples objetos `Trace` dentro del mismo `Stream` para cada bloque continuo de datos, cada uno con su propio `stats.starttime` y `stats.npts`:
* **Ventajas**:
  - Pureza sismológica absoluta: no introduce señales espurias de aceleración cero.
  - El formato MiniSEED está diseñado nativamente para almacenar trazas discontinuas en bloques de 512 bytes.
  - Herramientas profesionales (SeisComP, Earthworm, SAC) leen y gestionan gaps nativamente.
* **Desventajas**:
  - Podría romper scripts internos o herramientas de visualización existentes de la RSA que asuman que cada `.mseed` contiene estrictamente 3 trazas (CH1, CH2, CH3) con longitud fija.

---

### Recomendación Arquitectónica

> **Recomendación**: **Implementar la Opción A (Mapeo Vectorizado Directo $O(N)$) de manera inmediata**.
> 
> *Justificación*: Resuelve de raíz y con máxima urgencia el colapso de CPU en `CHA02` y garantiza que el pipeline de producción no sufra incompatibilidades de formato, corrigiendo al mismo tiempo la corrupción de fase y la inflación dimensional de muestras. Opcionalmente, se puede contemplar un flag futuro (`--split-gaps`) para habilitar la Opción B cuando la infraestructura de análisis esté adaptada.

---

## 7. Mitigaciones Aplicadas

1. **Aislamiento en Cuarentena de Archivos Afectados (`CHA02`)**:
   - Se trasladaron los 24 archivos `.dat` del día `2026-10-01` a un directorio independiente (`cuarentena/`) fuera de la ruta de escaneo de `procesarpendientes`.
   - Esto permitió desbloquear el gestor de contingencia, completando con éxito la conversión y extracción de más de 450 archivos `.dat` correspondientes a los días subsiguientes (2026-10-02 al 2026-10-21).

---

## 8. Backlog de Mejoras

1. **[Refactorización Crítica] Refactorizar `obtenerTraza` con Mapeo Directo por Rebanadas ($O(N)$)**:
   - Reemplazar completamente la lógica de `np.insert` por la asignación directa sobre arreglo pre-dimensionado de ceros.
   - Asegurar que `stats['npts']` coincida exactamente con la longitud del arreglo generado.
2. **[Robustecimiento Temporal] Normalizar Timestamps y Cruce de Medianoche en `leer_archivo_binario`**:
   - Ajustar el cálculo de diferencias temporales para manejar la transición `86399` $\rightarrow$ `0` mediante aritmética modular o timestamps continuos `datetime`.
   - Filtrar o resolver tramas con timestamps duplicados provenientes de saltos de GPS.
3. **[Calidad y Testing] Crear Suite de Pruebas Unitaria `test_binary_to_mseed.py`**:
   - Diseñar pruebas unitarias automatizadas con datos sintéticos que verifiquen:
     - Conversión de archivo continuo normal (sin gaps).
     - Conversión con 1 gap corto (5 s) verificando la posición exacta de las muestras.
     - Conversión con gap masivo (1,800 s) verificando tiempo de ejecución $< 0.2$ s y dimensión exacta de salida.
     - Archivo con cruce de medianoche.
4. **[Validación en Cuarentena] Procesar Archivo Real de `CHA02`**:
   - Ejecutar la conversión del archivo de prueba `CHA2_261001-000000.dat` en DEV00 y certificar la validez del MiniSEED resultante.
5. **[Robustecimiento] Unificar Timestamp de Referencia entre `extraer_tiempo_binario` y `leer_archivo_binario`** *(H6)*:
   - Refactorizar `leer_archivo_binario` para que retorne también el timestamp de la primera trama válida.
   - Eliminar la llamada separada a `extraer_tiempo_binario` en `procesar_archivo_individual`, usando en su lugar el timestamp validado.
   - Garantizar que `obtenerTraza` y `conversion_mseed_digital` reciban siempre un `tiempo_binario` coherente con los datos filtrados.

---

## 9. Dependencias y Prerrequisitos

| Prerrequisito | Estado | Acción Requerida |
|---|---|---|
| Entorno virtual `.venv` con ObsPy y NumPy | ✅ Disponible en DEV00 | Utilizar `$PROJECT_LOCAL_ROOT/.venv/bin/python3` para ejecutar pruebas. |
| Acceso a archivo con gaps reales (`CHA2_261001-000000.dat`) | ⚠️ Localizado en estación remota | Extraer una copia a DEV00 o simularlo sintéticamente para validación. |
| Blueprint de implementación estructurado | ⬜ Pendiente | Ejecutar skill `planning_guide` para estructurar la refactorización paso a paso. |

---

## 10. Plan de Validación Futuro

| # | Checkpoint | Criterio de Éxito |
|---|---|---|
| **CP-1** | **Test unitario de rendimiento con gap sintético masivo** | Crear un test con 1,800 s faltantes y verificar que `obtenerTraza()` complete en $< 0.15$ segundos en Python local. |
| **CP-2** | **Test unitario de integridad de fase y dimensiones** | Verificar que `len(traza.data) == stats['npts'] == 900,000` (para 1 hora a 250 Hz) y que los valores de aceleración reales se ubiquen en los índices de segundo correspondientes sin desfases. |
| **CP-3** | **Conversión del archivo real en cuarentena** | Ejecutar `binary_to_mseed.py 3 CHA2_261001-000000.dat` y verificar generación exitosa de `CHA2_20261001_000000.mseed` en $< 5$ segundos. |
| **CP-4** | **Inspección de traza MiniSEED con ObsPy** | Leer el archivo generado con `obspy.read()` y certificar que `st[0].stats.endtime` concuerde exactamente con la última trama esperada. |
