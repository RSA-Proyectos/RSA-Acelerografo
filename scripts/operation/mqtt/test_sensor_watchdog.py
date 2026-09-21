#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
test_sensor_watchdog.py — Tests unitarios para SensorWatchdog.

Prueba la auditoría de aceleraciones triaxiales en reposo, tolerancias físicas,
validación de fuentes de reloj y manejo defensivo de errores del wrapper.
"""

import os
import sys
import json
import subprocess
from unittest.mock import patch, MagicMock

_OPERATION_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _OPERATION_DIR not in sys.path:
    sys.path.insert(0, _OPERATION_DIR)

from mqtt.sensor_watchdog import SensorWatchdog


def test_lectura_nominal_retorna_ok():
    """Lectura triaxial en reposo dentro de tolerancias retorna status 'ok'."""
    salida_mock = {
        "hora_sistema": "16:14:37",
        "nombre_archivo": "DEV0_260914-160000.dat",
        "tamano_archivo": 2200268,
        "fuente_reloj": "RPi",
        "hora_uc": "16:14:37",
        "aceleracion_x": 0.0123,
        "aceleracion_y": -0.0456,
        "aceleracion_z": 9.8050,
        "error_reloj": None,
    }
    mock_res = MagicMock(returncode=0, stdout=json.dumps(salida_mock), stderr="")
    with patch("subprocess.run", return_value=mock_res):
        watchdog = SensorWatchdog(wrapper_path="/dummy/comprobar_registro_wrapper.py")
        with patch("os.path.isfile", return_value=True):
            res = watchdog.evaluar_integridad(station_id="DEV0")

    assert res["status"] == "ok", f"Esperado 'ok', obtenido {res['status']}"
    assert res["reason"] == "nominal"
    assert res["ax"] == 0.0123
    assert res["ay"] == -0.0456
    assert res["az"] == 9.8050
    assert res["clock_source"] == "RPi"
    assert res["clock_error"] is None
    assert res["station_id"] == "DEV0"
    assert "timestamp" in res


def test_anomalia_eje_z_fuera_de_rango():
    """Aceleración Z fuera de rango [9.01, 10.61] retorna status 'error' y 'accelerometer_anomaly'."""
    salida_mock = {
        "hora_sistema": "16:14:37",
        "nombre_archivo": "DEV0_260914-160000.dat",
        "tamano_archivo": 2200268,
        "fuente_reloj": "GPS",
        "hora_uc": "16:14:37",
        "aceleracion_x": 0.01,
        "aceleracion_y": 0.02,
        "aceleracion_z": 0.0,  # Sensor dañado o desconectado
        "error_reloj": None,
    }
    mock_res = MagicMock(returncode=0, stdout=json.dumps(salida_mock), stderr="")
    with patch("subprocess.run", return_value=mock_res):
        watchdog = SensorWatchdog(wrapper_path="/dummy/comprobar_registro_wrapper.py")
        with patch("os.path.isfile", return_value=True):
            res = watchdog.evaluar_integridad(station_id="DEV0")

    assert res["status"] == "error"
    assert res["reason"] == "accelerometer_anomaly"
    assert "az_out_of_range" in res["clock_error"]
    assert res["az"] == 0.0


def test_anomalia_eje_horizontal():
    """Aceleración horizontal superior a 0.5 m/s^2 retorna status 'error'."""
    salida_mock = {
        "hora_sistema": "16:14:37",
        "nombre_archivo": "DEV0_260914-160000.dat",
        "tamano_archivo": 2200268,
        "fuente_reloj": "GPS",
        "hora_uc": "16:14:37",
        "aceleracion_x": 0.85,  # Excede 0.5
        "aceleracion_y": 0.01,
        "aceleracion_z": 9.81,
        "error_reloj": None,
    }
    mock_res = MagicMock(returncode=0, stdout=json.dumps(salida_mock), stderr="")
    with patch("subprocess.run", return_value=mock_res):
        watchdog = SensorWatchdog(wrapper_path="/dummy/comprobar_registro_wrapper.py")
        with patch("os.path.isfile", return_value=True):
            res = watchdog.evaluar_integridad(station_id="DEV0")

    assert res["status"] == "error"
    assert res["reason"] == "accelerometer_anomaly"
    assert "ax_out_of_range" in res["clock_error"]


def test_error_reloj_reportado():
    """Falla de reloj en la trama retorna status 'error'."""
    salida_mock = {
        "hora_sistema": "16:14:37",
        "nombre_archivo": "DEV0_260914-160000.dat",
        "tamano_archivo": 2200268,
        "fuente_reloj": "E3",
        "hora_uc": "16:14:37",
        "aceleracion_x": 0.01,
        "aceleracion_y": 0.01,
        "aceleracion_z": 9.81,
        "error_reloj": "E3/GPS: No se pudo comprobar la sincronizacion",
    }
    mock_res = MagicMock(returncode=0, stdout=json.dumps(salida_mock), stderr="")
    with patch("subprocess.run", return_value=mock_res):
        watchdog = SensorWatchdog(wrapper_path="/dummy/comprobar_registro_wrapper.py")
        with patch("os.path.isfile", return_value=True):
            res = watchdog.evaluar_integridad(station_id="DEV0")

    assert res["status"] == "error"
    assert "clock_err" in res["clock_error"]


def test_wrapper_no_encontrado():
    """Ruta inexistente del wrapper retorna status 'error' con reason 'wrapper_not_found'."""
    watchdog = SensorWatchdog(wrapper_path="/ruta/inexistente/wrapper.py")
    res = watchdog.evaluar_integridad(station_id="DEV0")
    assert res["status"] == "error"
    assert res["reason"] == "wrapper_not_found"
    assert res["station_id"] == "DEV0"


def test_falla_ejecucion_wrapper():
    """Retorno con código distinto de 0 en el wrapper retorna 'execution_failed'."""
    mock_res = MagicMock(returncode=1, stdout="", stderr="Error critico en binario")
    with patch("subprocess.run", return_value=mock_res):
        watchdog = SensorWatchdog(wrapper_path="/dummy/comprobar_registro_wrapper.py")
        with patch("os.path.isfile", return_value=True):
            res = watchdog.evaluar_integridad(station_id="DEV0")

    assert res["status"] == "error"
    assert res["reason"] == "execution_failed"
    assert res["clock_error"] == "Error critico en binario"


def test_timeout_wrapper():
    """Timeout de ejecución del wrapper retorna 'timeout'."""
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="wrapper", timeout=12)):
        watchdog = SensorWatchdog(wrapper_path="/dummy/comprobar_registro_wrapper.py")
        with patch("os.path.isfile", return_value=True):
            res = watchdog.evaluar_integridad(station_id="DEV0")

    assert res["status"] == "error"
    assert res["reason"] == "timeout"


def test_valores_nulos_en_lectura():
    """Lectura incompleta con aceleraciones en None retorna 'null_readings'."""
    salida_mock = {
        "hora_sistema": "16:14:37",
        "nombre_archivo": "DEV0_260914-160000.dat",
        "tamano_archivo": 2200268,
        "fuente_reloj": "RPi",
        "hora_uc": "16:14:37",
        "aceleracion_x": None,
        "aceleracion_y": 0.01,
        "aceleracion_z": 9.81,
        "error_reloj": None,
    }
    mock_res = MagicMock(returncode=0, stdout=json.dumps(salida_mock), stderr="")
    with patch("subprocess.run", return_value=mock_res):
        watchdog = SensorWatchdog(wrapper_path="/dummy/comprobar_registro_wrapper.py")
        with patch("os.path.isfile", return_value=True):
            res = watchdog.evaluar_integridad(station_id="DEV0")

    assert res["status"] == "error"
    assert res["reason"] == "null_readings"


def test_error_en_salida_json():
    """JSON con clave error retorna 'sensor_check_error'."""
    salida_mock = {"error": "Binario comprobar_registro no encontrado"}
    mock_res = MagicMock(returncode=0, stdout=json.dumps(salida_mock), stderr="")
    with patch("subprocess.run", return_value=mock_res):
        watchdog = SensorWatchdog(wrapper_path="/dummy/comprobar_registro_wrapper.py")
        with patch("os.path.isfile", return_value=True):
            res = watchdog.evaluar_integridad(station_id="DEV0")

    assert res["status"] == "error"
    assert res["reason"] == "sensor_check_error"


if __name__ == "__main__":
    tests = [
        test_lectura_nominal_retorna_ok,
        test_anomalia_eje_z_fuera_de_rango,
        test_anomalia_eje_horizontal,
        test_error_reloj_reportado,
        test_wrapper_no_encontrado,
        test_falla_ejecucion_wrapper,
        test_timeout_wrapper,
        test_valores_nulos_en_lectura,
        test_error_en_salida_json,
    ]

    print("\n=================================================================")
    print("  Tests: mqtt/test_sensor_watchdog.py")
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
