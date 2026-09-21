"""
Módulo para gestionar el estado de subidas a Google Drive

Este módulo maneja un archivo JSON que registra:
- Archivos subidos exitosamente (para evitar duplicados)
- Archivos que fallaron al subirse (para protegerlos de eliminación)

Estructura del JSON (organizada por MODO/PROPÓSITO del archivo):
{
  "archivos_exitosos": {
    "continuous": {
      "registro_2025-12-10.dat": "2025-12-10 15:00:00"
    },
    "mseed": {
      "archivo.mseed": "2025-12-10 15:00:00"
    },
    "event": {},
    "tmp": {},
    "log": {}
  },
  "archivos_fallidos": {
    "continuous": {
      "archivo_problema.dat": "2025-12-10 15:00:00"
    },
    "mseed": {},
    "event": {},
    "tmp": {},
    "log": {}
  }
}
"""

import json
import os
from datetime import datetime
from threading import Lock

# Lock para operaciones thread-safe
_file_lock = Lock()

# Tipos de archivo soportados (por modo/propósito)
TIPOS_ARCHIVO = ["continuous", "mseed", "event", "tmp", "log"]


def _obtener_ruta_json(log_directory):
    """Obtiene la ruta completa del archivo JSON de estado"""
    return os.path.join(log_directory, "uploaded_files_registry.json")


def _inicializar_estructura():
    """Retorna la estructura inicial del JSON"""
    return {
        "archivos_exitosos": {
            "continuous": {},
            "mseed": {},
            "event": {},
            "tmp": {},
            "log": {}
        },
        "archivos_fallidos": {
            "continuous": {},
            "mseed": {},
            "event": {},
            "tmp": {},
            "log": {}
        }
    }


def _leer_json(log_directory):
    """
    Lee el archivo JSON de estado. Si no existe, crea uno nuevo.

    Args:
        log_directory: Directorio donde se guarda el JSON

    Returns:
        dict: Estructura de datos del JSON
    """
    ruta_json = _obtener_ruta_json(log_directory)

    if not os.path.exists(ruta_json):
        return _inicializar_estructura()

    try:
        with open(ruta_json, 'r') as f:
            data = json.load(f)

        # Validar estructura - asegurar que existan ambas secciones
        if "archivos_fallidos" not in data:
            data["archivos_fallidos"] = {}
        if "archivos_exitosos" not in data:
            data["archivos_exitosos"] = {}

        # Asegurar que existan todas las claves de tipos en ambas secciones
        for tipo in TIPOS_ARCHIVO:
            if tipo not in data["archivos_fallidos"]:
                data["archivos_fallidos"][tipo] = {}
            if tipo not in data["archivos_exitosos"]:
                data["archivos_exitosos"][tipo] = {}

        return data

    except (json.JSONDecodeError, IOError) as e:
        print(f"Error al leer {ruta_json}: {e}. Creando estructura nueva.")
        return _inicializar_estructura()


def _escribir_json(log_directory, data):
    """
    Escribe el archivo JSON de estado de forma segura.

    Args:
        log_directory: Directorio donde se guarda el JSON
        data: Estructura de datos a escribir
    """
    ruta_json = _obtener_ruta_json(log_directory)

    # Crear directorio si no existe
    if not os.path.exists(log_directory):
        os.makedirs(log_directory)

    try:
        # Escribir a archivo temporal primero (atomic write)
        ruta_temp = ruta_json + '.tmp'
        with open(ruta_temp, 'w') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        # Renombrar (operación atómica en sistemas UNIX)
        os.replace(ruta_temp, ruta_json)

    except IOError as e:
        print(f"Error al escribir {ruta_json}: {e}")


def marcar_como_fallido(log_directory, nombre_archivo, tipo_archivo, intentos, logger=None):
    """
    Marca un archivo como fallido en la subida a Google Drive.

    Args:
        log_directory: Directorio donde se guarda el JSON
        nombre_archivo: Nombre del archivo que falló
        tipo_archivo: Tipo de archivo ("continuous", "mseed", "event", "tmp", "log")
        intentos: Número de intentos realizados (no se usa en la estructura simplificada)
        logger: Logger opcional para registrar la operación
    """
    if tipo_archivo not in TIPOS_ARCHIVO:
        raise ValueError(f"Tipo de archivo inválido: {tipo_archivo}. Debe ser uno de {TIPOS_ARCHIVO}")

    with _file_lock:
        data = _leer_json(log_directory)

        # Agregar o actualizar el archivo fallido (solo fecha)
        data["archivos_fallidos"][tipo_archivo][nombre_archivo] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        _escribir_json(log_directory, data)

        if logger:
            logger.warning(f"Archivo marcado como fallido: {nombre_archivo} (tipo: {tipo_archivo}, intentos: {intentos})")


def marcar_como_exitoso(log_directory, nombre_archivo, tipo_archivo, drive_id=None, size_bytes=None, logger=None):
    """
    Marca un archivo como subido exitosamente a Google Drive.
    - Lo agrega a la sección de archivos_exitosos
    - Lo remueve de archivos_fallidos si existía

    Args:
        log_directory: Directorio donde se guarda el JSON
        nombre_archivo: Nombre del archivo que se subió exitosamente
        tipo_archivo: Tipo de archivo ("continuous", "mseed", "event", "tmp", "log")
        drive_id: No se usa (mantiene compatibilidad con subir_archivo.py)
        size_bytes: No se usa (mantiene compatibilidad con subir_archivo.py)
        logger: Logger opcional para registrar la operación
    """
    if tipo_archivo not in TIPOS_ARCHIVO:
        raise ValueError(f"Tipo de archivo inválido: {tipo_archivo}. Debe ser uno de {TIPOS_ARCHIVO}")

    with _file_lock:
        data = _leer_json(log_directory)

        # Agregar a archivos exitosos (solo fecha)
        data["archivos_exitosos"][tipo_archivo][nombre_archivo] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Remover de fallidos si existe
        if nombre_archivo in data["archivos_fallidos"][tipo_archivo]:
            del data["archivos_fallidos"][tipo_archivo][nombre_archivo]

        _escribir_json(log_directory, data)

        if logger:
            logger.info(f"Archivo registrado como exitoso: {nombre_archivo} (tipo: {tipo_archivo})")


def ya_fue_subido(log_directory, nombre_archivo, tipo_archivo):
    """
    Verifica si un archivo ya fue subido exitosamente a Google Drive.

    Args:
        log_directory: Directorio donde se guarda el JSON
        nombre_archivo: Nombre del archivo a verificar
        tipo_archivo: Tipo de archivo ("continuous", "mseed", "event", "tmp", "log")

    Returns:
        bool: True si el archivo ya fue subido, False si necesita subirse
    """
    if tipo_archivo not in TIPOS_ARCHIVO:
        return False

    with _file_lock:
        data = _leer_json(log_directory)
        return nombre_archivo in data["archivos_exitosos"][tipo_archivo]


def esta_protegido(log_directory, nombre_archivo, tipo_archivo):
    """
    Verifica si un archivo está protegido (falló al subirse).

    Args:
        log_directory: Directorio donde se guarda el JSON
        nombre_archivo: Nombre del archivo a verificar
        tipo_archivo: Tipo de archivo ("continuous", "mseed", "event", "tmp", "log")

    Returns:
        bool: True si el archivo está en la lista de fallidos, False si puede borrarse
    """
    if tipo_archivo not in TIPOS_ARCHIVO:
        return False

    with _file_lock:
        data = _leer_json(log_directory)
        return nombre_archivo in data["archivos_fallidos"][tipo_archivo]


def obtener_archivos_fallidos(log_directory, tipo_archivo=None):
    """
    Obtiene la lista de archivos fallidos.

    Args:
        log_directory: Directorio donde se guarda el JSON
        tipo_archivo: Tipo específico a consultar, o None para todos

    Returns:
        dict o list: Diccionario completo o lista de archivos según tipo
    """
    with _file_lock:
        data = _leer_json(log_directory)

        if tipo_archivo:
            if tipo_archivo not in TIPOS_ARCHIVO:
                return {}
            return data["archivos_fallidos"][tipo_archivo]

        return data["archivos_fallidos"]


def obtener_archivos_exitosos(log_directory, tipo_archivo=None):
    """
    Obtiene la lista de archivos subidos exitosamente.

    Args:
        log_directory: Directorio donde se guarda el JSON
        tipo_archivo: Tipo específico a consultar, o None para todos

    Returns:
        dict: Diccionario completo o lista de archivos según tipo
    """
    with _file_lock:
        data = _leer_json(log_directory)

        if tipo_archivo:
            if tipo_archivo not in TIPOS_ARCHIVO:
                return {}
            return data["archivos_exitosos"][tipo_archivo]

        return data["archivos_exitosos"]


def limpiar_archivos_inexistentes(log_directory, directorios_por_tipo, logger=None):
    """
    Limpia del JSON archivos que ya no existen en el sistema de archivos.
    Limpia tanto de archivos_exitosos como de archivos_fallidos.

    Args:
        log_directory: Directorio donde se guarda el JSON
        directorios_por_tipo: Dict con mapeo tipo -> ruta_directorio
            Ej: {"mseed": "/ruta/mseed", "continuous": "/ruta/dat"}
        logger: Logger opcional para registrar la operación

    Returns:
        dict: Métricas de archivos removidos por tipo y totales
    """
    with _file_lock:
        data = _leer_json(log_directory)
        archivos_removidos_exitosos = 0
        archivos_removidos_fallidos = 0
        por_tipo = {}

        for tipo, directorio in directorios_por_tipo.items():
            if tipo not in TIPOS_ARCHIVO:
                continue

            # Validación de seguridad: el directorio debe estar definido y existir físicamente
            if not directorio or not os.path.isdir(directorio):
                if logger:
                    logger.warning(f"Directorio omitido en limpieza (no existe o inválido): '{directorio}' para tipo '{tipo}'")
                continue

            removidos_tipo_exitosos = 0
            removidos_tipo_fallidos = 0

            # Limpiar archivos exitosos
            archivos_a_remover_exitosos = []
            for nombre_archivo in list(data["archivos_exitosos"][tipo].keys()):
                ruta_completa = os.path.join(directorio, nombre_archivo)
                if not os.path.exists(ruta_completa):
                    archivos_a_remover_exitosos.append(nombre_archivo)

            for nombre_archivo in archivos_a_remover_exitosos:
                del data["archivos_exitosos"][tipo][nombre_archivo]
                removidos_tipo_exitosos += 1
                if logger:
                    logger.info(f"Removido de exitosos (archivo ya no existe): {nombre_archivo}")

            # Limpiar archivos fallidos
            archivos_a_remover_fallidos = []
            for nombre_archivo in list(data["archivos_fallidos"][tipo].keys()):
                ruta_completa = os.path.join(directorio, nombre_archivo)
                if not os.path.exists(ruta_completa):
                    archivos_a_remover_fallidos.append(nombre_archivo)

            for nombre_archivo in archivos_a_remover_fallidos:
                del data["archivos_fallidos"][tipo][nombre_archivo]
                removidos_tipo_fallidos += 1
                if logger:
                    logger.info(f"Removido de fallidos (archivo ya no existe): {nombre_archivo}")

            archivos_removidos_exitosos += removidos_tipo_exitosos
            archivos_removidos_fallidos += removidos_tipo_fallidos
            por_tipo[tipo] = {
                "exitosos": removidos_tipo_exitosos,
                "fallidos": removidos_tipo_fallidos
            }

        total_removidos = archivos_removidos_exitosos + archivos_removidos_fallidos
        if total_removidos > 0:
            _escribir_json(log_directory, data)
            if logger:
                logger.info(f"Limpieza completada: {archivos_removidos_exitosos} exitosos, {archivos_removidos_fallidos} fallidos removidos")

        return {
            "exitosos_removidos": archivos_removidos_exitosos,
            "fallidos_removidos": archivos_removidos_fallidos,
            "total_removidos": total_removidos,
            "por_tipo": por_tipo
        }


def obtener_estadisticas(log_directory):
    """
    Obtiene estadísticas de archivos exitosos y fallidos.

    Args:
        log_directory: Directorio donde se guarda el JSON

    Returns:
        dict: Estadísticas por tipo de archivo con contadores de exitosos y fallidos
    """
    with _file_lock:
        data = _leer_json(log_directory)

        estadisticas = {
            "exitosos": {},
            "fallidos": {}
        }

        for tipo in TIPOS_ARCHIVO:
            estadisticas["exitosos"][tipo] = len(data["archivos_exitosos"][tipo])
            estadisticas["fallidos"][tipo] = len(data["archivos_fallidos"][tipo])

        estadisticas["exitosos"]["total"] = sum(estadisticas["exitosos"].values())
        estadisticas["fallidos"]["total"] = sum(estadisticas["fallidos"].values())

        return estadisticas


def obtener_resumen_diagnostico(log_directory, max_ultimos=5):
    """
    Genera un resumen estructurado para herramientas de diagnóstico.

    Args:
        log_directory: Directorio donde se guarda el JSON
        max_ultimos: Cantidad máxima de archivos recientes a listar por tipo

    Returns:
        dict: Resumen con totales, desglose por tipo con últimos N archivos,
              y lista detallada de fallidos activos.
    """
    with _file_lock:
        data = _leer_json(log_directory)

        resumen = {
            "totales": {
                "exitosos": 0,
                "fallidos": 0
            },
            "detalle_por_tipo": {},
            "fallidos_activos": []
        }

        for tipo in TIPOS_ARCHIVO:
            exitosos_tipo = data["archivos_exitosos"].get(tipo, {})
            fallidos_tipo = data["archivos_fallidos"].get(tipo, {})

            cant_exitosos = len(exitosos_tipo)
            cant_fallidos = len(fallidos_tipo)

            resumen["totales"]["exitosos"] += cant_exitosos
            resumen["totales"]["fallidos"] += cant_fallidos

            # Ordenar los exitosos por fecha descendente (más recientes primero)
            ultimos_exitosos = sorted(
                exitosos_tipo.items(),
                key=lambda item: item[1] if isinstance(item[1], str) else "",
                reverse=True
            )[:max_ultimos]

            resumen["detalle_por_tipo"][tipo] = {
                "exitosos": cant_exitosos,
                "fallidos": cant_fallidos,
                "ultimos": ultimos_exitosos
            }

            for nombre_archivo, fecha in fallidos_tipo.items():
                resumen["fallidos_activos"].append({
                    "tipo": tipo,
                    "archivo": nombre_archivo,
                    "fecha": fecha
                })

        return resumen
