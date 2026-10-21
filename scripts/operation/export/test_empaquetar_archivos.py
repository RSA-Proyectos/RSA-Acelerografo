#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_empaquetar_archivos.py — Pruebas Unitarias para empaquetar_archivos.py

NOTA INSTITUCIONAL:
    - Las pruebas se ejecutan exclusivamente con la estación de desarrollo DEV0 / DEV00.
    - Se verifica la conformidad con el Blueprint de Contingencia de Empaquetado Diario y SFTP.
    - Ejecución estándar con el entorno virtual:
      .venv/bin/python3 scripts/export/test_empaquetar_archivos.py
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock
import tempfile
import shutil
import tarfile
import hashlib
from datetime import date, datetime

# Configurar path para importar empaquetar_archivos
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_CURRENT_DIR)
for _p in [_CURRENT_DIR, _PARENT_DIR]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import empaquetar_archivos as emp


class TestEmpaquetarArchivos(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_emp_dev0_")
        self.mseed_dir = os.path.join(self.test_dir, "mseed")
        self.continuous_dir = os.path.join(self.test_dir, "registro-continuo")
        self.events_dir = os.path.join(self.test_dir, "eventos-extraidos")
        self.logs_dir = os.path.join(self.test_dir, "log-files")
        self.pkg_dir = os.path.join(self.test_dir, "packages")
        self.tmp_dir = os.path.join(self.test_dir, "tmp-files")

        for d in [self.mseed_dir, self.continuous_dir, self.events_dir, self.logs_dir, self.pkg_dir, self.tmp_dir]:
            os.makedirs(d, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    # ------------------------------------------------------------------------
    # 1. Pruebas de Funciones Auxiliares Criptográficas y Fechas
    # ------------------------------------------------------------------------
    def test_calcular_sha256(self):
        """Verifica el cálculo de SHA-256 sobre un archivo binario."""
        dummy_file = os.path.join(self.test_dir, "dummy.bin")
        content = b"PRUEBA_RSA_DEV0_SHA256"
        with open(dummy_file, "wb") as f:
            f.write(content)

        expected = hashlib.sha256(content).hexdigest()
        self.assertEqual(emp.calcular_sha256(dummy_file), expected)

    def test_extraer_fecha_de_archivo(self):
        """Verifica la extracción de fecha desde diferentes patrones de nombres sismológicos."""
        # Formato YYYYMMDD
        f_mseed = os.path.join(self.mseed_dir, "DEV0_20261019_170000.mseed")
        with open(f_mseed, "w") as f:
            f.write("data")
        self.assertEqual(emp.extraer_fecha_de_archivo(f_mseed), date(2026, 10, 19))

        # Formato YYMMDD
        f_dat = os.path.join(self.continuous_dir, "DEV00_261014-150000.dat")
        with open(f_dat, "w") as f:
            f.write("data")
        self.assertEqual(emp.extraer_fecha_de_archivo(f_dat), date(2026, 10, 14))

    def test_verificar_integridad_tar(self):
        """Verifica la validación de integridad de archivos .tar.gz válidos y corruptos."""
        tar_valido = os.path.join(self.test_dir, "valido.tar.gz")
        f_content = os.path.join(self.test_dir, "f1.txt")
        with open(f_content, "w") as f:
            f.write("contenido de prueba")

        with tarfile.open(tar_valido, "w:gz") as tar:
            tar.add(f_content, arcname="f1.txt")

        self.assertTrue(emp.verificar_integridad_tar(tar_valido))

        # Archivo corrupto (no gzip/tar)
        tar_corrupto = os.path.join(self.test_dir, "corrupto.tar.gz")
        with open(tar_corrupto, "wb") as f:
            f.write(b"ESTO NO ES UN ARCHIVO GZIP VALIDO")

        self.assertFalse(emp.verificar_integridad_tar(tar_corrupto))

    # ------------------------------------------------------------------------
    # 2. Pruebas de Empaquetado Diario, Checksum GNU y Purga de Fuentes
    # ------------------------------------------------------------------------
    def test_empaquetar_dia_mseed_exitoso_y_sha256(self):
        """
        Verifica:
        1. Creación del archivo .tar.gz.
        2. Generación del archivo .sha256 en formato estándar GNU.
        3. Conservación de archivos fuente cuando delete_source=False.
        """
        archivos = []
        for h in [10, 11, 12]:
            f = os.path.join(self.mseed_dir, f"DEV0_20261019_{h:02d}0000.mseed")
            with open(f, "wb") as fh:
                fh.write(b"MSEED_DATA_" + bytes([h]))
            archivos.append(f)

        dia = date(2026, 10, 19)
        res = emp.empaquetar_dia(
            dia=dia,
            tipo="mseed",
            id_estacion="DEV0",
            archivos=archivos,
            output_dir=self.pkg_dir,
            delete_source=False,
            force=False,
            dry_run=False
        )

        self.assertEqual(res["status"], "ok")
        pkg_tar = os.path.join(self.pkg_dir, "DEV0_20261019_mseed.tar.gz")
        pkg_sha = os.path.join(self.pkg_dir, "DEV0_20261019_mseed.tar.gz.sha256")

        self.assertTrue(os.path.isfile(pkg_tar))
        self.assertTrue(os.path.isfile(pkg_sha))

        # Verificar formato GNU: <hash>  <filename>\n
        with open(pkg_sha, "r", encoding="utf-8") as sf:
            line = sf.readline()
            partes = line.strip().split("  ")
            self.assertEqual(len(partes), 2)
            self.assertEqual(partes[1], "DEV0_20261019_mseed.tar.gz")
            self.assertEqual(partes[0], emp.calcular_sha256(pkg_tar))

        # Verificar contenido del tar
        with tarfile.open(pkg_tar, "r:gz") as tar:
            nombres = tar.getnames()
            self.assertEqual(len(nombres), 3)
            self.assertIn("DEV0_20261019_100000.mseed", nombres)

        # Los archivos originales deben permanecer intactos
        for f in archivos:
            self.assertTrue(os.path.exists(f))

    def test_empaquetar_dia_delete_source(self):
        """Verifica que con delete_source=True los archivos fuente se eliminan tras verificar integridad."""
        archivos = []
        for h in [1, 2]:
            f = os.path.join(self.mseed_dir, f"DEV0_20261019_{h:02d}0000.mseed")
            with open(f, "wb") as fh:
                fh.write(b"MSEED_DATA")
            archivos.append(f)

        dia = date(2026, 10, 19)
        res = emp.empaquetar_dia(
            dia=dia,
            tipo="mseed",
            id_estacion="DEV0",
            archivos=archivos,
            output_dir=self.pkg_dir,
            delete_source=True,
            force=False,
            dry_run=False
        )

        self.assertEqual(res["status"], "ok")
        pkg_tar = os.path.join(self.pkg_dir, "DEV0_20261019_mseed.tar.gz")
        self.assertTrue(os.path.isfile(pkg_tar))

        # Los archivos originales deben haber sido eliminados
        for f in archivos:
            self.assertFalse(os.path.exists(f))

    # ------------------------------------------------------------------------
    # 3. Pruebas de Salvaguardas Críticas de Seguridad
    # ------------------------------------------------------------------------
    def test_bloqueo_seguridad_logs_delete_source(self):
        """Verifica que --delete-source esté terminantemente prohibido con --logs."""
        test_args = ["empaquetar_archivos.py", "--logs", "--delete-source"]
        with patch.object(sys, "argv", test_args):
            with self.assertRaises(SystemExit) as cm:
                emp.main()
            self.assertEqual(cm.exception.code, 2)

    @patch("shutil.disk_usage")
    def test_empaquetar_dia_espacio_insuficiente(self, mock_usage):
        """Verifica que aborte la compresión de un día si el espacio libre no supera el estimado."""
        # 100 MB libres, pero se necesitan > 500 MB de margen de seguridad
        mock_usage.return_value = (10 * 1024**3, 9.9 * 1024**3, 100 * 1024**2)

        f = os.path.join(self.mseed_dir, "DEV0_20261019_100000.mseed")
        with open(f, "wb") as fh:
            fh.write(b"MSEED")

        res = emp.empaquetar_dia(
            dia=date(2026, 10, 19),
            tipo="mseed",
            id_estacion="DEV0",
            archivos=[f],
            output_dir=self.pkg_dir,
            delete_source=True,
            dry_run=False
        )

        self.assertEqual(res["status"], "error_space")
        # El archivo fuente no debe haberse borrado ni debe haber paquete
        self.assertTrue(os.path.exists(f))
        self.assertFalse(os.path.isfile(os.path.join(self.pkg_dir, "DEV0_20261019_mseed.tar.gz")))

    # ------------------------------------------------------------------------
    # 4. Pruebas de Cerrojo Atómico Flock
    # ------------------------------------------------------------------------
    def test_cerrojo_atomico_flock(self):
        """Verifica exclusión mutua de cerrojos no bloqueantes en empaquetar."""
        lock_file = os.path.join(self.tmp_dir, "empaquetar.lock")
        lock1 = emp.adquirir_cerrojo(lock_file)
        self.assertIsNotNone(lock1)

        # Segundo intento concurrente debe retornar None
        lock2 = emp.adquirir_cerrojo(lock_file)
        self.assertIsNone(lock2)

        # Liberar y volver a adquirir
        emp.liberar_cerrojo(lock1)
        lock3 = emp.adquirir_cerrojo(lock_file)
        self.assertIsNotNone(lock3)
        emp.liberar_cerrojo(lock3)

    # ------------------------------------------------------------------------
    # 5. Pruebas de Protección del Archivo Continuo Activo
    # ------------------------------------------------------------------------
    def test_obtener_archivos_activos_continuous(self):
        """Verifica que se identifique el archivo continuo activo reportado por NombreArchivoRegistroContinuo.tmp."""
        tmp_file = os.path.join(self.tmp_dir, "NombreArchivoRegistroContinuo.tmp")
        with open(tmp_file, "w") as f:
            f.write("DEV00_261019-150000.dat\n")

        activos = emp.obtener_archivos_activos(self.test_dir, self.continuous_dir)
        self.assertIn("DEV00_261019-150000.dat", activos)


# ============================================================================
# RUNNER COMPATIBLE CON FORMATO DE TEST_GESTOR_EVENTOS.PY Y UNITTEST
# ============================================================================
def run_all_tests():
    """Ejecuta todos los tests con reporte visual estándar de la RSA."""
    print("=" * 70)
    print("EJECUTANDO SUITE DE TESTS: test_empaquetar_archivos.py (DEV0)")
    print("=" * 70)

    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestEmpaquetarArchivos)

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
