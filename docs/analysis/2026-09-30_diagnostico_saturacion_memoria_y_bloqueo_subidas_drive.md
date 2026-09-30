---
proyecto: acelerografo-DEV00
tipo: diagnostico_tecnico
resolucion: pendiente
temas: [memoria, livelock, drive, bufferbloat, ssh, crontab, resiliencia, concurrencia]
fecha: 2026-09-30
---

# Diagnóstico Técnico: Saturación Crítica de Memoria, Concurrencia Descontrolada en Subidas a Drive y Bloqueo de Acceso Remoto en Estaciones Acelerográficas

**Fecha**: 2026-09-30  
**Proyecto / Repositorio**: `acelerografo-DEV00`  
**Componente(s) afectado(s)**: `scripts/task/registrocontinuo.sh`, `scripts/operation/drive/gestor_archivos_acq.py`, `scripts/operation/drive/subir_archivo.py`, `scripts/operation/mqtt/mqtt_coordinator.py`, subsistema de memoria del kernel (`sshd`, `crontab`)  
**Estación(es) bajo análisis**: `CHA02` (incidente crítico de livelock), con antecedentes correlacionados en `CHA01`  
**Estado**: Diagnosticado (pendiente de intervención física en campo y refactorización preventiva)  
**Severidad**: Crítica (provocó pérdida total de control remoto SSH y telemetría MQTT en la estación `CHA02`)  

---

## 1. Resumen Ejecutivo

Durante la última semana de septiembre de 2026 se registró una falla sistémica en la estación acelerográfica de campo **`CHA02`**, manifestada inicialmente por un consumo de memoria RAM superior al 98.5%, desconexiones recurrentes de telemetría MQTT y la imposibilidad absoluta de establecer sesiones remotas por SSH desde el concentrador local (`concentrador-CON00`).

La investigación técnica demostró que el incidente fue consecuencia de un **efecto dominó en cascada**:
1. Un fallo preliminar de resolución DNS corporativa y posterior agotamiento de cuota en Google Drive impidió la evacuación normal de datos MiniSEED, acumulando una cola masiva de **761 archivos pendientes ($\approx 2.6\text{ GB}$)**.
2. Al intentar procesar este volumen acumulado, el script [`scripts/operation/drive/gestor_archivos_acq.py`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/drive/gestor_archivos_acq.py) entró en un bucle voraz e ininterrumpido de transferencias HTTPS hacia Google Drive, saturando el ancho de banda de subida (*uplink*) y provocando un fenómeno severo de *bufferbloat*.
3. La tarea programada en [`scripts/task/crontab.txt`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/task/crontab.txt) invoca a cada hora en punto (`0 * * * *`) el script [`scripts/task/registrocontinuo.sh`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/task/registrocontinuo.sh), el cual lanza en segundo plano (`&`) una nueva instancia de `gestor_archivos_acq.py` **sin verificar si la ejecución de la hora previa seguía viva y sin mecanismos de exclusión mutua (*file lock*)**.
4. La acumulación de múltiples instancias concurrentes de Python compitiendo por memoria en una Raspberry Pi 3B+ (1 GB de RAM física) devoró la memoria disponible, empujando al kernel a un **livelock por memoria (*swap thrashing*)** en la tarjeta microSD lenta. En este estado, el daemon `sshd` no pudo realizar llamadas `fork()` para atender conexiones entrantes.

Se aplicaron maniobras de mitigación desde la red y la nube (incluyendo el envío temporal de la carpeta de Drive a la Papelera para abortar la transferencia pesada y el disparo del comando MQTT `stop_acquisition_safety`), logrando reabrir temporalmente los puertos TCP 22 y 5000 a nivel de socket. Sin embargo, el estado de livelock de memoria acumulado en los daemons de Supervisor impidió la recuperación interactiva sin un reinicio del sistema operativo. Se ha programado una visita presencial para la próxima semana a fin de ejecutar el ciclo de energía físico y se estructura el backlog para blindar el software contra esta clase de contingencias.

---

## 2. Estado Actual

| Componente / Servicio | Estado Operativo | Observaciones Técnicas |
|---|---|---|
| **Pipeline SPI (`rsa-acelerografo.service`)** | ⚠️ Desconocido / Reactivado | Fue detenido vía MQTT (liberó 2% de RAM), pero el cron horario a las 16:00 lo reactivó automáticamente. |
| **Daemon `stream_processor` (Supervisor)** | ❌ Inaccesible / Livelock | Retiene memoria pasiva tras >8 días de ejecución ininterrumpida; vulnerable a thrashing. |
| **Daemon `gpd_worker` (Supervisor)** | ❌ Inaccesible / Livelock | Modelo de IA residente en memoria; principal consumidor de RAM base en la Raspberry Pi. |
| **Daemon `mqtt_coordinator` (Supervisor)** | ❌ Desconectado | Dejó de emitir telemetría al broker tras colapso del bucle de eventos por memoria exhausta. |
| **Servidor Web `config_server` (Supervisor)** | ⚠️ Socket TCP Abierto | Puerto 5000 responde a nivel de SYN-ACK en red local, pero no atiende peticiones HTTP completas. |
| **Daemon SSH (`sshd`)** | ❌ Conexión Rechazada / Timeout | Puerto 22 responde TCP en red local (0.76 ms), pero aborta en autenticación o fork (`ENOMEM`). |
| **Conectividad de Red Local (LAN)** | ✅ Operativa | Ping entre `concentrador-CON00` y `172.22.150.53` responde con $0.76\text{ ms}$ y $0\%$ pérdida. |
| **Almacenamiento Local (microSD)** | ✅ Seguro | Datos protegidos bajo sistema ext4 con journal; retención estricta impide borrado de archivos no subidos. |

---

## 3. Evidencia y Análisis

### 3.1. Prueba de Enlace y Conectividad Física Local
Desde `concentrador-CON00` hacia la estación `CHA02` (`172.22.150.53`):
```text
rsa@concentrador-CON00:~ $ ping -c 5 172.22.150.53
PING 172.22.150.53 (172.22.150.53) 56(84) bytes of data.
64 bytes from 172.22.150.53: icmp_seq=1 ttl=64 time=0.790 ms
64 bytes from 172.22.150.53: icmp_seq=2 ttl=64 time=0.716 ms
64 bytes from 172.22.150.53: icmp_seq=3 ttl=64 time=0.761 ms
64 bytes from 172.22.150.53: icmp_seq=4 ttl=64 time=0.751 ms
64 bytes from 172.22.150.53: icmp_seq=5 ttl=64 time=0.787 ms

--- 172.22.150.53 ping statistics ---
5 packets transmitted, 5 received, 0% packet loss, time 136ms
rtt min/avg/max/mdev = 0.716/0.761/0.790/0.027 ms
```
*Análisis*: La infraestructura de red física, el conmutador Ethernet y la interfaz de red del kernel responden en submilisegundos ($0.76\text{ ms}$). No hay avería de cableado ni corte de energía.

---

### 3.2. Diagnóstico del Servicio SSH y Puertos Locales
Al probar la conexión mediante socket TCP puro en Python:
```python
Puerto    22: ABIERTO
Puerto  5000: ABIERTO
Puerto    80: RECHAZADO
```
Sin embargo, al intentar la sesión SSH con `ssh -vvv -o ConnectTimeout=60 -T CHA02 "sudo /sbin/reboot -f"`:
```text
debug1: Connecting to 172.22.150.53 [172.22.150.53] port 22.
debug2: fd 3 setting O_NONBLOCK
debug1: connect to address 172.22.150.53 port 22: Connection timed out
ssh: connect to host 172.22.150.53 port 22: Connection timed out
```
*Análisis*: El socket del puerto 22 se encuentra abierto en el stack TCP del kernel, pero cuando el cliente intenta progresar en la negociación del túnel cifrado, la llamada `fork()` de `sshd` o la lectura de disco en la swap congelada sobrepasa el tiempo límite, cerrando el canal antes de admitir comandos.

---

### 3.3. Inspección del Crontab Horario y Falta de Lock
En [`scripts/task/crontab.txt`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/task/crontab.txt#L2):
```cron
0 * * * * sleep 30 && /usr/local/bin/registrocontinuo start
```
Y en [`scripts/task/registrocontinuo.sh`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/task/registrocontinuo.sh#L18-L34):
```bash
case "$1" in
  start)
    if systemctl is-active --quiet rsa-acelerografo.service; then
      echo "registro_continuo ya está ejecutándose (systemd)"
    else
      echo "Iniciando registro_continuo via systemd..."
      sudo systemctl start rsa-acelerografo.service
      sleep 3
    fi
    
    # 2. Conversión horaria
    "$VENV_PYTHON" "$PROJECT_LOCAL_ROOT/scripts/mseed/binary_to_mseed.py" 1
    
    # 3. Gestor de archivos en background
    "$VENV_PYTHON" "$PROJECT_LOCAL_ROOT/scripts/drive/gestor_archivos_acq.py" &
    ;;
```
*Análisis*:
1. Si `rsa-acelerografo.service` fue detenido remotamente mediante una orden MQTT de contingencia, `registrocontinuo start` lo vuelve a encender incondicionalmente a los 30 segundos de la siguiente hora.
2. `gestor_archivos_acq.py &` se despacha en segundo plano **sin esperar a que termine** y **sin verificar si ya existe un proceso previo en ejecución**.
3. Al acumularse 761 archivos pendientes, cada corrida tarda mucho más de 60 minutos, provocando que múltiples instancias de `gestor_archivos_acq.py` se ejecuten en paralelo.

---

### 3.4. Cronología del Incidente

1. **2026-09-23**: En la red corporativa de las estaciones `CHA01` y `CHA02`, el servidor DNS interno (`192.170.100.250`) pierde la resolución de dominios del ecosistema Google (`*.google.com`, `*.googleapis.com`), mientras que `pool.ntp.org` y `github.com` se mantienen operativos.
2. **2026-09-23 a 2026-09-28**: Las subidas a Google Drive fallan con `Unable to find the server at www.googleapis.com` o cuota de almacenamiento llena. La política de retención y protección de archivos retiene en disco todos los MiniSEED horarios y eventos extraídos generados, acumulando **761 archivos ($\approx 2.6\text{ GB}$)** en `CHA02`.
3. **2026-09-29 mañana**: Se restablece el acceso a Internet o la cuota en Drive. El gestor de archivos comienza a transmitir la cola acumulada de forma ininterrumpida.
4. **2026-09-29 15:30**: La memoria RAM de `CHA02` alcanza el 98.5%. El usuario emite vía MQTT el comando `stop_acquisition_safety`, logrando una descompresión temporal del 2% (RAM desciende a 96.5%).
5. **2026-09-29 16:00:30**: El cron horario ejecuta `registrocontinuo start`. Revive `rsa-acelerografo.service`, ejecuta `binary_to_mseed.py` y lanza una nueva instancia concurrente de `gestor_archivos_acq.py &`. La memoria trepa a 97.7% - 100%, congelando `sshd` e induciendo *swap thrashing*.
6. **2026-09-29 17:40**: Se ejecuta la maniobra de enviar la carpeta de Google Drive a la Papelera desde la nube. Las llamadas a la API de Drive fallan en 0.1 s sin transmitir bytes. Los sockets TCP 22 y 5000 pasan a responder `ABIERTO` en red local, pero el livelock acumulado en el kernel impide completar la autenticación interactiva.

---

## 4. Hallazgos y Causa Raíz

### Hallazgo 1: Falta de Exclusión Mutua (*File Lock*) en `gestor_archivos_acq.py`
* **Descripción técnica**: Múltiples instancias del gestor de sincronización de Google Drive corren simultáneamente cuando el tiempo de transferencia excede la cadencia de invocación del cron (1 hora).
* **Causa**: [`gestor_archivos_acq.py`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/drive/gestor_archivos_acq.py) no implementa bloqueo por archivo (*flock*) ni comprobación de PID existente al inicio del script.
* **Condiciones de activación**: Ocurre siempre que haya un backlog sustancial de archivos acumulados (por caída de red, DNS o cuota de Drive) que demore el ciclo de subida más de 60 minutos.

### Hallazgo 2: Reactivación Incondicional de Servicios por `registrocontinuo.sh`
* **Descripción técnica**: Los comandos de parada de seguridad o contingencia administrativa son anulados a cada hora en punto.
* **Causa**: La lógica de `registrocontinuo.sh` evalúa `systemctl is-active rsa-acelerografo.service` e inicia el servicio si está inactivo, sin verificar si existe una bandera de parada intencional o mantenimiento (`/var/run/acq_maintenance.flag`).
* **Condiciones de activación**: Invocación horaria desatendida por el daemon `cron`.

### Hallazgo 3: *Bufferbloat* y Asfixia del Canal de Red por Cola Masiva
* **Descripción técnica**: La subida secuencial e ininterrumpida de 761 archivos MiniSEED ($\approx 2.6\text{ GB}$) saturó el buffer de salida de la Raspberry Pi hacia el router corporativo.
* **Causa**: Ausencia de limitación de tasa (*rate limiting*) o procesamiento por lotes acotados (*batch limit*) en `subir_archivo.py`. Cada archivo abre sockets HTTPS pesados con timeouts de hasta 300 segundos.
* **Condiciones de activación**: Reanudación de la conectividad tras períodos prolongados de retención local.

### Hallazgo 4: Livelock por Memoria (*Swap Thrashing*) en Arquitectura de 1 GB de RAM
* **Descripción técnica**: Cuando la memoria disponible desciende por debajo de `vm.min_free_kbytes` ($\approx 15\text{ MB}$), el kernel gasta casi todos los ciclos de CPU intercambiando páginas de memoria con la tarjeta microSD lenta.
* **Causa**: Los daemons de Supervisor (`gpd_worker` con IA, `stream_processor` y `mqtt_coordinator`) mantienen un consumo pasivo alto ($\approx 85\% - 90\%$). Al agregarse dos instancias de Python de 60 MB cada una, la RAM física se agota por completo.
* **Condiciones de activación**: Tiempo de actividad (*uptime*) prolongado (>8 días) con acumulación de memoria en heap de Python sumado a sobrecarga horaria.

---

## 5. Evaluación de Riesgo

| # | Escenario de Riesgo | Probabilidad | Impacto | Mitigación Requerida |
|---|---|---|---|---|
| **R1** | Recurrencia en estaciones de campo remotas (`CHA01`, `PNS01`, `TEST`) ante cortes de enlace de Drive. | **Alta** | **Crítico** | Implementar `fcntl.flock` inmediato en `gestor_archivos_acq.py`. |
| **R2** | Desgaste prematuro o corrupción de la tarjeta microSD por escritura continua en swap durante livelock. | **Media** | **Alto** | Configurar límites estrictos de swapiness y watchdog de memoria. |
| **R3** | Saturación del enlace WAN corporativo por ráfagas ininterrumpidas de MiniSEED afectando otros servicios de red. | **Alta** | **Medio** | Dosificación por lotes (*batch size max = 10 archivos/hora*). |
| **R4** | Imposibilidad de reiniciar remotamente estaciones colgadas sin acceso físico. | **Media** | **Crítico** | Añadir handler MQTT nativo `cmd/reboot_system` en `mqtt_coordinator.py`. |

---

## 6. Opciones y Decisiones de Arquitectura

### Decisión 1: Control de Concurrencia en la Sincronización a Google Drive

| Opción | Descripción | Ventajas | Desventajas |
|---|---|---|---|
| **A: File Lock Atómico en Python (`fcntl.flock`)** | Bloquear un archivo testigo (`/tmp/gestor_acq.lock`) en modo no bloqueante (`LOCK_EX \| LOCK_NB`). Si ya está bloqueado, salir con `sys.exit(0)`. | • Robusto y autocontenido.<br>• El lock se libera automáticamente si el proceso muere o cae por OOM.<br>• Cero dependencias externas. | Ninguna relevante. |
| **B: Validación de proceso por shell (`pgrep`) en `registrocontinuo.sh`** | Usar `pgrep -f gestor_archivos_acq.py` antes de ejecutar la línea con `&`. | • No requiere tocar el script de Python. | Vulnerable a condiciones de carrera y no protege si el gestor se ejecuta desde otra vía. |

> **Recomendación**: **Implementar Opción A como mecanismo primario** en `gestor_archivos_acq.py` reforzada con **Opción B** en `registrocontinuo.sh`.

---

### Decisión 2: Política de Evacuación de Colas Masivas (Store & Forward Dosificado)

| Opción | Descripción | Ventajas | Desventajas |
|---|---|---|---|
| **A: Lote Acotado por Corrida (`max_batch_size`)** | Subir como máximo $N$ archivos (ej. 10 archivos continuos + 5 eventos) por cada ciclo horario. | • Evita saturar la memoria y el ancho de banda.<br>• Garantiza que la ejecución termine en menos de 10 minutos.<br>• Deja el sistema libre para SSH y telemetría. | La cola grande tarda varios días en evacuarse por completo. |
| **B: Subida Continua Exhaustiva (Actual)** | Intentar subir todos los archivos pendientes en un único bucle hasta agotar la cola. | • Si la red es veloz, vacía la cola más rápido. | Provoca colapso de memoria y red si la cola supera los 50 archivos. |

> **Recomendación**: **Adoptar Opción A** configurando `max_batch_size: 15` en `configuracion_dispositivo.json`.

---

## 7. Mitigaciones Aplicadas Durante la Sesión

1. **Detención Parcial vía Telemetría MQTT**:
   * Se emitió la orden `cmd/stop_acquisition_safety` hacia `CHA02`, la cual ejecutó `sudo systemctl stop rsa-acelerografo.service`, logrando una descompresión inmediata del 2% de RAM (de 98.5% a 96.5%).
2. **Contención del Flujo de Datos en Google Drive (Nube)**:
   * Se trasladó temporalmente la carpeta de destino de `CHA02` en Google Drive a la **Papelera**.
   * Esto provocó que las peticiones de subida de la API fallaran de forma instantánea ($<100\text{ ms}$) sin transferir datos masivos, cortando la saturación de subida (*uplink*) y logrando que los sockets TCP de los puertos 22 y 5000 volvieran a reportar estado `ABIERTO` en pruebas de red local desde `concentrador-CON00`.
3. **Planificación de Intervención Presencial**:
   * Ante la irreversibilidad del livelock de memoria en los procesos del espacio de usuario, se programó visita presencial a la estación para la semana entrante para realizar un ciclo de energía físico limpio (*power cycle*).

---

## 8. Backlog de Mejoras Técnicas

1. **Incorporación de Mutex / Lock Atómico (`gestor_archivos_acq.py`)**:
   Implementar al inicio de la función principal `main()`:
   ```python
   import fcntl, sys
   lock_file = open('/tmp/gestor_archivos_acq.lock', 'w')
   try:
       fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
   except BlockingIOError:
       logger.warning("[SKIP] Ya existe otra instancia de gestor_archivos_acq en ejecución. Saliendo.")
       sys.exit(0)
   ```
2. **Control de Lote Máximo (*Batch Limit*)**:
   Limitar la lista de archivos a procesar por ciclo en `gestor_archivos_acq.py`:
   `archivos_a_subir = archivos_pendientes[:max_batch_files]`.
3. **Bandera de Mantenimiento en `registrocontinuo.sh`**:
   No iniciar `rsa-acelerografo.service` si existe el archivo de bloqueo `/var/run/acq_stop.flag` creado por el comando MQTT `stop_acquisition_safety`.
4. **Comando MQTT de Reinicio de Emergencia (`cmd/reboot_system`)**:
   Registrar en `CommandDispatcher` de [`mqtt_coordinator.py`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/mqtt/mqtt_coordinator.py):
   ```python
   def _cmd_reboot_system(self, payload: dict, client) -> dict:
       subprocess.Popen(["sudo", "/sbin/reboot", "-f"])
       return {"status": "accepted", "action": "reboot_system"}
   ```
5. **Watchdog de Memoria en Supervisor / Systemd**:
   Script o servicio periódico que monitoree el porcentaje de RAM disponible; si supera el 95% de forma sostenida por más de 15 minutos, ejecute una purga de caches (`drop_caches`) o reinicio programado de daemons secundarios.

---

## 9. Dependencias y Prerrequisitos para la Intervención

| Prerrequisito | Estado | Acción Requerida |
|---|---|---|
| **Acceso Físico a la Estación `CHA02`** | ⬜ Programado | Visita presencial planificada para la próxima semana por el operador. |
| **Reinicio Físico Limpio** | ⬜ Pendiente | Desconectar y reconectar alimentación para evacuar la memoria RAM y swap. |
| **Restauración de Carpeta en Google Drive** | ⬜ Pendiente | Restaurar la carpeta `CHA02` de la papelera una vez reiniciada la estación. |
| **Desactivación Preventiva del Cron** | ⬜ Por ejecutar | Comentar temporalmente la línea horaria de `crontab -e` tras arrancar para evitar saturación antes de desplegar mejoras. |

---

## 10. Plan de Validación Post-Reinicio

| # | Checkpoint | Criterio de Éxito |
|---|---|---|
| **CP-1** | Arranque del Sistema Operativo | RAM utilizada $< 35\%$ tras reinicio (`free -m`). |
| **CP-2** | Acceso SSH Restaurado | Conexión interactiva inmediata sin timeouts vía `ssh CHA02`. |
| **CP-3** | Telemetría MQTT Reactivada | Recepción de latido `online` y métricas `telemetry/health` en broker. |
| **CP-4** | Adquisición SPI Nominal | `systemctl status rsa-acelerografo` en estado `active (running)` y generación de tramas. |
| **CP-5** | Evacuación Dosificada a Drive | Subida escalonada de los 761 archivos pendientes sin picos de RAM superiores al 60%. |
