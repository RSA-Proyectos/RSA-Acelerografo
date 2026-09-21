#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
test_drive_status_manager.py — Tests unitarios para drive_status_manager.py.

Prueba el ciclo de vida del registro JSON de subidas a Google Drive:
- Inicialización y estructura segura.
- Transiciones de estado (exitoso, fallido, remoción de fallido al tener éxito).
- Consulta de estado (ya_fue_subido, esta_protegido).
- Poda de archivos inexistentes (limpiar_archivos_inexistentes) con validación de seguridad.
- Resumen sintético y estadísticas para diagnóstico automatizado.
"""

import json
import os
import shutil
import sys
import tempfile

# Asegurar que el directorio de operación esté en sys.path
_OPERATION_DRIVE_DIR = os.path.dirname(os.path.abspath(__file__))
if _OPERATION_DRIVE_DIR not in sys.path:
    sys.path.insert(0, _OPERATION_DRIVE_DIR)

import drive_status_manager as dsm


def test_inicializacion_estructura():
    """Verifica que un JSON nuevo se cree con la estructura esperada."""
    temp_dir = tempfile.mkdtemp(prefix="rsa_dsm_test_")
    try:
        data = dsm._leer_json(temp_dir)
        assert "archivos_exitosos" in data
        assert "archivos_fallidos" in data
        for tipo in dsm.TIPOS_ARCHIVO:
            assert tipo in data["archivos_exitosos"]
            assert tipo in data["archivos_fallidos"]
            assert data["archivos_exitosos"][tipo] == {}
            assert data["archivos_fallidos"][tipo] == {}
    finally:
        shutil.rmtree(temp_dir)


def test_marcar_exitoso_y_ya_fue_subido():
    """Verifica el registro de subida exitosa y su consulta."""
    temp_dir = tempfile.mkdtemp(prefix="rsa_dsm_test_")
    try:
        assert not dsm.ya_fue_subido(temp_dir, "DEV00_20260917_100000.mseed", "mseed")

        dsm.marcar_como_exitoso(temp_dir, "DEV00_20260917_100000.mseed", "mseed")
        assert dsm.ya_fue_subido(temp_dir, "DEV00_20260917_100000.mseed", "mseed")

        # Otro archivo o tipo diferente debe retornar False
        assert not dsm.ya_fue_subido(temp_dir, "OTRO.mseed", "mseed")
        assert not dsm.ya_fue_subido(temp_dir, "DEV00_20260917_100000.mseed", "continuous")
    finally:
        shutil.rmtree(temp_dir)


def test_marcar_fallido_y_recuperacion_al_tener_exito():
    """Verifica que un archivo fallido quede protegido y que al tener éxito se remueva de fallidos."""
    temp_dir = tempfile.mkdtemp(prefix="rsa_dsm_test_")
    try:
        archivo = "DEV00_20260917_110000.mseed"

        # 1. Marcar como fallido
        dsm.marcar_como_fallido(temp_dir, archivo, "mseed", intentos=3)
        assert dsm.esta_protegido(temp_dir, archivo, "mseed")
        assert not dsm.ya_fue_subido(temp_dir, archivo, "mseed")

        # 2. Marcar posteriormente como exitoso
        dsm.marcar_como_exitoso(temp_dir, archivo, "mseed")
        assert dsm.ya_fue_subido(temp_dir, archivo, "mseed")
        assert not dsm.esta_protegido(temp_dir, archivo, "mseed")
    finally:
        shutil.rmtree(temp_dir)


def test_limpiar_archivos_inexistentes_poda_correctamente():
    """Verifica que limpiar_archivos_inexistentes elimine solo archivos no presentes en disco."""
    temp_dir = tempfile.mkdtemp(prefix="rsa_dsm_test_")
    try:
        log_dir = os.path.join(temp_dir, "logs")
        mseed_dir = os.path.join(temp_dir, "mseed")
        cont_dir = os.path.join(temp_dir, "continuous")
        os.makedirs(log_dir)
        os.makedirs(mseed_dir)
        os.makedirs(cont_dir)

        # Crear archivos físicos presentes en disco
        open(os.path.join(mseed_dir, "presente1.mseed"), "w").close()
        open(os.path.join(mseed_dir, "presente2.mseed"), "w").close()
        open(os.path.join(mseed_dir, "fallido_en_disco.mseed"), "w").close()
        open(os.path.join(cont_dir, "presente.dat"), "w").close()

        # Registrar en JSON (mezcla de presentes y ausentes)
        dsm.marcar_como_exitoso(log_dir, "presente1.mseed", "mseed")
        dsm.marcar_como_exitoso(log_dir, "presente2.mseed", "mseed")
        dsm.marcar_como_exitoso(log_dir, "borrado_historico.mseed", "mseed")  # NO en disco

        dsm.marcar_como_fallido(log_dir, "fallido_en_disco.mseed", "mseed", 1)  # SÍ en disco
        dsm.marcar_como_fallido(log_dir, "fallido_huerfano.mseed", "mseed", 3)   # NO en disco

        dsm.marcar_como_exitoso(log_dir, "presente.dat", "continuous")
        dsm.marcar_como_exitoso(log_dir, "dat_antiguo.dat", "continuous")      # NO en disco

        directorios = {
            "mseed": mseed_dir,
            "continuous": cont_dir
        }

        # Ejecutar poda
        res = dsm.limpiar_archivos_inexistentes(log_dir, directorios)

        # Validar métricas retornadas
        assert res["exitosos_removidos"] == 2, f"Esperado 2 exitosos removidos, obtenido {res['exitosos_removidos']}"
        assert res["fallidos_removidos"] == 1, f"Esperado 1 fallido removido, obtenido {res['fallidos_removidos']}"
        assert res["total_removidos"] == 3
        assert res["por_tipo"]["mseed"]["exitosos"] == 1
        assert res["por_tipo"]["mseed"]["fallidos"] == 1
        assert res["por_tipo"]["continuous"]["exitosos"] == 1

        # Validar contenido final en JSON
        assert dsm.ya_fue_subido(log_dir, "presente1.mseed", "mseed")
        assert dsm.ya_fue_subido(log_dir, "presente2.mseed", "mseed")
        assert not dsm.ya_fue_subido(log_dir, "borrado_historico.mseed", "mseed")

        assert dsm.esta_protegido(log_dir, "fallido_en_disco.mseed", "mseed")
        assert not dsm.esta_protegido(log_dir, "fallido_huerfano.mseed", "mseed")

        assert dsm.ya_fue_subido(log_dir, "presente.dat", "continuous")
        assert not dsm.ya_fue_subido(log_dir, "dat_antiguo.dat", "continuous")
    finally:
        shutil.rmtree(temp_dir)


def test_limpiar_archivos_inexistentes_protege_ante_directorio_invalido():
    """Verifica que si un directorio no existe físicamente, no se borren sus registros del JSON."""
    temp_dir = tempfile.mkdtemp(prefix="rsa_dsm_test_")
    try:
        log_dir = os.path.join(temp_dir, "logs")
        os.makedirs(log_dir)

        # Registrar un archivo en "event"
        dsm.marcar_como_exitoso(log_dir, "DEV00_evento.mseed", "event")
        assert dsm.ya_fue_subido(log_dir, "DEV00_evento.mseed", "event")

        # Proveer una ruta inexistente
        directorios = {
            "event": "/directorio/inexistente/en/el/sistema"
        }

        res = dsm.limpiar_archivos_inexistentes(log_dir, directorios)

        # No debe haber removido nada
        assert res["total_removidos"] == 0
        assert dsm.ya_fue_subido(log_dir, "DEV00_evento.mseed", "event")
    finally:
        shutil.rmtree(temp_dir)


def test_obtener_estadisticas():
    """Verifica el cálculo de contadores por tipo y totales."""
    temp_dir = tempfile.mkdtemp(prefix="rsa_dsm_test_")
    try:
        dsm.marcar_como_exitoso(temp_dir, "m1.mseed", "mseed")
        dsm.marcar_como_exitoso(temp_dir, "m2.mseed", "mseed")
        dsm.marcar_como_fallido(temp_dir, "f1.mseed", "mseed", 1)
        dsm.marcar_como_exitoso(temp_dir, "c1.dat", "continuous")

        stats = dsm.obtener_estadisticas(temp_dir)

        assert stats["exitosos"]["mseed"] == 2
        assert stats["exitosos"]["continuous"] == 1
        assert stats["exitosos"]["total"] == 3
        assert stats["fallidos"]["mseed"] == 1
        assert stats["fallidos"]["total"] == 1
    finally:
        shutil.rmtree(temp_dir)


def test_obtener_resumen_diagnostico():
    """Verifica la generación del resumen sintético ordenado cronológicamente para diagnóstico."""
    temp_dir = tempfile.mkdtemp(prefix="rsa_dsm_test_")
    try:
        # Registrar múltiples archivos con timestamps simulados directamente en JSON
        data = dsm._inicializar_estructura()
        data["archivos_exitosos"]["mseed"] = {
            "m1.mseed": "2026-09-17 08:00:00",
            "m2.mseed": "2026-09-17 09:00:00",
            "m3.mseed": "2026-09-17 10:00:00",
            "m4.mseed": "2026-09-17 11:00:00",
        }
        data["archivos_fallidos"]["mseed"] = {
            "fallo.mseed": "2026-09-17 09:30:00"
        }
        dsm._escribir_json(temp_dir, data)

        resumen = dsm.obtener_resumen_diagnostico(temp_dir, max_ultimos=2)

        # Totales
        assert resumen["totales"]["exitosos"] == 4
        assert resumen["totales"]["fallidos"] == 1

        # Detalle mseed acotado a 2 más recientes
        detalle_mseed = resumen["detalle_por_tipo"]["mseed"]
        assert detalle_mseed["exitosos"] == 4
        assert detalle_mseed["fallidos"] == 1
        assert len(detalle_mseed["ultimos"]) == 2

        # Deben ser m4 (11:00) y m3 (10:00) en ese orden
        assert detalle_mseed["ultimos"][0][0] == "m4.mseed"
        assert detalle_mseed["ultimos"][1][0] == "m3.mseed"

        # Fallidos activos
        assert len(resumen["fallidos_activos"]) == 1
        assert resumen["fallidos_activos"][0]["archivo"] == "fallo.mseed"
        assert resumen["fallidos_activos"][0]["tipo"] == "mseed"
    finally:
        shutil.rmtree(temp_dir)


def run_all_tests():
    """Punto de entrada para ejecución como script autónomo."""
    tests = [
        test_inicializacion_estructura,
        test_marcar_exitoso_y_ya_fue_subido,
        test_marcar_fallido_y_recuperacion_al_tener_exito,
        test_limpiar_archivos_inexistentes_poda_correctamente,
        test_limpiar_archivos_inexistentes_protege_ante_directorio_invalido,
        test_obtener_estadisticas,
        test_obtener_resumen_diagnostico
    ]

    print("=" * 70)
    print("Ejecutando suite de pruebas unitarias: test_drive_status_manager.py")
    print("=" * 70)

    fallos = 0
    for t in tests:
        nombre = t.__name__
        try:
            t()
            print(f"  [PASS] {nombre}")
        except AssertionError as e:
            print(f"  [FAIL] {nombre}: {e}")
            fallos += 1
        except Exception as e:
            print(f"  [ERROR] {nombre}: {e}")
            fallos += 1

    print("=" * 70)
    if fallos == 0:
        print(f"TODOS LOS TESTS ({len(tests)}) PASARON EXITOSAMENTE.")
        return 0
    else:
        print(f"HUBO {fallos} FALLO(S) EN LA SUITE DE PRUEBAS.")
        return 1


if __name__ == "__main__":
    sys.exit(run_all_tests())
