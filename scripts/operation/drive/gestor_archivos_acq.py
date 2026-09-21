"""
Gestor de archivos para sistema de adquisición de datos sísmicos

DESCRIPCIÓN:
Script que gestiona archivos binarios y miniSEED según el modo de adquisición configurado.
Implementa políticas de gestión de almacenamiento basadas en dos modos de operación:

MODOS DE OPERACIÓN:
- online: Sube archivos a Google Drive, aplica retención temporal y control de espacio
- offline: Maximiza almacenamiento local, retiene solo archivos continuous necesarios

USO:
    python3 gestor_archivos_acq.py              # Ejecución normal
    python3 gestor_archivos_acq.py --dry-run    # Simulación sin cambios reales
    python3 gestor_archivos_acq.py --noauth_local_webserver # Para equipos remotos (SSH)

MODO DRY-RUN:
El parámetro --dry-run permite simular todas las operaciones sin realizar cambios:
- NO borra archivos locales
- NO sube archivos a Google Drive
- Registra en archivo separado: gestor_acq_dry-run.log
- Muestra qué operaciones se ejecutarían (prefijo [DRY-RUN])
- Útil para verificar comportamiento antes de ejecutar en producción

CONFIGURACIÓN:
El modo de operación y políticas se configuran en configuracion_dispositivo.json:
- dispositivo.modo_adquisicion: "online" o "offline"
- gestion_almacenamiento.umbrales: mínimo y crítico de espacio
- gestion_almacenamiento.politicas: configuración específica por modo

REQUISITOS:
- Variable de entorno PROJECT_LOCAL_ROOT debe estar definida
- Archivo configuracion_dispositivo.json con estructura completa
- Credenciales de Google Drive (para modo online)
"""

import os
import shutil
import socket
import json
import logging
import sys
from datetime import datetime, timedelta
import time

# Importar funciones de subir_archivo.py (mismo directorio)
from subir_archivo import (
    Try_Autenticar_Drive,
    subir_archivo_con_reintentos,
    SCOPES
)

# Importar el gestor de estado de subidas
from drive_status_manager import (
    esta_protegido,
    ya_fue_subido,
    limpiar_archivos_inexistentes,
    obtener_estadisticas
)
import drive_status_manager as dsm

# Configurar acceso a librería de logging estructurado
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from structured_logger import StructuredLogger

# ===== CONFIGURACIÓN DE LOGGING =====
VERBOSITY_LEVEL = "SUMMARY"  # DEBUG, INFO, SUMMARY
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5MB
LOG_BACKUP_COUNT = 3
# ====================================

######################################### ~Funciones~ #################################################

# Lee un archivo de configuración en formato JSON y devuelve su contenido como un diccionario.
def read_fileJSON(nameFile):
    try:
        with open(nameFile, 'r') as f:
            data = json.load(f)
        logging.info(f"Archivo de configuración {nameFile} leído correctamente.")
        return data
    except FileNotFoundError:
        logging.error(f"Archivo {nameFile} no encontrado.")
        return None
    except json.JSONDecodeError:
        logging.error(f"Error al decodificar el archivo {nameFile}.")
        return None

# Retorna el porcentaje de espacio libre en la partición donde se encuentra 'path'
def get_free_space_percentage(path):
    total, used, free = shutil.disk_usage(path)
    percentage = (free / total) * 100
    return percentage

# Verifica la conexión a internet intentando conectar al servidor DNS de Google
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

# Borra el archivo más antiguo con la extensión indicada en el directorio especificado
def delete_oldest_file(directory, extension, logger, dry_run=False):
    files = [os.path.join(directory, f) for f in os.listdir(directory) if f.endswith(extension)]
    if not files:
        logger.warning(f"No se encontraron archivos con extensión {extension} en {directory}.")
        return
    oldest_file = min(files, key=os.path.getmtime)
    filename = os.path.basename(oldest_file)

    tipo = "mseed" if extension == ".mseed" else "continuous"

    if dry_run:
        return

    try:
        os.remove(oldest_file)
        logger.delete_space(tipo, filename, "oldest_deleted")
    except Exception as e:
        logger.error(f"delete_oldest_{tipo}", str(e))

# Función para inicializar y obtener el logger estructurado
def obtener_logger_estructurado(id_estacion, log_directory, log_filename, verbosity="SUMMARY", dry_run=False):
    if dry_run:
        log_filename = log_filename.replace(".log", "_dry-run.log")
        
    return StructuredLogger(
        id_estacion=id_estacion,
        log_directory=log_directory,
        log_filename=log_filename,
        verbosity=verbosity,
        max_bytes=LOG_MAX_BYTES,
        backup_count=LOG_BACKUP_COUNT
    )

# ========== FUNCIONES AUXILIARES PARA GESTIÓN DE ARCHIVOS ==========

def obtener_archivo_mas_reciente(directorio, extension):
    """
    Obtiene el archivo más reciente con la extensión especificada.

    Args:
        directorio: Ruta del directorio
        extension: Extensión del archivo (ej: ".dat", ".mseed")

    Returns:
        str: Ruta completa del archivo más reciente, o None si no hay archivos
    """
    try:
        archivos = [os.path.join(directorio, f) for f in os.listdir(directorio) if f.endswith(extension)]
        if not archivos:
            return None
        return max(archivos, key=os.path.getmtime)
    except Exception:
        return None


def calcular_antiguedad_dias(ruta_archivo):
    """
    Calcula la antigüedad de un archivo en días.

    Args:
        ruta_archivo: Ruta completa del archivo

    Returns:
        int: Número de días desde la última modificación
    """
    tiempo_modificacion = os.path.getmtime(ruta_archivo)
    fecha_modificacion = datetime.fromtimestamp(tiempo_modificacion)
    antiguedad = datetime.now() - fecha_modificacion
    return antiguedad.days


def esta_protegido_por_fallo(nombre_archivo, tipo_archivo, log_directory):
    """
    Verifica si un archivo está protegido por haber fallado al subirse.

    Args:
        nombre_archivo: Nombre del archivo (sin ruta)
        tipo_archivo: Tipo de archivo ("continuous", "mseed", etc.)
        log_directory: Directorio de logs

    Returns:
        bool: True si está protegido, False si puede borrarse
    """
    try:
        return esta_protegido(log_directory, nombre_archivo, tipo_archivo)
    except Exception:
        return False


def eliminar_archivo_con_verificacion(ruta_archivo, tipo_archivo, log_directory, logger, dry_run=False):
    """
    Elimina un archivo verificando primero si está protegido.
    """
    nombre_archivo = os.path.basename(ruta_archivo)

    # Verificar si está protegido
    if esta_protegido_por_fallo(nombre_archivo, tipo_archivo, log_directory):
        logger.protected(tipo_archivo, nombre_archivo, "upload_failed")
        return False

    if dry_run:
        # Simular log de skip/delete en dry run si es necesario
        return True

    try:
        os.remove(ruta_archivo)
        return True
    except Exception as e:
        logger.error(f"delete_{tipo_archivo}", str(e))
        return False

#######################################################################################################

def main():
    # Obtiene la variable de entorno para definir la ruta del archivo de configuración:
    project_local_root = os.getenv("PROJECT_LOCAL_ROOT")
    if not project_local_root:
        logging.error("La variable de entorno PROJECT_LOCAL_ROOT no está definida.")
        return
    
    # Definir rutas de archivos y directorios
    config_dispositivo_path = os.path.join(project_local_root, "configuracion", "configuracion_dispositivo.json")
    log_directory = os.path.join(project_local_root, "log-files")

    # Lee el archivo de configuración del dispositivo
    config_dispositivo = read_fileJSON(config_dispositivo_path)
    if config_dispositivo is None:
        logging.error("No se pudo leer el archivo de configuración del dispositivo. Terminando el programa.")
        return

    # Obtener rutas desde configuracion_dispositivo.json
    mseed_directory = config_dispositivo.get("directorios", {}).get("archivos_mseed", "")
    binary_directory = config_dispositivo.get("directorios", {}).get("registro_continuo", "")
    eventos_directory = config_dispositivo.get("directorios", {}).get("eventos_extraidos", "")

    if not mseed_directory:
        logging.error("No se encontró la ruta 'archivos_mseed' en configuracion_dispositivo.json")
        return
    if not binary_directory:
        logging.error("No se encontró la ruta 'registro_continuo' en configuracion_dispositivo.json")
        return

    # Verificar que los directorios existen
    if not os.path.isdir(mseed_directory):
        logging.error(f"El directorio mseed no existe: {mseed_directory}")
        return
    if not os.path.isdir(binary_directory):
        logging.error(f"El directorio de archivos binarios no existe: {binary_directory}")
        return
    
    mode_acq = config_dispositivo.get("dispositivo", {}).get("modo_adquisicion", "Unknown")
    id_estacion = config_dispositivo.get("dispositivo", {}).get("id", "Unknown")

    # Obtener umbral de espacio libre mínimo (por defecto 10%)
    min_free_space_threshold = config_dispositivo.get("dispositivo", {}).get("umbral_espacio_minimo", 10)

    # Detectar argumentos de modo especial
    dry_run = "--dry-run" in sys.argv
    purge_registry = "--purge-registry" in sys.argv

    # Inicializa el logger estructurado
    logger = obtener_logger_estructurado(
        id_estacion, 
        log_directory, 
        "gestor_acq.log", 
        verbosity=VERBOSITY_LEVEL, 
        dry_run=dry_run
    )

    if dry_run:
        logger.warning("MODO DRY-RUN ACTIVADO - Las operaciones se simularán sin realizar cambios reales.")

    # Si se invoca exclusivamente para purgar el registro:
    if purge_registry:
        logger.init(f"modo=purge_registry | id_estacion={id_estacion}")
        print("=" * 65)
        print("SANEAMIENTO DIRECTO DEL REGISTRO DE SUBIDAS (uploaded_files_registry.json)")
        print(f"Estación: {id_estacion} | Modo Dry-Run: {dry_run}")
        print("=" * 65)

        directorios_limpieza = {}
        if mseed_directory and os.path.isdir(mseed_directory):
            directorios_limpieza["mseed"] = mseed_directory
        if binary_directory and os.path.isdir(binary_directory):
            directorios_limpieza["continuous"] = binary_directory

        eventos_dir = config_dispositivo.get("directorios", {}).get("eventos_extraidos", "")
        if eventos_dir and os.path.isdir(eventos_dir):
            directorios_limpieza["event"] = eventos_dir

        tmp_dir = config_dispositivo.get("directorios", {}).get("archivos_temporales", "")
        if tmp_dir and os.path.isdir(tmp_dir):
            directorios_limpieza["tmp"] = tmp_dir

        if log_directory and os.path.isdir(log_directory):
            directorios_limpieza["log"] = log_directory

        print("Directorios activos inspeccionados:")
        for t, d in directorios_limpieza.items():
            print(f"  • {t:<11}: {d}")
        print("")

        status_data = dsm._leer_json(log_directory)
        stats = obtener_estadisticas(log_directory)

        print("Estado actual del archivo de registro JSON:")
        print(f"  • Total exitosos registrados  : {stats['exitosos']['total']}")
        for tipo in dsm.TIPOS_ARCHIVO:
            c = stats['exitosos'][tipo]
            if c > 0:
                print(f"      - {tipo:<11}: {c} archivos")
        print(f"  • Total fallidos registrados  : {stats['fallidos']['total']}")
        for tipo in dsm.TIPOS_ARCHIVO:
            c = stats['fallidos'][tipo]
            if c > 0:
                print(f"      - {tipo:<11}: {c} archivos")
        print("")

        if dry_run:
            sim_exitosos = 0
            sim_fallidos = 0
            detalles_por_tipo = {}
            for tipo, d_path in directorios_limpieza.items():
                p_exit = 0
                p_fall = 0
                for f_name in status_data.get("archivos_exitosos", {}).get(tipo, {}):
                    if not os.path.exists(os.path.join(d_path, f_name)):
                        sim_exitosos += 1
                        p_exit += 1
                for f_name in status_data.get("archivos_fallidos", {}).get(tipo, {}):
                    if not os.path.exists(os.path.join(d_path, f_name)):
                        sim_fallidos += 1
                        p_fall += 1
                if p_exit > 0 or p_fall > 0:
                    detalles_por_tipo[tipo] = {"exitosos": p_exit, "fallidos": p_fall}

            print("Evaluación de presencia física en disco:")
            if sim_exitosos == 0 and sim_fallidos == 0:
                print("  ✓ Todos los archivos registrados en el JSON existen físicamente en disco.")
                print("  ✓ Ningún archivo es huérfano (no se requiere poda).")
            else:
                print(f"  ⚠️ [SIMULACIÓN] Se podarían {sim_exitosos} exitosos y {sim_fallidos} fallidos huérfanos:")
                for tipo, det in detalles_por_tipo.items():
                    print(f"      - {tipo}: {det['exitosos']} exitosos, {det['fallidos']} fallidos inexistentes")
            logger.summary(dry_run_purge=True, exitosos_podables=sim_exitosos, fallidos_podables=sim_fallidos)
        else:
            balance = limpiar_archivos_inexistentes(log_directory, directorios_limpieza, logger)
            stats_final = obtener_estadisticas(log_directory)
            print("Resultado de la depuración:")
            print(f"  • Exitosos huérfanos removidos : {balance.get('exitosos_removidos', 0)}")
            print(f"  • Fallidos huérfanos removidos : {balance.get('fallidos_removidos', 0)}")
            print(f"  • Total registros depurados    : {balance.get('total_removidos', 0)}")
            print("")
            print("Estado final en registro JSON:")
            print(f"  • Total exitosos persistentes  : {stats_final['exitosos']['total']}")
            print(f"  • Total fallidos activos       : {stats_final['fallidos']['total']}")
            logger.summary(
                purge_registry="completed",
                exitosos_purgados=balance.get("exitosos_removidos", 0),
                fallidos_purgados=balance.get("fallidos_removidos", 0),
                total_exitosos_final=stats_final["exitosos"]["total"]
            )
        print("=" * 65)
        return

    logger.init(f"modo={mode_acq} | id_estacion={id_estacion}")

    # ========== VALIDAR MODO DE OPERACIÓN ==========
    # Validar modo
    if mode_acq not in ["online", "offline"]:
        logger.error(f"Modo de adquisición inválido: {mode_acq}. Debe ser 'online' o 'offline'")
        return

    # ========== CARGAR CONFIGURACIÓN DE GESTIÓN DE ALMACENAMIENTO ==========
    gestion_almacenamiento = config_dispositivo.get("gestion_almacenamiento", {})

    # Cargar umbrales
    umbrales = gestion_almacenamiento.get("umbrales", {})
    umbral_minimo = umbrales.get("minimo", 10)
    umbral_critico = umbrales.get("critico", 5)

    # Cargar políticas
    politicas = gestion_almacenamiento.get("politicas", {})
    politica_modo = politicas.get(mode_acq, {})

    # Si no existe la política del modo, usar valores por defecto
    if not politica_modo:
        logger.warning(f"No se encontró política para modo '{mode_acq}'. Usando valores por defecto.")
        if mode_acq == "online":
            politica_modo = {
                "subir": ["mseed", "event"],
                "retener_dias": {"continuous": 30, "mseed": 30, "event": 30}
            }
        elif mode_acq == "offline":
            politica_modo = {
                "subir": [],
                "retener_dias": {"continuous": 7, "event": 30}
            }
    else:
        # Asegurar retrocompatibilidad: si la política existe pero no especifica 'event', incorporarlo si el directorio existe
        if eventos_directory and os.path.isdir(eventos_directory):
            if mode_acq == "online" and "subir" in politica_modo:
                if "event" not in politica_modo["subir"]:
                    politica_modo["subir"].append("event")
            if "retener_dias" in politica_modo:
                if "event" not in politica_modo["retener_dias"]:
                    politica_modo["retener_dias"]["event"] = 30

    logger.summary(
        config_cargada=True, 
        modo=mode_acq, 
        umbral_min=umbral_minimo, 
        umbral_crit=umbral_critico,
        retener_dias=politica_modo.get("retener_dias", {})
    )

    # Escanear el contenido de los directorios
    try:
        archivos_mseed = [f for f in os.listdir(mseed_directory) if f.endswith(".mseed")]
        archivos_binarios = [f for f in os.listdir(binary_directory) if f.endswith(".dat")]
        archivos_eventos = []
        if eventos_directory and os.path.isdir(eventos_directory):
            archivos_eventos = [f for f in os.listdir(eventos_directory) if f.endswith(".mseed")]
        logger.info(f"Archivos encontrados: {len(archivos_mseed)} mseed, {len(archivos_binarios)} continuous, {len(archivos_eventos)} event")
    except Exception as e:
        logger.error("list_files", str(e))
        return
    
    if mode_acq == "offline":
        logger.info("MODO OFFLINE ACTIVADO")

        # ========== POLÍTICA DE RETENCIÓN TEMPORAL ==========
        retener_dias_continuous = politica_modo.get("retener_dias", {}).get("continuous", 7)

        # Identificar archivo continuous más reciente (está en uso)
        archivo_mas_reciente_continuous = obtener_archivo_mas_reciente(binary_directory, ".dat")

        if archivo_mas_reciente_continuous:
            logger.protected("continuous", os.path.basename(archivo_mas_reciente_continuous), "active_file")

        # Listar archivos continuous
        archivos_continuous_paths = [os.path.join(binary_directory, f) for f in archivos_binarios]

        # Eliminar archivos continuous antiguos (excepto el más reciente)
        for ruta_archivo in archivos_continuous_paths:
            # Proteger el más reciente
            if archivo_mas_reciente_continuous and ruta_archivo == archivo_mas_reciente_continuous:
                continue

            # Verificar antigüedad
            antiguedad = calcular_antiguedad_dias(ruta_archivo)
            if antiguedad > retener_dias_continuous:
                nombre_archivo = os.path.basename(ruta_archivo)

                if not dry_run:
                    try:
                        os.remove(ruta_archivo)
                        logger.delete_age("continuous", nombre_archivo, antiguedad)
                    except Exception as e:
                        logger.error("delete_age_continuous", str(e))

        # ========== POLÍTICA DE RETENCIÓN TEMPORAL DE EVENTOS ==========
        retener_dias_event = politica_modo.get("retener_dias", {}).get("event", 30)
        if eventos_directory and os.path.isdir(eventos_directory) and retener_dias_event:
            archivos_event_paths = [os.path.join(eventos_directory, f) for f in archivos_eventos]
            for ruta_archivo in archivos_event_paths:
                antiguedad = calcular_antiguedad_dias(ruta_archivo)
                if antiguedad > retener_dias_event:
                    nombre_archivo = os.path.basename(ruta_archivo)
                    if not dry_run:
                        try:
                            os.remove(ruta_archivo)
                            logger.delete_age("event", nombre_archivo, antiguedad)
                        except Exception as e:
                            logger.error("delete_age_event", str(e))

        # ========== POLÍTICA DE CONTROL DE ESPACIO ==========
        free_space = get_free_space_percentage(mseed_directory)
        logger.info(f"Espacio libre | mseed | {free_space:.2f}%")

        # Umbral mínimo
        if free_space < umbral_minimo:
            logger.warning(f"Espacio bajo umbral mínimo | offline | {free_space:.2f}% < {umbral_minimo}%")

            # Eliminar TODOS los archivos continuous excepto el más reciente
            archivos_continuous_paths = [os.path.join(binary_directory, f) for f in os.listdir(binary_directory) if f.endswith(".dat")]
            for ruta_archivo in archivos_continuous_paths:
                if archivo_mas_reciente_continuous and ruta_archivo == archivo_mas_reciente_continuous:
                    continue
                nombre_archivo = os.path.basename(ruta_archivo)

                if dry_run:
                    continue
                else:
                    try:
                        os.remove(ruta_archivo)
                        logger.delete_space("continuous", nombre_archivo, "min_threshold")
                    except Exception as e:
                        logger.error("delete_space_continuous", str(e))

            # Verificar espacio nuevamente
            free_space = get_free_space_percentage(mseed_directory)

            # Si aún bajo umbral, eliminar mseed más antiguo
            if free_space < umbral_minimo:
                logger.warning(f"Espacio insuficiente tras eliminar continuous | offline | {free_space:.2f}%")
                delete_oldest_file(mseed_directory, ".mseed", logger, dry_run)

        # Umbral crítico
        if free_space < umbral_critico:
            logger.warning(f"Espacio crítico en modo offline - intervención necesaria | offline | {free_space:.2f}% < {umbral_critico}%")

            # Eliminar mseed más antiguo (FIFO)
            delete_oldest_file(mseed_directory, ".mseed", logger, dry_run)

            # Si aún bajo umbral crítico, desalojar eventos más antiguos (FIFO)
            free_space = get_free_space_percentage(mseed_directory)
            if free_space < umbral_critico and eventos_directory and os.path.isdir(eventos_directory):
                archivos_event_paths = [os.path.join(eventos_directory, f) for f in os.listdir(eventos_directory) if f.endswith(".mseed")]
                archivos_event_ordenados = sorted(archivos_event_paths, key=os.path.getmtime)
                for ruta_archivo in archivos_event_ordenados:
                    if dry_run:
                        continue
                    try:
                        os.remove(ruta_archivo)
                        logger.delete_space("event", os.path.basename(ruta_archivo), "critical_threshold")
                        free_space = get_free_space_percentage(mseed_directory)
                        if free_space >= umbral_critico:
                            break
                    except Exception as e:
                        logger.error("delete_space_event_offline", str(e))
    
    elif mode_acq == "online":
        logger.info("MODO ONLINE ACTIVADO")

        # Detectar conectividad
        tiene_conexion = check_internet_connection(logger)

        if tiene_conexion:

            # ========== POLÍTICA DE SUBIDA A DRIVE ==========
            tipos_a_subir = politica_modo.get("subir", [])

            # Preparar archivos para subir (empezar por los más antiguos)
            archivos_para_subir = []
            archivos_ya_subidos_count = 0

            if "mseed" in tipos_a_subir:
                archivos_mseed_paths = [os.path.join(mseed_directory, f) for f in archivos_mseed]
                archivos_mseed_ordenados = sorted(archivos_mseed_paths, key=os.path.getmtime)
                for path in archivos_mseed_ordenados:
                    nombre_archivo = os.path.basename(path)
                    # Verificar si ya fue subido
                    if ya_fue_subido(log_directory, nombre_archivo, "mseed"):
                        archivos_ya_subidos_count += 1
                        logger.skip("mseed", nombre_archivo, "already_uploaded")
                    else:
                        archivos_para_subir.append((path, "mseed"))

            if "continuous" in tipos_a_subir:
                archivos_binarios_paths = [os.path.join(binary_directory, f) for f in archivos_binarios]
                archivos_binarios_ordenados = sorted(archivos_binarios_paths, key=os.path.getmtime)
                # Proteger el más reciente (está en uso)
                archivo_mas_reciente_continuous = obtener_archivo_mas_reciente(binary_directory, ".dat")
                for path in archivos_binarios_ordenados:
                    nombre_archivo = os.path.basename(path)
                    # Proteger el más reciente
                    if archivo_mas_reciente_continuous and path == archivo_mas_reciente_continuous:
                        continue
                    # Verificar si ya fue subido
                    if ya_fue_subido(log_directory, nombre_archivo, "continuous"):
                        archivos_ya_subidos_count += 1
                        logger.skip("continuous", nombre_archivo, "already_uploaded")
                    else:
                        archivos_para_subir.append((path, "continuous"))

            if "event" in tipos_a_subir and eventos_directory and os.path.isdir(eventos_directory):
                archivos_eventos_paths = [os.path.join(eventos_directory, f) for f in archivos_eventos]
                archivos_eventos_ordenados = sorted(archivos_eventos_paths, key=os.path.getmtime)
                for path in archivos_eventos_ordenados:
                    nombre_archivo = os.path.basename(path)
                    # Verificar si ya fue subido
                    if ya_fue_subido(log_directory, nombre_archivo, "event"):
                        archivos_ya_subidos_count += 1
                        logger.skip("event", nombre_archivo, "already_uploaded")
                    else:
                        archivos_para_subir.append((path, "event"))

            # Registrar resumen de archivos ya subidos (solo si hay alguno)
            if archivos_ya_subidos_count > 0:
                logger.summary(archivos_omitidos=archivos_ya_subidos_count, razon="ya_subidos")

            # Subir archivos si hay alguno configurado
            if archivos_para_subir:
                if dry_run:
                    logger.info(f"[DRY-RUN] Se subirían {len(archivos_para_subir)} archivos a Google Drive")
                    for path_completo, tipo_archivo in archivos_para_subir:
                        nombre_archivo = os.path.basename(path_completo)
                        logger.info(f"[DRY-RUN] Se subiría | {tipo_archivo} | {nombre_archivo}")
                else:
                    logger.info(f"Archivos pendientes de subida | online | {len(archivos_para_subir)} archivos")

                    # Obtener parámetros de Drive
                    max_reintentos = config_dispositivo.get("drive", {}).get("config", {}).get("max_reintentos", 3)
                    tiempo_espera = config_dispositivo.get("drive", {}).get("config", {}).get("tiempo_espera", 2)
                    credentials_file = os.path.join(project_local_root, "configuracion", "drive_credentials.json")
                    token_file = os.path.join(project_local_root, "configuracion", "drive_token.json")

                    # Autenticar una sola vez
                    service = Try_Autenticar_Drive(SCOPES, credentials_file, token_file, logger)

                    if service:
                        archivos_subidos = 0
                        for path_completo, tipo_archivo in archivos_para_subir:
                            nombre_archivo = os.path.basename(path_completo)

                            # Obtener drive_id según tipo
                            if tipo_archivo == "mseed":
                                drive_id = config_dispositivo.get("drive", {}).get("carpetas", {}).get("mseed_id", "")
                            elif tipo_archivo == "continuous":
                                drive_id = config_dispositivo.get("drive", {}).get("carpetas", {}).get("continuos_id", "")
                            elif tipo_archivo == "event":
                                drive_id = config_dispositivo.get("drive", {}).get("carpetas", {}).get("events_id", "")
                            else:
                                logger.warning(f"Tipo de archivo desconocido | {tipo_archivo} | {nombre_archivo}")
                                continue

                            if not drive_id:
                                logger.error(f"ID de carpeta Drive no encontrado | {tipo_archivo}")
                                continue

                            logger.info(f"Subiendo archivo | {tipo_archivo} | {nombre_archivo}")

                            exito = subir_archivo_con_reintentos(
                                service=service,
                                nombre_archivo=nombre_archivo,
                                path_completo_archivo=path_completo,
                                drive_id=drive_id,
                                max_reintentos=max_reintentos,
                                tiempo_espera=tiempo_espera,
                                logger=logger,
                                borrar_despues=False,
                                tipo_archivo=tipo_archivo,
                                log_directory=log_directory
                            )

                            if exito:
                                archivos_subidos += 1

                        logger.summary(upload_resumen=f"{archivos_subidos}/{len(archivos_para_subir)}", status="success")
                    else:
                        logger.error("auth_drive", "Autenticación fallida")
            else:
                # No hay archivos para subir
                logger.info("Sin archivos pendientes para subida.")

            # ========== POLÍTICA DE RETENCIÓN TEMPORAL ==========
            retener_dias = politica_modo.get("retener_dias", {})

            # Identificar archivo continuous más reciente (está en uso)
            archivo_mas_reciente_continuous = obtener_archivo_mas_reciente(binary_directory, ".dat")
            if archivo_mas_reciente_continuous:
                logger.info(f"Archivo más reciente protegido | continuous | {os.path.basename(archivo_mas_reciente_continuous)}")

            # Contadores de retención temporal
            retencion_stats = {"continuous": 0, "mseed": 0, "event": 0}

            # Eliminar continuous antiguos (excepto el más reciente y los fallidos)
            if "continuous" in retener_dias:
                dias_continuous = retener_dias["continuous"]
                archivos_continuous_paths = [os.path.join(binary_directory, f) for f in archivos_binarios]

                for ruta_archivo in archivos_continuous_paths:
                    # Proteger el más reciente
                    if archivo_mas_reciente_continuous and ruta_archivo == archivo_mas_reciente_continuous:
                        continue

                    # Verificar antigüedad
                    antiguedad = calcular_antiguedad_dias(ruta_archivo)
                    if antiguedad > dias_continuous:
                        # Intentar eliminar (verifica automáticamente si está protegido)
                        eliminado = eliminar_archivo_con_verificacion(
                            ruta_archivo, "continuous", log_directory, logger, dry_run
                        )
                        if eliminado:
                            retencion_stats["continuous"] += 1
                            if not dry_run:
                                logger.delete_age("continuous", os.path.basename(ruta_archivo), antiguedad)

            # Eliminar mseed antiguos (solo los que NO están protegidos por fallo de subida)
            if "mseed" in retener_dias:
                dias_mseed = retener_dias["mseed"]
                archivos_mseed_paths = [os.path.join(mseed_directory, f) for f in archivos_mseed]

                for ruta_archivo in archivos_mseed_paths:
                    antiguedad = calcular_antiguedad_dias(ruta_archivo)
                    if antiguedad > dias_mseed:
                        # Intentar eliminar (verifica automáticamente si está protegido)
                        eliminado = eliminar_archivo_con_verificacion(
                            ruta_archivo, "mseed", log_directory, logger, dry_run
                        )
                        if eliminado:
                            retencion_stats["mseed"] += 1
                            if not dry_run:
                                logger.delete_age("mseed", os.path.basename(ruta_archivo), antiguedad)

            # Eliminar eventos antiguos (solo los que NO están protegidos por fallo de subida)
            if "event" in retener_dias and eventos_directory and os.path.isdir(eventos_directory):
                dias_event = retener_dias["event"]
                archivos_event_paths = [os.path.join(eventos_directory, f) for f in archivos_eventos]

                for ruta_archivo in archivos_event_paths:
                    antiguedad = calcular_antiguedad_dias(ruta_archivo)
                    if antiguedad > dias_event:
                        # Intentar eliminar (verifica automáticamente si está protegido)
                        eliminado = eliminar_archivo_con_verificacion(
                            ruta_archivo, "event", log_directory, logger, dry_run
                        )
                        if eliminado:
                            retencion_stats["event"] += 1
                            if not dry_run:
                                logger.delete_age("event", os.path.basename(ruta_archivo), antiguedad)

            # Emitir resumen explícito de retención temporal
            logger.summary(
                retencion_evaluada=True,
                modo_simulacion=dry_run,
                continuous_expirados=retencion_stats["continuous"],
                mseed_expirados=retencion_stats["mseed"],
                event_expirados=retencion_stats["event"]
            )

            # ========== POLÍTICA DE CONTROL DE ESPACIO ==========
            free_space = get_free_space_percentage(mseed_directory)
            logger.info(f"Espacio libre | mseed | {free_space:.2f}%")

            # Umbral mínimo
            if free_space < umbral_minimo:
                logger.warning(f"Espacio bajo umbral mínimo | online | {free_space:.2f}% < {umbral_minimo}%")

                # Eliminar archivos continuous antiguos (excepto el más reciente)
                archivos_continuous_paths = [os.path.join(binary_directory, f) for f in os.listdir(binary_directory) if f.endswith(".dat")]
                archivos_continuous_ordenados = sorted(archivos_continuous_paths, key=os.path.getmtime)

                for ruta_archivo in archivos_continuous_ordenados:
                    if archivo_mas_reciente_continuous and ruta_archivo == archivo_mas_reciente_continuous:
                        continue

                    eliminado = eliminar_archivo_con_verificacion(
                        ruta_archivo, "continuous", log_directory, logger, dry_run
                    )
                    if eliminado and not dry_run:
                        logger.delete_space("continuous", os.path.basename(ruta_archivo), "min_threshold")

                # Verificar espacio nuevamente
                free_space = get_free_space_percentage(mseed_directory)

                # Si aún bajo umbral, eliminar mseed más antiguo (verificando que no esté protegido)
                if free_space < umbral_minimo:
                    logger.warning(f"Espacio insuficiente tras eliminar continuous | online | {free_space:.2f}%")

                    archivos_mseed_paths = [os.path.join(mseed_directory, f) for f in os.listdir(mseed_directory) if f.endswith(".mseed")]
                    archivos_mseed_ordenados = sorted(archivos_mseed_paths, key=os.path.getmtime)

                    for ruta_archivo in archivos_mseed_ordenados:
                        eliminado = eliminar_archivo_con_verificacion(
                            ruta_archivo, "mseed", log_directory, logger, dry_run
                        )
                        if eliminado and not dry_run:
                            logger.delete_space("mseed", os.path.basename(ruta_archivo), "min_threshold")
                            break  # Eliminar solo uno a la vez

            # Umbral crítico
            if free_space < umbral_critico:
                logger.warning(f"Espacio crítico | online | {free_space:.2f}% < {umbral_critico}%")

                # Eliminar TODOS los continuous (excepto el más reciente)
                archivos_continuous_paths = [os.path.join(binary_directory, f) for f in os.listdir(binary_directory) if f.endswith(".dat")]
                for ruta_archivo in archivos_continuous_paths:
                    if archivo_mas_reciente_continuous and ruta_archivo == archivo_mas_reciente_continuous:
                        continue

                    eliminado = eliminar_archivo_con_verificacion(
                        ruta_archivo, "continuous", log_directory, logger, dry_run
                    )
                    if eliminado and not dry_run:
                        logger.delete_space("continuous", os.path.basename(ruta_archivo), "critical_threshold")

                # Verificar espacio nuevamente
                free_space = get_free_space_percentage(mseed_directory)

                # Si aún crítico, eliminar mseed más antiguos (FIFO, verificando protección)
                if free_space < umbral_critico:
                    archivos_mseed_paths = [os.path.join(mseed_directory, f) for f in os.listdir(mseed_directory) if f.endswith(".mseed")]
                    archivos_mseed_ordenados = sorted(archivos_mseed_paths, key=os.path.getmtime)

                    for ruta_archivo in archivos_mseed_ordenados:
                        eliminado = eliminar_archivo_con_verificacion(
                            ruta_archivo, "mseed", log_directory, logger, dry_run
                        )
                        if eliminado and not dry_run:
                            logger.delete_space("mseed", os.path.basename(ruta_archivo), "critical_threshold")
                            # Verificar si ya se alcanzó el umbral
                            free_space = get_free_space_percentage(mseed_directory)
                            if free_space >= umbral_critico:
                                break

                # Si aún crítico tras mseed, desalojar eventos más antiguos (FIFO, verificando protección)
                free_space = get_free_space_percentage(mseed_directory)
                if free_space < umbral_critico and eventos_directory and os.path.isdir(eventos_directory):
                    archivos_event_paths = [os.path.join(eventos_directory, f) for f in os.listdir(eventos_directory) if f.endswith(".mseed")]
                    archivos_event_ordenados = sorted(archivos_event_paths, key=os.path.getmtime)

                    for ruta_archivo in archivos_event_ordenados:
                        eliminado = eliminar_archivo_con_verificacion(
                            ruta_archivo, "event", log_directory, logger, dry_run
                        )
                        if eliminado and not dry_run:
                            logger.delete_space("event", os.path.basename(ruta_archivo), "critical_threshold")
                            free_space = get_free_space_percentage(mseed_directory)
                            if free_space >= umbral_critico:
                                break

        else:
            # ========== MODO ONLINE SIN CONECTIVIDAD ==========
            logger.warning("Modo online sin conectividad | online | acumulando archivos")

            # Solo aplicar control de espacio (no subir, no eliminar por retención)
            free_space = get_free_space_percentage(mseed_directory)
            logger.info(f"Espacio libre | mseed | {free_space:.2f}%")

            # Identificar archivo continuous más reciente
            archivo_mas_reciente_continuous = obtener_archivo_mas_reciente(binary_directory, ".dat")
            if archivo_mas_reciente_continuous:
                logger.info(f"Archivo más reciente protegido | continuous | {os.path.basename(archivo_mas_reciente_continuous)}")

            # Umbral mínimo
            if free_space < umbral_minimo:
                logger.warning(f"Espacio bajo umbral mínimo | online-sin-conexión | {free_space:.2f}% < {umbral_minimo}%")

                # Eliminar continuous más antiguo
                archivos_continuous_paths = [os.path.join(binary_directory, f) for f in os.listdir(binary_directory) if f.endswith(".dat")]
                archivos_continuous_ordenados = sorted(archivos_continuous_paths, key=os.path.getmtime)

                for ruta_archivo in archivos_continuous_ordenados:
                    if archivo_mas_reciente_continuous and ruta_archivo == archivo_mas_reciente_continuous:
                        continue

                    nombre_archivo = os.path.basename(ruta_archivo)

                    if dry_run:
                        logger.info(f"[DRY-RUN] Se borraría por espacio | continuous | {nombre_archivo}")
                        break
                    if not dry_run:
                        try:
                            os.remove(ruta_archivo)
                            logger.delete_space("continuous", nombre_archivo, "no_connection_min_threshold")
                            break
                        except Exception as e:
                            logger.error("delete_space_continuous_no_conn", str(e))
                            break

                # Verificar espacio nuevamente
                free_space = get_free_space_percentage(mseed_directory)

                # Si aún bajo umbral, eliminar mseed más antiguo (FIFO)
                if free_space < umbral_minimo:
                    logger.warning(f"Espacio insuficiente | online-sin-conexión | {free_space:.2f}%")
                    delete_oldest_file(mseed_directory, ".mseed", logger, dry_run)

            # Umbral crítico
            if free_space < umbral_critico:
                logger.warning(f"Espacio crítico | online-sin-conexión | {free_space:.2f}% < {umbral_critico}%")
                delete_oldest_file(mseed_directory, ".mseed", logger, dry_run)

                # Si aún crítico tras mseed, desalojar eventos más antiguos (FIFO, verificando protección)
                free_space = get_free_space_percentage(mseed_directory)
                if free_space < umbral_critico and eventos_directory and os.path.isdir(eventos_directory):
                    archivos_event_paths = [os.path.join(eventos_directory, f) for f in os.listdir(eventos_directory) if f.endswith(".mseed")]
                    archivos_event_ordenados = sorted(archivos_event_paths, key=os.path.getmtime)

                    for ruta_archivo in archivos_event_ordenados:
                        eliminado = eliminar_archivo_con_verificacion(
                            ruta_archivo, "event", log_directory, logger, dry_run
                        )
                        if eliminado and not dry_run:
                            logger.delete_space("event", os.path.basename(ruta_archivo), "no_connection_critical_threshold")
                            free_space = get_free_space_percentage(mseed_directory)
                            if free_space >= umbral_critico:
                                break

    else:
        logger.error("main", f"Modo de adquisición desconocido: {mode_acq}")

    # ========== POLÍTICA DE SANEAMIENTO DEL REGISTRO DE SUBIDAS ==========
    # Purgar de uploaded_files_registry.json los archivos que ya no residen físicamente en disco
    try:
        directorios_limpieza = {}
        if mseed_directory and os.path.isdir(mseed_directory):
            directorios_limpieza["mseed"] = mseed_directory
        if binary_directory and os.path.isdir(binary_directory):
            directorios_limpieza["continuous"] = binary_directory

        eventos_dir = config_dispositivo.get("directorios", {}).get("eventos_extraidos", "")
        if eventos_dir and os.path.isdir(eventos_dir):
            directorios_limpieza["event"] = eventos_dir

        tmp_dir = config_dispositivo.get("directorios", {}).get("archivos_temporales", "")
        if tmp_dir and os.path.isdir(tmp_dir):
            directorios_limpieza["tmp"] = tmp_dir

        if log_directory and os.path.isdir(log_directory):
            directorios_limpieza["log"] = log_directory

        if dry_run:
            status_data = dsm._leer_json(log_directory)
            sim_exitosos = 0
            sim_fallidos = 0
            for tipo, d_path in directorios_limpieza.items():
                for f_name in status_data.get("archivos_exitosos", {}).get(tipo, {}):
                    if not os.path.exists(os.path.join(d_path, f_name)):
                        sim_exitosos += 1
                for f_name in status_data.get("archivos_fallidos", {}).get(tipo, {}):
                    if not os.path.exists(os.path.join(d_path, f_name)):
                        sim_fallidos += 1
            logger.summary(
                dry_run_cleanup=True,
                exitosos_podables=sim_exitosos,
                fallidos_podables=sim_fallidos
            )
        else:
            balance_limpieza = limpiar_archivos_inexistentes(log_directory, directorios_limpieza, logger)
            removidos_totales = balance_limpieza.get("total_removidos", 0)
            if removidos_totales > 0:
                logger.summary(
                    registry_cleanup="success",
                    exitosos_purgados=balance_limpieza.get("exitosos_removidos", 0),
                    fallidos_purgados=balance_limpieza.get("fallidos_removidos", 0)
                )
            else:
                logger.summary(registry_cleanup="nominal", huerfanos=0)
    except Exception as e:
        logger.error("registry_cleanup", str(e))

    # Resumen final de ejecución
    logger.summary(ejecucion="completada", modo=mode_acq)
 

if __name__ == "__main__":
    main()
