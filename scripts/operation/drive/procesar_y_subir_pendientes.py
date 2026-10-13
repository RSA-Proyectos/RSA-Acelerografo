#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
procesar_y_subir_pendientes.py — Conversión y Subida Resiliente de Archivos Pendientes

DESCRIPCIÓN:
    Herramienta integral de sincronización y puesta al día para acelerógrafos DEV00.
    1. Fase 1 (Conversión Local Garantizada): Escanea archivos binarios (.dat) acumulados
       en el directorio de registro continuo, excluye el archivo activo en grabación SPI,
       comprueba si ya existe su equivalente .mseed (idempotencia) y convierte los pendientes
       de forma secuencial y con recolección de basura, sin requerir conexión de red.
    2. Fase 2 (Subida Resiliente a Drive): Verifica resolución DNS real a www.googleapis.com.
       Si el DNS falla, aplica aborto temprano (fail-fast) sin saturar RAM ni reintentar
       inútilmente. Si el DNS está nominal, sube archivos pendientes con cerrojo flock,
       dosificación por lotes (--batch-size), pausas anti-bufferbloat y registro en
       uploaded_files_registry.json.

USO:
    python3 procesar_y_subir_pendientes.py                    # Conversión y subida (lote de 20)
    python3 procesar_y_subir_pendientes.py --solo-convertir   # Solo conversión local de .dat a .mseed
    python3 procesar_y_subir_pendientes.py --solo-subir       # Solo subida de .mseed/eventos a Drive
    python3 procesar_y_subir_pendientes.py --dry-run          # Simulación y diagnóstico sin escrituras
    python3 procesar_y_subir_pendientes.py --batch-size 10    # Subir un lote de máximo 10 archivos
    python3 procesar_y_subir_pendientes.py --all              # Subir todos los archivos pendientes
"""

import os
import sys
import time
import socket
import fcntl
import json
import gc
import glob
import argparse
from datetime import datetime

# ============================================================================
# RESOLUCIÓN DINÁMICA DE IMPORTACIONES (COMPATIBLE CON DEV Y DEPLOY)
# ============================================================================
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_CURRENT_DIR)

# Agregar directorios locales al path
for _p in [_CURRENT_DIR, _PARENT_DIR]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Directorio de mseed dentro del repo
_MSEED_REPO_DIR = os.path.join(_PARENT_DIR, "mseed")
if os.path.isdir(_MSEED_REPO_DIR) and _MSEED_REPO_DIR not in sys.path:
    sys.path.insert(0, _MSEED_REPO_DIR)

# Soporte para despliegue en Raspberry Pi ($PROJECT_LOCAL_ROOT)
project_local_root = os.getenv("PROJECT_LOCAL_ROOT", "")
if project_local_root:
    for _sub in ["scripts", os.path.join("scripts", "drive"), os.path.join("scripts", "mseed")]:
        _full_p = os.path.join(project_local_root, _sub)
        if os.path.isdir(_full_p) and _full_p not in sys.path:
            sys.path.insert(0, _full_p)

# Importar funciones de módulos del proyecto
from subir_archivo import (
    Try_Autenticar_Drive,
    subir_archivo_con_reintentos,
    SCOPES
)
from drive_status_manager import (
    ya_fue_subido,
    marcar_como_exitoso,
    marcar_como_fallido
)
from binary_to_mseed import (
    procesar_archivo_individual,
    nombrar_archivo_mseed,
    extraer_tiempo_binario
)

try:
    from structured_logger import StructuredLogger
except ImportError:
    # Fallback si structured_logger está en scripts/
    _scripts_root = os.path.join(project_local_root, "scripts") if project_local_root else _PARENT_DIR
    sys.path.insert(0, _scripts_root)
    from structured_logger import StructuredLogger

# Constantes del sistema
LOCK_FILE_PATH = "/tmp/procesar_y_subir_pendientes.lock"
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
LOG_BACKUP_COUNT = 3


# ============================================================================
# FUNCIONES AUXILIARES DE ENTORNO Y CONFIGURACIÓN
# ============================================================================
def read_fileJSON(path_archivo):
    """Lee y parsea un archivo JSON de configuración de forma segura."""
    try:
        with open(path_archivo, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Error: Archivo no encontrado: {path_archivo}")
        return None
    except json.JSONDecodeError as e:
        print(f"Error al decodificar JSON en {path_archivo}: {e}")
        return None


def obtener_logger(id_estacion, log_directory, verbosity="SUMMARY", dry_run=False):
    """Inicializa el logger estructurado dedicado."""
    filename = "procesar_pendientes_dry-run.log" if dry_run else "procesar_pendientes.log"
    return StructuredLogger(
        id_estacion=id_estacion,
        log_directory=log_directory,
        log_filename=filename,
        verbosity=verbosity,
        max_bytes=LOG_MAX_BYTES,
        backup_count=LOG_BACKUP_COUNT
    )


def adquirir_cerrojo():
    """
    Intenta adquirir un cerrojo exclusivo no bloqueante en LOCK_FILE_PATH.
    Retorna el descriptor de archivo si tuvo éxito, o None si ya hay otro proceso.
    """
    try:
        lock_fd = open(LOCK_FILE_PATH, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_fd
    except (BlockingIOError, IOError):
        return None


def liberar_cerrojo(lock_fd):
    """Libera el cerrojo de exclusión mutua."""
    if lock_fd:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
        except Exception:
            pass


# ============================================================================
# FASE 1: CONVERSIÓN LOCAL DE ARCHIVOS BINARIOS PENDIENTES
# ============================================================================
def obtener_archivos_activos(local_root, path_registro_continuo):
    """
    Identifica el archivo binario actualmente en grabación para protegerlo de la conversión.
    Combina:
      1. El archivo temporal NombreArchivoRegistroContinuo.tmp (línea 1).
      2. Salvaguarda por mtime del archivo más reciente si tiene menos de 2 horas.
    """
    activos = set()

    # 1. Archivo reportado por el daemon C
    tmp_path = os.path.join(local_root, "tmp-files", "NombreArchivoRegistroContinuo.tmp")
    if os.path.isfile(tmp_path):
        try:
            with open(tmp_path, "r", encoding="utf-8") as f:
                lineas = [l.strip() for l in f.readlines() if l.strip()]
                if lineas:
                    activos.add(os.path.basename(lineas[0]))
        except Exception:
            pass

    # 2. Salvaguarda por tiempo de modificación reciente
    if os.path.isdir(path_registro_continuo):
        archivos_dat = glob.glob(os.path.join(path_registro_continuo, "*.dat"))
        if archivos_dat:
            mas_reciente = max(archivos_dat, key=os.path.getmtime)
            # Si fue modificado hace menos de 2 horas (7200 s), está potencialmente en uso
            if (time.time() - os.path.getmtime(mas_reciente)) < 7200:
                activos.add(os.path.basename(mas_reciente))

    return activos


def convertir_archivos_pendientes(
    path_registro_continuo,
    path_archivos_mseed,
    codigo_estacion,
    config_mseed,
    archivos_activos,
    logger,
    usar_fecha_filename=False,
    max_convertir=None,
    dry_run=False
):
    """
    Convierte secuencialmente archivos binarios .dat no convertidos a .mseed.
    Es 100% idempotente: si el .mseed ya existe en disco, se omite de inmediato.
    Si se especifica max_convertir (int), se detiene al alcanzar dicho cupo.
    """
    resumen = {
        "total_dat": 0,
        "excluidos_activos": 0,
        "ya_convertidos": 0,
        "convertidos_exitosos": 0,
        "fallidos": 0,
        "detalles_fallidos": []
    }

    if not os.path.isdir(path_registro_continuo):
        logger.error("dirs", f"Directorio de registro continuo no existe: {path_registro_continuo}")
        return resumen

    if not os.path.isdir(path_archivos_mseed):
        if not dry_run:
            os.makedirs(path_archivos_mseed, exist_ok=True)

    archivos_dat = sorted(glob.glob(os.path.join(path_registro_continuo, "*.dat")))
    resumen["total_dat"] = len(archivos_dat)

    if max_convertir is not None and max_convertir <= 0:
        return resumen

    for binary_file in archivos_dat:
        # Control de retro-presión: detener si se completó el cupo asignado
        if max_convertir is not None and resumen["convertidos_exitosos"] >= max_convertir:
            print(f"\n  [CUPO ALCANZADO] Se completó el cupo de conversión ({max_convertir} archivo(s)).")
            break

        binary_filename = os.path.basename(binary_file)

        # 1. Proteger archivo activo
        if binary_filename in archivos_activos:
            resumen["excluidos_activos"] += 1
            logger.info(f"Omitiendo archivo activo en grabación SPI: {binary_filename}")
            continue

        # 2. Extraer tiempo para determinar el nombre .mseed esperado
        tiempo_binario = extraer_tiempo_binario(binary_file, usar_fecha_filename)
        if tiempo_binario is None:
            resumen["fallidos"] += 1
            error_msg = "Trama binaria insuficiente o dañada"
            resumen["detalles_fallidos"].append((binary_filename, error_msg))
            logger.convert_fail(binary_filename, error_msg)
            continue

        # 3. Comprobar si el archivo .mseed ya existe (idempotencia)
        mseed_filename = nombrar_archivo_mseed(codigo_estacion, tiempo_binario)
        mseed_completo = os.path.join(path_archivos_mseed, mseed_filename)

        if os.path.isfile(mseed_completo) and os.path.getsize(mseed_completo) > 0:
            resumen["ya_convertidos"] += 1
            continue

        # 4. Archivo pendiente de conversión
        if dry_run:
            resumen["convertidos_exitosos"] += 1
            print(f"  [DRY-RUN] Se convertiría: {binary_filename} ──> {mseed_filename}")
        else:
            print(f"  [CONVIRTIENDO] {binary_filename} ──> {mseed_filename} ...", end="", flush=True)
            exito, mensaje, info = procesar_archivo_individual(
                binary_file,
                path_archivos_mseed,
                codigo_estacion,
                config_mseed,
                logger,
                usar_fecha_filename
            )

            if exito:
                resumen["convertidos_exitosos"] += 1
                tiempo_seg = info['tiempo_lectura'] if info else 0.0
                print(f" OK ({tiempo_seg:.2f}s)")
                gc.collect()  # Liberar memoria de numpy/obspy
            else:
                resumen["fallidos"] += 1
                resumen["detalles_fallidos"].append((binary_filename, mensaje))
                print(f" ERROR ({mensaje})")

    return resumen


# ============================================================================
# FASE 2: VERIFICACIÓN DNS REAL Y SUBIDA RESILIENTE A GOOGLE DRIVE
# ============================================================================
def verificar_dns_drive(host="www.googleapis.com", port=443, timeout=5):
    """
    Verifica resolución DNS real del dominio de Google Drive.
    Previene falsos positivos cuando el servidor corporativo (ej. 192.170.100.250)
    tiene salida IP pura pero falla en resolver nombres de dominio.
    """
    try:
        socket.setdefaulttimeout(timeout)
        socket.getaddrinfo(host, port)
        return True
    except Exception:
        return False


def obtener_cola_subida_drive(config_dispositivo, log_directory):
    """
    Construye la lista ordenada de archivos miniSEED y eventos en disco
    pendientes de subir a Google Drive, priorizando eventos sísmicos.
    Retorna una lista de tuplas: (f_path, fname, tipo, folder_id).
    """
    cola = []
    drive_config = config_dispositivo.get("drive", {})
    carpetas_drive = drive_config.get("carpetas", {})
    mseed_id = carpetas_drive.get("mseed_id", "")
    events_id = carpetas_drive.get("events_id", "")

    path_mseed = config_dispositivo.get("directorios", {}).get("archivos_mseed", "")
    path_eventos = config_dispositivo.get("directorios", {}).get("eventos_extraidos", "")

    # Prioridad 1: Eventos sísmicos extraídos
    if path_eventos and os.path.isdir(path_eventos) and events_id:
        archivos_ev = sorted(glob.glob(os.path.join(path_eventos, "*.*")), key=os.path.getmtime)
        for f_path in archivos_ev:
            fname = os.path.basename(f_path)
            if fname.endswith((".mseed", ".dat")) and not ya_fue_subido(log_directory, fname, "event"):
                cola.append((f_path, fname, "event", events_id))

    # Prioridad 2: Archivos continuos miniSEED
    if path_mseed and os.path.isdir(path_mseed) and mseed_id:
        archivos_ms = sorted(glob.glob(os.path.join(path_mseed, "*.mseed")), key=os.path.getmtime)
        for f_path in archivos_ms:
            fname = os.path.basename(f_path)
            if not ya_fue_subido(log_directory, fname, "mseed"):
                cola.append((f_path, fname, "mseed", mseed_id))

    return cola


def subir_archivos_pendientes(
    local_root,
    config_dispositivo,
    log_directory,
    logger,
    batch_size=20,
    all_files=False,
    dns_verificado=False,
    dry_run=False
):
    """
    Sube archivos miniSEED y eventos pendientes a Google Drive con control de lotes,
    espaciado anti-bufferbloat y aborto temprano (fail-fast) ante caídas de DNS.
    """
    resumen = {
        "status": "ok",
        "dns_ok": True,
        "total_pendientes": 0,
        "subidos_exitosos": 0,
        "subidos_fallidos": 0,
        "detalles_fallidos": []
    }

    # 1. Comprobación real de resolución DNS (si no fue validada previamente)
    if not dns_verificado and not verificar_dns_drive():
        resumen["dns_ok"] = False
        resumen["status"] = "dns_unreachable"
        msg = (
            "[FAIL-FAST DNS] No es posible resolver 'www.googleapis.com'.\n"
            "El servidor DNS corporativo no responde o no resuelve dominios de Google.\n"
            "Aviso: Se omite la fase de subida a Drive para proteger la memoria RAM.\n"
            "Los archivos miniSEED convertidos se mantienen resguardados en almacenamiento local (>90 días de autonomía)."
        )
        print(f"\n{msg}\n")
        logger.warning(msg)
        return resumen

    # 2. Rutas e IDs de Drive
    drive_config = config_dispositivo.get("drive", {})

    # 3. Autenticación con Google Drive
    credentials_file = os.path.join(local_root, "configuracion", "drive_credentials.json")
    token_file = os.path.join(local_root, "configuracion", "drive_token.json")

    service = None
    if not dry_run:
        service = Try_Autenticar_Drive(SCOPES, credentials_file, token_file, logger)
        if not service:
            resumen["status"] = "auth_error"
            logger.error("auth_drive", "Fallo al autenticar cliente de Google Drive")
            print("Error: No se pudo autenticar con Google Drive.")
            return resumen

    # 4. Construir cola de archivos pendientes (priorizando eventos)
    cola_subida = obtener_cola_subida_drive(config_dispositivo, log_directory)

    resumen["total_pendientes"] = len(cola_subida)

    if not cola_subida:
        print("  Todos los archivos están sincronizados con Google Drive (0 pendientes).")
        return resumen

    # 5. Dosificar lote
    lote = cola_subida if (all_files or batch_size <= 0) else cola_subida[:batch_size]
    print(f"  Archivos pendientes totales: {len(cola_subida)}. Procesando lote de {len(lote)} archivos...")

    max_reintentos = drive_config.get("config", {}).get("max_reintentos", 3)
    tiempo_espera = drive_config.get("config", {}).get("tiempo_espera", 2)

    for idx, (f_path, fname, tipo, folder_id) in enumerate(lote, 1):
        prefijo = f"[{idx:02d}/{len(lote):02d}] ({tipo})"

        if dry_run:
            print(f"  [DRY-RUN] {prefijo} Se subiría: {fname} a carpeta {folder_id}")
            resumen["subidos_exitosos"] += 1
            continue

        print(f"  {prefijo} Subiendo: {fname} ...", end="", flush=True)
        exito = subir_archivo_con_reintentos(
            service=service,
            nombre_archivo=fname,
            path_completo_archivo=f_path,
            drive_id=folder_id,
            max_reintentos=max_reintentos,
            tiempo_espera=tiempo_espera,
            logger=logger,
            borrar_despues=False,
            tipo_archivo=tipo,
            log_directory=log_directory
        )

        if exito:
            marcar_como_exitoso(log_directory, fname, tipo, logger=logger)
            resumen["subidos_exitosos"] += 1
            print(" OK")
            time.sleep(1.5)  # Espaciado anti-bufferbloat
            gc.collect()     # Liberación de memoria tras subida
        else:
            marcar_como_fallido(log_directory, fname, tipo, intentos=max_reintentos, logger=logger)
            resumen["subidos_fallidos"] += 1
            resumen["detalles_fallidos"].append(fname)
            print(" ERROR")

            # Fail-fast: si el DNS o la red se cayó a mitad del lote, abortar de inmediato
            if not verificar_dns_drive():
                print("\n  [FAIL-FAST] Caída de DNS detectada durante la subida del lote. Abortando lote restante.\n")
                logger.warning("[FAIL_FAST] Caída de DNS corporativo en lote. Abortando.")
                resumen["status"] = "interrupted_by_dns_failure"
                break

    return resumen


# ============================================================================
# PROGRAMA PRINCIPAL (CLI)
# ============================================================================
def main():
    parser = argparse.ArgumentParser(
        description="Conversión y Subida Resiliente de Archivos Pendientes (acelerógrafo DEV00)"
    )
    parser.add_argument(
        "--solo-convertir",
        action="store_true",
        help="Ejecuta exclusivamente la Fase 1 (conversión local de binarios a miniSEED)"
    )
    parser.add_argument(
        "--solo-subir",
        action="store_true",
        help="Ejecuta exclusivamente la Fase 2 (subida resiliente a Google Drive)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Modo simulación y diagnóstico: no altera archivos en disco ni contacta a Drive"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=20,
        help="Cantidad máxima de archivos a subir en esta ejecución (default: 20)"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Procesa todos los archivos pendientes de subida sin límite de lote"
    )
    parser.add_argument(
        "--verbosity",
        choices=["DEBUG", "INFO", "SUMMARY"],
        default="SUMMARY",
        help="Nivel de verbosidad del logger (default: SUMMARY)"
    )

    args = parser.parse_args()

    # 1. Cerrojo atómico para impedir concurrencia
    lock_fd = adquirir_cerrojo()
    if not lock_fd:
        print("[AVISO] Ya existe una instancia de procesar_y_subir_pendientes en ejecución.")
        sys.exit(0)

    try:
        # 2. Resolver rutas del sistema
        local_root = os.getenv("PROJECT_LOCAL_ROOT")
        if not local_root:
            # Fallback a ruta estándar
            default_root = "/home/rsa/projects/acelerografo"
            if os.path.isdir(default_root):
                local_root = default_root
            else:
                # Entorno de desarrollo local
                local_root = os.path.abspath(os.path.join(_PARENT_DIR, ".."))

        config_dir = os.path.join(local_root, "configuracion")
        log_dir = os.path.join(local_root, "log-files")
        os.makedirs(log_dir, exist_ok=True)

        config_disp_file = os.path.join(config_dir, "configuracion_dispositivo.json")
        config_mseed_file = os.path.join(config_dir, "configuracion_mseed.json")

        config_dispositivo = read_fileJSON(config_disp_file)
        if not config_dispositivo:
            print(f"Error crítico: No se pudo cargar {config_disp_file}")
            sys.exit(1)

        config_mseed = read_fileJSON(config_mseed_file)
        if not config_mseed:
            print(f"Error crítico: No se pudo cargar {config_mseed_file}")
            sys.exit(1)

        id_estacion = config_dispositivo.get("dispositivo", {}).get("id", "Unknown")
        codigo_estacion = config_mseed.get("CODIGO(1)", id_estacion)
        usar_fecha_filename = config_mseed.get("USAR_FECHA_FILENAME", False)

        path_rc = config_dispositivo.get("directorios", {}).get("registro_continuo", "/home/rsa/data/registro-continuo/")
        path_ms = config_dispositivo.get("directorios", {}).get("archivos_mseed", "/home/rsa/data/mseed/")

        logger = obtener_logger(id_estacion, log_dir, verbosity=args.verbosity, dry_run=args.dry_run)
        logger.init(f"Iniciando procesar_y_subir_pendientes (dry_run={args.dry_run})")

        print("=" * 65)
        print(f"  SINCRONIZACIÓN DE ARCHIVOS PENDIENTES — Estación: {id_estacion}")
        print(f"  Modo: {'[SIMULACIÓN DRY-RUN]' if args.dry_run else '[EJECUCIÓN REAL]'}")
        print("=" * 65)

        # 3. VERIFICACIÓN PREVIA DE DNS (FAIL-FAST)
        dns_ok_previo = False
        if not args.solo_convertir:
            print("\n[VERIFICACIÓN PREVIA DNS] Comprobando conectividad con Google Drive...")
            dns_ok_previo = verificar_dns_drive()
            if not dns_ok_previo:
                msg = (
                    "[FAIL-FAST DNS] No es posible resolver 'www.googleapis.com'.\n"
                    "El servidor DNS corporativo no responde o no resuelve dominios de Google.\n"
                    "Aviso: Se omite la conversión y la subida para proteger el almacenamiento en disco y la RAM.\n"
                    "Los archivos binarios .dat se mantienen resguardados localmente (>90 días de autonomía)."
                )
                print(f"  FALLÓ.\n\n{msg}\n")
                logger.warning(msg)
                print("=" * 65)
                print("  Proceso finalizado preventivamente por indisponibilidad de DNS.")
                print("=" * 65 + "\n")
                return
            print("  Resolución DNS: OK (servicios de Google disponibles).")

        # 4. FASE 1: Conversión Local de Binarios Pendientes con Cupo Dinámico
        if not args.solo_subir:
            print("\n[FASE 1] Conversión Local de Archivos Binarios a miniSEED:")
            activos = obtener_archivos_activos(local_root, path_rc)
            if activos:
                print(f"  Archivo(s) activo(s) en grabación protegidos: {', '.join(activos)}")

            # Cálculo de retro-presión: sincronizar conversiones con archivos en cola de subida
            max_a_convertir = None
            if not args.all and args.batch_size > 0:
                if args.solo_convertir:
                    max_a_convertir = args.batch_size
                    print(f"  [MODO SOLO CONVERSIÓN] Lote máximo a convertir: {max_a_convertir} archivo(s).")
                else:
                    cola_existente = obtener_cola_subida_drive(config_dispositivo, log_dir)
                    num_existentes = len(cola_existente)
                    max_a_convertir = max(0, args.batch_size - num_existentes)
                    if max_a_convertir == 0:
                        print(f"  [RETRO-PRESIÓN] Existen {num_existentes} archivo(s) .mseed/eventos en disco esperando subida.")
                        print(f"  Se alcanza el límite del lote (--batch-size {args.batch_size}). Se omite conversión de nuevos .dat.")
                    else:
                        print(f"  [CUPO DINÁMICO] En cola de disco: {num_existentes}. Cupo a convertir: {max_a_convertir} (para lote de {args.batch_size}).")

            res_conv = convertir_archivos_pendientes(
                path_registro_continuo=path_rc,
                path_archivos_mseed=path_ms,
                codigo_estacion=codigo_estacion,
                config_mseed=config_mseed,
                archivos_activos=activos,
                logger=logger,
                usar_fecha_filename=usar_fecha_filename,
                max_convertir=max_a_convertir,
                dry_run=args.dry_run
            )

            print("\n  --- Resumen de Conversión Local ---")
            print(f"  Total .dat encontrados : {res_conv['total_dat']}")
            print(f"  Excluidos (activos)     : {res_conv['excluidos_activos']}")
            print(f"  Omitidos (ya convertidos): {res_conv['ya_convertidos']}")
            print(f"  Convertidos con éxito   : {res_conv['convertidos_exitosos']}")
            print(f"  Fallidos                : {res_conv['fallidos']}")

        # 5. FASE 2: Subida Resiliente a Google Drive
        if not args.solo_convertir:
            print("\n[FASE 2] Subida Controlada a Google Drive:")
            res_sub = subir_archivos_pendientes(
                local_root=local_root,
                config_dispositivo=config_dispositivo,
                log_directory=log_dir,
                logger=logger,
                batch_size=args.batch_size,
                all_files=args.all,
                dns_verificado=dns_ok_previo,
                dry_run=args.dry_run
            )

            print("\n  --- Resumen de Subida a Drive ---")
            print(f"  Resolución DNS Google  : {'OK' if res_sub['dns_ok'] else 'FALLÓ (corporativo)'}")
            print(f"  Total pendientes cola  : {res_sub['total_pendientes']}")
            print(f"  Subidos exitosamente   : {res_sub['subidos_exitosos']}")
            print(f"  Fallidos en lote       : {res_sub['subidos_fallidos']}")
            print(f"  Estado de finalización : {res_sub['status']}")

        print("\n" + "=" * 65)
        print("  Proceso completado satisfactoriamente.")
        print("=" * 65 + "\n")

    finally:
        liberar_cerrojo(lock_fd)


if __name__ == "__main__":
    main()
