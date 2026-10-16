---
id: ADR-024
titulo: Blindaje del Pipeline de Subida a Google Drive contra Livelock de Memoria, Concurrencia Descontrolada y Bufferbloat
estado: Aceptado
fecha: 2026-10-16
temas: [drive, memoria, flock, dns, bufferbloat, concurrencia, resiliencia, acelerografo, raspberry-pi]
entorno: acelerografo
---

# ADR-024: Blindaje del Pipeline de Subida a Google Drive contra Livelock de Memoria, Concurrencia Descontrolada y Bufferbloat

## Estado

**Aceptado** | Fecha: 2026-10-16

## Contexto

Las estaciones acelerográficas de la Red Sísmica del Austro operan sobre hardware restringido Raspberry Pi 3B+ (922 MB de RAM, 100 MB de swap) enlazadas mediante enlaces de internet celular o inalámbrico de alta latencia y estabilidad variable. La sincronización horaria de datos con Google Drive se ejecuta a través del script Bash `scripts/task/registrocontinuo.sh` disparado cada hora por crontab (`0 * * * *`), el cual invoca en segundo plano a `gestor_archivos_acq.py`.

A finales de septiembre de 2026, la estación de campo `CHA02` y posteriormente `CHA01` experimentaron un colapso sistémico severo caracterizado por:
1. **Livelock y Bloqueo de Acceso SSH**: CPU en 100% de utilización, swap thrashing violento y bloqueo total de sesiones SSH entrantes.
2. **Multiplicación y Acumulación de Procesos Durmientes**: Hasta 5 a 8 instancias concurrentes de Python consumiendo más de 700 MB de RAM acumulada, dejando menos de 30 MB disponibles para el adquisidor C y servicios críticos.
3. **Duplicación Masiva en Google Drive**: Al restablecerse la conectividad, las múltiples instancias compitiendo sin coordinación subieron miles de archivos duplicados con el mismo nombre y diferentes IDs de Google Drive.

La investigación técnica documentada en [`docs/analysis/2026-09-30_diagnostico_saturacion_memoria_y_bloqueo_subidas_drive.md`](../analysis/2026-09-30_diagnostico_saturacion_memoria_y_bloqueo_subidas_drive.md) identificó 5 causas raíz interconectadas:
- **Ausencia de Exclusión Mutua**: `gestor_archivos_acq.py` carecía de cerrojo a nivel de sistema operativo (`fcntl.flock`). Si una subida demoraba más de 60 minutos, la siguiente tarea de crontab lanzaba otra instancia que operaba simultáneamente sobre el mismo árbol de directorios y registro JSON.
- **Falso Positivo de Red por Ping IP Crudo**: La comprobación de internet (`check_internet_connection`) realizaba un socket TCP a `8.8.8.8:53`. En entornos con routers celulares con saldo agotado, captive portals o fallas en resolvedores DNS, el socket a la IP cruda respondía con éxito, pero la llamada subsiguiente a la API de Google fallaba por `socket.gaierror` tras largos retrasos.
- **Timeout HTTP Excesivo en Transporte**: La sesión HTTP en `subir_archivo.py` utilizaba `Http(timeout=300)`. Tres reintentos ante un socket colgado (*silent drop*) mantenían el proceso congelado durante 15 minutos por archivo.
- **Falta de Acotamiento de Lotes (Unbounded Ingestion)**: El gestor intentaba procesar de forma voraz todos los archivos pendientes en una sola corrida. Ante un backlog acumulado de miles de archivos, la carga de metadatos y payloads SSL agotaba la memoria RAM de la Raspberry Pi.
- **Bufferbloat en Interfaces Celulares**: Subidas continuas y masivas saturaban los buffers del módem celular, disparando la latencia del canal de control a decenas de segundos y desconectando los túneles SSH de mantenimiento.

---

## Opciones Evaluadas

### Opción A: Arquitectura de Blindaje Multicapa Integrado (Flock + DNS Real + Timeout 60s + Batching 20 + Anti-Bufferbloat + GC) [SELECCIONADA]
Reutilizar y adaptar los mecanismos de resiliencia probados en `procesar_y_subir_pendientes.py` directamente dentro del pipeline productivo (`registrocontinuo.sh`, `gestor_archivos_acq.py` y `subir_archivo.py`):
1. **Doble Exclusión Mutua**: Guarda preventiva `is_running` en `registrocontinuo.sh` combinada con cerrojo atómico exclusivo `fcntl.flock` no bloqueante en `/tmp/gestor_archivos_acq.lock` dentro de `gestor_archivos_acq.py`, con liberación garantizada en bloque `finally:`.
2. **DNS Real con Fail-Fast**: `socket.getaddrinfo("www.googleapis.com", 443, timeout=5)`. Si falla, aborta inmediatamente el intento de subida sin tocar credenciales ni colgar sockets.
3. **Timeout de Transporte Acotado**: Reducir `Http(timeout=300)` a `Http(timeout=60)` en `subir_archivo.py`.
4. **Colas de Subida Prioritarias por Lotes**: Orden de subida: Eventos extraídos (`event`, P1) → MiniSEED continuo (`mseed`, P2) → Binarios (`continuous`, P3), con límite configurable `drive.config.batch_size` (default: 20 archivos por invocación).
5. **Mitigación de Bufferbloat**: Pausa determinista de 1.5 s entre archivos (`time.sleep(1.5)`).
6. **Gestión Determinista de Memoria**: Invocación explícita de `gc.collect()` tras cada lote procesado.
7. **Protección Multicapa de Archivos Activos**: `obtener_archivos_activos_continuous()` combinando lectura de `NombreArchivoRegistroContinuo.tmp` y exclusión de archivos con `mtime < 2h`.

- **Ventajas:**
  - Erradica de raíz la concurrencia descontrolada y la acumulación de procesos durmientes.
  - Elimina falsos positivos de conectividad y subidas ciegas.
  - Prioriza la telemetría y datos sismológicos de alta prioridad (eventos) sobre datos continuos brutos.
  - Mantiene la latencia interactiva de SSH utilizable durante la sincronización a Drive.
  - Mantiene la arquitectura y configuración institucional sin requerir sustitución de daemons ni dependencias externas pesadas.
- **Desventajas:**
  - La evacuación de backlogs muy extensos (> 500 archivos) a través de cron horario toma varios días si no se apoya puntualmente con `procesar_y_subir_pendientes.py`.

### Opción B: Sustitución de `gestor_archivos_acq.py` por `procesar_y_subir_pendientes.py` en Crontab
Eliminar `gestor_archivos_acq.py` y configurar `registrocontinuo.sh` para que invoque directamente la herramienta de evacuación manual con parámetros `--batch-size 20`.
- **Ventajas:**
  - Unifica todo el código de subida en un único script.
- **Desventajas:**
  - `procesar_y_subir_pendientes.py` está diseñado para evacuación de contingencia, carece de las políticas completas de retención temporal por días (`retener_dias`), del saneamiento automático de archivos huérfanos en `uploaded_files_registry.json` y de la gestión de espacio en disco en modo offline/emergencia.

### Opción C: Aumento de Memoria Swap y Control Exclusivo por Script Bash
Configurar swap en la Raspberry Pi a 1 GB o 2 GB y delegar el control de concurrencia exclusivamente a `pgrep` en el script wrapper de bash.
- **Ventajas:**
  - Menor número de líneas de código modificadas en Python.
- **Desventajas:**
  - Agrava el swap thrashing destruyendo la tarjeta microSD por sobreescritura continua.
  - `pgrep` es susceptible a truncamiento de cadenas en `/proc/[pid]/cmdline` y condiciones de carrera si dos procesos se lanzan con desfases de milisegundos.
  - No resuelve el bufferbloat ni los falsos positivos de red.

---

## Decisión

Se eligió la **Opción A** debido a que proporciona una arquitectura de defensa en profundidad que ataca sistemáticamente todas las causas raíz identificadas en el diagnóstico sin comprometer la gobernanza de datos sismológicos ni la vida útil del hardware embebido.

### Directrices de Implementación Aprobadas:
1. **Cerrojo Atómico**: `/tmp/gestor_archivos_acq.lock` como único punto de verdad para la ejecución del gestor. Cierre explícito del descriptor de archivo ante contienda de lock (`lock_fd.close()`).
2. **Validación de Red Robusta**: Resolución del FQDN real del endpoint de la API (`www.googleapis.com:443`) con timeout de 5 segundos.
3. **Prioridad Sismológica**: Los eventos extraídos se despachan en la primera posición de la cola de subida antes de procesar el MiniSEED continuo.
4. **Defensa de Recursos Embebidos**: Lotes de máximo 20 archivos por ejecución, pausas de 1.5 s entre transferencias y recolección forzada de basura tras cada lote.
5. **Cobertura de Pruebas Automatizadas**: Creación obligatoria de la suite `test_gestor_archivos_acq.py` para verificar cerrojos, resolución DNS y exclusión de archivos activos sin tocar el hardware en producción.

---

## Consecuencias

### Consecuencias Positivas
- **Estabilidad de Memoria**: La memoria RAM ocupada por el proceso de subida se mantiene acotada (< 80 MB pico), liberando recursos para la adquisición y previniendo el livelock del kernel Linux.
- **Integridad en Google Drive**: Se elimina la creación de archivos duplicados por carreras concurrentes de subida.
- **Resiliencia de Red**: Si la red presenta caídas de DNS o corte de enlace, el gestor realiza fail-fast inmediato (< 5 s), permitiendo que la Raspberry Pi continúe adquiriendo y aplicando el control de espacio en disco.
- **Garantía Operativa de Acceso Remoto**: La cadencia con pausas anti-bufferbloat asegura que las sesiones remotas de SSH y telemetría MQTT no se caigan por saturación de ancho de banda de subida.
- **Preservación del Archivo Activo C**: Detección estricta del binario `.dat` activo mediante el archivo temporal del daemon C y ventana de seguridad de `mtime < 2h`.

### Consecuencias Negativas o Deuda Técnica
- **Drenaje Progresivo de Backlog**: En escenarios donde una estación acumula cientos de archivos tras semanas de desconexión, la política de 20 archivos por hora evacua 480 archivos al día. Para descargas inmediatas masivas en intervenciones de campo, se debe continuar utilizando `procesar_y_subir_pendientes.py --batch-size 50`.

---

## Referencias

- Diagnóstico técnico de origen: [`docs/analysis/2026-09-30_diagnostico_saturacion_memoria_y_bloqueo_subidas_drive.md`](../analysis/2026-09-30_diagnostico_saturacion_memoria_y_bloqueo_subidas_drive.md)
- Blueprint de implementación: [`docs/blueprints/2026-10-14_plan_blindaje_scripts_livelock_memoria.md`](../blueprints/2026-10-14_plan_blindaje_scripts_livelock_memoria.md)
- Contextos técnicos relacionados:
  - [`docs/context/gestor_archivos_acq_context.md`](../context/gestor_archivos_acq_context.md)
  - [`docs/context/subir_archivo_context.md`](../context/subir_archivo_context.md)
  - [`docs/context/registrocontinuo_context.md`](../context/registrocontinuo_context.md)
  - [`docs/context/test_gestor_archivos_acq_context.md`](../context/test_gestor_archivos_acq_context.md)
