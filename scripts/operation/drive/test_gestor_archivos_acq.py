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
    # 2. Pruebas de Cerrojo Atómico Exclusivo (flock) y Manejo de Permisos
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

    def test_adquirir_cerrojo_permisos_y_concurrencia(self):
        """Verifica que adquirir_cerrojo distingue concurrencia ('ocupado') de fallos de permisos ('error_permiso')."""
        lock_fd, estado = g_acq.adquirir_cerrojo(self.lock_file, return_status=True)
        self.assertIsNotNone(lock_fd)
        self.assertEqual(estado, "ok")

        lock2, estado2 = g_acq.adquirir_cerrojo(self.lock_file, return_status=True)
        self.assertIsNone(lock2)
        self.assertEqual(estado2, "ocupado")

        g_acq.liberar_cerrojo(lock_fd)

        # Simular fallo de permisos sin colisión de concurrencia
        with patch("builtins.open", side_effect=PermissionError("Permission denied")):
            with patch("sys.stderr"):
                lock3, estado3 = g_acq.adquirir_cerrojo(self.lock_file, return_status=True)
                self.assertIsNone(lock3)
                self.assertEqual(estado3, "error_permiso")

    def test_obtener_ruta_cerrojo(self):
        """Verifica resolución prioritaria de lockfile en tmp-files/ y fallback a usuario."""
        with patch.dict(os.environ, {"PROJECT_LOCAL_ROOT": self.test_dir, "USER": "rsa"}):
            ruta = g_acq.obtener_ruta_cerrojo()
            self.assertEqual(ruta, os.path.join(self.tmp_dir, "gestor_archivos_acq.lock"))

        with patch.dict(os.environ, {"PROJECT_LOCAL_ROOT": "", "USER": "rsa"}):
            ruta_fallback = g_acq.obtener_ruta_cerrojo()
            self.assertEqual(ruta_fallback, "/tmp/gestor_archivos_acq_rsa.lock")

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
    # 4. Pruebas de Exclusión Concurrente y Errores en main()
    # ------------------------------------------------------------------------
    @patch("builtins.print")
    @patch("gestor_archivos_acq.adquirir_cerrojo")
    @patch("gestor_archivos_acq._ejecutar_gestor")
    def test_main_cerrojo_ocupado_aborta_limpiamente(self, mock_ejecutar, mock_adquirir, mock_print):
        """Verifica que si el cerrojo ya está tomado, main() no ejecuta trabajo y emite aviso limpio."""
        mock_adquirir.return_value = (None, "ocupado")
        g_acq.main()
        mock_ejecutar.assert_not_called()
        mock_print.assert_called_once()
        self.assertIn("Otra instancia", mock_print.call_args[0][0])

    @patch("builtins.print")
    @patch("sys.exit")
    @patch("gestor_archivos_acq.adquirir_cerrojo")
    @patch("gestor_archivos_acq._ejecutar_gestor")
    def test_main_reacciona_a_error_permiso(self, mock_ejecutar, mock_adquirir, mock_exit, mock_print):
        """Verifica que si ocurre PermissionError en cerrojo, main() sale con exit(1) reportando el error."""
        mock_adquirir.return_value = (None, "error_permiso")
        g_acq.main()
        mock_ejecutar.assert_not_called()
        mock_exit.assert_called_once_with(1)
        mock_print.assert_called_once()
        self.assertIn("Permiso denegado", mock_print.call_args[0][0])

    # ------------------------------------------------------------------------
    # 5. Pruebas de Control de Espacio: Sin NameError y con Desalojo Dinámico
    # ------------------------------------------------------------------------
    @patch("gestor_archivos_acq.esta_protegido_por_fallo", return_value=False)
    def test_control_espacio_sin_name_error_y_desalojo_dinamico(self, mock_prot):
        """
        Verifica que el bucle de control de espacio:
        1. No crashea por NameError ('archivo_mas_reciente_continuous' erradicada).
        2. Protege incondicionalmente el archivo .dat activo en grabación SPI.
        3. Desaloja archivos iterativamente hasta que el espacio recupera el umbral mínimo (>= 10%).
        """
        activo_nombre = "DEV00_activo_261019-120000.dat"
        tmp_file = os.path.join(self.tmp_dir, "NombreArchivoRegistroContinuo.tmp")
        with open(tmp_file, "w") as f:
            f.write(f"{activo_nombre}\n")

        with open(os.path.join(self.rc_dir, activo_nombre), "w") as f:
            f.write("active_data")

        antiguos = []
        for i in range(1, 4):
            path_antiguo = os.path.join(self.rc_dir, f"DEV00_antiguo_{i}.dat")
            with open(path_antiguo, "w") as f:
                f.write("old_data")
            os.utime(path_antiguo, (100000 + i * 100, 100000 + i * 100))
            antiguos.append(path_antiguo)

        espacios = [4.0, 7.0, 11.0, 12.0]
        def mock_espacio(path):
            if espacios:
                return espacios.pop(0)
            return 12.0

        mock_logger = MagicMock()
        archivos_activos = g_acq.obtener_archivos_activos_continuous(self.rc_dir, self.test_dir)
        self.assertIn(activo_nombre, archivos_activos)

        free_space = mock_espacio(self.ms_dir)
        umbral_minimo = 10

        archivos_paths = [os.path.join(self.rc_dir, f) for f in os.listdir(self.rc_dir) if f.endswith(".dat")]
        archivos_ordenados = sorted(archivos_paths, key=os.path.getmtime)

        for ruta_archivo in archivos_ordenados:
            if os.path.basename(ruta_archivo) in archivos_activos:
                continue

            eliminado = g_acq.eliminar_archivo_con_verificacion(
                ruta_archivo, "continuous", self.log_dir, mock_logger, dry_run=False
            )
            if eliminado:
                free_space = mock_espacio(self.ms_dir)
                if free_space >= umbral_minimo:
                    break

        self.assertTrue(os.path.exists(os.path.join(self.rc_dir, activo_nombre)))
        self.assertFalse(os.path.exists(antiguos[0]))
        self.assertFalse(os.path.exists(antiguos[1]))
        self.assertTrue(os.path.exists(antiguos[2]))


if __name__ == "__main__":
    unittest.main()
