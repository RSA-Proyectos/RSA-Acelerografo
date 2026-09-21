# mqtt_coordinator.py - Agente Reactivo MQTT

import os
import json
import time
import threading
import subprocess
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import paho.mqtt.client as mqtt

# Importar StructuredLogger del proyecto
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from structured_logger import StructuredLogger

# Importar orquestador de extracción de eventos
from event_extractor import extraer_y_subir_evento

# Importar EventLogger para registro CSV de detecciones sísmicas
from core.event_logger import EventLogger

# Importar auditores especializados (adquisición, sensor físico y Google Drive)
from mqtt.acquisition_watchdog import AcquisitionWatchdog
try:
    from mqtt.sensor_watchdog import SensorWatchdog
    from mqtt.drive_watchdog import DriveWatchdog
except ImportError:
    from sensor_watchdog import SensorWatchdog
    from drive_watchdog import DriveWatchdog

# ═══════════════════════════════════════════════════════════════════════════
# CONFIGURACIÓN Y CONSTANTES
# ═══════════════════════════════════════════════════════════════════════════

HEALTH_INTERVAL = 300    # segundos (5 minutos)
ACQUISITION_CHECK_INTERVAL = 300  # segundos entre chequeos (unificado a 5 minutos)
DAILY_REPUBLISH_HOUR = 0  # Hora para re-publicar estado diario (00:00)
START_TIME = time.time()

# ═══════════════════════════════════════════════════════════════════════════
# UTILIDADES
# ═══════════════════════════════════════════════════════════════════════════

def cargar_configuracion(config_path: str, env_path: str) -> dict:
    """Carga configuración JSON y credenciales desde .env"""
    load_dotenv(env_path)
    
    with open(config_path, 'r') as f:
        config = json.load(f)
    
    config["broker"] = {
        "address": os.getenv("MQTT_BROKER"),
        "port": int(os.getenv("MQTT_PORT", 1883)),
        "username": os.getenv("MQTT_USERNAME"),
        "password": os.getenv("MQTT_PASSWORD")
    }
    return config

def resolver_topico(config: dict, topic_key: str, **kwargs) -> str:
    """Resuelve template de tópico con valores de configuración."""
    template = config["topics"][topic_key]
    return template.format(
        org=config["org"],
        app=config["app"],
        cap=config["cap"],
        id=config["id"],
        **kwargs
    )

def timestamp_iso() -> str:
    """Retorna timestamp actual en formato ISO8601 UTC."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def guardar_estado(estado: str, timestamp: str, state_file_path: str, logger: StructuredLogger):
    """Guarda el estado y timestamp en un archivo JSON local, manteniendo los 3 estados."""
    try:
        data = {}
        if os.path.exists(state_file_path) and os.path.getsize(state_file_path) > 0:
            with open(state_file_path, 'r') as f:
                data = json.load(f)
        
        # Actualizar solo el timestamp del estado correspondiente
        data[estado] = timestamp
        
        with open(state_file_path, 'w') as f:
            json.dump(data, f, indent=4)
        logger.info(f"[STATE_SAVE] Estado '{estado}' persistido localmente.")
    except Exception as e:
        logger.error(f"[STATE_SAVE_ERR] No se pudo guardar estado: {e}")

# ═══════════════════════════════════════════════════════════════════════════
# DISPATCHER DE COMANDOS
# ═══════════════════════════════════════════════════════════════════════════

class CommandDispatcher:
    """Centraliza el manejo de comandos recibidos via MQTT."""
    
    def __init__(self, config: dict, logger: StructuredLogger, event_logger=None, watchdog=None, sensor_watchdog=None, drive_watchdog=None):
        self.config = config
        self.logger = logger
        self.event_logger = event_logger  # EventLogger para CSV de detecciones
        self.watchdog = watchdog          # AcquisitionWatchdog para latencia del Ring Buffer
        self.sensor_watchdog = sensor_watchdog  # SensorWatchdog para integridad triaxial/reloj
        self.drive_watchdog = drive_watchdog    # DriveWatchdog para auditoría Drive/disco
        self.handlers = {
            "restart_acquisition": self._cmd_restart_acquisition,
            "stop_acquisition_safety": self._cmd_stop_acquisition_safety,
            "cleanup_files": self._cmd_cleanup_files,
            "get_status": self._cmd_get_status,
            "get_acquisition_status": self._cmd_get_acquisition_status,
            "get_sensor_status": self._cmd_get_sensor_status,
            "get_drive_status": self._cmd_get_drive_status,
            "extract_event": self._cmd_extract_event,
        }
    
    def dispatch(self, task_name: str, payload: dict, client) -> dict:
        """Ejecuta el handler correspondiente al comando."""
        handler = self.handlers.get(task_name)
        if handler:
            self.logger.info(f"[CMD_DISPATCH] Ejecutando: {task_name}")
            return handler(payload, client)
        else:
            self.logger.warning(f"[CMD_UNKNOWN] Comando no reconocido: {task_name}")
            return {"status": "error", "message": f"Unknown command: {task_name}"}
    
    def _cmd_restart_acquisition(self, payload: dict, client) -> dict:
        """Reinicia el proceso de adquisición."""
        # TODO: Implementar interacción con capa de adquisición
        return {"status": "pending", "message": "Not implemented yet"}

    def _cmd_stop_acquisition_safety(self, payload: dict, client) -> dict:
        """
        Detiene ordenadamente el servicio systemd de adquisición por contingencia de seguridad
        (p. ej. si se detecta daño físico permanente en el acelerómetro para no inundar el disco ni disparar falsas alarmas).
        """
        self.logger.warning("[CMD_SAFETY] Recibida orden de parada de seguridad de adquisición continua.")
        try:
            res = subprocess.run(
                ["sudo", "systemctl", "stop", "rsa-acelerografo.service"],
                capture_output=True,
                text=True,
                timeout=15
            )
            if res.returncode == 0:
                self.logger.warning("[CMD_SAFETY] Servicio rsa-acelerografo.service detenido exitosamente.")
                return {
                    "status": "completed",
                    "action": "stop_acquisition_safety",
                    "message": "Servicio rsa-acelerografo detenido ordenadamente.",
                    "timestamp": timestamp_iso()
                }
            else:
                stderr = res.stderr.strip() or "Código de retorno no cero al detener servicio"
                self.logger.error(f"[CMD_SAFETY_ERR] Falló detención: {stderr}")
                return {
                    "status": "error",
                    "action": "stop_acquisition_safety",
                    "message": stderr,
                    "timestamp": timestamp_iso()
                }
        except subprocess.TimeoutExpired:
            self.logger.error("[CMD_SAFETY_ERR] Timeout al ejecutar systemctl stop rsa-acelerografo.service")
            return {
                "status": "error",
                "action": "stop_acquisition_safety",
                "message": "Timeout ejecutando systemctl stop",
                "timestamp": timestamp_iso()
            }
        except Exception as exc:
            self.logger.error(f"[CMD_SAFETY_ERR] Excepción en parada de seguridad: {exc}")
            return {
                "status": "error",
                "action": "stop_acquisition_safety",
                "message": str(exc),
                "timestamp": timestamp_iso()
            }
    
    def _cmd_cleanup_files(self, payload: dict, client) -> dict:
        """Limpieza de archivos temporales."""
        # TODO: Implementar limpieza
        return {"status": "pending", "message": "Not implemented yet"}
    
    def _cmd_get_status(self, payload: dict, client) -> dict:
        """Retorna estado actual del sistema."""
        return {
            "status": "ok",
            "uptime_s": int(time.time() - START_TIME),
            "timestamp": timestamp_iso()
        }

    def _cmd_get_acquisition_status(self, payload: dict, client) -> dict:
        """Retorna el estado de latencia y salud de adquisición bajo demanda."""
        if self.watchdog is not None:
            return self.watchdog.evaluar_salud(station_id=self.config["id"])
        return {
            "status": "error",
            "reason": "watchdog_not_available",
            "timestamp": timestamp_iso()
        }

    def _cmd_get_sensor_status(self, payload: dict, client) -> dict:
        """Retorna el estado de integridad física del sensor bajo demanda."""
        if self.sensor_watchdog is not None:
            return self.sensor_watchdog.evaluar_integridad(station_id=self.config["id"])
        return {
            "status": "error",
            "reason": "sensor_watchdog_not_available",
            "timestamp": timestamp_iso()
        }

    def _cmd_get_drive_status(self, payload: dict, client) -> dict:
        """Retorna el estado de sincronización de Google Drive bajo demanda."""
        if self.drive_watchdog is not None:
            return self.drive_watchdog.evaluar_sincronizacion(station_id=self.config["id"])
        return {
            "status": "error",
            "reason": "drive_watchdog_not_available",
            "timestamp": timestamp_iso()
        }

    def _cmd_extract_event(self, payload: dict, client) -> None:
        """
        Extrae un evento sísmico y opcionalmente lo sube a Drive.

        Publica un ACK inmediato ('accepted') y delega el pipeline
        de extracción+subida a un hilo separado para no bloquear
        el loop MQTT.

        Payload esperado:
            start (str, requerido):               Tiempo inicio ISO UTC con 'Z'
            duration (float, requerido):           Duración en segundos
            upload (bool, opcional, default True): Sube a Drive tras extraer
            delete_after_upload (bool, opcional):  Borra local tras subida
            request_id (str, opcional):            ID de rastreo

        Returns:
            None — la respuesta se publica manualmente en el hilo.
        """
        # Validar campos requeridos
        start = payload.get("start")
        duration = payload.get("duration")

        if not start or duration is None:
            res_topic = resolver_topico(self.config, "cmd_response", task_name="extract_event")
            client.publish(res_topic, json.dumps({
                "status": "error",
                "message": "Campos requeridos: 'start' (str) y 'duration' (float)"
            }), qos=1)
            return None

        request_id = payload.get("request_id", f"auto-{timestamp_iso()}")
        upload = payload.get("upload", True)
        delete_after = payload.get("delete_after_upload", False)

        # ACK inmediato
        res_topic = resolver_topico(self.config, "cmd_response", task_name="extract_event")
        client.publish(res_topic, json.dumps({
            "status": "accepted",
            "request_id": request_id,
            "timestamp": timestamp_iso(),
            "message": "Extracción encolada"
        }), qos=1)

        self.logger.info(f"[EXTRACT_EVENT] Solicitud aceptada → request_id={request_id}, start={start}, duration={duration}s")

        # Ejecutar pipeline en hilo separado
        hilo = threading.Thread(
            target=self._run_extraction_pipeline,
            args=(client, request_id, start, duration, upload, delete_after),
            daemon=True
        )
        hilo.start()

        return None  # on_message verifica None antes de publicar

    def _run_extraction_pipeline(self, client, request_id, start, duration, upload, delete_after):
        """Pipeline de extracción + subida ejecutado en hilo separado."""
        res_topic = resolver_topico(self.config, "cmd_response", task_name="extract_event")

        try:
            resultado = extraer_y_subir_evento(
                start=start,
                duration=duration,
                upload=upload,
                delete_after_upload=delete_after,
                logger=self.logger
            )
            resultado["request_id"] = request_id
            resultado["timestamp"] = timestamp_iso()
            client.publish(res_topic, json.dumps(resultado), qos=1)

            # Actualizar CSV si la extracción fue exitosa (disparada por comando de red externo)
            if resultado.get("status") == "completed" and self.event_logger is not None:
                archivo = resultado.get("output_file", "")
                actualizado = self.event_logger.actualizar_confirmacion(
                    timestamp_centro=start,
                    confirmado=True,
                    archivo_mseed=archivo,
                )
                if not actualizado:
                    # No hay detección local previa → registrar como evento externo de red
                    self.event_logger.registrar_evento_externo(
                        timestamp_centro=start,
                        archivo_mseed=archivo,
                    )

            self.logger.info(
                f"[EXTRACT_EVENT] Pipeline finalizado → "
                f"status={resultado['status']}, archivo={resultado.get('output_file')}"
            )

        except Exception as e:
            self.logger.error(f"[EXTRACT_EVENT_ERR] Excepción inesperada en pipeline: {e}")
            client.publish(res_topic, json.dumps({
                "status": "error",
                "request_id": request_id,
                "timestamp": timestamp_iso(),
                "phase": "pipeline",
                "message": str(e)
            }), qos=1)

# ═══════════════════════════════════════════════════════════════════════════
# CORRELACIÓN DE EVENTOS (PLACEHOLDER)
# ═══════════════════════════════════════════════════════════════════════════

class EventCorrelator:
    """Placeholder para lógica de correlación regional."""
    
    def __init__(self, config: dict, logger: StructuredLogger):
        self.config = config
        self.logger = logger
        self.recent_events = []  # Buffer de eventos recientes
    
    def on_regional_event(self, station_id: str, event_data: dict):
        """Procesa detección de otra estación."""
        self.logger.info(f"[EVENT_REGIONAL] Recibido de {station_id}")
        self.recent_events.append({
            "station": station_id,
            "data": event_data,
            "received_at": timestamp_iso()
        })
        # TODO: Implementar lógica de correlación con módulo externo

# ═══════════════════════════════════════════════════════════════════════════
# PUBLICACIÓN DE TELEMETRÍA
# ═══════════════════════════════════════════════════════════════════════════

def publicar_state(client, config: dict, estado: str, logger: StructuredLogger, timestamp_override: str = None):
    """Publica estado operacional (online/offline/on). Retorna la información del mensaje (MQTTMessageInfo)."""
    topic = resolver_topico(config, "telemetry_state")
    ts = timestamp_override if timestamp_override else timestamp_iso()
    payload = {"status": estado, "timestamp": ts}
    qos = config["qos"]["telemetry"]
    retain = config["retain"]["telemetry_state"]
    result = client.publish(topic, json.dumps(payload), qos=qos, retain=retain)
    logger.mqtt_publish(topic, "ok" if result.rc == 0 else "fail")
    return result

def obtener_metricas_hardware() -> dict:
    """Obtiene métricas de hardware de Raspberry Pi."""
    metricas = {}
    
    # 1. Porcentaje de disco en uso
    try:
        statvfs = os.statvfs('/')
        total = statvfs.f_blocks * statvfs.f_frsize
        free = statvfs.f_bavail * statvfs.f_frsize
        metricas["disk_percent"] = round(((total - free) / total) * 100, 1)
    except:
        metricas["disk_percent"] = -1
    
    # 2. Porcentaje de RAM en uso
    try:
        with open('/proc/meminfo', 'r') as f:
            mem_total = None
            mem_available = None
            for line in f:
                if line.startswith('MemTotal:'):
                    mem_total = int(line.split()[1])
                elif line.startswith('MemAvailable:'):
                    mem_available = int(line.split()[1])
                if mem_total and mem_available:
                    break
        if mem_total and mem_available:
            metricas["ram_percent"] = round(((mem_total - mem_available) / mem_total) * 100, 1)
        else:
            metricas["ram_percent"] = -1
    except:
        metricas["ram_percent"] = -1
    
    # 3. Load average 15 minutos
    try:
        metricas["load_avg_15m"] = round(os.getloadavg()[2], 2)
    except:
        metricas["load_avg_15m"] = -1
    
    # 4. Temperatura CPU (Raspberry Pi)
    try:
        import subprocess
        result = subprocess.run(
            ['vcgencmd', 'measure_temp'],
            capture_output=True, text=True, timeout=5
        )
        # Formato: temp=52.3'C
        temp_str = result.stdout.strip().split('=')[1].split("'")[0]
        metricas["cpu_temp_c"] = float(temp_str)
    except:
        metricas["cpu_temp_c"] = -1
    
    # 5. Estado de throttling (Raspberry Pi)
    try:
        import subprocess
        result = subprocess.run(
            ['vcgencmd', 'get_throttled'],
            capture_output=True, text=True, timeout=5
        )
        # Formato: throttled=0x0
        throttled_hex = result.stdout.strip().split('=')[1]
        metricas["throttled"] = throttled_hex
    except:
        metricas["throttled"] = "unknown"
    
    return metricas

def publicar_health(client, config: dict, logger: StructuredLogger):
    """Publica métricas de hardware cada HEALTH_INTERVAL segundos."""
    topic = resolver_topico(config, "telemetry_health")
    
    metricas = obtener_metricas_hardware()
    metricas["timestamp"] = timestamp_iso()
    metricas["uptime_s"] = int(time.time() - START_TIME)
    
    qos = config["qos"]["telemetry"]
    retain = config["retain"]["telemetry_health"]
    client.publish(topic, json.dumps(metricas), qos=qos, retain=retain)

def publicar_acquisition_status(client, config: dict, watchdog: AcquisitionWatchdog, logger: StructuredLogger):
    """Publica estado de frescura y latencia de adquisición en MQTT cada 300 segundos."""
    try:
        topic = resolver_topico(config, "status_acquisition")
    except KeyError:
        template = "{org}/{app}/{cap}/{id}/status/acquisition"
        topic = template.format(
            org=config.get("org", "rsa"),
            app=config.get("app", "seismic"),
            cap=config.get("cap", "smart"),
            id=config.get("id", "UNKNOWN")
        )

    payload = watchdog.evaluar_salud(station_id=config["id"])
    qos = config.get("qos", {}).get("telemetry", 1)
    retain = config.get("retain", {}).get("status_acquisition", True)
    client.publish(topic, json.dumps(payload), qos=qos, retain=retain)

    status = payload.get("status")
    if status == "warning":
        logger.warning(
            f"[ACQUISITION_STALE] Adquisición estancada: última trama hace "
            f"{payload.get('age_seconds')}s (umbral: {payload.get('threshold_seconds')}s)"
        )
    elif status == "error":
        logger.error(
            f"[ACQUISITION_ERROR] Error evaluando adquisición: {payload.get('reason')}"
        )
    else:
        logger.info(
            f"[ACQUISITION_OK] Adquisición nominal: age={payload.get('age_seconds')}s"
        )

def publicar_sensor_status(client, config: dict, sensor_watchdog: SensorWatchdog, logger: StructuredLogger):
    """Publica estado de integridad física del sensor y reloj en MQTT cada 300 segundos."""
    try:
        topic = resolver_topico(config, "status_sensor")
    except KeyError:
        template = "{org}/{app}/{cap}/{id}/status/sensor"
        topic = template.format(
            org=config.get("org", "rsa"),
            app=config.get("app", "seismic"),
            cap=config.get("cap", "smart"),
            id=config.get("id", "UNKNOWN")
        )

    payload = sensor_watchdog.evaluar_integridad(station_id=config["id"])
    qos = config.get("qos", {}).get("telemetry", 1)
    retain = config.get("retain", {}).get("status_sensor", True)
    client.publish(topic, json.dumps(payload), qos=qos, retain=retain)

    status = payload.get("status")
    if status == "ok":
        logger.info(
            f"[SENSOR_OK] Integridad nominal: ax={payload.get('ax')} "
            f"ay={payload.get('ay')} az={payload.get('az')} "
            f"clock={payload.get('clock_source')}"
        )
    else:
        logger.warning(
            f"[SENSOR_ANOMALY] Anomalía en sensor/reloj: {payload.get('reason')} "
            f"({payload.get('clock_error')})"
        )

def publicar_drive_status(client, config: dict, drive_watchdog: DriveWatchdog, logger: StructuredLogger):
    """Publica estado de sincronización con Google Drive y espacio en disco en MQTT cada 300 segundos."""
    try:
        topic = resolver_topico(config, "status_drive")
    except KeyError:
        template = "{org}/{app}/{cap}/{id}/status/drive"
        topic = template.format(
            org=config.get("org", "rsa"),
            app=config.get("app", "seismic"),
            cap=config.get("cap", "smart"),
            id=config.get("id", "UNKNOWN")
        )

    payload = drive_watchdog.evaluar_sincronizacion(station_id=config["id"])
    qos = config.get("qos", {}).get("telemetry", 1)
    retain = config.get("retain", {}).get("status_drive", True)
    client.publish(topic, json.dumps(payload), qos=qos, retain=retain)

    status = payload.get("status")
    if status == "ok":
        logger.info(
            f"[DRIVE_SYNC_OK] Sincronización nominal: pendientes={payload.get('pending_mseed')}, "
            f"protegidos={payload.get('failed_uploads_protected')}, "
            f"disco_libre={payload.get('free_disk_percent')}%"
        )
    else:
        logger.warning(
            f"[DRIVE_WARN] Alerta en sincronización: {payload.get('reason')} "
            f"(pendientes={payload.get('pending_mseed')}, "
            f"protegidos={payload.get('failed_uploads_protected')})"
        )

# ═══════════════════════════════════════════════════════════════════════════
# CALLBACKS MQTT
# ═══════════════════════════════════════════════════════════════════════════

def on_publish(client, userdata, mid, *args, **kwargs):
    """Callback invocado cuando el broker confirma la recepción (para QoS > 0)."""
    # Delegamos la validación QoS 1 a la librería Paho-MQTT nativa
    pass

def on_connect(client, userdata, flags, rc, properties=None):
    """Callback de conexión al broker."""
    logger = userdata["logger"]
    config = userdata["config"]
    
    if rc == 0:
        logger.mqtt_connect(config["broker"]["address"], "ok")
        userdata["is_connected"] = True
        
        # Publicar estado online diferido si es el primer arranque
        state_file = userdata["state_file_path"]
        boot_ts = None
        if not userdata["boot_published"]:
            try:
                if os.path.exists(state_file) and os.path.getsize(state_file) > 0:
                    with open(state_file, 'r') as f:
                        saved_data = json.load(f)
                        boot_ts = saved_data.get("on")
                
                if boot_ts:
                    logger.info(f"[BOOT_SYNC] Publicando estado 'on' diferido de {boot_ts}")
                    publicar_state(client, config, "on", logger, timestamp_override=boot_ts)
                
                userdata["boot_published"] = True
            except Exception as e:
                logger.error(f"[BOOT_SYNC_ERR] Error leyendo {state_file}: {e}")

        # Publicar 'online' de forma prioritaria inmediatamente al conectar
        now_ts = timestamp_iso()
        msg_info = publicar_state(client, config, "online", logger, timestamp_override=now_ts)
        
        if msg_info.rc == mqtt.MQTT_ERR_SUCCESS:
            logger.info(f"[CONNECT_SENT] Publish 'online' encolado (mid={msg_info.mid}). Delegando a Paho-MQTT.")
            guardar_estado("online", now_ts, userdata["state_file_path"], logger)
            userdata["last_state_change"] = now_ts
            userdata["is_disconnected_logged"] = False

        # Suscribirse a tópicos configurados
        for sub_key in config["subscriptions"]:
            topic = resolver_topico(config, sub_key)
            client.subscribe(topic, qos=config["qos"].get("commands", 1))
            logger.mqtt_subscribe(topic, 1)
    else:
        logger.mqtt_error("connect", f"Código de error: {rc}")

def on_disconnect(client, userdata, flags, rc=None, properties=None):
    """Callback de desconexión del broker."""
    logger = userdata["logger"]
    userdata["is_connected"] = False
    # En v1, flags es el código de retorno (rc). En v2, rc es el reason_code.
    real_rc = rc if rc is not None else flags
    
    if real_rc != 0 and not userdata.get("is_disconnected_logged", False):
        logger.mqtt_disconnect(f"Inesperada, código: {real_rc}")
        
        # Reporte offline reactivo (inmediato)
        now_ts = timestamp_iso()
        guardar_estado("offline", now_ts, userdata["state_file_path"], logger)
        userdata["last_state_change"] = now_ts
        userdata["is_disconnected_logged"] = True


# ═══════════════════════════════════════════════════════════════════════════
# HANDLER DE DETECCIÓN GPD LOCAL
# ═══════════════════════════════════════════════════════════════════════════

def _manejar_deteccion_gpd_local(client, userdata: dict, payload: dict) -> None:
    """
    Maneja una detección GPD local publicada por gpd_stream_worker.

    Solo actúa en modo ONLINE. En modo offline, el worker ya extrajo directamente
    sin pasar por MQTT, por lo que este handler simplemente ignora el mensaje.

    Flujo:
    1. Validar payload (timestamp, type, probability).
    2. Verificar modo de adquisición (solo actúa en 'online').
    3. Verificar que auto_extract esté habilitado en configuración GPD.
    4. Calcular rango de extracción: start = timestamp - ventana_pre, duration = pre + post.
    5. Publicar ACK inmediato en cmd_response.
    6. Lanzar _run_gpd_extraction_pipeline() en hilo separado.
    """
    logger = userdata["logger"]
    device_config = userdata.get("device_config", {})
    event_logger = userdata.get("event_logger")
    config = userdata["config"]

    # 1. Validar payload mínimo
    timestamp_str = payload.get("timestamp")
    phase_type = payload.get("type")
    probability = payload.get("probability", 0.0)

    if not timestamp_str or not phase_type:
        logger.warning(
            f"[GPD_LOCAL] Payload inválido (faltan 'timestamp' o 'type'): {payload}"
        )
        return

    # 2. Verificar modo de adquisición — solo actuar en modo online
    modo = device_config.get("dispositivo", {}).get("modo_adquisicion", "online")
    if modo == "offline":
        logger.info(
            "[GPD_LOCAL] Modo offline — extracción gestionada por el worker directamente."
        )
        return

    # 3. Verificar auto_extract en configuración GPD
    gpd_config = device_config.get("streaming", {}).get("gpd", {})
    if not gpd_config.get("auto_extract", True):
        logger.info(
            f"[GPD_LOCAL] auto_extract deshabilitado — detección {phase_type} "
            f"(prob={probability}) registrada en CSV pero no extraída."
        )
        return

    # 4. Calcular ventana de extracción
    ventana_pre = int(gpd_config.get("ventana_pre_evento_s", 60))
    ventana_post = int(gpd_config.get("ventana_post_evento_s", 60))
    auto_upload = gpd_config.get("auto_upload", True)

    try:
        dt_centro = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
    except ValueError as exc:
        logger.warning(f"[GPD_LOCAL] Timestamp inválido '{timestamp_str}': {exc}")
        return

    dt_start = dt_centro - timedelta(seconds=ventana_pre)
    start_str = dt_start.strftime("%Y-%m-%dZ%H:%M:%S.%f")[:-3]
    duration = ventana_pre + ventana_post
    request_id = f"gpd-auto-{timestamp_str}"

    logger.info(
        f"[GPD_LOCAL_EXTRACT] Detección {phase_type} (prob={probability:.4f}) → "
        f"start={start_str} duration={duration}s upload={auto_upload} req={request_id}"
    )

    # 5. ACK inmediato (trazabilidad en tópico de respuesta)
    res_topic = resolver_topico(config, "cmd_response", task_name="extract_event")
    client.publish(res_topic, json.dumps({
        "status": "accepted",
        "request_id": request_id,
        "timestamp": timestamp_iso(),
        "message": "Extracción GPD automática encolada",
        "source": "gpd_auto",
    }), qos=1)

    # 6. Pipeline en hilo separado (no bloquea el loop MQTT)
    hilo = threading.Thread(
        target=_run_gpd_extraction_pipeline,
        args=(client, userdata, request_id, start_str, duration,
              auto_upload, timestamp_str, phase_type, event_logger),
        daemon=True,
    )
    hilo.start()


def _run_gpd_extraction_pipeline(
    client,
    userdata: dict,
    request_id: str,
    start: str,
    duration: float,
    upload: bool,
    timestamp_centro: str,
    phase_type: str,
    event_logger,
) -> None:
    """
    Pipeline de extracción GPD automática ejecutado en hilo separado.

    Invoca extraer_y_subir_evento() y actualiza el CSV de detecciones:
    - Si la extracción es exitosa, actualiza confirmado=True en el registro
      que el worker GPD registró previamente (buscando por timestamp_centro).
    - Si no encuentra el registro (condición de carrera improbable), registra
      la detección como confirmada directamente.
    """
    logger = userdata["logger"]
    config = userdata["config"]
    res_topic = resolver_topico(config, "cmd_response", task_name="extract_event")

    try:
        resultado = extraer_y_subir_evento(
            start=start,
            duration=duration,
            upload=upload,
            delete_after_upload=False,
            logger=logger,
        )

        resultado["request_id"] = request_id
        resultado["timestamp"] = timestamp_iso()
        resultado["source"] = "gpd_auto"
        client.publish(res_topic, json.dumps(resultado), qos=1)

        if resultado.get("status") == "completed" and event_logger is not None:
            archivo = resultado.get("output_file", "")
            # Actualizar el CSV: confirmar la detección del worker
            actualizado = event_logger.actualizar_confirmacion(
                timestamp_centro=timestamp_centro,
                confirmado=True,
                archivo_mseed=archivo,
            )
            if not actualizado:
                # Condición de carrera improbable: worker aún no escribió en CSV.
                # Registrar como confirmado directamente para no perder el evento.
                event_logger.registrar_deteccion(
                    timestamp_centro=timestamp_centro,
                    fase=phase_type,
                    probabilidad=0.0,
                    confirmado=True,
                    archivo_mseed=archivo,
                    metodo="local_gpd",
                )
            logger.info(
                f"[GPD_LOCAL_OK] Extracción GPD completada → "
                f"archivo={archivo} req={request_id}"
            )
        elif resultado.get("status") != "completed":
            logger.warning(
                f"[GPD_LOCAL_FAIL] Extracción GPD fallida → {resultado.get('message')}"
            )

    except Exception as exc:  # pylint: disable=broad-except
        logger.error(f"[GPD_LOCAL_ERROR] Excepción en pipeline GPD: {exc}")
        client.publish(res_topic, json.dumps({
            "status": "error",
            "request_id": request_id,
            "timestamp": timestamp_iso(),
            "phase": "pipeline",
            "source": "gpd_auto",
            "message": str(exc),
        }), qos=1)


def on_message(client, userdata, msg):
    """Callback para mensajes recibidos."""
    logger = userdata["logger"]
    config = userdata["config"]
    dispatcher = userdata["dispatcher"]
    correlator = userdata["correlator"]
    
    topic = msg.topic
    try:
        payload = json.loads(msg.payload.decode())
    except json.JSONDecodeError:
        logger.mqtt_error("parse", f"JSON inválido en {topic}")
        return
    
    # Detectar tipo de mensaje por el tópico
    if "/cmd/" in topic:
        # Extraer nombre del comando del tópico
        task_name = topic.split("/cmd/")[-1]
        response = dispatcher.dispatch(task_name, payload, client)

        # Publicar respuesta solo si el handler no la publicó por sí mismo
        # (los handlers que gestionan su propia publicación retornan None)
        if response is not None:
            res_topic = resolver_topico(config, "cmd_response", task_name=task_name)
            client.publish(res_topic, json.dumps(response), qos=1)
        
    elif "/events/detected" in topic and config["id"] not in topic:
        # Evento de otra estación (correlación regional)
        station_id = topic.split("/")[3]  # Extraer ID de estación
        correlator.on_regional_event(station_id, payload)

    elif "/events/detected" in topic and config["id"] in topic:
        # Detección GPD local — evaluar extracción automática (modo online)
        _manejar_deteccion_gpd_local(client, userdata, payload)

    elif "/config/set" in topic:
        # Configuración dinámica (placeholder)
        logger.info(f"[CONFIG_SET] Recibido: {payload}")

# ═══════════════════════════════════════════════════════════════════════════
# INICIALIZACIÓN Y LOOP PRINCIPAL
# ═══════════════════════════════════════════════════════════════════════════

def iniciar_cliente(config: dict, logger: StructuredLogger, userdata: dict):
    """Inicializa y configura el cliente MQTT."""
    event_logger = userdata.get("event_logger")
    watchdog = userdata.get("watchdog")
    sensor_watchdog = userdata.get("sensor_watchdog")
    drive_watchdog = userdata.get("drive_watchdog")
    dispatcher = CommandDispatcher(
        config, logger,
        event_logger=event_logger,
        watchdog=watchdog,
        sensor_watchdog=sensor_watchdog,
        drive_watchdog=drive_watchdog
    )
    correlator = EventCorrelator(config, logger)

    userdata.update({
        "dispatcher": dispatcher,
        "correlator": correlator,
    })
    
    try:
        # Soporta Paho-MQTT v2.x con Callback API Version 2
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, userdata=userdata)
    except AttributeError:
        # Soporta Paho-MQTT v1.x
        client = mqtt.Client(userdata=userdata)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.on_publish = on_publish
    
    # Configurar LWT
    lwt_topic = resolver_topico(config, "telemetry_state")
    lwt_payload = json.dumps({"status": "offline", "timestamp": timestamp_iso()})
    client.will_set(lwt_topic, lwt_payload, qos=1, retain=True)
    
    # Conectar
    broker = config["broker"]
    client.username_pw_set(broker["username"], broker["password"])
    
    retry_delay = 2
    while True:
        try:
            client.connect(broker["address"], broker["port"], keepalive=60)
            break
        except OSError as e:
            if "101" in str(e) or e.errno == 101:
                logger.warning(f"[NETWORK_ERR] Red inalcanzable (Errno 101). Reintentando en {retry_delay}s...")
            else:
                logger.warning(f"[NETWORK_ERR] Error de conexión OS: {e}. Reintentando en {retry_delay}s...")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)
    
    return client

def main():
    # Obtiene la variable de entorno para definir la raíz del proyecto local
    project_local_root = os.getenv("PROJECT_LOCAL_ROOT")
    if not project_local_root:
        print("La variable de entorno PROJECT_LOCAL_ROOT no está definida.")
        return

    # Definir rutas de configuración y logs siguiendo el estándar del proyecto
    CONFIG_PATH = os.path.join(project_local_root, "configuracion", "configuracion_mqtt.json")
    ENV_PATH = os.path.join(project_local_root, "configuracion", ".env")
    LOG_DIR = os.path.join(project_local_root, "log-files")
    
    # Cargar configuración
    config = cargar_configuracion(CONFIG_PATH, ENV_PATH)
    
    # Inicializar logger
    logger = StructuredLogger(
        id_estacion=config["id"],
        log_directory=LOG_DIR,
        log_filename="mqtt_coordinator.log",
        verbosity="INFO"
    )
    logger.init({"component": "mqtt_coordinator", "version": "1.0.0"})
    
    # Persistir estado 'on' inicial localmente
    state_file = os.path.join(LOG_DIR, "mqtt_state.json")
    now_ts = timestamp_iso()
    guardar_estado("on", now_ts, state_file, logger)

    # Cargar configuración del dispositivo (para parámetros GPD y modo_adquisicion)
    device_config_path = os.path.join(
        project_local_root, "configuracion", "configuracion_dispositivo.json"
    )
    device_config = {}
    try:
        with open(device_config_path, "r", encoding="utf-8") as f:
            device_config = json.load(f)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(f"[CONFIG] No se pudo cargar configuracion_dispositivo.json: {exc}")

    # Inicializar EventLogger para registro CSV de detecciones
    event_logger = EventLogger(logger=logger)

    # Inicializar AcquisitionWatchdog para monitoreo de frescura del Ring Buffer
    ring_dir = device_config.get("streaming", {}).get("ring_buffer", {}).get(
        "directorio", "/home/rsa/data/ring-buffer/"
    )
    watchdog = AcquisitionWatchdog(ring_dir=ring_dir, logger=logger)

    # Inicializar SensorWatchdog y DriveWatchdog
    sensor_watchdog = SensorWatchdog()
    mseed_dir = device_config.get("directorios", {}).get("archivos_mseed", "/home/rsa/data/mseed/")
    drive_watchdog = DriveWatchdog(mseed_dir=mseed_dir)

    # Preparar userdata para el cliente
    userdata = {
        "config": config,
        "logger": logger,
        "device_config": device_config,
        "event_logger": event_logger,
        "watchdog": watchdog,
        "sensor_watchdog": sensor_watchdog,
        "drive_watchdog": drive_watchdog,
        "state_file_path": state_file,
        "boot_published": False,
        "last_state_change": None,
        "is_disconnected_logged": False,
        "is_connected": False
    }
    
    # Iniciar cliente
    client = iniciar_cliente(config, logger, userdata)
    client.loop_start()
    
    # Loop principal con timers de telemetría sincronizados cada 300 s (5 minutos)
    last_health = 0
    last_day = datetime.now(timezone.utc).day
    
    try:
        while True:
            now = time.time()
            
            if now - last_health >= HEALTH_INTERVAL:
                publicar_health(client, config, logger)
                publicar_acquisition_status(client, config, watchdog, logger)
                publicar_sensor_status(client, config, sensor_watchdog, logger)
                publicar_drive_status(client, config, drive_watchdog, logger)
                
                # Heartbeat periódico de estado (Opción A: refresca retained en broker con timestamp original)
                if client.is_connected() or userdata.get("is_connected", False):
                    online_ts = userdata.get("last_state_change")
                    if online_ts:
                        logger.info(f"[HEARTBEAT_STATE] Refrescando telemetry_state (online desde {online_ts})")
                        publicar_state(client, config, "online", logger, timestamp_override=online_ts)
                
                last_health = now
            
            # Re-publicación diaria a las 00:00
            now_dt = datetime.now(timezone.utc)
            if now_dt.day != last_day and now_dt.hour >= DAILY_REPUBLISH_HOUR:
                last_change = userdata.get("last_state_change")
                # Si no hay last_change (no conectó), no re-publicamos 'online'
                if last_change:
                    logger.info(f"[DAILY_SYNC] Re-publicando estado online (vía {last_change})")
                    publicar_state(client, config, "online", logger, timestamp_override=last_change)
                last_day = now_dt.day
            
            time.sleep(1)
            
    except KeyboardInterrupt:
        logger.info("[SHUTDOWN] Deteniendo coordinador...")
        publicar_state(client, config, "offline", logger)
        client.loop_stop()
        client.disconnect()

if __name__ == "__main__":
    main()
