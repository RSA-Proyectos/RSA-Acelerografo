#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
drive_watchdog.py — Auditor de Sincronización Google Drive y Espacio en Disco

Examina drive_status.json (o uploaded_files_registry.json) y los directorios de
almacenamiento de MiniSEED para reportar archivos retenidos por error y archivos
pendientes de subida.
"""

import json
import os
import shutil
from datetime import datetime, timezone


class DriveWatchdog:
    def __init__(self, mseed_dir: str = None, status_file: str = None):
        project_root = os.getenv("PROJECT_LOCAL_ROOT")
        if not project_root:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            project_root = os.path.dirname(base_dir)

        # Resolución defensiva de mseed_dir:
        # Prioridad: parámetro explícito > /home/rsa/data/mseed/ > datos/MSEED > datos/mseed
        if mseed_dir:
            self.mseed_dir = mseed_dir
        elif os.path.isdir("/home/rsa/data/mseed"):
            self.mseed_dir = "/home/rsa/data/mseed"
        elif os.path.isdir(os.path.join(project_root, "datos", "MSEED")):
            self.mseed_dir = os.path.join(project_root, "datos", "MSEED")
        elif os.path.isdir(os.path.join(project_root, "datos", "mseed")):
            self.mseed_dir = os.path.join(project_root, "datos", "mseed")
        else:
            self.mseed_dir = "/home/rsa/data/mseed"

        # Resolución defensiva de status_file:
        # Prioridad: parámetro explícito > drive_status.json > uploaded_files_registry.json
        if status_file:
            self.status_file = status_file
        else:
            cand1 = os.path.join(project_root, "log-files", "drive_status.json")
            cand2 = os.path.join(project_root, "log-files", "uploaded_files_registry.json")
            if os.path.isfile(cand1):
                self.status_file = cand1
            elif os.path.isfile(cand2):
                self.status_file = cand2
            else:
                self.status_file = cand1

    def evaluar_sincronizacion(self, station_id: str) -> dict:
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # 1. Espacio en disco
        free_disk_percent = 100.0
        try:
            target_dir = self.mseed_dir if os.path.exists(self.mseed_dir) else "/"
            usage = shutil.disk_usage(target_dir)
            free_disk_percent = round((usage.free / usage.total) * 100, 1)
        except Exception:
            pass

        # 2. Leer archivo de estado
        ya_subidos = set()
        protegidos = set()
        last_upload_utc = None

        if os.path.isfile(self.status_file):
            try:
                with open(self.status_file, "r", encoding="utf-8") as f:
                    data = json.load(f)

                    # Formato Blueprint / TIG
                    mseed_data = data.get("mseed", {})
                    if isinstance(mseed_data, dict):
                        up = mseed_data.get("uploaded", [])
                        prot = mseed_data.get("protected", [])
                        if isinstance(up, (list, set)):
                            ya_subidos.update(up)
                        elif isinstance(up, dict):
                            ya_subidos.update(up.keys())
                        if isinstance(prot, (list, set)):
                            protegidos.update(prot)
                        elif isinstance(prot, dict):
                            protegidos.update(prot.keys())

                    last_upload_utc = data.get("last_upload_utc")

                    # Formato drive_status_manager.py
                    if "archivos_exitosos" in data:
                        exitosos_mseed = data["archivos_exitosos"].get("mseed", {})
                        if isinstance(exitosos_mseed, dict):
                            ya_subidos.update(exitosos_mseed.keys())
                            for ts_val in exitosos_mseed.values():
                                if isinstance(ts_val, str) and (not last_upload_utc or ts_val > last_upload_utc):
                                    last_upload_utc = ts_val
                        elif isinstance(exitosos_mseed, (list, set)):
                            ya_subidos.update(exitosos_mseed)

                    if "archivos_fallidos" in data:
                        fallidos_mseed = data["archivos_fallidos"].get("mseed", {})
                        if isinstance(fallidos_mseed, dict):
                            protegidos.update(fallidos_mseed.keys())
                        elif isinstance(fallidos_mseed, (list, set)):
                            protegidos.update(fallidos_mseed)
            except Exception:
                pass

        # 3. Contar archivos pendientes en disco y validar existencia de protegidos
        pending_mseed = 0
        if os.path.isdir(self.mseed_dir):
            try:
                archivos_disco = set(
                    f for f in os.listdir(self.mseed_dir)
                    if f.endswith(".mseed") or f.endswith(".MSEED")
                )
                pending_mseed = len([f for f in archivos_disco if f not in ya_subidos])
                # Solo alertar por archivos protegidos que efectivamente sigan en disco
                protegidos = {f for f in protegidos if f in archivos_disco}
            except Exception:
                pass
        else:
            protegidos = set()

        failed_uploads_protected = len(protegidos)

        # 4. Clasificación de estado
        if failed_uploads_protected > 0 or pending_mseed > 3:
            status = "warning"
            reason = "upload_retry_retained" if failed_uploads_protected > 0 else "pending_backlog"
        else:
            status = "ok"
            reason = "all_synced"

        return {
            "status": status,
            "pending_mseed": pending_mseed,
            "failed_uploads_protected": failed_uploads_protected,
            "free_disk_percent": free_disk_percent,
            "last_upload_utc": last_upload_utc,
            "reason": reason,
            "station_id": station_id,
            "timestamp": now_utc,
        }
