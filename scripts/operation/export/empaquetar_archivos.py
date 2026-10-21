#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
empaquetar_archivos.py — Empaquetado Diario y Exportación SFTP para Estaciones Acelerográficas

DESCRIPCIÓN:
    Herramienta modular y robusta para la compresión, verificación de integridad y
    exportación de datos sismológicos (miniSEED, binarios continuos, eventos o logs).
    - Exclusión mutua de tipos (--mseed [default], --continuous, --events, --logs).
    - Agrupación por días calendario (YYYY-MM-DD) y rango de fechas.
    - Exclusión automática del día en curso en --all-days (salvo --include-today).
    - Bloqueo de seguridad: --delete-source está terminantemente prohibido con --logs.
    - Protección incondicional del archivo activo en grabación SPI (--continuous).
    - Verificación previa de espacio libre en disco (60% estimado + 500 MB libres).
    - Verificación de integridad con tarfile antes de purgar archivos fuentes.
    - Formato de suma de verificación SHA-256 estándar GNU (.sha256).
    - Cerrojo de exclusión mutua no bloqueante (fcntl.flock) apto para crontab.
"""

import os
import sys
import time
import glob
import re
import json
import fcntl
import hashlib
import tarfile
import shutil
import argparse
import logging
from datetime import datetime, date

# ============================================================================
# RESOLUCIÓN DINÁMICA DE RUTAS Y CONFIGURACIÓN
# ============================================================================
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_CURRENT_DIR)

for _p in [_CURRENT_DIR, _PARENT_DIR]:
    if _p not in sys.path:
        sys.path.insert(0, _p)


def obtener_local_root():
    """Resuelve la raíz local del proyecto."""
    local_root = os.getenv("PROJECT_LOCAL_ROOT")
    if local_root and os.path.isdir(local_root):
        return local_root
    default_root = "/home/rsa/projects/acelerografo"
    if os.path.isdir(default_root):
        return default_root
    # Fallback para entorno de desarrollo local
    return os.path.abspath(os.path.join(_PARENT_DIR, ".."))


def cargar_configuracion(local_root):
    """Carga configuracion_dispositivo.json y configuracion_mseed.json si existen."""
    config_disp = {}
    config_mseed = {}
    cfg_dir = os.path.join(local_root, "configuracion")

    path_disp = os.path.join(cfg_dir, "configuracion_dispositivo.json")
    if os.path.isfile(path_disp):
        try:
            with open(path_disp, "r", encoding="utf-8") as f:
                config_disp = json.load(f)
        except Exception:
            pass

    path_ms = os.path.join(cfg_dir, "configuracion_mseed.json")
    if os.path.isfile(path_ms):
        try:
            with open(path_ms, "r", encoding="utf-8") as f:
                config_mseed = json.load(f)
        except Exception:
            pass

    id_estacion = config_disp.get("dispositivo", {}).get("id") or config_mseed.get("CODIGO(1)") or "EST00"
    return id_estacion, config_disp, config_mseed


# ============================================================================
# CERROJO DE EXCLUSIÓN MUTUA (FLOCK)
# ============================================================================
def adquirir_cerrojo(lock_path):
    """
    Adquiere un cerrojo exclusivo no bloqueante en lock_path.
    Retorna el descriptor de archivo o None si ya hay otro proceso activo.
    """
    try:
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        lock_fd = open(lock_path, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_fd
    except (BlockingIOError, IOError):
        return None
    except Exception:
        return None


def liberar_cerrojo(lock_fd):
    """Libera el cerrojo flock y cierra el descriptor."""
    if lock_fd:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
        except Exception:
            pass


# ============================================================================
# IDENTIFICACIÓN DE ARCHIVOS ACTIVOS (GRABACIÓN CONTINUA SPI)
# ============================================================================
def obtener_archivos_activos(local_root, path_registro_continuo):
    """Identifica el archivo binario actualmente en grabación para protegerlo."""
    activos = set()

    tmp_path = os.path.join(local_root, "tmp-files", "NombreArchivoRegistroContinuo.tmp")
    if os.path.isfile(tmp_path):
        try:
            with open(tmp_path, "r", encoding="utf-8") as f:
                lineas = [l.strip() for l in f.readlines() if l.strip()]
                if lineas:
                    activos.add(os.path.basename(lineas[0]))
        except Exception:
            pass

    if os.path.isdir(path_registro_continuo):
        archivos_dat = glob.glob(os.path.join(path_registro_continuo, "*.dat"))
        if archivos_dat:
            mas_reciente = max(archivos_dat, key=os.path.getmtime)
            if (time.time() - os.path.getmtime(mas_reciente)) < 7200:
                activos.add(os.path.basename(mas_reciente))

    return activos


# ============================================================================
# EXTRACCIÓN Y PARSEO DE FECHAS
# ============================================================================
def parsear_fecha_string(fecha_str):
    """Parsea cadenas YYYY-MM-DD o YYYYMMDD a objeto date."""
    if not fecha_str:
        return None
    s = fecha_str.replace("-", "").strip()
    if len(s) == 8 and s.isdigit():
        return datetime.strptime(s, "%Y%m%d").date()
    raise ValueError(f"Formato de fecha inválido: {fecha_str}. Use YYYY-MM-DD.")


def extraer_fecha_de_archivo(file_path):
    """
    Extrae la fecha (objeto date) del nombre de un archivo sismológico.
    Soporta:
      - DEV0_20261019_170000.mseed (YYYYMMDD)
      - DEV00_261019-170000.dat (YYMMDD -> 20YYMMDD)
      - Archivos con timestamps ISO o estándar
      - Fallback: fecha de modificación del archivo (mtime)
    """
    fname = os.path.basename(file_path)

    # 1. Buscar YYYYMMDD (8 dígitos comenzando con 20 o 19)
    m8 = re.search(r"(?:19|20)\d{6}", fname)
    if m8:
        try:
            return datetime.strptime(m8.group(0), "%Y%m%d").date()
        except ValueError:
            pass

    # 2. Buscar patrón YYMMDD_ o YYMMDD- (6 dígitos)
    m6 = re.search(r"(?:_|-)(\d{2})(\d{2})(\d{2})(?:_|-)", fname)
    if m6:
        yy, mm, dd = m6.groups()
        try:
            return datetime.strptime(f"20{yy}{mm}{dd}", "%Y%m%d").date()
        except ValueError:
            pass

    # 3. Fallback: fecha de modificación en disco
    try:
        return datetime.fromtimestamp(os.path.getmtime(file_path)).date()
    except Exception:
        return None


# ============================================================================
# OPERACIONES CRIPTOGRÁFICAS Y COMPRESIÓN
# ============================================================================
def calcular_sha256(file_path, chunk_size=65536):
    """Calcula la suma SHA-256 de un archivo binario."""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            data = f.read(chunk_size)
            if not data:
                break
            sha256.update(data)
    return sha256.hexdigest()


def verificar_integridad_tar(tar_path):
    """Verifica que el archivo .tar.gz pueda abrirse y leer sus miembros correctamente."""
    try:
        with tarfile.open(tar_path, "r:gz") as tar:
            members = tar.getmembers()
            return len(members) > 0
    except Exception:
        return False


def verificar_espacio_para_empaquetar(directorio_destino, tamano_fuentes_bytes, margen_seguridad_mb=500):
    """
    Verifica si el disco tiene espacio libre suficiente para comprimir.
    Estima el tamaño comprimido al 60% del tamaño original más el margen de seguridad.
    """
    try:
        target = directorio_destino if os.path.exists(directorio_destino) else "/"
        total, used, free = shutil.disk_usage(target)
        estimado_necesario = (tamano_fuentes_bytes * 0.6) + (margen_seguridad_mb * 1024 * 1024)
        return free >= estimado_necesario, free / (1024 * 1024), estimado_necesario / (1024 * 1024)
    except Exception:
        return True, 99999.0, 0.0


# ============================================================================
# EMPAQUETADO POR DÍA
# ============================================================================
def empaquetar_dia(
    dia,
    tipo,
    id_estacion,
    archivos,
    output_dir,
    delete_source=False,
    force=False,
    dry_run=False,
    verbose=False,
    logger=None
):
    """
    Empaqueta los archivos de un día específico en un .tar.gz y genera su .sha256.
    Retorna un diccionario de resultado.
    """
    dia_str = dia.strftime("%Y%m%d")
    nombre_base = f"{id_estacion}_{dia_str}_{tipo}"
    tar_nombre = f"{nombre_base}.tar.gz"
    tar_path = os.path.join(output_dir, tar_nombre)
    sha_path = os.path.join(output_dir, f"{tar_nombre}.sha256")
    tmp_tar_path = os.path.join(output_dir, f"{tar_nombre}.tmp")

    res = {
        "dia": dia_str,
        "paquete": tar_nombre,
        "archivos_count": len(archivos),
        "status": "ok",
        "error": None,
        "bytes_liberados": 0
    }

    tamano_total = sum(os.path.getsize(f) for f in archivos if os.path.isfile(f))

    # 1. Comprobar si ya existe
    if os.path.isfile(tar_path) and not force:
        res["status"] = "skipped_exists"
        msg = f"  [OMITIDO] Paquete ya existe: {tar_nombre} (use --force para sobrescribir)"
        print(msg)
        if logger:
            logger.info(msg)
        return res

    # 2. Comprobar espacio suficiente
    espacio_ok, free_mb, req_mb = verificar_espacio_para_empaquetar(output_dir, tamano_total)
    if not espacio_ok:
        res["status"] = "error_space"
        res["error"] = f"Espacio insuficiente: {free_mb:.1f} MB libres < {req_mb:.1f} MB estimados"
        msg = f"  [ERROR ESPACIO] {dia_str}: {res['error']}. Omitiendo compresión de este día."
        print(msg)
        if logger:
            logger.error(msg)
        return res

    if dry_run:
        del_msg = " (y eliminaría fuentes)" if delete_source else ""
        print(f"  [DRY-RUN] Se empaquetaría: {dia_str} ({len(archivos)} archivos, {tamano_total / (1024*1024):.2f} MB) ──> {tar_nombre}{del_msg}")
        if verbose:
            for f in archivos:
                print(f"    - {os.path.basename(f)}")
        res["status"] = "dry_run"
        return res

    print(f"  [EMPAQUETANDO] {dia_str} ({len(archivos)} archivos) ──> {tar_nombre} ...", end="", flush=True)

    try:
        os.makedirs(output_dir, exist_ok=True)

        # 3. Comprimir a archivo temporal .tmp
        with tarfile.open(tmp_tar_path, "w:gz") as tar:
            for f in archivos:
                tar.add(f, arcname=os.path.basename(f))

        # 4. Verificar integridad del archivo temporal
        if not verificar_integridad_tar(tmp_tar_path):
            if os.path.exists(tmp_tar_path):
                os.remove(tmp_tar_path)
            res["status"] = "error_integrity"
            res["error"] = "Fallo en verificación de integridad tarfile"
            print(" ERROR (Integridad inválida)")
            if logger:
                logger.error(f"Fallo de integridad al crear {tar_nombre}")
            return res

        # 5. Renombrar atómicamente a .tar.gz
        os.replace(tmp_tar_path, tar_path)

        # 6. Generar SHA-256 en formato estándar GNU (dos espacios entre hash y filename)
        sha_hash = calcular_sha256(tar_path)
        with open(sha_path, "w", encoding="utf-8") as sf:
            sf.write(f"{sha_hash}  {tar_nombre}\n")

        print(" OK")
        if logger:
            logger.info(f"Paquete creado: {tar_nombre} (SHA256: {sha_hash})")

        # 7. Si delete_source está activo, eliminar fuentes tras verificación total
        if delete_source:
            eliminados = 0
            for f in archivos:
                try:
                    os.remove(f)
                    eliminados += 1
                except Exception as e:
                    if logger:
                        logger.error(f"No se pudo eliminar archivo fuente {f}: {e}")
            res["bytes_liberados"] = tamano_total
            print(f"  [PURGADO FUENTES] Eliminados {eliminados}/{len(archivos)} archivos originales ({tamano_total / (1024*1024):.2f} MB liberados).")

    except Exception as e:
        if os.path.exists(tmp_tar_path):
            try:
                os.remove(tmp_tar_path)
            except Exception:
                pass
        res["status"] = "error_exception"
        res["error"] = str(e)
        print(f" ERROR ({e})")
        if logger:
            logger.error(f"Excepción empaquetando {dia_str}: {e}")

    return res


# ============================================================================
# PROGRAMA PRINCIPAL
# ============================================================================
def main():
    parser = argparse.ArgumentParser(
        description="Empaquetado Diario y Exportación SFTP para Estaciones Acelerográficas"
    )

    # 1. Tipo de archivo a empaquetar (exclusión mutua)
    tipo_group = parser.add_mutually_exclusive_group()
    tipo_group.add_argument(
        "--mseed",
        action="store_true",
        default=False,
        help="Empaqueta archivos miniSEED horarios (default si no se especifica otro)"
    )
    tipo_group.add_argument(
        "--continuous",
        action="store_true",
        default=False,
        help="Empaqueta archivos binarios brutos de registro continuo (.dat)"
    )
    tipo_group.add_argument(
        "--events",
        action="store_true",
        default=False,
        help="Empaqueta eventos sísmicos extraídos"
    )
    tipo_group.add_argument(
        "--logs",
        action="store_true",
        default=False,
        help="Empaqueta registros de actividad del sistema (log-files)"
    )

    # 2. Filtro temporal
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Empaqueta exactamente el día indicado (YYYY-MM-DD o YYYYMMDD)"
    )
    parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="Inicio de rango de días (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--end-date",
        type=str,
        default=None,
        help="Fin de rango de días (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--all-days",
        action="store_true",
        default=False,
        help="Escanea el directorio y empaqueta todos los días detectados"
    )

    # 3. Opciones de destino y seguridad
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Directorio de destino para los paquetes .tar.gz (default: /home/rsa/data/packages/)"
    )
    parser.add_argument(
        "--delete-source",
        action="store_true",
        default=False,
        help="Elimina los archivos fuente tras verificar integridad (prohibido con --logs)"
    )
    parser.add_argument(
        "--include-today",
        action="store_true",
        default=False,
        help="Permite empaquetar el día en curso en --all-days (por default se omite)"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Sobrescribe paquetes existentes en el directorio de destino"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Muestra detalles de los archivos incluidos"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Modo simulación: no genera paquetes ni elimina archivos"
    )

    args = parser.parse_args()

    # SALVAGUARDA CRÍTICA 1: Prohibición estricta de --delete-source con --logs
    if args.logs and args.delete_source:
        print("\n[ERROR DE SEGURIDAD] --delete-source está terminantemente prohibido con --logs para proteger los daemons activos.\n", file=sys.stderr)
        sys.exit(2)

    # Resolver tipo predeterminado (--mseed)
    if args.continuous:
        tipo_seleccionado = "continuous"
    elif args.events:
        tipo_seleccionado = "events"
    elif args.logs:
        tipo_seleccionado = "logs"
    else:
        tipo_seleccionado = "mseed"

    local_root = obtener_local_root()
    id_estacion, config_disp, config_mseed = cargar_configuracion(local_root)

    # Configuración de Logging
    log_dir = os.path.join(local_root, "log-files")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "empaquetar.log")

    logger = logging.getLogger("empaquetar")
    logger.setLevel(logging.INFO)
    logger.handlers = []  # Limpiar handlers previos
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(fh)

    # SALVAGUARDA CRÍTICA 2: Cerrojo atómico flock no bloqueante
    lock_file = os.path.join(local_root, "tmp-files", "empaquetar.lock")
    if not os.path.isdir(os.path.join(local_root, "tmp-files")):
        lock_file = f"/tmp/empaquetar_{os.getenv('USER', 'rsa')}.lock"

    lock_fd = adquirir_cerrojo(lock_file)
    if not lock_fd:
        msg = "[AVISO] Ya existe otra instancia de empaquetar en ejecución. Saliendo limpiamente."
        print(msg)
        logger.info(msg)
        sys.exit(0)

    try:
        # Resolver directorio fuente según tipo
        directorios = config_disp.get("directorios", {})
        if tipo_seleccionado == "mseed":
            source_dir = directorios.get("archivos_mseed", "/home/rsa/data/mseed/")
            patron_glob = "*.mseed"
        elif tipo_seleccionado == "continuous":
            source_dir = directorios.get("registro_continuo", "/home/rsa/data/registro-continuo/")
            patron_glob = "*.dat"
        elif tipo_seleccionado == "events":
            source_dir = directorios.get("eventos_extraidos", "/home/rsa/data/eventos-extraidos/")
            patron_glob = "*.*"
        else:  # logs
            source_dir = log_dir
            patron_glob = "*.log*"

        # Resolver directorio destino
        output_dir = args.output_dir or "/home/rsa/data/packages/"
        if not os.path.isabs(output_dir):
            output_dir = os.path.abspath(output_dir)

        print("=" * 68)
        print(f"  EMPAQUETADO Y EXPORTACIÓN SFTP — Estación: {id_estacion}")
        print(f"  Tipo: {tipo_seleccionado.upper()} | Origen: {source_dir}")
        print(f"  Destino: {output_dir}")
        print(f"  Modo: {'[SIMULACIÓN DRY-RUN]' if args.dry_run else '[EJECUCIÓN REAL]'}")
        if args.delete_source:
            print("  Purga: ACTIVADA (--delete-source: se eliminarán originales tras validación)")
        print("=" * 68)

        if not os.path.isdir(source_dir):
            print(f"Directorio fuente no existe: {source_dir}")
            return

        archivos_candidatos = sorted(glob.glob(os.path.join(source_dir, patron_glob)))

        # Protección del archivo continuo activo en grabación SPI
        if tipo_seleccionado == "continuous":
            archivos_activos = obtener_archivos_activos(local_root, source_dir)
            if archivos_activos:
                print(f"  Archivo(s) activo(s) en grabación protegidos: {', '.join(archivos_activos)}")
                archivos_candidatos = [f for f in archivos_candidatos if os.path.basename(f) not in archivos_activos]

        if not archivos_candidatos:
            print("  No se encontraron archivos para empaquetar.")
            return

        # Agrupar archivos por día
        archivos_por_dia = {}
        for f in archivos_candidatos:
            if not os.path.isfile(f):
                continue
            f_date = extraer_fecha_de_archivo(f)
            if f_date:
                archivos_por_dia.setdefault(f_date, []).append(f)

        # Aplicar filtros temporales
        dias_a_procesar = sorted(archivos_por_dia.keys())
        hoy = date.today()

        if args.date:
            target_date = parsear_fecha_string(args.date)
            dias_a_procesar = [d for d in dias_a_procesar if d == target_date]
            if not dias_a_procesar:
                print(f"  No se encontraron archivos correspondientes a la fecha {args.date}.")
                return
        elif args.start_date or args.end_date:
            d_start = parsear_fecha_string(args.start_date) if args.start_date else None
            d_end = parsear_fecha_string(args.end_date) if args.end_date else None

            if d_start:
                dias_a_procesar = [d for d in dias_a_procesar if d >= d_start]
            if d_end:
                dias_a_procesar = [d for d in dias_a_procesar if d <= d_end]
        elif args.all_days:
            # En --all-days se omite el día en curso salvo que se use --include-today
            if not args.include_today:
                if hoy in dias_a_procesar:
                    print(f"  [SALVAGUARDA] Omitiendo día en curso ({hoy}) para no generar paquetes incompletos.")
                    dias_a_procesar = [d for d in dias_a_procesar if d < hoy]
        else:
            # Si no se pasó filtro temporal en logs, empaquetar todo bajo hoy
            if tipo_seleccionado == "logs":
                dias_a_procesar = [hoy]
                archivos_por_dia[hoy] = archivos_candidatos
            else:
                # Default para datos: requiere --date, --start-date o --all-days
                print("\n[AVISO] Especifique el rango temporal: --date YYYY-MM-DD, --start-date/--end-date, o --all-days.")
                print("Ejemplo: empaquetar --all-days\n")
                return

        print(f"\nDías a procesar: {len(dias_a_procesar)}")
        total_paquetes = 0
        total_liberados = 0

        for d in dias_a_procesar:
            archivos_dia = archivos_por_dia.get(d, [])
            if not archivos_dia:
                continue

            res_dia = empaquetar_dia(
                dia=d,
                tipo=tipo_seleccionado,
                id_estacion=id_estacion,
                archivos=archivos_dia,
                output_dir=output_dir,
                delete_source=args.delete_source,
                force=args.force,
                dry_run=args.dry_run,
                verbose=args.verbose,
                logger=logger
            )

            if res_dia["status"] in ["ok", "dry_run"]:
                total_paquetes += 1
                total_liberados += res_dia.get("bytes_liberados", 0)

        print("\n" + "=" * 68)
        print(f"  Resumen: {total_paquetes} paquete(s) procesado(s).")
        if args.delete_source and not args.dry_run:
            print(f"  Espacio liberado total: {total_liberados / (1024 * 1024):.2f} MB")
        print("=" * 68 + "\n")

    finally:
        liberar_cerrojo(lock_fd)


if __name__ == "__main__":
    main()
