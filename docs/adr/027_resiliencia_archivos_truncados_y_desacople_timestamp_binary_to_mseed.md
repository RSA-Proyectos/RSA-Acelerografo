---
id: ADR-027
titulo: Resiliencia ante Archivos Truncados por Corte de Energía y Desacople Autónomo de Timestamp en binary_to_mseed
estado: Aceptado
fecha: 2026-10-27
temas: [binary_to_mseed, desacople_timestamp, archivos_truncados, corrupcion_trama, apagones, resiliencia, obspy, numpy]
entorno: acelerografo
---

# ADR-027: Resiliencia ante Archivos Truncados por Corte de Energía y Desacople Autónomo de Timestamp en binary_to_mseed

## Estado

**Aceptado** | Fecha: 2026-10-27

## Contexto

Durante la certificación masiva en lote sobre el repositorio de 25 archivos binarios reales procedentes de la estación acelerográfica `CHA02` en `/home/rsa/data/test-bin`, tras la implementación de la dispersión vectorizada $O(N)$ y el blindaje SPI ([ADR-026](026_dispersion_vectorizada_gaps_y_blindaje_spi_binary_to_mseed.md)), 24 archivos convirtieron con tiempos sub-segundo (~0.9 s). Sin embargo, se identificó un fallo en cascada crítico en el archivo `CHA2_261001-150002.dat`:

```text
Procesando /home/rsa/data/test-bin/CHA2_261001-150002.dat...
Error: Error inesperado al procesar CHA2_261001-150002.dat: 'anio'
```

El análisis forense documentado en `RSA-Acelerografo/docs/analysis/2026-10-26_diagnostico_desacople_timestamp_archivos_incompletos_binary_to_mseed.md` demostró que dicho archivo coincidió con un apagón o corte abrupto de suministro eléctrico en la estación sísmica durante la hora 15 (la estación cesó de registrar a las 15:52:58 y reinició a las 16:06:48). El corte de energía interrumpió la escritura física del archivo en disco, dejando una última trama truncada con bytes residuales incompletos ($< 2506$ bytes).

El motor `binary_to_mseed.py` presentaba tres fragilidades de diseño:
1. **Acople Frágil de Cola en `extraer_timestamp_completo_binario`**: Intentaba leer la última trama mediante un salto ciego `f.seek(-2506, 2)`. Al toparse con bytes truncados de cola, detonaba una excepción interna que retornaba `(None, None)` para `(ts_inicio, ts_final)`, anulando el timestamp de inicio válido a pesar de que la cabecera del archivo estaba íntegra.
2. **Dependencia de `ts_inicio` para Inyectar Fecha**: `leer_archivo_binario` solo inyectaba las claves `'anio'`, `'mes'` y `'dia'` en `tiempo_binario_validado` si `ts_inicio is not None`. Al recibir `None`, retornaba un diccionario con solo la hora y minuto de la primera trama, detonando `KeyError: 'anio'` al pasar a `obtenerTraza`.
3. **Rechazo Prematuro por Ruido en Trama Cero**: `procesar_archivo_individual` ejecutaba una lectura cruda de la primera trama en disco con `extraer_tiempo_binario()`; si la primera trama física tenía ruido eléctrico (común al energizar la estación), abortaba todo el archivo antes de que `leer_archivo_binario()` pudiera descartar esa trama y sincronizarse con la trama 1.

Sismológicamente rige el principio: **es mejor no tener datos a tener datos con tiempo aproximado o inventado**. Sin embargo, **descartar un archivo completo con 52 minutos de registro sísmico válido previo a un corte de energía constituye una pérdida inaceptable de información científica**, máxime cuando un terremoto fuerte puede ser precisamente la causa del colapso de la red eléctrica.

---

## Opciones Evaluadas

### Decisión 1: Estrategia de Extracción de Fecha y Hora Canónica
- **Opción A (Seleccionada)**: *Autonomía Total de Fecha y Hora en Memoria*.
  `leer_archivo_binario` construye de forma completamente autónoma el diccionario `tiempo_binario_validado`:
  - Si `usar_fecha_filename=True` ([ADR-007](007_estrategia_dual_fecha_mseed.md)), extrae el año, mes y día mediante expresión regular del nombre canónico del archivo `.dat`.
  - Si `usar_fecha_filename=False`, extrae el año, mes y día de los bytes de cabecera de la primera trama física válida en memoria (`chunk_valido[0, 2500:2503]`).
  - La hora, minuto y segundo siempre se derivan de la primera trama física válida (`chunk_valido[0, 2503:2506]`).
  - *Ventajas*: `tiempo_binario_validado` jamás carece de las llaves de fecha; no depende del retorno de funciones auxiliares ni de la integridad de la cola del archivo.
  - *Desventajas*: Ninguna.
- **Opción B**: Suponer la fecha actual del sistema operativo de la Raspberry Pi si `ts_inicio` es `None`.
  - *Ventajas*: Evita `KeyError`.
  - *Desventajas*: Inaceptable sismológicamente; en estaciones sin RTC o sin sincronización NTP inmediata tras el apagón, fecharía datos históricos con la fecha actual del reloj desfasado.
- **Opción C**: Rechazar cualquier archivo cuyo tamaño no sea múltiplo exacto de 2506 bytes.
  - *Ventajas*: Pureza de archivos.
  - *Desventajas*: Se pierden todos los registros sísmicos previos a cualquier corte de energía.

### Decisión 2: Aislamiento de Bloques en `extraer_timestamp_completo_binario`
- **Opción A (Seleccionada)**: *Aislamiento Estricto de Bloques Inicio/Fin con Bloques `try/except` Independientes*.
  La extracción de `ts_inicio` (al principio del archivo) y `ts_final` (al final del archivo) se desacoplan por completo. Si la lectura de la última trama falla por fin de archivo truncado, `ts_final` se asigna como `None`, pero `ts_inicio` se retorna intacto con su timestamp certificado.
  - *Ventajas*: Máxima resiliencia ante cortes de luz; el conversor procesa todas las tramas completas precedentes y descarta únicamente los bytes residuales incompletos.
  - *Desventajas*: `ts_final` puede ser `None` para archivos truncados (manejado limpiamente por el logger y reportes).
- **Opción B**: Rebobinar byte por byte desde el final del archivo buscando el patrón de cabecera de la última trama.
  - *Ventajas*: Intentaría encontrar la última trama válida.
  - *Desventajas*: Alto costo de E/S en tarjetas SD de Raspberry Pi y riesgo de falsos positivos en datos aleatorios de aceleración.

### Decisión 3: Resolución del Bug de Medianoche en Firmware vs. Conversor
- **Opción A (Seleccionada)**: *Separación de Responsabilidades: Conversor Resiliente + Corrección en Firmware*.
  El bug histórico del firmware MikroC (donde `horaSistema` cicla a 0 a medianoche pero `fechaSistema` permanece congelada en el día de arranque) se mitiga en el conversor tomando la fecha del nombre del archivo (`USAR_FECHA_FILENAME=True`), mientras que la corrección de raíz del contador de fecha se delega formalmente al firmware cuando se migre a MPLABX (para que el GPS/RTC mantenga la verdad cronológica absoluta en hardware).
  - *Ventajas*: Mantiene la compatibilidad con todas las estaciones desplegadas y datos históricos; no introduce parches heurísticos inestables en el conversor Python.
  - *Desventajas*: Requiere que los nombres de los archivos generados por el digitalizador o scripts de adquisición sigan el patrón estándar `CODIGO_AAMMDD-HHMMSS.dat`.

---

## Decisión

Se implementa la **Opción A en las tres decisiones**:

1. **Autonomía Total en `leer_archivo_binario`**: Se garantiza que el sexto elemento retornado (`tiempo_binario_validado`) siempre contenga la estructura completa:
   `{'anio': YYYY, 'mes': MM, 'dia': DD, 'hora': HH, 'minuto': MM, 'segundo': SS, 'n_segundo': 0}`.
2. **Aislamiento en `extraer_timestamp_completo_binario`**: Los bloques de lectura de cabecera y cola se ejecutan de forma independiente; colas truncadas descartan los bytes huérfanos ($< 2506$ B) y retornan `ts_inicio` íntegro.
3. **Delegación de Validación a Memoria en `procesar_archivo_individual`**: Se elimina el aborto prematuro por fallo de lectura de la trama cero física; el archivo se envía a `leer_archivo_binario()`, el cual valida y purga tramas corruptas vectorialmente.
4. **Enrutamiento de Salida en Modo Directorio**: En `binary_to_mseed.py` (L989), se pasa `path_archivo_salida` a `procesar_archivo_individual`, respetando rigurosamente el flag `-o` / `--output-dir` para aislar pruebas sin comprometer los directorios de producción (`/home/rsa/data/mseed/` y `events/`).
5. **Suite de Pruebas Unitarias Ampliada**: Se incorpora el test sintético **T12** (`test_archivo_truncado_por_apagon_conserva_fecha_t12`) en `test_binary_to_mseed.py`, validando que un archivo truncado con bytes residuales conserve la fecha y convierta a MiniSEED sin excepciones.

---

## Consecuencias

### Positivas
- **Recuperación Total de Datos Sísmicos ante Apagones**: En el caso real `CHA2_261001-150002.dat`, se recuperaron exitosamente **52 minutos y 58 segundos** de señal acelerográfica continua previa al corte eléctrico ($794{,}500$ muestras triaxiales), en lugar de perderse el archivo completo.
- **Tasa de Éxito del 100% en Producción y Pruebas**: El lote completo de 30 archivos en `/home/rsa/data/test-bin` (26 de `CHA02` y 4 de `DEV0`) convirtió con estatus `OK` (30/30 exitosos, 0 fallidos).
- **Protección de Directorios Institucionales**: Pruebas con directorios temporales (`-o`) ya no generan colisiones de permisos ni sobreescrituras en `/home/rsa/data/mseed/`.
- **Certificación Automatizada**: La suite de pruebas unitarias cuenta ahora con 13 tests sintéticos que se ejecutan en DEV00 en $0.176$ s con estatus `OK`.

### Negativas / Deuda Técnica
- El firmware MikroC aún mantiene el bug de medianoche para estaciones que permanezcan encendidas por semanas continuas; requiere que `USAR_FECHA_FILENAME=True` permanezca activo en `configuracion_mseed.json` hasta la migración a MPLABX.

---

## Referencias

- Diagnóstico técnico de origen: `RSA-Acelerografo/docs/analysis/2026-10-26_diagnostico_desacople_timestamp_archivos_incompletos_binary_to_mseed.md`
- Blueprint de implementación: `RSA-Acelerografo/docs/blueprints/2026-10-27_plan_resiliencia_archivos_incompletos_y_desacople_timestamp_binary_to_mseed.md`
- ADR predecesor: [ADR-026: Dispersión Vectorizada O(N) para Gaps Temporales, Preservación de Fase y Blindaje de Ruido SPI en binary_to_mseed](026_dispersion_vectorizada_gaps_y_blindaje_spi_binary_to_mseed.md)
- ADR de estrategia de fecha: [ADR-007: Estrategia Dual de Fecha en MiniSEED](007_estrategia_dual_fecha_mseed.md)
- Contexto técnico: `RSA-Acelerografo/docs/context/binary_to_mseed_context.md`
