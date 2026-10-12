# Resumen de Sesión: Diagnóstico Forense Post-Reinicio, Evacuación Controlada de 1,934 Archivos a Drive y Caracterización de Falla DNS Corporativa en CHA02 y CHA01

**Fecha**: 2026-10-12  
**Repositorio**: `acelerografo-DEV00`  
**Agente de IA**: Antigravity  
**Usuario**: Milton Muñoz  

---

## 🎯 Objetivo de la Sesión

Diagnosticar el estado operativo posreinicio físico de la estación acelerográfica `CHA02`, recuperar el enlace de sincronización tras el vaciado accidental de la papelera en Google Drive, desarrollar e implementar un script de evacuación por lotes con exclusión mutua (`evacuacion_controlada_drive.py`) para evacuar un backlog masivo de 1,934 archivos acumulados (~2.65 GB), identificar la causa raíz de la duplicación masiva en Drive y la falla de persistencia en `uploaded_files_registry.json`, diagnosticar el falso positivo de conectividad en `check_internet_connection()` frente a fallas de resolución en el servidor DNS corporativo `192.170.100.250`, verificar la correlación del problema en la estación hermana `CHA01`, actualizar el documento de análisis técnico en `docs/analysis/` y formalizar la hoja de ruta hacia el blueprint de refactorización definitiva.

---

## 📂 Estructura del Repositorio Implementada

```text
montajes/acelerografo-DEV00/
├── docs/
│   ├── analysis/
│   │   └── 2026-09-30_diagnostico_saturacion_memoria_y_bloqueo_subidas_drive.md # [MODIFICADO] Actualización con análisis DNS corporativo, falsos positivos de IP, duplicación en Drive, mitigaciones temporales y hoja de ruta hacia blueprint
│   └── progress/
│       ├── 2026-09-18_contexto-agente.md
│       └── 2026-10-12_contexto-agente.md                                         # [NUEVO] Documento de transición técnica de la sesión actual
└── scripts/
    └── operation/
        └── drive/
            ├── drive_status_manager.py                                           # Módulo de persistencia JSON de subidas y saneamiento
            ├── gestor_archivos_acq.py                                            # Gestor horario de sincronización analizado a nivel forense
            └── subir_archivo.py                                                  # Módulo de subida con API de Google Drive v3

(En la estación remota CHA02 bajo /home/rsa/projects/acelerografo/):
├── configuracion/
│   └── configuracion_dispositivo.json                                            # [MODIFICADO] Actualización con nuevos IDs de Drive para mseed y eventos
├── log-files/
│   ├── evacuacion_drive.log                                                      # [NUEVO] Log dedicado de subida controlada
│   └── uploaded_files_registry.json                                              # [SANEADO] Registro reseteado y actualizado a 1,934 éxitos
└── scripts/
    └── drive/
        └── evacuacion_controlada_drive.py                                        # [NUEVO] Script de contingencia con cerrojo flock, priorización y anti-bufferbloat

(En el repositorio institucional federado rsa/RSA-Metodologias/):
└── indice/
    └── indice_tematico.md                                                        # [MODIFICADO] Estado del diagnóstico actualizado a 'en mitigación'
```

---

## ⚙️ Configuración del Entorno Virtual (`.venv`)

* **Ubicación del Entorno**:
  `/home/rsa/projects/acelerografo/.venv/` (Python 3.12/Debian 12 Bookworm).
* **Corrección de Ruta**:
  Se corrigió la invocación de comandos desde SSH al identificar que el entorno virtual del proyecto lleva punto inicial (`.venv`, no `venv`), resolviendo el error `Exit 127: Command not found`.
* **Bibliotecas Críticas Utilizadas**:
  - `google-api-python-client`, `oauth2client`, `httplib2`: Protocolos de subida resumable y llamadas a la API de Drive v3.
  - `fcntl`: Cerrojo atómico a nivel de kernel (`fcntl.flock`) para exclusión mutua estricta de procesos.
  - `signal`: Manejo limpio de señales (`signal.SIGHUP`, `signal.SIGINT`, `signal.SIGTERM`) para inmunidad ante caídas de la sesión SSH.
  - `gc`: Recolección forzada de basura (`gc.collect()`) para mantener la huella de memoria plana en ~35 MiB.

---

## 🛠️ Modificaciones de Código, Diagnósticos y Hallazgos

### 1. Diagnóstico Forense Post-Reinicio Físico (`CHA02`)
Tras el ciclo de energía físico en campo ejecutado por un colega del usuario el 1 de octubre de 2026:
* **Hardware e Instrumentación**: 100% nominales. Carga de CPU baja (`0.56`), temperatura fresca (`48.9 °C`), memoria RAM libre (630 MiB disponibles, 0 swap).
* **Pipeline SPI**: Servicio `rsa-acelerografo.service` en estado `active (running)`. Named pipe `/tmp/my_pipe` abierto con descriptores válidos en `stream_processor` (PID 702). Ring buffer escribiendo tramas de 2506 bytes de forma ininterrumpida.
* **Sensor y Reloj**: Wrapper directo respondió en $<1$ segundo (`ax=0.58, ay=-0.14, az=9.62, clock=RPi, error_reloj=null`), demostrando que los anteriores errores `[SENSOR_ANOMALY] timeout` y `[ACQUISITION_STALE]` eran provocados por el livelock de memoria y no por averías de hardware.

### 2. Hallazgo de Duplicación Masiva en Drive y Pérdida de Registro
Antes de que se vaciara la papelera de Drive, el usuario constató que existían **hasta 9 copias duplicadas del mismo archivo** en la carpeta remota:
* **Mecanismo de Duplicación**: Google Drive indexa archivos por `id` interno y admite múltiples archivos con el mismo nombre. Como `registrocontinuo.sh` lanzaba `gestor_archivos_acq.py &` a cada hora en segundo plano sin cerrojo ni verificación previa, se acumularon hasta 9 procesos simultáneos. Cada proceso iteraba sobre los archivos pendientes y subía copias redundantes, llenando la cuota de Drive sin control.
* **Falla en `uploaded_files_registry.json`**:
  - Las múltiples instancias concurrentes leían y escribían en el JSON mediante reemplazo atómico (`os.replace`), generando condiciones de carrera donde unas pisaban el progreso de otras.
  - Además, el archivo presentaba propiedad `root:root` (`-rw-r--r--`), provocando que las instancias ejecutadas bajo el usuario `rsa` fallaran silenciosamente con `PermissionError` al intentar registrar los archivos subidos.

### 3. Implementación del Script `evacuacion_controlada_drive.py`
Para rescatar los 2,066 archivos acumulados (~2.65 GB) sin repetir el colapso de memoria, se diseñó e implementó el script de contingencia `/home/rsa/projects/acelerografo/scripts/drive/evacuacion_controlada_drive.py`:
1. **Cerrojo Exclusivo**: `fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)` sobre `/tmp/evacuacion_drive.lock`.
2. **Inmunidad SSH**: Ignora `SIGHUP` para seguir corriendo desatendido en segundo plano con `nohup`.
3. **Priorización en 2 Fases**:
   - Fase 1: 1,283 eventos sísmicos extraídos (`/home/rsa/data/eventos-extraidos/`, ~145 MB). Subida rápida y crítica.
   - Fase 2: 651 archivos MiniSEED continuos (`/home/rsa/data/mseed/`, ~2.5 GB).
4. **Protección Anti-Bufferbloat**: Pausa controlada de 1.5 segundos entre archivos para evitar saturación del enlace celular.
5. **Estabilidad de RAM**: `gc.collect()` cada 25 archivos, manteniendo el consumo en ~35 MiB.
* **Resultado**: **1,934 archivos subidos exitosamente** durante la noche del 1 de octubre. La política de retención temporal se ejecutó normalmente y purgó 131 archivos expirados, recuperando el espacio libre en disco a **7.8 GB (72% de uso)**.

### 4. Falso Positivo en `check_internet_connection()` y Falla en DNS Corporativo (`192.170.100.250`)
El 2 de octubre, los dos nuevos archivos horarios (`15:00` y `16:00` UTC) quedaron retenidos:
* **El Falso Positivo**: `check_internet_connection()` evalúa socket TCP a `8.8.8.8:53`. Como el gateway corporativo enruta paquetes IP (ping `8.8.8.8` respondió en 28 ms con 0% pérdida), devolvía `True`.
* **La Falla Real**: El servidor DNS corporativo asignado por `/etc/resolv.conf` (`192.170.100.250`) falló en resolver dominios externos de Google:
  ```text
  ping: www.googleapis.com: Temporary failure in name resolution
  ```
* **Impacto en el Gestor**: El gestor creía tener Internet, pero cada archivo agotaba 5 reintentos de conexión contra `www.googleapis.com`, consumiendo 30-60 segundos por archivo y demorando la corrida por más de una hora.
* **Decisión de Red Corporativa**: Se acordó no violar las políticas institucionales forzando DNS públicos (8.8.8.8 / 1.1.1.1) en `CHA02`, confiando en la autonomía de almacenamiento local (con 7.8 GB libres, la estación puede retener datos durante **más de 90 días** sin pérdida).

### 5. Efecto Colateral de la Pausa del Crontab y Correlación en `CHA01`
* **Efecto Colateral en `CHA02`**: Para evitar que la falla de DNS volviera a acumular procesos y desbordar la RAM, se pausó la línea horaria de `crontab -e` (`#0 * * * * ... registrocontinuo start`). Esto protegió la memoria, pero **paralizó colateralmente la conversión horaria de binario a miniSEED (`binary_to_mseed.py 1`)**.
* **Correlación en `CHA01`**: La estación hermana `CHA01`, conectada a la misma red corporativa con el mismo cron activo, evidenció picos horarios periódicos de consumo de RAM, confirmando la vulnerabilidad sistémica de la arquitectura actual.

---

## 📋 Pasos Sugeridos para el Siguiente Agente

Para solucionar de forma definitiva estas fallas de arquitectura sin comprometer la conversión horaria ni arriesgar la memoria de las estaciones, el siguiente agente debe seguir este flujo estructurado:

1. **Elaborar el Blueprint de Planificación (`planning_guide`)**:
   - Crear el plan estructurado en `montajes/acelerografo-DEV00/docs/blueprints/YYYY-MM-DD_plan_desacoplamiento_conversion_y_blindaje_drive.md`.

2. **Fase 1: Desacoplar Tareas en `registrocontinuo.sh`**:
   - Separar la ejecución de `binary_to_mseed.py` del despacho del gestor de Drive.
   - La conversión horaria a miniSEED debe ejecutarse de forma garantizada y sincrónica cada hora, independientemente del estado de la red.
   - Proteger el lanzamiento en segundo plano de `gestor_archivos_acq.py &` con una guarda de proceso (`if ! pgrep -f "gestor_archivos_acq.py"; then ...`).

3. **Fase 2: Blindaje con Cerrojo Atómico en `gestor_archivos_acq.py`**:
   - Incorporar al inicio de `gestor_archivos_acq.py` la lógica de cerrojo no bloqueante probada en la contingencia:
     ```python
     lock_fd = open('/tmp/gestor_archivos_acq.lock', 'w')
     fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
     ```
   - Si otra instancia ya está corriendo, registrar aviso limpio y salir de inmediato con código 0.

4. **Fase 3: Validación DNS Real y Mecanismo *Fail-Fast***:
   - Modificar `check_internet_connection()` en `gestor_archivos_acq.py` para validar resolución DNS real mediante `socket.getaddrinfo("www.googleapis.com", 443, timeout=5)`.
   - Implementar aborto temprano (*fail-fast*): si la resolución DNS o la subida del primer archivo falla con error de red/DNS, abortar todo el lote de subida de inmediato sin iterar el resto de la cola.

5. **Fase 4: Dosificación por Lotes (*Batch Size*) y Pacing**:
   - Incorporar parámetro configurable `max_batch_size` (ej. 15-20 archivos por corrida) y una pausa de 1.0-1.5s entre transferencias para evitar *bufferbloat* y proteger la telemetría MQTT.

6. **Fase 5: Despliegue y Validación en Flota (`CHA01` y `CHA02`)**:
   - Desplegar la solución en `CHA02` y reactivar su crontab.
   - Desplegar preventivamente en `CHA01` para erradicar sus picos de consumo de RAM.
