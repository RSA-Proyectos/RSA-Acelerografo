#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
test_gestor_eventos.py — Tests unitarios para la administración de eventos extraídos en gestor_archivos_acq.py.

Verifica:
1. Detección y encolamiento de eventos pendientes para subida a Drive.
2. Omisión de eventos ya subidos (ya_fue_subido == True).
3. Política de retención temporal: eventos > 30 días se purgan, eventos recientes se preservan.
4. Protección ante fallos: eventos marcados en archivos_fallidos.event no se eliminan.
5. Poda del registro JSON: al eliminar un evento de disco, limpiar_archivos_inexistentes()
   lo remueve del registro JSON manteniendo el principio de Registro Espejo.
"""

import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta

# Asegurar que el directorio de operación esté en sys.path
_OPERATION_DRIVE_DIR = os.path.dirname(os.path.abspath(__file__))
if _OPERATION_DRIVE_DIR not in sys.path:
    sys.path.insert(0, _OPERATION_DRIVE_DIR)

import drive_status_manager as dsm
import gestor_archivos_acq as gacq


class DummyLogger:
    def __init__(self):
        self.logs = []

    def info(self, msg, *args):
        self.logs.append(("INFO", msg))

    def warning(self, msg, *args):
        self.logs.append(("WARNING", msg))

    def error(self, msg, *args):
        self.logs.append(("ERROR", msg))

    def skip(self, type_file, name_file, reason):
        self.logs.append(("SKIP", type_file, name_file, reason))

    def delete_age(self, type_file, name_file, age):
        self.logs.append(("DELETE_AGE", type_file, name_file, age))

    def delete_space(self, type_file, name_file, threshold):
        self.logs.append(("DELETE_SPACE", type_file, name_file, threshold))

    def protected(self, type_file, name_file, reason):
        self.logs.append(("PROTECTED", type_file, name_file, reason))

    def summary(self, **kwargs):
        self.logs.append(("SUMMARY", kwargs))


def test_deteccion_y_encolamiento_eventos():
    """Verifica que los eventos no subidos se detecten y los ya subidos se omitan."""
    temp_dir = tempfile.mkdtemp(prefix="rsa_event_test_")
    try:
        log_dir = os.path.join(temp_dir, "logs")
        eventos_dir = os.path.join(temp_dir, "eventos")
        os.makedirs(log_dir)
        os.makedirs(eventos_dir)

        # Crear 2 eventos de prueba
        evt1 = "DEV00_20260917_010000_010500.mseed"
        evt2 = "DEV00_20260917_020000_020500.mseed"
        with open(os.path.join(eventos_dir, evt1), "wb") as f:
            f.write(b"DATA1")
        with open(os.path.join(eventos_dir, evt2), "wb") as f:
            f.write(b"DATA2")

        # Marcar evt1 como ya subido en el registro
        dsm.marcar_como_exitoso(log_dir, evt1, "event", drive_id="folder123", size_bytes=5)

        logger = DummyLogger()
        archivos_para_subir = []
        archivos_ya_subidos_count = 0

        archivos_eventos = [f for f in os.listdir(eventos_dir) if f.endswith(".mseed")]
        archivos_eventos_paths = [os.path.join(eventos_dir, f) for f in archivos_eventos]
        archivos_eventos_ordenados = sorted(archivos_eventos_paths, key=os.path.getmtime)

        for path in archivos_eventos_ordenados:
            nombre_archivo = os.path.basename(path)
            if dsm.ya_fue_subido(log_dir, nombre_archivo, "event"):
                archivos_ya_subidos_count += 1
                logger.skip("event", nombre_archivo, "already_uploaded")
            else:
                archivos_para_subir.append((path, "event"))

        assert archivos_ya_subidos_count == 1, "Debe omitir 1 evento ya subido"
        assert len(archivos_para_subir) == 1, "Debe encolar 1 evento pendiente"
        assert archivos_para_subir[0][0] == os.path.join(eventos_dir, evt2)
        assert archivos_para_subir[0][1] == "event"
    finally:
        shutil.rmtree(temp_dir)


def test_retencion_temporal_eventos():
    """Verifica que eventos con antigüedad > 30 días se eliminen y recientes se preserven."""
    temp_dir = tempfile.mkdtemp(prefix="rsa_event_test_")
    try:
        log_dir = os.path.join(temp_dir, "logs")
        eventos_dir = os.path.join(temp_dir, "eventos")
        os.makedirs(log_dir)
        os.makedirs(eventos_dir)

        evt_antiguo = "DEV00_20260801_000000.mseed"
        evt_reciente = "DEV00_20260916_000000.mseed"

        path_antiguo = os.path.join(eventos_dir, evt_antiguo)
        path_reciente = os.path.join(eventos_dir, evt_reciente)

        with open(path_antiguo, "wb") as f:
            f.write(b"OLD")
        with open(path_reciente, "wb") as f:
            f.write(b"NEW")

        # Simular fecha de modificación de 45 días atrás para el antiguo
        hace_45_dias = time.time() - (45 * 86400)
        os.utime(path_antiguo, (hace_45_dias, hace_45_dias))

        logger = DummyLogger()
        retener_dias_event = 30
        archivos_event_paths = [path_antiguo, path_reciente]

        for ruta_archivo in archivos_event_paths:
            antiguedad = gacq.calcular_antiguedad_dias(ruta_archivo)
            if antiguedad > retener_dias_event:
                eliminado = gacq.eliminar_archivo_con_verificacion(
                    ruta_archivo, "event", log_dir, logger, dry_run=False
                )
                if eliminado:
                    logger.delete_age("event", os.path.basename(ruta_archivo), antiguedad)

        assert not os.path.exists(path_antiguo), "El evento antiguo de 45 días debe haberse eliminado"
        assert os.path.exists(path_reciente), "El evento reciente debe conservarse"
    finally:
        shutil.rmtree(temp_dir)


def test_proteccion_evento_fallido():
    """Verifica que un evento con fallo de subida NO se elimine aunque supere la retención."""
    temp_dir = tempfile.mkdtemp(prefix="rsa_event_test_")
    try:
        log_dir = os.path.join(temp_dir, "logs")
        eventos_dir = os.path.join(temp_dir, "eventos")
        os.makedirs(log_dir)
        os.makedirs(eventos_dir)

        evt_fallido = "DEV00_20260801_FALLIDO.mseed"
        path_fallido = os.path.join(eventos_dir, evt_fallido)
        with open(path_fallido, "wb") as f:
            f.write(b"FAIL")

        # Marcar como fallido en el registro JSON
        dsm.marcar_como_fallido(log_dir, evt_fallido, "event", intentos=3)
        assert dsm.esta_protegido(log_dir, evt_fallido, "event")

        # Modificar timestamp a 45 días atrás
        hace_45_dias = time.time() - (45 * 86400)
        os.utime(path_fallido, (hace_45_dias, hace_45_dias))

        logger = DummyLogger()
        eliminado = gacq.eliminar_archivo_con_verificacion(
            path_fallido, "event", log_dir, logger, dry_run=False
        )

        assert not eliminado, "eliminar_archivo_con_verificacion debe retornar False para archivo protegido"
        assert os.path.exists(path_fallido), "El archivo fallido protegido NO debe eliminarse de disco"
    finally:
        shutil.rmtree(temp_dir)


def test_poda_registro_tras_borrado_evento():
    """Verifica que al borrarse un evento, la poda automática lo elimine del registro JSON."""
    temp_dir = tempfile.mkdtemp(prefix="rsa_event_test_")
    try:
        log_dir = os.path.join(temp_dir, "logs")
        eventos_dir = os.path.join(temp_dir, "eventos")
        os.makedirs(log_dir)
        os.makedirs(eventos_dir)

        evt1 = "EVT_EXISTE.mseed"
        evt2 = "EVT_BORRADO.mseed"

        with open(os.path.join(eventos_dir, evt1), "wb") as f:
            f.write(b"1")
        # evt2 NO se crea físicamente en disco

        dsm.marcar_como_exitoso(log_dir, evt1, "event")
        dsm.marcar_como_exitoso(log_dir, evt2, "event")

        logger = DummyLogger()
        directorios_limpieza = {"event": eventos_dir}
        balance = dsm.limpiar_archivos_inexistentes(log_dir, directorios_limpieza, logger)

        assert balance["exitosos_removidos"] == 1
        assert not dsm.ya_fue_subido(log_dir, evt2, "event"), "El archivo inexistente debe ser purgado del JSON"
        assert dsm.ya_fue_subido(log_dir, evt1, "event"), "El archivo existente debe permanecer en el JSON"
    finally:
        shutil.rmtree(temp_dir)


def test_simulacion_dry_run():
    """Verifica que el flag dry_run impida la eliminación física del archivo."""
    temp_dir = tempfile.mkdtemp(prefix="rsa_event_test_")
    try:
        log_dir = os.path.join(temp_dir, "logs")
        eventos_dir = os.path.join(temp_dir, "eventos")
        os.makedirs(log_dir)
        os.makedirs(eventos_dir)

        evt = "DEV00_20260801_DRY.mseed"
        path_evt = os.path.join(eventos_dir, evt)
        with open(path_evt, "wb") as f:
            f.write(b"DRY")

        hace_45_dias = time.time() - (45 * 86400)
        os.utime(path_evt, (hace_45_dias, hace_45_dias))

        logger = DummyLogger()
        eliminado = gacq.eliminar_archivo_con_verificacion(
            path_evt, "event", log_dir, logger, dry_run=True
        )

        assert eliminado, "En dry_run retorna True simulando la acción"
        assert os.path.exists(path_evt), "En dry_run el archivo físico NO debe ser borrado"
    finally:
        shutil.rmtree(temp_dir)


def main():
    print("=" * 65)
    print("EJECUTANDO SUITE DE TESTS: test_gestor_eventos.py")
    print("=" * 65)

    tests = [
        ("test_deteccion_y_encolamiento_eventos", test_deteccion_y_encolamiento_eventos),
        ("test_retencion_temporal_eventos", test_retencion_temporal_eventos),
        ("test_proteccion_evento_fallido", test_proteccion_evento_fallido),
        ("test_poda_registro_tras_borrado_evento", test_poda_registro_tras_borrado_evento),
        ("test_simulacion_dry_run", test_simulacion_dry_run),
    ]

    exitos = 0
    for nombre, fn in tests:
        try:
            fn()
            print(f"  ✓ {nombre:<45}: OK")
            exitos += 1
        except AssertionError as e:
            print(f"  ✗ {nombre:<45}: FALLO ({e})")
        except Exception as e:
            print(f"  ✗ {nombre:<45}: ERROR INESPERADO ({e})")

    print("-" * 65)
    print(f"Resultado final: {exitos}/{len(tests)} tests aprobados")
    print("=" * 65)

    if exitos == len(tests):
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
