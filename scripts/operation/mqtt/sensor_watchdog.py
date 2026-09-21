#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
sensor_watchdog.py — Auditor de Integridad Física del Acelerómetro

Ejecuta comprobar_registro_wrapper, evalúa que las aceleraciones triaxiales en reposo
se encuentren dentro de las tolerancias físicas normales y audita la sincronización del reloj.
"""

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone

logger = logging.getLogger("SensorWatchdog")

# Umbrales nominales en reposo (m/s^2)
MAX_ABS_HORIZONTAL_ACCEL = 0.5   # X e Y cercanos a 0
TARGET_GRAVITY_Z = 9.81          # Z cercano a 1g
TOLERANCE_GRAVITY_Z = 0.8        # Rango aceptable [9.01, 10.61]


class SensorWatchdog:
    def __init__(self, wrapper_path: str = None):
        if wrapper_path:
            self.wrapper_path = wrapper_path
        else:
            project_local_root = os.getenv("PROJECT_LOCAL_ROOT")
            if not project_local_root:
                scripts_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                project_local_root = os.path.dirname(scripts_dir)
            self.wrapper_path = os.path.join(
                project_local_root, "scripts", "acelerografo", "comprobar_registro_wrapper.py"
            )

    def evaluar_integridad(self, station_id: str) -> dict:
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        if not os.path.isfile(self.wrapper_path):
            return {
                "status": "error",
                "ax": None, "ay": None, "az": None,
                "clock_source": None,
                "clock_error": "wrapper_not_found",
                "reason": "wrapper_not_found",
                "station_id": station_id,
                "timestamp": now_utc,
            }

        try:
            # Asegurar herencia de entorno y propagar PROJECT_LOCAL_ROOT
            env = os.environ.copy()
            if "PROJECT_LOCAL_ROOT" not in env:
                project_local_root = os.getenv("PROJECT_LOCAL_ROOT")
                if not project_local_root:
                    scripts_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                    project_local_root = os.path.dirname(scripts_dir)
                env["PROJECT_LOCAL_ROOT"] = project_local_root

            res = subprocess.run(
                [sys.executable, self.wrapper_path],
                capture_output=True,
                text=True,
                timeout=12,
                env=env,
            )
            if res.returncode != 0:
                return {
                    "status": "error",
                    "ax": None, "ay": None, "az": None,
                    "clock_source": None,
                    "clock_error": res.stderr.strip() or "execution_failed",
                    "reason": "execution_failed",
                    "station_id": station_id,
                    "timestamp": now_utc,
                }

            datos = json.loads(res.stdout)
            if "error" in datos and datos["error"]:
                return {
                    "status": "error",
                    "ax": None, "ay": None, "az": None,
                    "clock_source": None,
                    "clock_error": str(datos["error"]),
                    "reason": "sensor_check_error",
                    "station_id": station_id,
                    "timestamp": now_utc,
                }

            ax = datos.get("aceleracion_x")
            ay = datos.get("aceleracion_y")
            az = datos.get("aceleracion_z")
            clock_source = datos.get("fuente_reloj")
            clock_error = datos.get("error_reloj")

            # Validar valores nulos
            if ax is None or ay is None or az is None:
                return {
                    "status": "error",
                    "ax": ax, "ay": ay, "az": az,
                    "clock_source": clock_source,
                    "clock_error": clock_error or "null_readings",
                    "reason": "null_readings",
                    "station_id": station_id,
                    "timestamp": now_utc,
                }

            # Validar tolerancias físicas y reloj
            error_msg = []
            if abs(ax) > MAX_ABS_HORIZONTAL_ACCEL:
                error_msg.append(f"ax_out_of_range({ax:.3f})")
            if abs(ay) > MAX_ABS_HORIZONTAL_ACCEL:
                error_msg.append(f"ay_out_of_range({ay:.3f})")
            if abs(az - TARGET_GRAVITY_Z) > TOLERANCE_GRAVITY_Z:
                error_msg.append(f"az_out_of_range({az:.3f})")
            if clock_error:
                error_msg.append(f"clock_err({clock_error})")
            if clock_source and str(clock_source).startswith("E"):
                error_msg.append(f"clock_src_err({clock_source})")

            if error_msg:
                return {
                    "status": "error",
                    "ax": round(ax, 4),
                    "ay": round(ay, 4),
                    "az": round(az, 4),
                    "clock_source": clock_source,
                    "clock_error": "; ".join(error_msg),
                    "reason": "accelerometer_anomaly",
                    "station_id": station_id,
                    "timestamp": now_utc,
                }

            return {
                "status": "ok",
                "ax": round(ax, 4),
                "ay": round(ay, 4),
                "az": round(az, 4),
                "clock_source": clock_source,
                "clock_error": None,
                "reason": "nominal",
                "station_id": station_id,
                "timestamp": now_utc,
            }

        except subprocess.TimeoutExpired:
            return {
                "status": "error",
                "ax": None, "ay": None, "az": None,
                "clock_source": None,
                "clock_error": "timeout",
                "reason": "timeout",
                "station_id": station_id,
                "timestamp": now_utc,
            }
        except Exception as exc:
            return {
                "status": "error",
                "ax": None, "ay": None, "az": None,
                "clock_source": None,
                "clock_error": str(exc),
                "reason": "exception",
                "station_id": station_id,
                "timestamp": now_utc,
            }
