#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_procesar_y_subir_pendientes.py — Pruebas unitarias para procesar_y_subir_pendientes.py

NOTA INSTITUCIONAL:
    - Las pruebas se ejecutan exclusivamente con la estación de desarrollo DEV0 / DEV00.
    - Las estaciones CHA1 y CHA2 corresponden a producción y nunca se usan en tests.
    - Ejecución estándar con el Python del entorno virtual:
      $PROJECT_LOCAL_ROOT/.venv/bin/python3 test_procesar_y_subir_pendientes.py

Prueba las características críticas de resiliencia:
- Detección real de DNS y comportamiento fail-fast.
- Identificación y protección estricta del archivo activo en grabación SPI.
- Idempotencia en conversión (omisión inmediata de archivos ya convertidos).
- Dosificación de lotes (--batch-size) y registro atómico.
- Cerrojo atómico de exclusión mutua (fcntl.flock).
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock
import tempfile
import shutil
import socket

# Configurar path para importar procesar_y_subir_pendientes (soporta repo y projects/acelerografo)
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_CURRENT_DIR)
for _p in [_CURRENT_DIR, _PARENT_DIR]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import procesar_y_subir_pendientes as psp


class TestProcesarYSubirPendientes(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_psp_dev0_")
        self.tmp_dir = os.path.join(self.test_dir, "tmp-files")
        self.rc_dir = os.path.join(self.test_dir, "registro-continuo")
        self.ms_dir = os.path.join(self.test_dir, "mseed")
        self.ev_dir = os.path.join(self.test_dir, "eventos-extraidos")
        self.log_dir = os.path.join(self.test_dir, "log-files")
        self.cfg_dir = os.path.join(self.test_dir, "configuracion")

        for d in [self.tmp_dir, self.rc_dir, self.ms_dir, self.ev_dir, self.log_dir, self.cfg_dir]:
            os.makedirs(d, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    # ------------------------------------------------------------------------
    # 1. Pruebas de Resolución DNS y Fail-Fast
    # ------------------------------------------------------------------------
    @patch("socket.getaddrinfo")
    def test_verificar_dns_drive_exitoso(self, mock_getaddrinfo):
        """Verifica que retorna True cuando el DNS resuelve correctamente."""
        mock_getaddrinfo.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.190.42", 443))]
        self.assertTrue(psp.verificar_dns_drive())

    @patch("socket.getaddrinfo")
    def test_verificar_dns_drive_fallo(self, mock_getaddrinfo):
        """Verifica que retorna False ante fallo en resolución de nombres (gaierror)."""
        mock_getaddrinfo.side_effect = socket.gaierror(-3, "Temporary failure in name resolution")
        self.assertFalse(psp.verificar_dns_drive())

    # ------------------------------------------------------------------------
    # 2. Pruebas de Detección y Protección del Archivo Activo (DEV00)
    # ------------------------------------------------------------------------
    def test_obtener_archivos_activos_desde_tmp(self):
        """Verifica que lee el archivo activo de la línea 1 de NombreArchivoRegistroContinuo.tmp."""
        tmp_file = os.path.join(self.tmp_dir, "NombreArchivoRegistroContinuo.tmp")
        with open(tmp_file, "w") as f:
            f.write("DEV00_261013-100000.dat\nDEV00_261013-090000.dat\n")

        activos = psp.obtener_archivos_activos(self.test_dir, self.rc_dir)
        self.assertIn("DEV00_261013-100000.dat", activos)
        self.assertNotIn("DEV00_261013-090000.dat", activos)

    def test_obtener_archivos_activos_fallback_mtime(self):
        """Verifica que detecta el archivo modificado más recientemente como activo si no hay .tmp."""
        f1 = os.path.join(self.rc_dir, "DEV00_261013-080000.dat")
        f2 = os.path.join(self.rc_dir, "DEV00_261013-090000.dat")
        with open(f1, "w") as f:
            f.write("data1")
        with open(f2, "w") as f:
            f.write("data2")

        os.utime(f1, (1000, 1000))
        activos = psp.obtener_archivos_activos(self.test_dir, self.rc_dir)
        self.assertIn("DEV00_261013-090000.dat", activos)

    # ------------------------------------------------------------------------
    # 3. Pruebas de Conversión e Idempotencia (DEV0)
    # ------------------------------------------------------------------------
    @patch("procesar_y_subir_pendientes.procesar_archivo_individual")
    @patch("procesar_y_subir_pendientes.extraer_tiempo_binario")
    @patch("procesar_y_subir_pendientes.nombrar_archivo_mseed")
    def test_convertir_archivos_pendientes_idempotencia_y_exclusion(
        self, mock_nombrar, mock_extraer, mock_procesar
    ):
        """
        Verifica en entorno de desarrollo DEV0:
        1. El archivo activo es excluido.
        2. Los archivos con .mseed ya existente se omiten inmediatamente (idempotencia).
        3. Solo los verdaderos pendientes invocan procesar_archivo_individual.
        """
        f_activo = os.path.join(self.rc_dir, "DEV00_261013-100000.dat")
        f_convertido = os.path.join(self.rc_dir, "DEV00_261013-080000.dat")
        f_pendiente = os.path.join(self.rc_dir, "DEV00_261013-090000.dat")

        for f in [f_activo, f_convertido, f_pendiente]:
            with open(f, "wb") as fh:
                fh.write(b"\x00" * 2506)

        mseed_existente = os.path.join(self.ms_dir, "DEV0_20261013_080000.mseed")
        with open(mseed_existente, "wb") as fh:
            fh.write(b"MSEED_DATA")

        def fake_nombrar(estacion, tiempo):
            if "080000" in getattr(mock_nombrar, "current_file", ""):
                return "DEV0_20261013_080000.mseed"
            else:
                return "DEV0_20261013_090000.mseed"

        mock_nombrar.side_effect = fake_nombrar

        def side_effect_extraer(file_path, usar_fecha):
            mock_nombrar.current_file = file_path
            return {"anio": 2026, "mes": 10, "dia": 13, "hora": 8, "minuto": 0, "segundo": 0}

        mock_extraer.side_effect = side_effect_extraer
        mock_procesar.return_value = (True, "OK", {"tiempo_lectura": 0.42})

        archivos_activos = {"DEV00_261013-100000.dat"}
        mock_logger = MagicMock()

        resumen = psp.convertir_archivos_pendientes(
            path_registro_continuo=self.rc_dir,
            path_archivos_mseed=self.ms_dir,
            codigo_estacion="DEV0",
            config_mseed={"CODIGO(1)": "DEV0"},
            archivos_activos=archivos_activos,
            logger=mock_logger,
            usar_fecha_filename=False,
            dry_run=False
        )

        self.assertEqual(resumen["total_dat"], 3)
        self.assertEqual(resumen["excluidos_activos"], 1)
        self.assertEqual(resumen["ya_convertidos"], 1)
        self.assertEqual(resumen["convertidos_exitosos"], 1)
        self.assertEqual(resumen["fallidos"], 0)
        self.assertEqual(mock_procesar.call_count, 1)

    # ------------------------------------------------------------------------
    # 4. Pruebas de Subida y Control de Lotes (DEV0)
    # ------------------------------------------------------------------------
    @patch("procesar_y_subir_pendientes.verificar_dns_drive")
    def test_subir_archivos_pendientes_fail_fast_dns(self, mock_dns):
        """Verifica que si el DNS falla, se activa fail-fast y no se intenta subir nada."""
        mock_dns.return_value = False
        mock_logger = MagicMock()

        resumen = psp.subir_archivos_pendientes(
            local_root=self.test_dir,
            config_dispositivo={"drive": {}},
            log_directory=self.log_dir,
            logger=mock_logger,
            batch_size=20,
            dry_run=False
        )

        self.assertFalse(resumen["dns_ok"])
        self.assertEqual(resumen["status"], "dns_unreachable")
        self.assertEqual(resumen["subidos_exitosos"], 0)

    @patch("procesar_y_subir_pendientes.verificar_dns_drive")
    @patch("procesar_y_subir_pendientes.Try_Autenticar_Drive")
    @patch("procesar_y_subir_pendientes.subir_archivo_con_reintentos")
    @patch("procesar_y_subir_pendientes.marcar_como_exitoso")
    @patch("procesar_y_subir_pendientes.ya_fue_subido")
    @patch("time.sleep")
    def test_subir_archivos_pendientes_control_lotes(
        self, mock_sleep, mock_ya_subido, mock_marcar, mock_subir, mock_auth, mock_dns
    ):
        """Verifica que el límite de lote (--batch-size) se respete estrictamente."""
        mock_dns.return_value = True
        mock_auth.return_value = MagicMock()
        mock_subir.return_value = True
        mock_ya_subido.return_value = False

        for i in range(5):
            f = os.path.join(self.ms_dir, f"DEV0_20261013_{i:02d}0000.mseed")
            with open(f, "wb") as fh:
                fh.write(b"DATA")

        config_disp = {
            "directorios": {
                "archivos_mseed": self.ms_dir,
                "eventos_extraidos": self.ev_dir
            },
            "drive": {
                "carpetas": {
                    "mseed_id": "DRIVE_MSEED_ID_DEV0",
                    "events_id": "DRIVE_EVENTS_ID_DEV0"
                },
                "config": {"max_reintentos": 1, "tiempo_espera": 0}
            }
        }

        mock_logger = MagicMock()

        resumen = psp.subir_archivos_pendientes(
            local_root=self.test_dir,
            config_dispositivo=config_disp,
            log_directory=self.log_dir,
            logger=mock_logger,
            batch_size=2,
            all_files=False,
            dry_run=False
        )

        self.assertEqual(resumen["total_pendientes"], 5)
        self.assertEqual(resumen["subidos_exitosos"], 2)
        self.assertEqual(mock_subir.call_count, 2)
        self.assertEqual(mock_marcar.call_count, 2)

    # ------------------------------------------------------------------------
    # 5. Pruebas de Cerrojo Atómico (Exclusión Mutua)
    # ------------------------------------------------------------------------
    def test_cerrojo_atomico(self):
        """Verifica que no se puedan adquirir dos cerrojos concurrentes sobre el mismo archivo."""
        lock1 = psp.adquirir_cerrojo()
        self.assertIsNotNone(lock1)

        lock2 = psp.adquirir_cerrojo()
        self.assertIsNone(lock2)

        psp.liberar_cerrojo(lock1)
        lock3 = psp.adquirir_cerrojo()
        self.assertIsNotNone(lock3)
        psp.liberar_cerrojo(lock3)


# ============================================================================
# 6. RUNNER COMPATIBLE CON FORMATO DE TEST_GESTOR_EVENTOS.PY Y UNITTEST
# ============================================================================
def run_all_tests():
    """Ejecuta todos los tests con reporte visual estándar de la RSA."""
    print("=" * 70)
    print("EJECUTANDO SUITE DE TESTS: test_procesar_y_subir_pendientes.py (DEV0)")
    print("=" * 70)

    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestProcesarYSubirPendientes)

    fallos = 0
    total = suite.countTestCases()
    print(f"Total de casos a verificar: {total}\n")

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    print("\n" + "=" * 70)
    if result.wasSuccessful():
        print(f"TODOS LOS TESTS ({total}) PASARON EXITOSAMENTE.")
        return 0
    else:
        print(f"HUBO {len(result.failures) + len(result.errors)} FALLO(S) EN LA SUITE.")
        return 1


if __name__ == "__main__":
    sys.exit(run_all_tests())
