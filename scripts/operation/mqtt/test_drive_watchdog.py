#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
test_drive_watchdog.py — Tests unitarios para DriveWatchdog.

Prueba la auditoría de sincronización con Google Drive, detección de backlog de
archivos pendientes, archivos protegidos por reintentos fallidos, compatibilidad
de formatos JSON y espacio libre en disco.
"""

import json
import os
import shutil
import sys
import tempfile

_OPERATION_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _OPERATION_DIR not in sys.path:
    sys.path.insert(0, _OPERATION_DIR)

from mqtt.drive_watchdog import DriveWatchdog


def test_sincronizacion_nominal_retorna_ok():
    """Con archivos subidos y sin protegidos, el estado es 'ok'."""
    temp_dir = tempfile.mkdtemp(prefix="rsa_drive_test_")
    try:
        mseed_dir = os.path.join(temp_dir, "mseed")
        os.makedirs(mseed_dir)
        status_file = os.path.join(temp_dir, "drive_status.json")

        # Crear 2 archivos en disco
        open(os.path.join(mseed_dir, "DEV0_1.mseed"), "w").close()
        open(os.path.join(mseed_dir, "DEV0_2.mseed"), "w").close()

        # Registrar ambos como subidos
        status_data = {
            "mseed": {
                "uploaded": ["DEV0_1.mseed", "DEV0_2.mseed"],
                "protected": []
            },
            "last_upload_utc": "2026-09-14T10:00:00Z"
        }
        with open(status_file, "w", encoding="utf-8") as f:
            json.dump(status_data, f)

        watchdog = DriveWatchdog(mseed_dir=mseed_dir, status_file=status_file)
        res = watchdog.evaluar_sincronizacion(station_id="DEV0")

        assert res["status"] == "ok", f"Esperado 'ok', obtenido {res['status']}"
        assert res["reason"] == "all_synced"
        assert res["pending_mseed"] == 0
        assert res["failed_uploads_protected"] == 0
        assert res["station_id"] == "DEV0"
        assert res["last_upload_utc"] == "2026-09-14T10:00:00Z"
        assert isinstance(res["free_disk_percent"], (int, float))
    finally:
        shutil.rmtree(temp_dir)


def test_backlog_archivos_pendientes_retorna_warning():
    """Más de 3 archivos en disco no subidos genera estado 'warning' y 'pending_backlog'."""
    temp_dir = tempfile.mkdtemp(prefix="rsa_drive_test_")
    try:
        mseed_dir = os.path.join(temp_dir, "mseed")
        os.makedirs(mseed_dir)
        status_file = os.path.join(temp_dir, "drive_status.json")

        # Crear 4 archivos pendientes en disco
        for i in range(1, 5):
            open(os.path.join(mseed_dir, f"DEV0_{i}.mseed"), "w").close()

        status_data = {
            "mseed": {
                "uploaded": [],
                "protected": []
            }
        }
        with open(status_file, "w", encoding="utf-8") as f:
            json.dump(status_data, f)

        watchdog = DriveWatchdog(mseed_dir=mseed_dir, status_file=status_file)
        res = watchdog.evaluar_sincronizacion(station_id="DEV0")

        assert res["status"] == "warning", f"Esperado 'warning', obtenido {res['status']}"
        assert res["reason"] == "pending_backlog"
        assert res["pending_mseed"] == 4
        assert res["failed_uploads_protected"] == 0
    finally:
        shutil.rmtree(temp_dir)


def test_archivos_protegidos_fallidos_retorna_warning():
    """Archivos protegidos por reintentos fallidos genera estado 'warning' y 'upload_retry_retained'."""
    temp_dir = tempfile.mkdtemp(prefix="rsa_drive_test_")
    try:
        mseed_dir = os.path.join(temp_dir, "mseed")
        os.makedirs(mseed_dir)
        status_file = os.path.join(temp_dir, "drive_status.json")

        open(os.path.join(mseed_dir, "DEV0_error.mseed"), "w").close()

        status_data = {
            "mseed": {
                "uploaded": [],
                "protected": ["DEV0_error.mseed"]
            }
        }
        with open(status_file, "w", encoding="utf-8") as f:
            json.dump(status_data, f)

        watchdog = DriveWatchdog(mseed_dir=mseed_dir, status_file=status_file)
        res = watchdog.evaluar_sincronizacion(station_id="DEV0")

        assert res["status"] == "warning", f"Esperado 'warning', obtenido {res['status']}"
        assert res["reason"] == "upload_retry_retained"
        assert res["failed_uploads_protected"] == 1
    finally:
        shutil.rmtree(temp_dir)


def test_compatibilidad_formato_uploaded_files_registry():
    """Valida lectura correcta del formato utilizado por drive_status_manager.py."""
    temp_dir = tempfile.mkdtemp(prefix="rsa_drive_test_")
    try:
        mseed_dir = os.path.join(temp_dir, "mseed")
        os.makedirs(mseed_dir)
        status_file = os.path.join(temp_dir, "uploaded_files_registry.json")

        open(os.path.join(mseed_dir, "DEV0_exitoso.mseed"), "w").close()
        open(os.path.join(mseed_dir, "DEV0_fallido.mseed"), "w").close()

        status_data = {
            "archivos_exitosos": {
                "mseed": {
                    "DEV0_exitoso.mseed": "2026-09-14 10:30:00"
                }
            },
            "archivos_fallidos": {
                "mseed": {
                    "DEV0_fallido.mseed": "2026-09-14 10:31:00"
                }
            }
        }
        with open(status_file, "w", encoding="utf-8") as f:
            json.dump(status_data, f)

        watchdog = DriveWatchdog(mseed_dir=mseed_dir, status_file=status_file)
        res = watchdog.evaluar_sincronizacion(station_id="DEV0")

        assert res["status"] == "warning"
        assert res["reason"] == "upload_retry_retained"
        assert res["failed_uploads_protected"] == 1
        assert res["pending_mseed"] == 1  # Solo el fallido está pendiente de subida
        assert res["last_upload_utc"] == "2026-09-14 10:30:00"
    finally:
        shutil.rmtree(temp_dir)


def test_directorios_vacios_e_inexistentes():
    """Directorios no existentes reportan 0 pendientes y estado 'ok'."""
    watchdog = DriveWatchdog(
        mseed_dir="/tmp/directorio_inexistente_rsa_mseed",
        status_file="/tmp/archivo_inexistente_drive.json"
    )
    res = watchdog.evaluar_sincronizacion(station_id="DEV0")

    assert res["status"] == "ok"
    assert res["reason"] == "all_synced"
    assert res["pending_mseed"] == 0
    assert res["failed_uploads_protected"] == 0


def test_json_corrupto_en_status_file():
    """Archivo de estado dañado no causa excepción y es manejado defensivamente."""
    temp_dir = tempfile.mkdtemp(prefix="rsa_drive_test_")
    try:
        mseed_dir = os.path.join(temp_dir, "mseed")
        os.makedirs(mseed_dir)
        status_file = os.path.join(temp_dir, "drive_status.json")

        with open(status_file, "w", encoding="utf-8") as f:
            f.write("ESTO NO ES UN JSON VALIDO {{{{")

        watchdog = DriveWatchdog(mseed_dir=mseed_dir, status_file=status_file)
        res = watchdog.evaluar_sincronizacion(station_id="DEV0")

        assert res["status"] == "ok"
        assert res["reason"] == "all_synced"
        assert res["pending_mseed"] == 0
    finally:
        shutil.rmtree(temp_dir)


def test_tolerancia_hasta_tres_pendientes():
    """Hasta 3 archivos pendientes en disco es considerado nominal (ok)."""
    temp_dir = tempfile.mkdtemp(prefix="rsa_drive_test_")
    try:
        mseed_dir = os.path.join(temp_dir, "mseed")
        os.makedirs(mseed_dir)
        status_file = os.path.join(temp_dir, "drive_status.json")

        # 3 archivos pendientes
        for i in range(1, 4):
            open(os.path.join(mseed_dir, f"DEV0_{i}.mseed"), "w").close()

        with open(status_file, "w", encoding="utf-8") as f:
            json.dump({"mseed": {"uploaded": [], "protected": []}}, f)

        watchdog = DriveWatchdog(mseed_dir=mseed_dir, status_file=status_file)
        res = watchdog.evaluar_sincronizacion(station_id="DEV0")

        assert res["status"] == "ok"
        assert res["reason"] == "all_synced"
        assert res["pending_mseed"] == 3
        assert res["failed_uploads_protected"] == 0
    finally:
        shutil.rmtree(temp_dir)


def test_archivos_protegidos_inexistentes_en_disco_no_generan_warning():
    """Archivos listados en fallidos pero ya inexistentes físicamente en disco no generan falso positivo."""
    temp_dir = tempfile.mkdtemp(prefix="rsa_drive_test_")
    try:
        mseed_dir = os.path.join(temp_dir, "mseed")
        os.makedirs(mseed_dir)
        status_file = os.path.join(temp_dir, "uploaded_files_registry.json")

        # El archivo NO se crea en disco (es un registro huérfano/rotado)
        status_data = {
            "archivos_exitosos": {"mseed": {}},
            "archivos_fallidos": {
                "mseed": {
                    "DEV0_huerfano.mseed": "2026-09-07 10:00:00"
                }
            }
        }
        with open(status_file, "w", encoding="utf-8") as f:
            json.dump(status_data, f)

        watchdog = DriveWatchdog(mseed_dir=mseed_dir, status_file=status_file)
        res = watchdog.evaluar_sincronizacion(station_id="DEV0")

        assert res["status"] == "ok", f"Esperado 'ok', obtenido {res['status']}"
        assert res["reason"] == "all_synced"
        assert res["pending_mseed"] == 0
        assert res["failed_uploads_protected"] == 0
    finally:
        shutil.rmtree(temp_dir)


if __name__ == "__main__":
    tests = [
        test_sincronizacion_nominal_retorna_ok,
        test_backlog_archivos_pendientes_retorna_warning,
        test_archivos_protegidos_fallidos_retorna_warning,
        test_compatibilidad_formato_uploaded_files_registry,
        test_directorios_vacios_e_inexistentes,
        test_json_corrupto_en_status_file,
        test_tolerancia_hasta_tres_pendientes,
        test_archivos_protegidos_inexistentes_en_disco_no_generan_warning,
    ]

    print("\n=================================================================")
    print("  Tests: mqtt/test_drive_watchdog.py")
    print("=================================================================\n")

    todos_ok = True
    for test_fn in tests:
        try:
            test_fn()
            print(f"  ✅ {test_fn.__name__}")
        except Exception as e:
            print(f"  ❌ {test_fn.__name__}: {e}")
            todos_ok = False

    print("\n=================================================================")
    if todos_ok:
        print(f"  Resultado: {len(tests)}/{len(tests)} tests pasados — Todo OK ✅")
        sys.exit(0)
    else:
        print("  Resultado: FALLARON algunos tests ❌")
        sys.exit(1)
    print("=================================================================\n")
