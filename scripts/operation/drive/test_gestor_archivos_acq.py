#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_gestor_archivos_acq.py — Pruebas unitarias para gestor_archivos_acq.py blindado

NOTA INSTITUCIONAL:
    - Las pruebas se ejecutan exclusivamente con la estación de desarrollo DEV0 / DEV00.
    - Ejecución estándar con el Python del entorno virtual:
      $PROJECT_LOCAL_ROOT/.venv/bin/python3 test_gestor_archivos_acq.py
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock
import tempfile
import shutil
import socket
import fcntl

# Configurar path para importar módulos locales
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_CURRENT_DIR)
for _p in [_CURRENT_DIR, _PARENT_DIR]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import gestor_archivos_acq as g_acq


class TestGestorArchivosAcq(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_gestor_dev0_")
        self.tmp_dir = os.path.join(self.test_dir, "tmp-files")
        self.rc_dir = os.path.join(self.test_dir, "registro-continuo")
        self.ms_dir = os.path.join(self.test_dir, "mseed")
        self.ev_dir = os.path.join(self.test_dir, "eventos-extraidos")
        self.log_dir = os.path.join(self.test_dir, "log-files")
        self.cfg_dir = os.path.join(self.test_dir, "configuracion")

        for d in [self.tmp_dir, self.rc_dir, self.ms_dir, self.ev_dir, self.log_dir, self.cfg_dir]:
            os.makedirs(d, exist_ok=True)

        self.lock_file = os.path.join(self.tmp_dir, "test_gestor.lock")

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    # ------------------------------------------------------------------------
    # 1. Pruebas de Resolución DNS Real
    # ------------------------------------------------------------------------
    @patch("socket.getaddrinfo")
    def test_check_internet_connection_exito(self, mock_getaddrinfo):
        """Verifica que retorna True cuando el DNS resuelve correctamente."""
        mock_getaddrinfo.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.190.42", 443))]
        self.assertTrue(g_acq.check_internet_connection(None))

    @patch("socket.getaddrinfo")
    def test_check_internet_connection_fallo_dns(self, mock_getaddrinfo):
        """Verifica que retorna False ante fallo en resolución de nombres (falso positivo IP evitado)."""
        mock_getaddrinfo.side_effect = socket.gaierror(-3, "Temporary failure in name resolution")
        self.assertFalse(g_acq.check_internet_connection(None))

    # ------------------------------------------------------------------------
    # 2. Pruebas de Cerrojo Atómico Exclusivo (flock)
    # ------------------------------------------------------------------------
    def test_adquirir_y_liberar_cerrojo(self):
        """Verifica exclusión mutua: dos procesos simultáneos no pueden poseer el lock."""
        lock1 = g_acq.adquirir_cerrojo(self.lock_file)
        self.assertIsNotNone(lock1, "El primer intento de cerrojo debe ser exitoso")

        lock2 = g_acq.adquirir_cerrojo(self.lock_file)
        self.assertIsNone(lock2, "El segundo intento concurrente debe retornar None")

        g_acq.liberar_cerrojo(lock1)

        lock3 = g_acq.adquirir_cerrojo(self.lock_file)
        self.assertIsNotNone(lock3, "Tras liberar el cerrojo, debe poder adquirirse nuevamente")
        g_acq.liberar_cerrojo(lock3)

    # ------------------------------------------------------------------------
    # 3. Pruebas de Detección y Protección del Archivo Activo SPI
    # ------------------------------------------------------------------------
    def test_obtener_archivos_activos_desde_tmp(self):
        """Verifica que lee el archivo activo de NombreArchivoRegistroContinuo.tmp."""
        tmp_file = os.path.join(self.tmp_dir, "NombreArchivoRegistroContinuo.tmp")
        with open(tmp_file, "w") as f:
            f.write("DEV00_261014-170000.dat\nDEV00_261014-160000.dat\n")

        activos = g_acq.obtener_archivos_activos_continuous(self.rc_dir, self.test_dir)
        self.assertIn("DEV00_261014-170000.dat", activos)
        self.assertNotIn("DEV00_261014-160000.dat", activos)

    def test_obtener_archivos_activos_fallback_mtime(self):
        """Verifica que el archivo modificado hace menos de 2 horas se considera activo."""
        f1 = os.path.join(self.rc_dir, "DEV00_261014-150000.dat")
        with open(f1, "w") as f:
            f.write("bin_data")

        activos = g_acq.obtener_archivos_activos_continuous(self.rc_dir, self.test_dir)
        self.assertIn("DEV00_261014-150000.dat", activos)

    # ------------------------------------------------------------------------
    # 4. Pruebas de Exclusión Concurrente en main()
    # ------------------------------------------------------------------------
    @patch("gestor_archivos_acq.adquirir_cerrojo")
    @patch("gestor_archivos_acq._ejecutar_gestor")
    def test_main_cerrojo_ocupado_aborta_limpiamente(self, mock_ejecutar, mock_adquirir):
        """Verifica que si el cerrojo ya está tomado, main() no ejecuta trabajo."""
        mock_adquirir.return_value = None
        g_acq.main()
        mock_ejecutar.assert_not_called()


if __name__ == "__main__":
    unittest.main()
