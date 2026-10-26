# Resumen de Sesión: Implementación de Fase 6 en binary_to_mseed, Validación Masiva CHA02, Diagnóstico de Truncamiento y ADR-026

**Fecha**: 2026-10-26  
**Repositorio**: `RSA-Acelerografo` (`montajes/acelerografo-DEV00`)  
**Agente de IA**: Antigravity  
**Usuario**: Milton  

---

## 🎯 Objetivo de la Sesión

Implementar y certificar la **Fase 6** del blueprint de refactorización de `binary_to_mseed.py` para blindar la conversión ante ruido de bus SPI (glitches con bytes de tiempo repetidos `04:04:04` y outliers temporales aislados) y acotar estrictamente los cruces de medianoche a la hora 23 $\to$ 00. Validar la suite sintética automatizada con el nuevo test T11 (12 tests pasando en $< 0.1$ s) y procesar en lote los 25 archivos binarios reales de la estación `CHA02` en `/home/rsa/data/test-bin`. Analizar el caso anómalo de corte de energía en `CHA2_261001-150002.dat`, formalizar el nuevo diagnóstico técnico forense, documentar el ADR-026 de dispersión vectorizada y actualizar los contextos técnicos e índices federados del exocortex RSA.

---

## 📂 Estructura del Repositorio Implementada

```text
RSA-Acelerografo/
├── docs/
│   ├── analysis/
│   │   ├── 2026-10-22_diagnostico_bloqueo_cpu_relleno_gaps_binary_to_mseed.md                     # [RESUELTO] Diagnóstico de livelock original
│   │   └── 2026-10-26_diagnostico_desacople_timestamp_archivos_incompletos_binary_to_mseed.md     # [NUEVO] Diagnóstico de truncamiento y fecha huérfana
│   ├── blueprints/
│   │   └── 2026-10-23_plan_refactorizacion_gaps_binary_to_mseed.md                               # [ACTUALIZADO] Fases 1 a 6 completadas (100%)
│   ├── context/
│   │   ├── binary_to_mseed_context.md                                                            # [ACTUALIZADO] LOC 1044, O(N), flags CLI, purga SPI
│   │   └── test_binary_to_mseed_context.md                                                       # [NUEVO] Contexto de suite unitaria (T1 a T11)
│   └── progress/
│       ├── 2026-10-22_contexto-agente.md                                                         # Sesión previa
│       └── 2026-10-26_contexto-agente.md                                                         # [NUEVO] Esta transición técnica
└── scripts/
    └── operation/
        └── mseed/
            ├── binary_to_mseed.py                                                                # [ACTUALIZADO] Fases 1 a 6, dispersión O(N), --output-dir
            └── test_binary_to_mseed.py                                                           # [ACTUALIZADO] Suite con 12 tests (T1 a T11)

Exocortex Metodologías (RSA-Metodologias/):
├── decisiones/
│   ├── 026_dispersion_vectorizada_gaps_y_blindaje_spi_binary_to_mseed.md                         # [NUEVO] ADR-026 formal
│   └── index.md                                                                                  # [ACTUALIZADO] Enlace a ADR-026 activo
└── indice/
    └── indice_tematico.md                                                                        # [ACTUALIZADO] Registros de ADR-026, contextos y diagnósticos
```

---

## ⚙️ Configuración del Entorno Virtual (`.venv`)

* **Entorno**: Raspberry Pi 3B+ (`DEV00`), Debian Bullseye, Python 3.9.2 (`/home/rsa/projects/acelerografo/.venv`).
* **Sincronización de dos niveles**:
  Los cambios en el repositorio montado vía SSHFS (`/home/rsa/git/RSA-Acelerografo/scripts/operation/mseed/`) se sincronizaron al entorno operativo desplegado (`/home/rsa/projects/acelerografo/scripts/mseed/`) mediante:
  ```bash
  cp -v /home/rsa/git/RSA-Acelerografo/scripts/operation/mseed/*.py scripts/mseed/
  ```
* **Certificación Unitaria**:
  ```bash
  .venv/bin/python3 scripts/mseed/test_binary_to_mseed.py
  # Ran 12 tests in 0.096s — OK (T1 a T11 exitosos)
  ```
* **Certificación con ObsPy**:
  Inspección sismológica de cabeceras STEIM1 y sincronía de los 3 canales triaxiales (`ENX`, `ENY`, `ENZ`) a 250 Hz.

---

## 🛠️ Modificaciones de Código y Refactorización

### 1. Implementación de la Fase 6 en `binary_to_mseed.py`
* **Descarte de Glitches SPI con Bytes Idénticos**: En el desempaquetado de cabeceras crudas, se detecta y purga inmediatamente cualquier trama donde `año == mes == día == hora == minuto == segundo` (`0x04 0x04 0x04 0x04 0x04 0x04`). Se restringe el año a `20 <= anio <= 60` (o `0` para tramas de prueba).
* **Filtro de Coherencia Cronológica**: Pasada vectorial que identifica y purga tramas intermedias outlier ($|t_k - t_{k-1}| > 60$ s) que retornan de inmediato a la secuencia continua circundante ($|t_{k+1} - t_{k-1}| \le 60$ s).
* **Acotamiento Contextual de Medianoche**: La suma modular $+86400$ s para tramas con salto negativo solo se habilita si $t_{\text{ant}} \ge 86000$ (hora 23) y $t_{\text{sig}} \le 3600$ (hora 00), evitando falsos cruces de día diurnos.
* **Flag CLI `--output-dir` / `-o`**: Permite especificar un directorio de salida independiente para aislar pruebas sin colisionar con permisos de producción en `/home/rsa/data/mseed/`.

### 2. Suite de Pruebas Unitarias en `test_binary_to_mseed.py`
* Incorporación de **T11** (`test_purga_glitch_spi_y_outlier_temporal_t11`): Inyecta sintéticamente una trama con bytes repetidos `0x04` y un outlier temporal aislado (`04:04:04` a las 17h) dentro de una secuencia continua de 6 segundos. Verifica que ambas sean purgadas, que no se generen gaps artificiales de 24 horas y que la traza resultante tenga una duración nominal exacta de 6 segundos ($1500$ muestras).

### 3. Resultados de la Validación Masiva en Lote con CHA02
* Se procesaron los 25 archivos reales de CHA02 en `/home/rsa/data/test-bin` (`CHA2_260928-170000.dat` y los 24 archivos de `CHA2_261001-*.dat`):
  * **24 de 25 archivos convirtieron con éxito**: Tiempo promedio de procesamiento de **~0.95 s** por hora nominal en RPi 3B+, con duración sismológica exacta de **3599.996 s (900,000 muestras)**.
  * El archivo patológico que originó el problema (`CHA2_260928-170000.dat`) se redujo de **81 MB (25 horas)** a **8.6 MB (1.00 hora nominal)**.
  * Archivos interrumpidos y reinicios (`010001.dat`, `020202.dat`, `070000.dat`, `160648.dat`, `160752.dat`) calcularon su duración real exacta sin inflaciones.

### 4. Diagnóstico de Truncamiento por Apagón en `CHA2_261001-150002.dat`
* **Error**: `Error inesperado al procesar CHA2_261001-150002.dat: 'anio'`.
* **Causa Raíz**: Durante la hora 15 la estación sufrió un corte de energía (no hubo registros hasta las 16:06:48). La última trama en disco quedó cortada a mitad de bytes. La función `extraer_timestamp_completo_binario` falló con excepción en `datetime` al hacer `f.seek(-2506, 2)` y retornó `(None, None)`.
* **Fallo en cascada**: Al ser `ts_inicio is None`, `leer_archivo_binario` omitió inyectar las claves `'anio'`, `'mes'` y `'dia'` en `tiempo_binario_validado` a pesar de tener miles de tramas válidas con fecha completa en memoria. Al propagarse a `obtenerTraza`, se produjo un `KeyError: 'anio'`.
* **Principio Sismológico Acordado**: Es mejor no tener datos a tener datos falsos o con tiempo aproximado. La fecha debe extraerse de forma estricta y autónoma de la primera trama válida comprobada (`chunk_valido[0]`), sin depender de la cola del archivo ni inventar tiempos.

---

## 📋 Pasos Sugeridos para el Siguiente Agente

1. **Planificar la Solución al Diagnóstico `2026-10-26`**:
   Ejecutar el skill `planning_guide` para estructurar la resolución del desacople de fecha y truncamiento de cola en `binary_to_mseed.py`.
2. **Autonomía de Fecha en `leer_archivo_binario`**:
   Construir `tiempo_binario_validado` extrayendo `anio = chunk_valido[0, 2500] + 2000`, `mes = chunk_valido[0, 2501]` y `dia = chunk_valido[0, 2502]` de la primera trama válida en memoria, desacoplándolo definitivamente de `ts_inicio`.
3. **Aislamiento en `extraer_timestamp_completo_binario`**:
   Separar la obtención de `ts_inicio` y `ts_final` en bloques `try...except` independientes para que una trama final truncada no anule los metadatos de inicio del archivo.
4. **Implementar Test Unitario T12**:
   Crear `test_archivo_truncado_en_cola_conserva_fecha_t12` en `test_binary_to_mseed.py` para asegurar que archivos con bytes finales incompletos conviertan sin `KeyError`.
5. **Certificación Final del Lote Completo (25/25)**:
   Sincronizar cambios a DEV00 (`cp -v ... scripts/mseed/`), reprocesar `CHA2_261001-150002.dat` y verificar que el 100% de los 25 archivos de CHA02 generen su MiniSEED correspondiente.
