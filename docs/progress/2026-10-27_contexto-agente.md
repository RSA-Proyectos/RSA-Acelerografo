# Resumen de Sesión: Resiliencia ante Archivos Truncados por Apagón, Desacople Autónomo de Timestamp en binary_to_mseed y Certificación 100% de Lote

**Fecha**: 2026-10-27  
**Repositorio**: `RSA-Acelerografo` (`montajes/acelerografo-DEV00`)  
**Agente de IA**: Antigravity  
**Usuario**: Milton  

---

## 🎯 Objetivo de la Sesión

Resolver la falla en cascada por desacople de timestamp y pérdida de metadatos de fecha en `binary_to_mseed.py` (evidenciada por `KeyError: 'anio'` en el archivo real `CHA2_261001-150002.dat` tras un corte abrupto de energía en la estación sísmica). Diseñar y ejecutar el blueprint formal de 4 fases para dotar al conversor de autonomía total de fecha y hora, desacoplar la lectura de inicio y fin ante colas truncadas con bytes residuales ($< 2506$ bytes), implementar el test sintético unitario T12, certificar el 100% del lote de archivos binarios en `/home/rsa/data/test-bin` (incluyendo archivos de `CHA02` y `DEV0`), y formalizar el ADR-027 junto a los contextos técnicos y el índice temático federado.

---

## 📂 Estructura del Repositorio Implementada

```text
RSA-Acelerografo/
├── docs/
│   ├── adr/
│   │   └── 027_resiliencia_archivos_truncados_y_desacople_timestamp_binary_to_mseed.md             # [NUEVO] ADR-027 en proyecto
│   ├── analysis/
│   │   ├── 2026-10-26_diagnostico_desacople_timestamp_archivos_incompletos_binary_to_mseed.md     # [RESUELTO] Diagnóstico de apagón y colas truncadas
│   │   └── 2026-10-26_diagnostico_vulnerabilidades_firmware_dspic_y_glitches_spi.md               # [REFERENCIA] Diagnóstico de causa física en firmware
│   ├── blueprints/
│   │   └── 2026-10-27_plan_resiliencia_archivos_incompletos_y_desacople_timestamp_binary_to_mseed.md # [NUEVO] Blueprint de 4 fases ejecutado al 100%
│   ├── context/
│   │   ├── binary_to_mseed_context.md                                                             # [ACTUALIZADO] LOC 1086, autonomía fecha/hora, resiliencia apagón
│   │   └── test_binary_to_mseed_context.md                                                        # [ACTUALIZADO] Suite ampliada a 13 tests (T1 a T12)
│   └── progress/
│       ├── 2026-10-26_contexto-agente.md                                                          # Sesión previa
│       └── 2026-10-27_contexto-agente.md                                                          # [NUEVO] Esta transición técnica
└── scripts/
    └── operation/
        └── mseed/
            ├── binary_to_mseed.py                                                                 # [ACTUALIZADO] Fases 1 a 3, aislamiento cola, fix salida -o
            └── test_binary_to_mseed.py                                                            # [ACTUALIZADO] Fase 4: Test T12 apagón y cola truncada

Exocortex Metodologías (RSA-Metodologias/):
├── decisiones/
│   ├── 027_resiliencia_archivos_truncados_y_desacople_timestamp_binary_to_mseed.md                  # [NUEVO] ADR-027 institucional
│   └── index.md                                                                                   # [ACTUALIZADO] Enlace a ADR-027 activo
└── indice/
    └── indice_tematico.md                                                                         # [ACTUALIZADO] Registro ADR-027, contextos técnicos y diagnóstico resuelto
```

---

## ⚙️ Configuración del Entorno Virtual (`.venv`)

* **Entorno**: Raspberry Pi 3B+ (`DEV00`), Debian Bullseye, Python 3.9.2 (`/home/rsa/projects/acelerografo/.venv`).
* **Directorio de Pruebas Aisladas**: `/home/rsa/projects/acelerografo/tmp-files/test_mseed_resiliencia/`.
* **Sincronización SSHFS de dos niveles**:
  Los cambios en el repositorio montado vía SSHFS (`/home/rsa/git/RSA-Acelerografo/scripts/operation/mseed/`) se sincronizaron al entorno operativo desplegado (`/home/rsa/projects/acelerografo/scripts/mseed/`) mediante:
  ```bash
  cp -v /home/rsa/git/RSA-Acelerografo/scripts/operation/mseed/*.py scripts/mseed/
  ```
* **Certificación Unitaria**:
  ```bash
  .venv/bin/python3 scripts/mseed/test_binary_to_mseed.py
  # Ran 13 tests in 0.176s — OK (T1 a T12 exitosos al 100%)
  ```

---

## 🛠️ Modificaciones de Código y Refactorización

### 1. `scripts/operation/mseed/binary_to_mseed.py`
* **Fase 1 (Autonomía total de fecha y hora)**:
  * En `leer_archivo_binario`, se eliminó la dependencia de `ts_inicio is not None` para inyectar la fecha.
  * Si `usar_fecha_filename=True`, se extrae `'anio'`, `'mes'` y `'dia'` del nombre del archivo mediante regex; si es `False`, se extraen de la primera trama física válida en memoria (`chunk_valido[0, 2500:2503]`).
  * La hora, minuto y segundo siempre se extraen de `chunk_valido[0, 2503:2506]`.
  * Se garantiza que el sexto elemento retornado (`tiempo_binario_validado`) siempre contenga las 7 llaves requeridas por `obtenerTraza`.
* **Fase 2 (Aislamiento de extremos ante truncamiento)**:
  * En `extraer_timestamp_completo_binario`, se envolvieron las lecturas de la primera trama y de la última trama en bloques `try/except` independientes.
  * Si la última trama está truncada o incompleta por corte de luz ($< 2506$ bytes), `ts_final` queda como `None`, pero `ts_inicio` se retorna intacto con su timestamp certificado.
* **Fase 3 (Tolerancia a ruido inicial y enrutamiento CLI)**:
  * En `procesar_archivo_individual`, se eliminó el aborto prematuro si `extraer_tiempo_binario()` no puede leer la trama cero física, delegando la validación y purga al motor vectorial en memoria.
  * Añadidos alias cortos `-f` (para `--file`) y `-d` (para `--dir`) en `argparse`.
  * **Corrección de Lote (L989)**: Se reemplazó el argumento hardcodeado `path_archivos_mseed` por `path_archivo_salida` dentro del bucle del modo `--dir`, permitiendo que el flag `-o` aísle salidas en carpetas temporales y proteja los directorios institucionales de producción (`/home/rsa/data/mseed/` y `events/`).

### 2. `scripts/operation/mseed/test_binary_to_mseed.py`
* **Fase 4 (Test Unitario T12)**:
  * Implementado `test_archivo_truncado_por_apagon_conserva_fecha_t12`.
  * Genera un archivo `.dat` sintético con 5 tramas válidas seguidas de una cola truncada residual (1200 bytes arbitrarios).
  * Verifica que `leer_archivo_binario` extraiga año, mes, día y hora sin error, que `ts_inicio` esté intacto, que `obtenerTraza` no detone `KeyError: 'anio'`, y que `procesar_archivo_individual` genere el MiniSEED válido.

### 3. Validación y Certificación en Lote Real (Raspberry Pi 3B+)
* **Caso Patológico de Apagón (`CHA2_261001-150002.dat`)**:
  * Tiempo de conversión: **0.83 s**.
  * Rango de señal recuperada: `15:00:00` $\to$ `15:52:58` ($3177.996$ s, $794{,}500$ muestras).
* **Lote Completo (`/home/rsa/data/test-bin`)**:
  * 30 archivos procesados: 26 de `CHA02` y 4 de `DEV0`.
  * **30 exitosos (100%), 0 fallidos**. Tiempo total: ~24.5 s.

---

## 📋 Pasos Sugeridos para el Siguiente Agente

1. **Monitoreo de Adquisición Continua**:
   * Verificar en los próximos ciclos horarios de DEV00 que el daemon de adquisición continua y los cronjobs ejecuten la conversión con normalidad hacia `/home/rsa/data/mseed/`.
2. **Migración a MPLABX (Firmware dsPIC)**:
   * Cuando se aborde el firmware en C, consultar el diagnóstico `2026-10-26_diagnostico_vulnerabilidades_firmware_dspic_y_glitches_spi.md` para corregir el bug de medianoche de raíz (avance del contador de fecha a medianoche y sincronización por GPS/RTC) e implementar doble buffer (ping-pong) con DMA en el esclavo SPI.
3. **Commit y Sincronización Institucional**:
   * Verificar que los cambios en `RSA-Acelerografo` y `RSA-Metodologias` estén sincronizados con sus remotos en GitHub.
