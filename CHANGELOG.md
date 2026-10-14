# Registro de Cambios (CHANGELOG) — RSA Acelerógrafo

Todos los cambios notables en el firmware y software del sistema acelerográfico de la **Red Sísmica del Austro (RSA)** se documentan en este archivo.

El formato se basa en [Keep a Changelog](https://keepachangelog.com/es-ES/1.0.0/) y este proyecto se adhiere a [Semantic Versioning (SemVer)](https://semver.org/lang/es/).

---

## [v4.5.4] — 2026-10-14

### Añadido
- **Control de Flujo y Retro-presión en `procesar_y_subir_pendientes.py`**:
  - Comprobación previa de resolución DNS hacia Google Drive (*fail-fast*): cancela el ciclo preventivamente sin generar archivos `.mseed` huérfanos si la red no está operativa.
  - Sincronización 1:1 entre el tamaño de lote (`--batch-size`) y los archivos pendientes en disco: omite conversiones innecesarias si la cola ya tiene suficientes archivos esperando subida.
  - Parámetro `max_convertir` para acotar la conversión local al cupo estricto del lote.
- **Políticas de Liberación de Almacenamiento en Campo**:
  - Bandera `--delete-uploaded`: Elimina el archivo `.mseed` o evento local inmediatamente después de que Google Drive confirma recepción con éxito HTTP 200 y registro atómico.
  - Bandera `--purge-dat`: Fase 3 que elimina archivos binarios `.dat` históricos cuyo `.mseed` equivalente ya consta como subido en `uploaded_files_registry.json`.
  - Salvaguarda incondicional que protege el archivo `.dat` en grabación activa por el dsPIC33 y preserva los binarios aún no respaldados en la nube.
- **Ampliación de Pruebas Unitarias (`test_procesar_y_subir_pendientes.py`)**:
  - Casos de prueba para cupo dinámico de conversión, delegación de borrado post-subida y purga selectiva de binarios respaldados.


---

## [v4.5.3] — 2026-10-13

### Añadido
- **Recuperación y Subida Desacoplada de Archivos Pendientes (`procesar_y_subir_pendientes.py`)**:
  - Procesamiento secuencial en dos fases independientes (conversión local `.dat` a `.mseed` y subida a Google Drive).
  - Protección automática del archivo binario actualmente en grabación SPI para evitar corrupción.
  - Idempotencia total: omisión instantánea de archivos `.dat` que ya cuenten con su archivo `.mseed` generado.
  - Verificación previa de conectividad DNS con socket probe a Google (timeout 5s) antes de iniciar transferencias.
  - Soporte de banderas por CLI (`--dry-run`, `--limit`, `--skip-conversion`, `--skip-upload`).
- **Wrapper Ejecutable de Sistema (`/usr/local/bin/procesarpendientes`)**:
  - Script bash `procesarpendientes.sh` que garantiza la invocación automática dentro del entorno virtual Python (`.venv`).
- **Suite de Pruebas Unitarias (`test_procesar_y_subir_pendientes.py`)**:
  - Cobertura completa de casos de uso (protección de archivo activo, omisión por idempotencia y fallo controlado de DNS).

### Corregido
- **Blindaje de Permisos en `update.sh`**:
  - Aplicación incondicional de `chown -R` y `chmod -R u+rwX` sobre `PROJECT_LOCAL_ROOT` y `log-files/` asignados al usuario operativo (`rsa`), erradicando bloqueos por `PermissionError`.

---

## [v4.5.2] — 2026-09-21

### Añadido
- **Latido Periódico de Estado (`telemetry/state`)**: Emisión de heartbeat cada 300 s con QoS 1 y `retain=True`, preservando el timestamp de inicio de la sesión activa (`last_state_change`) para mitigar desincronizaciones por LWT.
- **Suite de Pruebas de Integración**: 3 nuevos tests unitarios en `test_mqtt_coordinator_integration.py` validando el ciclo de vida del estado de conexión y heartbeat (10/10 tests aprobados).
- Documentación de arquitectura: [ADR-023](docs/adr/023_heartbeat_periodico_telemetria_state_y_resiliencia_lwt.md) y diagnóstico de reconexión MQTT.

### Corregido
- **Condición de Carrera con LWT en Broker**: Priorización de publicación de estado `online` de forma inmediata en `on_connect()` antes de procesar suscripciones de tópicos.
- **Rastreo de Estado de Conexión**: Actualización determinista de flag `is_connected` en `on_connect()` y `on_disconnect()` para evitar publicaciones erróneas con socket caído.

---

## [v4.5.1] — 2026-09-03

### Añadido
- **Espera Dinámica de Sincronización NTP (`wait_for_ntp.sh`)**: Script helper (`/usr/local/bin/wait_for_ntp`) con timeout configurable (120 s) integrado en `ExecStartPre` de Systemd. Arranca inmediatamente al sincronizar (`ntpstat == 0`) y finaliza con `exit 0` no bloqueante ante modo offline.
- Contexto técnico [`docs/context/wait_for_ntp_context.md`](docs/context/wait_for_ntp_context.md).
- Documento de transición técnica [`docs/progress/2026-09-03_contexto-agente.md`](docs/progress/2026-09-03_contexto-agente.md).

### Modificado
- **Plantilla de Servicio Systemd (`rsa-acelerografo.service.template`)**: Incorporación de directivas `After=network.target local-fs.target` y `ExecStartPre=/usr/local/bin/wait_for_ntp 120`.
- **Script de Actualización (`scripts/setup/update.sh`)**: Auto-habilitación incondicional (`systemctl enable rsa-acelerografo.service`) en la función `update_systemd_service`.
- **Rendimiento de Booteo**: Reducción del tiempo muerto tras reinicio a 108 segundos (ahorro de 85 s / 44% más rápido que el viejo cron).
- Actualización de [`docs/adr/018_resiliencia_pipeline_adquisicion_acelerografo.md`](docs/adr/018_resiliencia_pipeline_adquisicion_acelerografo.md) y [`docs/context/registro_continuo_context.md`](docs/context/registro_continuo_context.md).

---

## [v4.5.0] — 2026-09-02

### Añadido
- **Gobernanza por Systemd de Registro Continuo**: Plantilla `rsa-acelerografo.service.template` con auto-reinicio incondicional `Restart=always` (retardo 5 s) y reseteo por hardware del dsPIC33 con `reset_master` en `ExecStartPre`.
- **ADR-018**: Arquitectura de resiliencia y defensa en profundidad en 4 capas para el pipeline de adquisición.
- Blueprint de ejecución [`docs/blueprints/2026-09-02_plan_resiliencia_pipeline_adquisicion.md`](docs/blueprints/2026-09-02_plan_resiliencia_pipeline_adquisicion.md).

### Modificado
- **Refactorización de `registrocontinuo.sh`**: Control de ciclo de vida migrado a `systemctl` (`start`, `stop`, `restart`, `status`).
- **Saneamiento de Crontab (`crontab.txt`)**: Eliminación de tareas `@reboot` asíncronas para evitar condiciones de carrera en el bus SPI.

---

## [v4.4.0] — 2026-09-02

### Añadido
- **Watchdog de Latencia de Adquisición (`acquisition_watchdog.py`)**: Monitor de Ring Buffer con cálculo de antigüedad (`age_seconds`) y emisión periódica cada 60 s en el tópico MQTT `status/acquisition`.
- **Comando MQTT Bajo Demanda**: Soporte del comando `get_acquisition_status` en `CommandDispatcher`.
- Contexto técnico [`docs/context/acquisition_watchdog_context.md`](docs/context/acquisition_watchdog_context.md) y suite de pruebas unitarias (5/5 tests).

### Modificado
- **Reintentos Resilientes en `stream_processor.py`**: Implementación de `_abrir_pipe_con_retry()` con backoff exponencial (0.5 s a 8.0 s, máx 120 s) para desacoplar caídas del proceso de adquisición (20/20 tests aprobados).

---

## [v4.3.0] — 2026-07-15

### Añadido
- **Pipeline de Inferencia GPD en Tiempo Real (`gpd_stream_worker.py`)**: Daemon de inferencia de fases sísmicas P y S utilizando TensorFlow Lite (`models/gpd.tflite`) sobre buffers deslizantes de 8 segundos.
- **Bifurcación de Modos Online/Offline**: Registro CSV mensual thread-safe (`event_logger.py`), alertas MQTT y extracción autónoma de miniSEED en modo offline.
- Daemon de Supervisor `gpd_worker.conf` y automatización en `update.sh`.

### Corregido
- Restricción de dependencia `numpy<2.0.0` para compatibilidad con `tflite-runtime` en Python 3.9 ARM.
- Casting explícito a `float64` en `signal_preprocessor.py` para evitar anomalías en remuestreo ARM.

---

## [v4.2.0] — 2026-06-30

### Añadido
- **Memoria Compartida con Seqlock (`shared_memory_publisher.py`)**: Publicación y lectura lockless de tramas decodificadas en `/dev/shm` a 250 Hz.
- **Preprocesamiento de Señales Sísmicas (`signal_preprocessor.py`)**: Remuestreo poli-fase (250 Hz a 100 Hz), filtrado pasabanda Butterworth (1 a 20 Hz) y normalización robusta de 3 componentes (Z, N, E).

---

## [v4.1.0] — 2026-06-04

### Añadido
- **Panel Web de Configuración Local**: Servidor web Flask y frontend interactivo para configuración en campo (`config_server`).
- **Punto de Acceso Wi-Fi Seguro (`wifiap.sh`)**: Creación dinámica de AP con `hostapd` y aislamiento de red para mantenimiento local.
- **Unificación de Plantillas de Configuración**: Sistema de hidratación automática desde `configuracion_maestra.json`.

---

## [v4.0.0] — 2026-06-16

### Añadido
- **Ring Buffer Persistente en Disco (`ring_buffer_store.py`)**: Almacén rotativo FIFO con retención de 3600 segundos (1 hora) y resolución temporal de microsegundos.
- **Decodificador Binario de Tramas (`frame_decoder.py`)**: Parser validado de paquetes SPI de 2506 bytes del dsPIC33 con verificación de checksum y decodificación de cabeceras.
- **Extractor de Eventos (`event_extractor.py` / `mseed_event_extractor.py`)**: Extracción bajo demanda de ventanas temporales miniSEED ante disparos locales o remotos.

---

## [v3.0.0] — 2026-04-27

### Añadido
- **Coordinador MQTT Centralizado (`mqtt_coordinator.py`)**: Daemon supervisor con reconexión automática, telemetría de salud (`status/system`), estado de hardware, temperatura de CPU y comandos remotos por MQTT.
- Integración con red privada y segura mediante **Tailscale**.
- Estandarización del script interactivo `menu.sh` para administración de la estación.

---

## [v2.0.0] — 2024-11-15

### Añadido
- **Gestión de Procesos con Supervisor**: Aislamiento y control de daemons en espacio de usuario (`rsa`).
- **Subida Resiliente a Google Drive (`subir_pendientes_drive.py`)**: Sincronización automática de archivos miniSEED horarios hacia almacenamiento en la nube institucional.

---

## [v1.0.0] — 2024-10-08

### Añadido
- Versión base inicial del acelerógrafo digital triaxial RSA.
- Adquisición continua por bus SPI mediante binario en C (`registro_continuo`).
- Conversión automática de archivos binarios `.dat` a formato estándar `miniSEED` (`binary_to_mseed`).
- Scripts de despliegue base `deploy.sh` y `update.sh`.
