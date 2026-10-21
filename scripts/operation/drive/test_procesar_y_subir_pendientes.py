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

    # ------------------------------------------------------------------------
    # 6. Pruebas de Cupo Dinámico y Retro-presión (DEV0)
    # ------------------------------------------------------------------------
    @patch("procesar_y_subir_pendientes.procesar_archivo_individual")
    @patch("procesar_y_subir_pendientes.extraer_tiempo_binario")
    @patch("procesar_y_subir_pendientes.nombrar_archivo_mseed")
    def test_convertir_archivos_pendientes_con_cupo_maximo(
        self, mock_nombrar, mock_extraer, mock_procesar
    ):
        """Verifica que max_convertir detiene la conversión al alcanzar el cupo solicitado."""
        for i in range(5):
            f = os.path.join(self.rc_dir, f"DEV00_261013-0{i}0000.dat")
            with open(f, "wb") as fh:
                fh.write(b"\x00" * 2506)

        mock_nombrar.side_effect = lambda est, t: f"DEV0_20261013_fake_{t['hora']}.mseed"
        mock_extraer.side_effect = lambda f, u: {"anio": 2026, "mes": 10, "dia": 13, "hora": int(f[-10:-8]), "minuto": 0, "segundo": 0}
        mock_procesar.return_value = (True, "OK", {"tiempo_lectura": 0.1})

        resumen = psp.convertir_archivos_pendientes(
            path_registro_continuo=self.rc_dir,
            path_archivos_mseed=self.ms_dir,
            codigo_estacion="DEV0",
            config_mseed={"CODIGO(1)": "DEV0"},
            archivos_activos=set(),
            logger=MagicMock(),
            max_convertir=2,
            dry_run=False
        )

        self.assertEqual(resumen["total_dat"], 5)
        self.assertEqual(resumen["convertidos_exitosos"], 2)
        self.assertEqual(mock_procesar.call_count, 2)

    def test_obtener_cola_subida_drive_prioridades(self):
        """Verifica que obtener_cola_subida_drive lista eventos primero y luego miniSEED horarios."""
        f_ev = os.path.join(self.ev_dir, "DEV0_evento_1.mseed")
        f_ms = os.path.join(self.ms_dir, "DEV0_continuo_1.mseed")
        for f in [f_ev, f_ms]:
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
                }
            }
        }

        cola = psp.obtener_cola_subida_drive(config_disp, self.log_dir)
        self.assertEqual(len(cola), 2)
        self.assertEqual(cola[0][2], "event")
        self.assertEqual(cola[1][2], "mseed")

    # ------------------------------------------------------------------------
    # 7. Pruebas de Liberación de Espacio (--delete-uploaded y --purge-dat)
    # ------------------------------------------------------------------------
    @patch("procesar_y_subir_pendientes.subir_archivo_con_reintentos")
    @patch("procesar_y_subir_pendientes.marcar_como_exitoso")
    @patch("procesar_y_subir_pendientes.Try_Autenticar_Drive")
    def test_subir_archivos_con_delete_uploaded(self, mock_auth, mock_marcar, mock_subir):
        """Verifica que delete_uploaded=True transmita borrar_despues=True a subir_archivo_con_reintentos."""
        f_ms = os.path.join(self.ms_dir, "DEV0_20261013_110000.mseed")
        with open(f_ms, "wb") as fh:
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

        mock_auth.return_value = MagicMock()
        mock_subir.return_value = True

        resumen = psp.subir_archivos_pendientes(
            local_root=self.test_dir,
            config_dispositivo=config_disp,
            log_directory=self.log_dir,
            logger=MagicMock(),
            batch_size=1,
            dns_verificado=True,
            delete_uploaded=True,
            dry_run=False
        )

        self.assertEqual(resumen["subidos_exitosos"], 1)
        # Verificar que borrar_despues se pasó como True
        args, kwargs = mock_subir.call_args
        self.assertTrue(kwargs.get("borrar_despues"))

    @patch("procesar_y_subir_pendientes.nombrar_archivo_mseed")
    @patch("procesar_y_subir_pendientes.extraer_tiempo_binario")
    def test_purgar_binarios_respaldados(self, mock_extraer, mock_nombrar):
        """
        Verifica en DEV0 (desacoplado de Google Drive):
        1. Elimina .dat cuyo .mseed correspondiente ya existe localmente con tamaño > 0.
        2. Protege incondicionalmente el archivo activo en grabación SPI.
        3. Conserva .dat cuyo .mseed aún no existe localmente.
        """
        f_activo = os.path.join(self.rc_dir, "DEV00_261013-100000.dat")
        f_con_mseed = os.path.join(self.rc_dir, "DEV00_261013-080000.dat")
        f_sin_mseed = os.path.join(self.rc_dir, "DEV00_261013-090000.dat")

        for f in [f_activo, f_con_mseed, f_sin_mseed]:
            with open(f, "wb") as fh:
                fh.write(b"\x00" * 2506)

        # Crear el .mseed local para 080000
        mseed_local = os.path.join(self.ms_dir, "DEV0_20261013_080000.mseed")
        with open(mseed_local, "wb") as fh:
            fh.write(b"MSEED_DATA_BYTES")

        mock_extraer.return_value = {"anio": 2026, "mes": 10, "dia": 13, "hora": 8, "minuto": 0, "segundo": 0}
        mock_nombrar.side_effect = lambda est, t: "DEV0_20261013_080000.mseed" if "080000" in getattr(mock_nombrar, "curr", "") else "DEV0_20261013_090000.mseed"

        def fake_extraer(fpath, usar_f):
            mock_nombrar.curr = fpath
            return {"anio": 2026, "mes": 10, "dia": 13, "hora": 8, "minuto": 0, "segundo": 0}
        mock_extraer.side_effect = fake_extraer

        archivos_activos = {"DEV00_261013-100000.dat"}

        resumen = psp.purgar_binarios_respaldados(
            path_registro_continuo=self.rc_dir,
            codigo_estacion="DEV0",
            path_archivos_mseed=self.ms_dir,
            archivos_activos=archivos_activos,
            logger=MagicMock(),
            usar_fecha_filename=False,
            dry_run=False
        )

        self.assertEqual(resumen["total_evaluados"], 3)
        self.assertEqual(resumen["omitidos_activos"], 1)
        self.assertEqual(resumen["purgados"], 1)
        self.assertEqual(resumen["omitidos_sin_mseed"], 1)

        # Comprobar el estado real en disco
        self.assertTrue(os.path.exists(f_activo))
        self.assertTrue(os.path.exists(f_sin_mseed))
        self.assertFalse(os.path.exists(f_con_mseed))

    # ------------------------------------------------------------------------
    # 8. Pruebas de Guarda Preventiva de Espacio y Flags Unificados
    # ------------------------------------------------------------------------
    @patch("shutil.disk_usage")
    def test_verificar_espacio_libre(self, mock_usage):
        """Verifica la lógica de cálculo de espacio porcentual y absoluto."""
        # 100 GB total, 50 GB usado, 50 GB libre (50%, 51200 MB) -> Suficiente
        mock_usage.return_value = (100 * 1024**3, 50 * 1024**3, 50 * 1024**3)
        suficiente, pct, mb = psp.verificar_espacio_libre("/dummy", umbral_minimo_pct=10.0, min_mb_libres=500.0)
        self.assertTrue(suficiente)
        self.assertAlmostEqual(pct, 50.0)

        # 100 GB total, 95 GB usado, 5 GB libre (5%, 5120 MB) -> Falla por porcentaje (<10%)
        mock_usage.return_value = (100 * 1024**3, 95 * 1024**3, 5 * 1024**3)
        suficiente, pct, mb = psp.verificar_espacio_libre("/dummy", umbral_minimo_pct=10.0, min_mb_libres=500.0)
        self.assertFalse(suficiente)

        # 1 GB total, 0.7 GB usado, 0.3 GB libre (30%, 307 MB) -> Falla por MB libres (<500 MB)
        mock_usage.return_value = (1024 * 1024**2, 700 * 1024**2, 324 * 1024**2)
        suficiente, pct, mb = psp.verificar_espacio_libre("/dummy", umbral_minimo_pct=10.0, min_mb_libres=500.0)
        self.assertFalse(suficiente)

    @patch("procesar_y_subir_pendientes.verificar_espacio_libre")
    @patch("procesar_y_subir_pendientes.procesar_archivo_individual")
    @patch("procesar_y_subir_pendientes.extraer_tiempo_binario")
    @patch("procesar_y_subir_pendientes.nombrar_archivo_mseed")
    def test_convertir_archivos_pendientes_guarda_espacio(
        self, mock_nombrar, mock_extraer, mock_procesar, mock_espacio
    ):
        """Verifica que la conversión se aborta preventivamente si el espacio libre es <10%."""
        f_dat = os.path.join(self.rc_dir, "DEV00_261013-090000.dat")
        with open(f_dat, "wb") as fh:
            fh.write(b"\x00" * 2506)

        mock_extraer.return_value = {"anio": 2026, "mes": 10, "dia": 13, "hora": 9, "minuto": 0, "segundo": 0}
        mock_nombrar.return_value = "DEV0_20261013_090000.mseed"
        # Simular espacio insuficiente (<10%)
        mock_espacio.return_value = (False, 8.5, 420.0)

        resumen = psp.convertir_archivos_pendientes(
            path_registro_continuo=self.rc_dir,
            path_archivos_mseed=self.ms_dir,
            codigo_estacion="DEV0",
            config_mseed={"CODIGO(1)": "DEV0"},
            archivos_activos=set(),
            logger=MagicMock(),
            purge_dat=False,
            dry_run=False
        )

        self.assertTrue(resumen["abortado_por_espacio"])
        self.assertEqual(resumen["convertidos_exitosos"], 0)
        mock_procesar.assert_not_called()

    @patch("procesar_y_subir_pendientes.procesar_archivo_individual")
    @patch("procesar_y_subir_pendientes.extraer_tiempo_binario")
    @patch("procesar_y_subir_pendientes.nombrar_archivo_mseed")
    def test_convertir_archivos_pendientes_purga_inmediata(
        self, mock_nombrar, mock_extraer, mock_procesar
    ):
        """Verifica que si purge_dat=True, el .dat se elimina inmediatamente tras generarse el .mseed."""
        f_dat = os.path.join(self.rc_dir, "DEV00_261013-090000.dat")
        with open(f_dat, "wb") as fh:
            fh.write(b"\x00" * 2506)

        mseed_esperado = os.path.join(self.ms_dir, "DEV0_20261013_090000.mseed")

        def fake_procesar(*args, **kwargs):
            # Simular la creación del .mseed en disco por binary_to_mseed
            with open(mseed_esperado, "wb") as fh:
                fh.write(b"MSEED_DATA")
            return (True, "OK", {"tiempo_lectura": 0.25})

        mock_procesar.side_effect = fake_procesar
        mock_extraer.return_value = {"anio": 2026, "mes": 10, "dia": 13, "hora": 9, "minuto": 0, "segundo": 0}
        mock_nombrar.return_value = "DEV0_20261013_090000.mseed"

        resumen = psp.convertir_archivos_pendientes(
            path_registro_continuo=self.rc_dir,
            path_archivos_mseed=self.ms_dir,
            codigo_estacion="DEV0",
            config_mseed={"CODIGO(1)": "DEV0"},
            archivos_activos=set(),
            logger=MagicMock(),
            purge_dat=True,
            dry_run=False
        )

        self.assertEqual(resumen["convertidos_exitosos"], 1)
        self.assertEqual(resumen["purgados_inmediatos"], 1)
        # El .mseed debe existir y el .dat debe haberse purgado
        self.assertTrue(os.path.exists(mseed_esperado))
        self.assertFalse(os.path.exists(f_dat))

    def test_flags_unificados_y_retrocompatibilidad(self):
        """Verifica que los flags en inglés y los alias en español sean parseados correctamente."""
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--convert-only", dest="convert_only", action="store_true")
        parser.add_argument("--solo-convertir", dest="convert_only", action="store_true")
        parser.add_argument("--upload-only", dest="upload_only", action="store_true")
        parser.add_argument("--solo-subir", dest="upload_only", action="store_true")
        parser.add_argument("--purge-dat", action="store_true")

        args1 = parser.parse_args(["--convert-only", "--purge-dat"])
        self.assertTrue(args1.convert_only)
        self.assertTrue(args1.purge_dat)
        self.assertFalse(args1.upload_only)

        args2 = parser.parse_args(["--solo-convertir"])
        self.assertTrue(args2.convert_only)

        args3 = parser.parse_args(["--upload-only"])
        self.assertTrue(args3.upload_only)
        self.assertFalse(args3.convert_only)

        args4 = parser.parse_args(["--solo-subir"])
        self.assertTrue(args4.upload_only)


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
