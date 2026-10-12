---
proyecto: acelerografo-DEV00
tipo: diagnostico_tecnico
resolucion: en_mitigacion
temas: [memoria, livelock, drive, bufferbloat, ssh, crontab, resiliencia, concurrencia, dns, store-and-forward]
fecha: 2026-09-30
fecha_actualizacion: 2026-10-02
---

# Diagnóstico Técnico: Saturación Crítica de Memoria, Concurrencia Descontrolada en Subidas a Drive y Bloqueo de Acceso Remoto en Estaciones Acelerográficas

**Fecha Inicial**: 2026-09-30  
**Última Actualización**: 2026-10-02  
**Proyecto / Repositorio**: `acelerografo-DEV00`  
**Componente(s) afectado(s)**: `scripts/task/registrocontinuo.sh`, `scripts/operation/drive/gestor_archivos_acq.py`, `scripts/operation/drive/subir_archivo.py`, `scripts/operation/drive/drive_status_manager.py`, `scripts/operation/mqtt/mqtt_coordinator.py`, subsistema de resolución DNS (`/etc/resolv.conf`), `crontab` y subsistema de memoria (`sshd`)  
**Estación(es) bajo análisis**: `CHA02` (incidente crítico de livelock resuelto) y `CHA01` (comportamiento correlacionado en la misma red corporativa)  
**Estado**: En Mitigación (intervención física completada, backlog histórico evacuado, fallos de arquitectura caracterizados y mitigaciones temporales activas; pendiente planificación de refactorización definitiva)  
**Severidad**: Crítica (provocó pérdida de control remoto SSH y telemetría MQTT en `CHA02`, duplicación masiva en Google Drive y riesgo de recurrencia en `CHA01`)  

---

## 1. Resumen Ejecutivo

Durante la última semana de septiembre de 2026 se registró una falla sistémica en la estación acelerográfica de campo **`CHA02`**, manifestada inicialmente por un consumo de memoria RAM superior al 98.5%, desconexiones recurrentes de telemetría MQTT y la imposibilidad absoluta de establecer sesiones remotas por SSH desde el concentrador local (`concentrador-CON00`).

La investigación técnica forense demostró una cadena de fallas interconectadas:

1. **Falso Positivo en Detección de Conectividad**: La función `check_internet_connection()` en `gestor_archivos_acq.py` validaba salida a Internet mediante socket TCP directo a la IP `8.8.8.8:53`. En la red corporativa donde operan las estaciones `CHA01` y `CHA02`, la conectividad IP pura estaba activa, pero el servidor DNS corporativo asignado (`192.170.100.250`) fallaba sistemáticamente en resolver los dominios de Google (`www.googleapis.com` -> `Temporary failure in name resolution`).
2. **Bucle de Timeouts Extendido y Avalancha Horaria**: Al asumir erróneamente que había conexión plena a Google Drive, el gestor iniciaba la subida de cada archivo en cola, ejecutando 5 reintentos con timeouts de hasta 30-60 segundos por archivo. Con una cola de decenas o cientos de archivos, la ejecución tardaba más de 1 hora.
3. **Invocación Concurrente sin Exclusión Mutua (*Race Condition*)**: El script `registrocontinuo.sh`, invocado cada hora en punto (`0 * * * *`) por `cron`, lanzaba una nueva instancia de `gestor_archivos_acq.py &` en segundo plano sin verificar si la ejecución previa continuaba viva ni usar cerrojos (`flock` o `pgrep`).
4. **Duplicación Masiva en Drive y Desincronización de Registro**: Se llegaron a acumular hasta 9 procesos concurrentes del gestor. Dado que Google Drive permite múltiples archivos con el mismo nombre en una carpeta, cada proceso subía réplicas de los mismos archivos (hasta 9 copias duplicadas por archivo), saturando la cuota de almacenamiento de Drive. Asimismo, la concurrencia desordenada de escrituras atómicas en `uploaded_files_registry.json` y problemas de permisos (`root:root`) provocaban que las subidas exitosas no se persistieran localmente.
5. **Livelock de Memoria (*Swap Thrashing*)**: Los múltiples procesos de Python acumulados en la Raspberry Pi 3B+ (1 GB de RAM física) agotaron la memoria disponible, forzando un intercambio continuo con la tarjeta microSD lenta que impidió a `sshd` invocar `fork()` para atender sesiones remotas.

### Evolución y Mitigaciones Temporales (01 y 02 de Octubre de 2026)
* **01 de Octubre**: Se realizó un reseteo físico en campo de `CHA02`. Se comprobó que el hardware, los sensores y la adquisición SPI estaban 100% nominales. Tras la eliminación accidental de la carpeta en Google Drive, se configuraron nuevos destinos y se desarrolló e implementó el script de contingencia `evacuacion_controlada_drive.py`. Dicho script —blindado con `flock`, priorización de eventos, pausas de 1.5s y recolección de basura periódica— **evacuó con éxito 1,934 archivos acumulados** (1,283 eventos y 651 miniSEED), permitiendo que la política de retención liberara espacio hasta 7.8 GB libres (72% de uso).
* **02 de Octubre**: Al generarse nuevos archivos horarios, volvieron a estancarse por persistencia de la falla de resolución DNS en el servidor corporativo `192.170.100.250`. Se adoptó el criterio prudencial de **no modificar los DNS corporativos**, pausar el cron horario en `CHA02` y monitorear `CHA01` (que reporta picos de RAM análogos en cada hora), operando con almacenamiento local seguro (capacidad para >90 días).
* **Efecto Colateral Actual**: La desactivación del cron de emergencia previene el desborde de memoria, pero **paraliza también la conversión horaria de binario a miniSEED (`binary_to_mseed.py`)**, lo cual exige una refactorización desacoplada definitiva.

---

## 2. Estado Actual de las Estaciones (al 2026-10-02)

| Componente / Subsistema | `CHA02` | `CHA01` | Observaciones Técnicas |
|---|---|---|---|
| **Pipeline SPI (`rsa-acelerografo.service`)** | ✅ Operativo | ✅ Operativo | Activo y nominal; genera tramas y ring buffer continuamente. |
| **Daemons Supervisor (`stream`, `gpd`, `mqtt`)** | ✅ Operativo | ✅ Operativo | Todos en estado `RUNNING` con consumo de memoria estabilizado. |
| **Acceso Remoto SSH (`sshd`)** | ✅ Operativo | ✅ Operativo | Restaurado tras el ciclo de energía físico; responde sin latencia. |
| **Sensores e Integridad de Reloj** | ✅ Nominal | ✅ Nominal | Triaxial respondiendo en milisegundos; NTP sincronizado (`0.0s`). |
| **Almacenamiento Local (Disco)** | ✅ 7.8 GB libres (72%) | ✅ Saludable | Espacio recuperado tras evacuación exitosa de 1,934 archivos. |
| **Resolución DNS Corporativa** | ❌ Falla (`192.170.100.250`) | ❌ Falla (`192.170.100.250`) | `ping 8.8.8.8` OK (28 ms), pero `www.googleapis.com` falla por resolución. |
| **Tarea Programada `crontab`** | ⏸️ Pausada (`#0 * * * *`) | ⚠️ Activa con picos RAM | En `CHA02` pausada para evitar livelock; en `CHA01` genera picos horarios. |
| **Conversión Horaria (`binary_to_mseed.py`)** | ⏸️ Paralizada (por pausa de cron) | ⚠️ Activa en cron | Requiere desacoplamiento urgente de la subida a Drive. |

---

## 3. Evidencia y Análisis Forense

### 3.1. Prueba de Enlace y Conectividad Física Local
Desde `concentrador-CON00` hacia `CHA02` (`172.22.150.53`):
```text
rsa@concentrador-CON00:~ $ ping -c 5 172.22.150.53
5 packets transmitted, 5 received, 0% packet loss, time 136ms
rtt min/avg/max/mdev = 0.716/0.761/0.790/0.027 ms
```
*Diagnóstico*: La conectividad local y el hardware de red operan en submilisegundos sin pérdidas.

---

### 3.2. Falso Positivo en Detección de Conectividad (`check_internet_connection`)
En [`scripts/operation/drive/gestor_archivos_acq.py`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/drive/gestor_archivos_acq.py#L95-L105):
```python
def check_internet_connection(logger, host="8.8.8.8", port=53, timeout=3):
    try:
        socket.setdefaulttimeout(timeout)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((host, port))
        sock.close()
        logger.info("Conexión a internet verificada.")
        return True
    except Exception as e:
        logger.warning(f"Fallo en la conexión a internet: {e}")
        return False
```
*Evidencia en la estación*:
```text
rsa@CHA02:~$ ping -c 3 8.8.8.8
3 packets transmitted, 3 received, 0% packet loss, time 2003ms
rtt min/avg/max/mdev = 27.867/29.928/33.891/2.802 ms

rsa@CHA02:~$ ping -c 3 www.googleapis.com
ping: www.googleapis.com: Temporary failure in name resolution
```
*Análisis*: La prueba conecta a la IP `8.8.8.8:53`. Como el gateway corporativo enruta paquetes hacia el exterior, la función devuelve `True`, engañando al gestor para iniciar la transferencia de archivos sin validar si el dominio del servicio destino (`www.googleapis.com`) es resoluble.

---

### 3.3. Acumulación por Timeouts de DNS y Concurrencia en `registrocontinuo.sh`
En [`scripts/task/registrocontinuo.sh`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/task/registrocontinuo.sh#L27-L34):
```bash
    # 2. Ejecutar conversión binary_to_mseed (esperar a que termine)
    echo "Ejecutando conversión a miniSEED..."
    "$VENV_PYTHON" "$PROJECT_LOCAL_ROOT/scripts/mseed/binary_to_mseed.py" 1
    
    # 3. Ejecutar gestor de archivos
    echo "Ejecutando gestor de archivos..."
    "$VENV_PYTHON" "$PROJECT_LOCAL_ROOT/scripts/drive/gestor_archivos_acq.py" &
```
*Análisis del Acoplamiento*:
1. `binary_to_mseed.py` corre primero y tarda $<5$ segundos en empaquetar la hora.
2. `gestor_archivos_acq.py &` se lanza en segundo plano sin esperar finalización ni verificar duplicados.
3. Ante fallos de DNS, cada archivo agota 5 reintentos de conexión fallida:
   ```text
   2026-10-02 17:01:45,449 - CHA2_gestor_acq.log - INFO - [ERROR] | error=Error en intento 5/5 subiendo CHA2_20261002_150000.mseed. Codigo: Unable to find the server at www.googleapis.com
   ```
4. Con 60 archivos en cola, el script tarda $>45$ minutos reintentando. Cuando llega el minuto `0` de la hora siguiente, el cron dispara otro proceso igual, multiplicando exponencialmente el consumo de RAM.

---

### 3.4. Mecanismo de Duplicación Masiva en Google Drive y Fallo de Registro
Antes del saneamiento, se observaron **hasta 9 copias duplicadas del mismo archivo** en Google Drive.
* **Causa**: Google Drive indexa archivos por `id` interno y permite nombres repetidos en un directorio. Nueve instancias concurrentes leyeron el inventario local simultáneamente e insertaron el mismo archivo MiniSEED en Drive.
* **Fallo de Registro**:
  - `drive_status_manager.py` aplica escrituras atómicas (`os.replace` de `.tmp` a `.json`). Cuando múltiples procesos compiten por reemplazar el archivo, la última instancia sobrescribe las actualizaciones de las anteriores.
  - Además, el archivo `uploaded_files_registry.json` presentaba propiedad `root:root` (`-rw-r--r--`), lo que generaba `PermissionError` silencioso cuando el gestor corría bajo el usuario `rsa`.

---

### 3.5. Cronología Integral del Incidente

| Fecha / Hora | Evento | Impacto Técnico |
|---|---|---|
| **2026-09-23** | Servidor DNS corporativo `192.170.100.250` pierde resolución de dominios Google. | Fallo intermitente de subidas en `CHA01` y `CHA02`. |
| **2026-09-23/28** | Acumulación de 2,066 archivos locales (768 continuous mseed y 1,298 eventos extraídos, ~2.65 GB). | Retención local previene borrado, pero satura inventario. |
| **2026-09-29 16:00** | Crontab horario lanza múltiples instancias concurrentes de `gestor_archivos_acq.py &`. | Livelock de RAM (98.5% -> 100%), swap thrashing y bloqueo de `sshd`. |
| **2026-09-30** | Mitigación remota: envío de carpeta de Drive a Papelera y comando MQTT de parada. | Puertos TCP 22/5000 responden SYN-ACK, pero persistencia de livelock exige reinicio físico. |
| **2026-10-01 16:07** | Intervención presencial: ciclo de energía físico completado en `CHA02`. | Sistema arranca nominal: CPU 1.68->0.56, Temp 48.9°C, RAM 233 MiB, sensor nominal. |
| **2026-10-01 17:00** | Vaciado accidental de la papelera en Google Drive por un operador. | Carpeta destino eliminada permanentemente en la nube. |
| **2026-10-01 18:02** | Creación de nuevas carpetas y despliegue de `evacuacion_controlada_drive.py`. | **1,934 archivos subidos exitosamente** con cerrojo `flock` y pausas controladas. |
| **2026-10-01 20:40** | Poda automática de retención temporal: 131 archivos expirados purgados. | Espacio libre en disco se recupera a **7.8 GB (72% de uso)**. |
| **2026-10-02 17:01** | Nuevos archivos horarios (15:00 y 16:00 UTC) fallan por DNS corporativo. | Dos archivos retenidos con error `Unable to find the server at www.googleapis.com`. |
| **2026-10-02 18:00** | Diagnóstico confirma falla en `192.170.100.250`. Se pausa cron horario como precaución. | Estación queda protegida de livelock, pero se suspende conversión horaria de miniSEED. |

---

## 4. Hallazgos y Causa Raíz

### Hallazgo 1: Falta de Exclusión Mutua (*File Lock*) en Procesos de Sincronización
* **Causa**: Ausencia de cerrojo a nivel de sistema operativo (`fcntl.flock`) al iniciar `gestor_archivos_acq.py`.
* **Efecto**: Acumulación descontrolada de instancias concurrentes ejecutadas por `cron`.

### Hallazgo 2: Detección Falsa de Internet por Evaluación Exclusiva a Nivel IP
* **Causa**: `check_internet_connection()` evalúa socket a `8.8.8.8:53`.
* **Efecto**: Retorna `True` en redes con enrutamiento IP pero sin resolución de nombres, atrapando al gestor en bucles masivos de reintentos con timeouts de DNS.

### Hallazgo 3: Acoplamiento Indebido entre Conversión Local y Subida a Drive
* **Causa**: `registrocontinuo.sh` empaqueta en una misma secuencia sincrónica/asincrónica la conversión `binary_to_mseed.py` y la subida `gestor_archivos_acq.py`.
* **Efecto**: Al detener el cron para proteger la RAM ante caídas de Drive, se detiene colateralmente la generación de archivos miniSEED horarios.

### Hallazgo 4: Ausencia de *Fail-Fast* en Ciclos de Subida
* **Causa**: Si el primer archivo falla por error de DNS o red inalcanzable, el gestor no aborta el ciclo; continúa iterando los restantes $N$ archivos pendientes ejecutando 5 reintentos por cada uno.
* **Efecto**: Un lote con 50 archivos pendientes consume más de 50 minutos de CPU y sockets inútilmente.

### Hallazgo 5: Livelock por Memoria (*Swap Thrashing*) en Arquitectura de 1 GB de RAM
* **Causa**: Daemons de Supervisor consumen base de 350-400 MiB; cada instancia de Python de Drive retiene 40-60 MiB. Múltiples instancias saturan la memoria física forzando swap en microSD lenta y bloqueando llamadas `fork()` de `sshd`.

---

## 5. Mitigaciones Temporales Implementadas

### Mitigación 1: Script de Evacuación Controlada (`evacuacion_controlada_drive.py`)
Para subsanar el colapso sin riesgo de saturar la memoria ni la red celular, se desarrolló y ejecutó en `CHA02` el script `/home/rsa/projects/acelerografo/scripts/drive/evacuacion_controlada_drive.py`:
* **Cerrojo Atómico**: Implementación de `fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)` sobre `/tmp/evacuacion_drive.lock`.
* **Inmunidad a Desconexión SSH**: Manejo de señales con `signal.signal(signal.SIGHUP, signal.SIG_IGN)` y ejecución con `nohup`.
* **Priorización Científica de Datos**: Primero evacuación de 1,283 eventos sísmicos extraídos (145 MB en total) y posteriormente 651 archivos continuos MiniSEED (2.5 GB).
* **Control de Tráfico (*Anti-Bufferbloat*)**: Pausa deliberada de 1.5 segundos entre archivos.
* **Recolección Forzada de Basura**: Invocación de `gc.collect()` cada 25 archivos, manteniendo la huella de RAM fija en ~35 MiB.
* **Resultado**: **1,934 archivos subidos exitosamente** en una sola noche y espacio en disco liberado a 7.8 GB.

### Mitigación 2: Gestión de IDs de Google Drive
Tras el vaciado de la papelera en Drive:
* Creación de nuevas carpetas dedicadas para `mseed` y `eventos`.
* Actualización de `mseed_id` y `events_id` en `configuracion_dispositivo.json`.
* Saneamiento y corrección de permisos en `uploaded_files_registry.json` para el usuario `rsa`.

### Mitigación 3: Política de Espera Prudencial de Red y Pausa de Cron
* **Respeto a la Gobernanza de Red Corporativa**: Se desestimó alterar manualmente `/etc/resolv.conf` con DNS públicos (8.8.8.8 / 1.1.1.1) para cumplir con las políticas institucionales de TI y evitar conflictos de firewall.
* **Autonomía Local Verificada**: Con 7.8 GB libres en disco y una tasa de generación de ~82 MB/día, la estación tiene capacidad de almacenamiento autónomo local de **más de 90 días**. Los datos no se pierden y quedan retenidos de forma segura.
* **Pausa de Cron**: Desactivación temporal de la línea horaria en `crontab -e` de `CHA02` para impedir acumulación de procesos mientras el DNS corporativo continúe inestable.

---

## 6. Hoja de Ruta para Planificación Definitiva (Hacia el Blueprint)

Con base en la caracterización completa de estas fallas, se define la hoja de ruta técnica que deberá estructurarse formalmente en un **Blueprint de Planificación** (`docs/blueprints/`):

### Fase 1: Desacoplamiento de Tareas en `registrocontinuo.sh`
* Separar la conversión `binary_to_mseed.py` del proceso de sincronización con Google Drive.
* La conversión horaria debe ejecutarse de forma independiente y garantizada a cada hora, independientemente del estado de la red o de Drive.
* Incorporar verificación de proceso activo (`pgrep`) previo a cualquier invocación en segundo plano.

### Fase 2: Blindaje y Exclusión Mutua en `gestor_archivos_acq.py`
* Migrar formalmente el cerrojo atómico `fcntl.flock` al inicio de `gestor_archivos_acq.py`.
* Si existe un cerrojo activo, el script debe registrar un aviso limpio y salir inmediatamente con código 0.

### Fase 3: Validación Real de DNS y Mecanismo *Fail-Fast*
* Reemplazar la prueba ciega a `8.8.8.8:53` en `check_internet_connection()` por una verificación de resolución de dominio:
  ```python
  socket.getaddrinfo("www.googleapis.com", 443, timeout=5)
  ```
* Implementar aborto temprano (*fail-fast*): si la autenticación o la subida del primer archivo falla por resolución de red/DNS, abortar el ciclo de subida completo sin iterar el resto de la cola.

### Fase 4: Dosificación por Lotes (*Batch Limiting*) y Pacing de Red
* Incorporar parámetro configurable `max_batch_size` (ej. 15-20 archivos por corrida) en `configuracion_dispositivo.json`.
* Mantener una pausa breve (1 a 1.5s) entre transferencias para proteger el enlace celular y preservar la telemetría MQTT.

### Fase 5: Watchdog y Despliegue en Flota (`CHA01`, `CHA02`)
* Sincronizar y validar la solución en `CHA01` (previniendo sus picos de RAM actuales) y `CHA02`.
* Incorporar en la telemetría MQTT métricas sobre archivos en cola y estado de resolución DNS.

---

## 7. Referencias y Documentos Relacionados

* [`docs/adr/021_sincronizacion_espejo_registro_drive_y_sintesis_diagnostico.md`](file:///home/rsa/git/montajes/acelerografo-DEV00/docs/adr/021_sincronizacion_espejo_registro_drive_y_sintesis_diagnostico.md): Sincronización espejo del registro de Drive con almacenamiento local.
* [`scripts/drive/evacuacion_controlada_drive.py`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/drive/evacuacion_controlada_drive.py): Script de contingencia con exclusión mutua y control de recursos.
* [`scripts/task/diagnostico.sh`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/task/diagnostico.sh): Rutina de diagnóstico integral de la estación.
* [`scripts/task/registrocontinuo.sh`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/task/registrocontinuo.sh): Orquestador horario de adquisición, conversión y subida.
