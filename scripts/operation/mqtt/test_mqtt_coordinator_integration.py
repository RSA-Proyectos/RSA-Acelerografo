#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
test_mqtt_coordinator_integration.py — Tests de integración para las extensiones de telemetría y comandos en mqtt_coordinator.py.

Verifica:
1. Despacho y ejecución segura del comando stop_acquisition_safety.
2. Despacho de comandos de consulta bajo demanda get_sensor_status y get_drive_status.
3. Publicación de status/sensor con QoS 1 y retain = true.
4. Publicación de status/drive con QoS 1 y retain = true.
5. Publicación de status/acquisition con QoS 1 y retain = true.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

_OPERATION_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _OPERATION_DIR not in sys.path:
    sys.path.insert(0, _OPERATION_DIR)

try:
    from mqtt.mqtt_coordinator import (
        CommandDispatcher,
        publicar_acquisition_status,
        publicar_sensor_status,
        publicar_drive_status,
        publicar_state,
    )
except ImportError:
    from mqtt_coordinator import (
        CommandDispatcher,
        publicar_acquisition_status,
        publicar_sensor_status,
        publicar_drive_status,
        publicar_state,
    )


def _crear_config_dummy():
    return {
        "id": "DEV0",
        "org": "rsa",
        "app": "seismic",
        "cap": "smart",
        "topics": {
            "telemetry_state": "{org}/{app}/{cap}/{id}/telemetry/state",
            "status_acquisition": "{org}/{app}/{cap}/{id}/status/acquisition",
            "status_sensor": "{org}/{app}/{cap}/{id}/status/sensor",
            "status_drive": "{org}/{app}/{cap}/{id}/status/drive",
            "cmd_response": "{org}/{app}/{cap}/{id}/cmd/{task_name}/res",
        },
        "qos": {"telemetry": 1, "commands": 1},
        "retain": {
            "telemetry_state": True,
            "status_acquisition": True,
            "status_sensor": True,
            "status_drive": True,
        },
    }


def test_cmd_stop_acquisition_safety_exitoso():
    """Valida ejecución de systemctl stop rsa-acelerografo.service con retorno exitoso."""
    config = _crear_config_dummy()
    mock_logger = MagicMock()
    mock_client = MagicMock()

    dispatcher = CommandDispatcher(config, mock_logger)

    mock_res = MagicMock(returncode=0, stdout="", stderr="")
    with patch("subprocess.run", return_value=mock_res) as mock_run:
        res = dispatcher.dispatch("stop_acquisition_safety", {"action": "stop"}, mock_client)

    assert res["status"] == "completed"
    assert res["action"] == "stop_acquisition_safety"
    assert "timestamp" in res
    mock_run.assert_called_once_with(
        ["sudo", "systemctl", "stop", "rsa-acelerografo.service"],
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_cmd_stop_acquisition_safety_fallo():
    """Valida manejo de error si systemctl stop retorna código de error."""
    config = _crear_config_dummy()
    mock_logger = MagicMock()
    mock_client = MagicMock()

    dispatcher = CommandDispatcher(config, mock_logger)

    mock_res = MagicMock(returncode=1, stdout="", stderr="Failed to stop unit")
    with patch("subprocess.run", return_value=mock_res):
        res = dispatcher.dispatch("stop_acquisition_safety", {"action": "stop"}, mock_client)

    assert res["status"] == "error"
    assert "Failed to stop unit" in res["message"]


def test_cmd_get_sensor_status_bajo_demanda():
    """Valida consulta bajo demanda del sensor."""
    config = _crear_config_dummy()
    mock_logger = MagicMock()
    mock_client = MagicMock()
    mock_sensor = MagicMock()
    mock_sensor.evaluar_integridad.return_value = {
        "status": "ok",
        "station_id": "DEV0",
        "reason": "nominal",
    }

    dispatcher = CommandDispatcher(config, mock_logger, sensor_watchdog=mock_sensor)
    res = dispatcher.dispatch("get_sensor_status", {}, mock_client)

    assert res["status"] == "ok"
    assert res["reason"] == "nominal"
    mock_sensor.evaluar_integridad.assert_called_once_with(station_id="DEV0")


def test_cmd_get_drive_status_bajo_demanda():
    """Valida consulta bajo demanda de sincronización con Google Drive."""
    config = _crear_config_dummy()
    mock_logger = MagicMock()
    mock_client = MagicMock()
    mock_drive = MagicMock()
    mock_drive.evaluar_sincronizacion.return_value = {
        "status": "ok",
        "station_id": "DEV0",
        "reason": "all_synced",
    }

    dispatcher = CommandDispatcher(config, mock_logger, drive_watchdog=mock_drive)
    res = dispatcher.dispatch("get_drive_status", {}, mock_client)

    assert res["status"] == "ok"
    assert res["reason"] == "all_synced"
    mock_drive.evaluar_sincronizacion.assert_called_once_with(station_id="DEV0")


def test_publicacion_sensor_status_qos_y_retain():
    """Valida publicación en tópico status/sensor con QoS 1 y retain = True."""
    config = _crear_config_dummy()
    mock_client = MagicMock()
    mock_logger = MagicMock()
    mock_sensor = MagicMock()
    mock_sensor.evaluar_integridad.return_value = {
        "status": "ok",
        "ax": 0.01,
        "ay": 0.02,
        "az": 9.81,
        "clock_source": "RPi",
        "station_id": "DEV0",
    }

    publicar_sensor_status(mock_client, config, mock_sensor, mock_logger)

    mock_client.publish.assert_called_once()
    args, kwargs = mock_client.publish.call_args
    topic = args[0]
    payload = json.loads(args[1])
    assert topic == "rsa/seismic/smart/DEV0/status/sensor"
    assert payload["status"] == "ok"
    assert kwargs.get("qos") == 1
    assert kwargs.get("retain") is True


def test_publicacion_drive_status_qos_y_retain():
    """Valida publicación en tópico status/drive con QoS 1 y retain = True."""
    config = _crear_config_dummy()
    mock_client = MagicMock()
    mock_logger = MagicMock()
    mock_drive = MagicMock()
    mock_drive.evaluar_sincronizacion.return_value = {
        "status": "ok",
        "pending_mseed": 0,
        "failed_uploads_protected": 0,
        "free_disk_percent": 85.0,
        "station_id": "DEV0",
    }

    publicar_drive_status(mock_client, config, mock_drive, mock_logger)

    mock_client.publish.assert_called_once()
    args, kwargs = mock_client.publish.call_args
    topic = args[0]
    payload = json.loads(args[1])
    assert topic == "rsa/seismic/smart/DEV0/status/drive"
    assert payload["pending_mseed"] == 0
    assert kwargs.get("qos") == 1
    assert kwargs.get("retain") is True


def test_publicacion_acquisition_status_qos_y_retain():
    """Valida publicación en tópico status/acquisition con QoS 1 y retain = True."""
    config = _crear_config_dummy()
    mock_client = MagicMock()
    mock_logger = MagicMock()
    mock_watchdog = MagicMock()
    mock_watchdog.evaluar_salud.return_value = {
        "status": "ok",
        "age_seconds": 2.5,
        "station_id": "DEV0",
    }

    publicar_acquisition_status(mock_client, config, mock_watchdog, mock_logger)

    mock_client.publish.assert_called_once()
    args, kwargs = mock_client.publish.call_args
    topic = args[0]
    payload = json.loads(args[1])
    assert topic == "rsa/seismic/smart/DEV0/status/acquisition"
    assert payload["age_seconds"] == 2.5
    assert kwargs.get("qos") == 1
    assert kwargs.get("retain") is True


def test_publicacion_state_online_qos_y_retain():
    """Valida publicación en tópico telemetry/state con QoS 1, retain = True y payload esperado."""
    config = _crear_config_dummy()
    mock_client = MagicMock()
    mock_logger = MagicMock()
    now_ts = "2026-09-18T10:30:00Z"

    publicar_state(mock_client, config, "online", mock_logger, timestamp_override=now_ts)

    mock_client.publish.assert_called_once()
    args, kwargs = mock_client.publish.call_args
    topic = args[0]
    payload = json.loads(args[1])
    assert topic == "rsa/seismic/smart/DEV0/telemetry/state"
    assert payload["status"] == "online"
    assert payload["timestamp"] == now_ts
    assert kwargs.get("qos") == 1
    assert kwargs.get("retain") is True


def test_heartbeat_state_preserva_timestamp_ultimo_cambio():
    """Valida que el heartbeat periódico de state preserve el timestamp de conexión en userdata."""
    config = _crear_config_dummy()
    mock_client = MagicMock()
    mock_client.is_connected.return_value = True
    mock_logger = MagicMock()
    
    session_ts = "2026-09-18T08:15:00Z"
    userdata = {
        "is_connected": True,
        "last_state_change": session_ts
    }

    # Simular la lógica de heartbeat periódico del main loop
    if mock_client.is_connected() or userdata.get("is_connected", False):
        online_ts = userdata.get("last_state_change")
        if online_ts:
            publicar_state(mock_client, config, "online", mock_logger, timestamp_override=online_ts)

    mock_client.publish.assert_called_once()
    args, kwargs = mock_client.publish.call_args
    topic = args[0]
    payload = json.loads(args[1])
    assert topic == "rsa/seismic/smart/DEV0/telemetry/state"
    assert payload["status"] == "online"
    assert payload["timestamp"] == session_ts
    assert kwargs.get("qos") == 1
    assert kwargs.get("retain") is True


def test_heartbeat_state_no_publica_si_desconectado():
    """Valida que el heartbeat periódico no emita si el cliente está desconectado."""
    config = _crear_config_dummy()
    mock_client = MagicMock()
    mock_client.is_connected.return_value = False
    mock_logger = MagicMock()
    userdata = {
        "is_connected": False,
        "last_state_change": "2026-09-18T08:15:00Z"
    }

    # Simular la lógica de heartbeat periódico del main loop
    if mock_client.is_connected() or userdata.get("is_connected", False):
        online_ts = userdata.get("last_state_change")
        if online_ts:
            publicar_state(mock_client, config, "online", mock_logger, timestamp_override=online_ts)

    mock_client.publish.assert_not_called()


if __name__ == "__main__":
    tests = [
        test_cmd_stop_acquisition_safety_exitoso,
        test_cmd_stop_acquisition_safety_fallo,
        test_cmd_get_sensor_status_bajo_demanda,
        test_cmd_get_drive_status_bajo_demanda,
        test_publicacion_sensor_status_qos_y_retain,
        test_publicacion_drive_status_qos_y_retain,
        test_publicacion_acquisition_status_qos_y_retain,
        test_publicacion_state_online_qos_y_retain,
        test_heartbeat_state_preserva_timestamp_ultimo_cambio,
        test_heartbeat_state_no_publica_si_desconectado,
    ]

    print("\n=================================================================")
    print("  Tests: mqtt/test_mqtt_coordinator_integration.py")
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
