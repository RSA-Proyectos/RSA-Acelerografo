#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_binary_to_mseed.py — Suite de pruebas unitarias para binary_to_mseed.py

Valida la corrección sismológica de la conversión de archivos binarios .dat
a formato MiniSEED, con énfasis en el manejo de gaps temporales, integridad
de fase, rendimiento, deduplicación y robustez ante tramas corruptas.

Ejecutar en la estación de desarrollo:
    .venv/bin/python3 scripts/mseed/test_binary_to_mseed.py
"""

import os
import sys
import time
import unittest
import tempfile
import numpy as np
from obspy import read

# Configurar path para imports en DEV0 y entorno de desarrollo
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_CURRENT_DIR)
_GRANDPARENT_DIR = os.path.dirname(_PARENT_DIR)

for _p in [
    _CURRENT_DIR,
    _PARENT_DIR,
    os.path.join(_PARENT_DIR, 'core'),
    os.path.join(_PARENT_DIR, 'mseed'),
    os.path.join(_GRANDPARENT_DIR, 'core'),
    os.path.join(_GRANDPARENT_DIR, 'mseed'),
    os.path.join(_GRANDPARENT_DIR, 'scripts', 'operation', 'core'),
    os.path.join(_GRANDPARENT_DIR, 'scripts', 'core'),
]:
    if os.path.exists(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from core.frame_decoder import build_test_frame, FRAME_SIZE
except ImportError:
    try:
        from frame_decoder import build_test_frame, FRAME_SIZE
    except ImportError:
        # Fallback si se ejecuta desde otro directorio
        from scripts.operation.core.frame_decoder import build_test_frame, FRAME_SIZE

try:
    from binary_to_mseed import (
        leer_archivo_binario,
        obtenerTraza,
        conversion_mseed_digital,
        extraer_tiempo_binario,
        procesar_archivo_individual,
    )
except ImportError:
    from scripts.mseed.binary_to_mseed import (
        leer_archivo_binario,
        obtenerTraza,
        conversion_mseed_digital,
        extraer_tiempo_binario,
        procesar_archivo_individual,
    )


CONFIG_MSEED_TEST = {
    "CODIGO(1)": "DEV0",
    "SENSOR(2)": "ACELEROGRAFO",
    "CALIDAD(16)": "D",
    "UBICACION(17)": "00",
    "CANAL(18)": "ZNE",
    "RED(19)": "EC",
    "MUESTREO(20)": 250,
}


class MockLogger:
    """Logger silencioso compatible con StructuredLogger para tests."""
    def data_warning(self, *args, **kwargs): pass
    def convert_fail(self, *args, **kwargs): pass
    def convert_ok(self, *args, **kwargs): pass
    def read_ok(self, *args, **kwargs): pass
    def convert_start(self, *args, **kwargs): pass
    def info(self, *args, **kwargs): pass
    def error(self, *args, **kwargs): pass
    def summary(self, *args, **kwargs): pass
    def config_error(self, *args, **kwargs): pass


def crear_archivo_binario_sintetico(tramas_specs, filepath):
    """
    Genera un archivo binario .dat temporal a partir de especificaciones de tramas.
    """
    with open(filepath, 'wb') as f:
        for spec in tramas_specs:
            frame = build_test_frame(
                year=spec.get('year', 2026),
                month=spec.get('month', 10),
                day=spec.get('day', 20),
                hour=spec.get('hour', 14),
                minute=spec.get('minute', 0),
                second=spec.get('second', 0),
                x_value=spec.get('x_value', 0),
                y_value=spec.get('y_value', 0),
                z_value=spec.get('z_value', 0),
            )
            f.write(frame)
    return filepath


class TestBinaryToMseed(unittest.TestCase):
    """Suite de pruebas unitarias para binary_to_mseed."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_mseed_")
        self.logger = MockLogger()

    def tearDown(self):
        # Limpieza recursiva
        for root, dirs, files in os.walk(self.temp_dir, topdown=False):
            for f in files:
                try:
                    os.remove(os.path.join(root, f))
                except OSError:
                    pass
            for d in dirs:
                try:
                    os.rmdir(os.path.join(root, d))
                except OSError:
                    pass
        try:
            os.rmdir(self.temp_dir)
        except OSError:
            pass

    # -------------------------------------------------------------------------
    # T1: Conversión nominal sin gaps
    # -------------------------------------------------------------------------
    def test_conversion_sin_gaps(self):
        """T1: Verifica conversión de archivo continuo sin gaps (10 segundos)."""
        specs = [
            {'hour': 14, 'minute': 0, 'second': s, 'x_value': 100, 'y_value': -50, 'z_value': 200}
            for s in range(10)
        ]
        dat_path = os.path.join(self.temp_dir, "DEV0_261020-140000.dat")
        crear_archivo_binario_sintetico(specs, dat_path)

        datos_np, gaps, _, _, _, t_valid = leer_archivo_binario(dat_path, self.logger)
        self.assertIsNone(gaps)
        self.assertEqual(datos_np.shape[0], 3)
        self.assertEqual(datos_np.shape[1], 10 * 250)

        traza = obtenerTraza("HN", 1, datos_np[0], t_valid, gaps, CONFIG_MSEED_TEST)
        self.assertEqual(traza.stats.npts, 2500)
        self.assertEqual(len(traza.data), 2500)
        self.assertEqual(traza.stats.sampling_rate, 250)

    # -------------------------------------------------------------------------
    # T2: Conversión con gap corto y no truncamiento post-gap
    # -------------------------------------------------------------------------
    def test_conversion_con_gap_corto(self):
        """T2: Verifica que un gap de 2s rellene ceros y NO trunque tramas posteriores."""
        # 10 tramas en total: segs 0-4 (5s) y segs 7-11 (5s). Gaps: segs 5 y 6 (2s). Total = 12s.
        segs = [0, 1, 2, 3, 4, 7, 8, 9, 10, 11]
        specs = [{'hour': 14, 'minute': 0, 'second': s, 'x_value': 1000} for s in segs]
        dat_path = os.path.join(self.temp_dir, "gap_corto.dat")
        crear_archivo_binario_sintetico(specs, dat_path)

        datos_np, gaps, _, _, _, t_valid = leer_archivo_binario(dat_path, self.logger)
        self.assertIsNotNone(gaps)
        self.assertEqual(gaps, [14 * 3600 + 5, 14 * 3600 + 6])

        traza = obtenerTraza("HN", 1, datos_np[0], t_valid, gaps, CONFIG_MSEED_TEST)
        # 12 segundos * 250 = 3000 muestras
        self.assertEqual(traza.stats.npts, 3000)
        self.assertEqual(len(traza.data), 3000)

        # Muestras en gaps (seg 5 y 6 -> índices 1250 a 1749) deben ser cero
        self.assertTrue(np.all(traza.data[1250:1750] == 0))
        # Muestras posteriores al gap (seg 7 a 11 -> índices 1750 a 3000) NO deben truncarse
        self.assertEqual(traza.data[1750], 1000)
        self.assertEqual(traza.data[2999], 1000)

    # -------------------------------------------------------------------------
    # T3: Rendimiento con gap masivo (sin livelock)
    # -------------------------------------------------------------------------
    def test_conversion_gap_masivo_rendimiento(self):
        """T3: Simula archivo de CHA02 con 3500s faltantes y verifica tiempo < 0.5s."""
        # 100 tramas presentes, 3500 faltantes
        fsample = 250
        datos_canal = np.ones(100 * fsample, dtype=np.int32)
        # Gaps: 3500 segundos intermedios
        seg_inicio = 14 * 3600
        gaps = list(range(seg_inicio + 50, seg_inicio + 3550))
        t_valid = {"anio": 2026, "mes": 10, "dia": 20, "hora": 14, "minuto": 0, "segundo": 0}

        t0 = time.time()
        traza = obtenerTraza("HN", 1, datos_canal, t_valid, gaps, CONFIG_MSEED_TEST)
        elapsed = time.time() - t0

        self.assertLess(elapsed, 0.5, f"Livelock detectado: tardó {elapsed:.3f}s")
        self.assertEqual(traza.stats.npts, 3600 * fsample)
        self.assertEqual(len(traza.data), 3600 * fsample)

    # -------------------------------------------------------------------------
    # T4: Integridad de fase exacta (prueba contra H2)
    # -------------------------------------------------------------------------
    def test_integridad_fase_sin_desfase(self):
        """T4: Verifica que las ondas no se rebanen y cada segundo mantenga su posición exacta."""
        # seg 0 -> 10, seg 1 -> 20, seg 2 -> 30, gap en 3 y 4, seg 5 -> 50, seg 6 -> 60, seg 7 -> 70
        specs = [
            {'hour': 14, 'minute': 0, 'second': 0, 'x_value': 10},
            {'hour': 14, 'minute': 0, 'second': 1, 'x_value': 20},
            {'hour': 14, 'minute': 0, 'second': 2, 'x_value': 30},
            {'hour': 14, 'minute': 0, 'second': 5, 'x_value': 50},
            {'hour': 14, 'minute': 0, 'second': 6, 'x_value': 60},
            {'hour': 14, 'minute': 0, 'second': 7, 'x_value': 70},
        ]
        dat_path = os.path.join(self.temp_dir, "fase_exacta.dat")
        crear_archivo_binario_sintetico(specs, dat_path)

        datos_np, gaps, _, _, _, t_valid = leer_archivo_binario(dat_path, self.logger)
        traza = obtenerTraza("HN", 1, datos_np[0], t_valid, gaps, CONFIG_MSEED_TEST)

        # Verificación exacta de bloques de 250 muestras
        self.assertTrue(np.all(traza.data[0:250] == 10))      # seg 0
        self.assertTrue(np.all(traza.data[250:500] == 20))    # seg 1
        self.assertTrue(np.all(traza.data[500:750] == 30))    # seg 2
        self.assertTrue(np.all(traza.data[750:1000] == 0))    # seg 3 (gap)
        self.assertTrue(np.all(traza.data[1000:1250] == 0))   # seg 4 (gap)
        self.assertTrue(np.all(traza.data[1250:1500] == 50))  # seg 5 (datos reales preservados)
        self.assertTrue(np.all(traza.data[1500:1750] == 60))  # seg 6
        self.assertTrue(np.all(traza.data[1750:2000] == 70))  # seg 7

    # -------------------------------------------------------------------------
    # T5: Dimensión exacta sin inflación dimensional (H3)
    # -------------------------------------------------------------------------
    def test_dimension_exacta_sin_inflacion(self):
        """T5: Verifica que stats['npts'] sea idéntico a len(traza.data) sin doble conteo."""
        fsample = 250
        num_tramas = 3000
        num_gaps = 600
        datos_canal = np.zeros(num_tramas * fsample, dtype=np.int32)
        gaps = list(range(num_gaps))
        t_valid = {"anio": 2026, "mes": 10, "dia": 20, "hora": 0, "minuto": 0, "segundo": 0}

        traza = obtenerTraza("HN", 1, datos_canal, t_valid, gaps, CONFIG_MSEED_TEST)
        npts_esperado = (num_tramas + num_gaps) * fsample
        self.assertEqual(len(traza.data), npts_esperado)
        self.assertEqual(traza.stats.npts, npts_esperado)

    # -------------------------------------------------------------------------
    # T6: Cruce de medianoche continuo (sin gaps)
    # -------------------------------------------------------------------------
    def test_cruce_medianoche(self):
        """T6: Transición 23:59:58 -> 00:00:01 continua no debe generar gaps falsos."""
        specs = [
            {'hour': 23, 'minute': 59, 'second': 58, 'x_value': 1},
            {'hour': 23, 'minute': 59, 'second': 59, 'x_value': 2},
            {'hour': 0, 'minute': 0, 'second': 0, 'x_value': 3},
            {'hour': 0, 'minute': 0, 'second': 1, 'x_value': 4},
        ]
        dat_path = os.path.join(self.temp_dir, "medianoche_continua.dat")
        crear_archivo_binario_sintetico(specs, dat_path)

        datos_np, gaps, _, _, _, _ = leer_archivo_binario(dat_path, self.logger)
        self.assertIsNone(gaps)
        self.assertEqual(datos_np.shape[1], 4 * 250)

    # -------------------------------------------------------------------------
    # T6b: Cruce de medianoche con gaps
    # -------------------------------------------------------------------------
    def test_cruce_medianoche_con_gap(self):
        """T6b: Transición 23:59:58 a 00:00:02 debe detectar exactamente 3 gaps (86399, 0, 1)."""
        specs = [
            {'hour': 23, 'minute': 59, 'second': 58, 'x_value': 100},
            {'hour': 0, 'minute': 0, 'second': 2, 'x_value': 200},
        ]
        dat_path = os.path.join(self.temp_dir, "medianoche_con_gap.dat")
        crear_archivo_binario_sintetico(specs, dat_path)

        _, gaps, _, _, _, _ = leer_archivo_binario(dat_path, self.logger)
        self.assertIsNotNone(gaps)
        self.assertEqual(len(gaps), 3)
        self.assertEqual(gaps, [86399, 0, 1])

    # -------------------------------------------------------------------------
    # T7: Filtrado de tramas duplicadas (H4c)
    # -------------------------------------------------------------------------
    def test_tramas_duplicadas_filtradas(self):
        """T7: Tramas repetidas con el mismo timestamp deben ser deduplicadas preservando la primera."""
        specs = [
            {'hour': 14, 'minute': 0, 'second': 0, 'x_value': 10},
            {'hour': 14, 'minute': 0, 'second': 0, 'x_value': 99}, # Duplicada
            {'hour': 14, 'minute': 0, 'second': 1, 'x_value': 20},
            {'hour': 14, 'minute': 0, 'second': 1, 'x_value': 99}, # Duplicada
            {'hour': 14, 'minute': 0, 'second': 2, 'x_value': 30},
        ]
        dat_path = os.path.join(self.temp_dir, "duplicados.dat")
        crear_archivo_binario_sintetico(specs, dat_path)

        datos_np, gaps, _, _, _, _ = leer_archivo_binario(dat_path, self.logger)
        self.assertIsNone(gaps)
        # Solo deben quedar 3 tramas únicas = 750 muestras
        self.assertEqual(datos_np.shape[1], 3 * 250)
        self.assertEqual(datos_np[0, 0], 10)
        self.assertEqual(datos_np[0, 250], 20)
        self.assertEqual(datos_np[0, 500], 30)

    # -------------------------------------------------------------------------
    # T8: Primera trama inválida descartada y timestamp validado (H6)
    # -------------------------------------------------------------------------
    def test_primera_trama_invalida_timestamp_validado(self):
        """T8: Primera trama con hora=255 debe descartarse y usar segunda trama como referencia."""
        specs = [
            {'hour': 14, 'minute': 0, 'second': 1, 'x_value': 111},
            {'hour': 14, 'minute': 0, 'second': 2, 'x_value': 222},
        ]
        dat_path = os.path.join(self.temp_dir, "trama_corrupta.dat")
        crear_archivo_binario_sintetico(specs, dat_path)

        # Inyectar una trama con hora corrupta (255) al inicio
        frame_corrupto = bytearray(build_test_frame(hour=14, minute=0, second=0))
        frame_corrupto[2503] = 255  # hora = 255 inválido
        with open(dat_path, 'rb') as f:
            contenido_original = f.read()
        with open(dat_path, 'wb') as f:
            f.write(frame_corrupto)
            f.write(contenido_original)

        datos_np, _, _, _, _, t_valid = leer_archivo_binario(dat_path, self.logger)
        self.assertIsNotNone(t_valid)
        self.assertEqual(t_valid['hora'], 14)
        self.assertEqual(t_valid['segundo'], 1)  # Debe tomar la primera trama VÁLIDA
        self.assertEqual(datos_np.shape[1], 2 * 250)

    # -------------------------------------------------------------------------
    # T9: Rendimiento de archivo continuo
    # -------------------------------------------------------------------------
    def test_archivo_sin_gaps_rendimiento(self):
        """T9: Conversión nominal completa debe completarse en < 3s."""
        fsample = 250
        datos_canal = np.ones(3600 * fsample, dtype=np.int32)
        t_valid = {"anio": 2026, "mes": 10, "dia": 20, "hora": 14, "minuto": 0, "segundo": 0}

        t0 = time.time()
        traza = obtenerTraza("HN", 1, datos_canal, t_valid, None, CONFIG_MSEED_TEST)
        elapsed = time.time() - t0

        self.assertLess(elapsed, 0.1)
        self.assertEqual(traza.stats.npts, 900000)

    # -------------------------------------------------------------------------
    # T10: Retorno del sexto valor (tiempo_binario_validado)
    # -------------------------------------------------------------------------
    def test_retorno_sexto_valor_tiempo_validado(self):
        """T10: Verifica que leer_archivo_binario retorne 6 valores estructurados."""
        specs = [{'hour': 10, 'minute': 15, 'second': s} for s in range(3)]
        dat_path = os.path.join(self.temp_dir, "seis_valores.dat")
        crear_archivo_binario_sintetico(specs, dat_path)

        res = leer_archivo_binario(dat_path, self.logger)
        self.assertEqual(len(res), 6)
        t_valid = res[5]
        self.assertIsInstance(t_valid, dict)
        self.assertEqual(t_valid['hora'], 10)
        self.assertEqual(t_valid['minuto'], 15)
        self.assertEqual(t_valid['segundo'], 0)

    # -------------------------------------------------------------------------
    # T11: Purga de tramas corruptas (glitch SPI) y outliers temporales (Fase 6 / H7)
    # -------------------------------------------------------------------------
    def test_purga_glitch_spi_y_outlier_temporal_t11(self):
        """T11: Verifica que tramas con bytes repetidos (0x04) o timestamps outlier
        no causen falsos cruces de medianoche ni inflen la traza a 25 horas.
        """
        dat_path = os.path.join(self.temp_dir, "glitch_outlier.dat")

        f0 = build_test_frame(year=2026, month=10, day=20, hour=17, minute=0, second=0, x_value=100)
        f1 = build_test_frame(year=2026, month=10, day=20, hour=17, minute=0, second=1, x_value=101)
        f2 = build_test_frame(year=2026, month=10, day=20, hour=17, minute=0, second=2, x_value=102)

        # 1. Trama corrupta con glitch SPI (bytes idénticos 0x04)
        f_glitch_spi = bytearray(build_test_frame(hour=17, minute=0, second=2))
        f_glitch_spi[2500] = 4
        f_glitch_spi[2501] = 4
        f_glitch_spi[2502] = 4
        f_glitch_spi[2503] = 4
        f_glitch_spi[2504] = 4
        f_glitch_spi[2505] = 4

        # 2. Trama con timestamp outlier aislado (año 2026 válido, pero hora=4, min=4, seg=4)
        f_outlier = bytearray(build_test_frame(year=2026, month=10, day=20, hour=4, minute=4, second=4))

        f3 = build_test_frame(year=2026, month=10, day=20, hour=17, minute=0, second=3, x_value=103)
        f4 = build_test_frame(year=2026, month=10, day=20, hour=17, minute=0, second=4, x_value=104)
        f5 = build_test_frame(year=2026, month=10, day=20, hour=17, minute=0, second=5, x_value=105)

        with open(dat_path, 'wb') as f:
            f.write(f0)
            f.write(f1)
            f.write(f2)
            f.write(f_glitch_spi)
            f.write(f_outlier)
            f.write(f3)
            f.write(f4)
            f.write(f5)

        datos_np, gaps, _, _, _, t_valid = leer_archivo_binario(dat_path, self.logger)

        # Ambas tramas espurias deben ser descartadas/purgadas:
        # Quedan exactamente 6 tramas válidas (6 * 250 = 1500 muestras)
        self.assertEqual(datos_np.shape[1], 6 * 250)
        # No debe haber gaps falsos de medianoche ni desfases masivos
        self.assertIsNone(gaps)
        self.assertEqual(t_valid['hora'], 17)
        self.assertEqual(t_valid['segundo'], 0)

        # La traza final debe durar exactamente 6 segundos (1500 muestras) y no 25 horas
        traza = obtenerTraza("HN", 1, datos_np[0], t_valid, gaps, CONFIG_MSEED_TEST)
        self.assertEqual(traza.stats.npts, 1500)
        self.assertEqual(len(traza.data), 1500)
        self.assertAlmostEqual(traza.stats.endtime - traza.stats.starttime, (1500 - 1) / 250.0, places=3)

    def test_archivo_truncado_por_apagon_conserva_fecha_t12(self):
        """
        T12: Simula un corte abrupto de energía a mitad de escritura (caso CHA2_261001-150002.dat).
        El archivo contiene 5 tramas válidas seguidas de 1200 bytes residuales (trama cortada).
        Verifica que leer_archivo_binario y procesar_archivo_individual extraigan la fecha y hora
        completas de forma autónoma sin KeyError: 'anio' y generen el miniSEED correspondiente.
        """
        dat_path = os.path.join(self.temp_dir, "DEV00_261001-150002.dat")
        frames = [
            build_test_frame(year=2026, month=10, day=1, hour=15, minute=0, second=s, x_value=200 + s)
            for s in range(5)
        ]

        # Simular 1200 bytes residuales de una trama cortada a mitad por corte de luz
        cola_truncada = b"\x00\xFF\x55\xAA" * 300

        with open(dat_path, 'wb') as f:
            for fr in frames:
                f.write(fr)
            f.write(cola_truncada)

        # 1. Verificar lectura y extracción autónoma de fecha
        datos_np, gaps, _, ts_i, ts_f, t_valid = leer_archivo_binario(dat_path, self.logger)

        self.assertEqual(datos_np.shape[1], 5 * 250)
        self.assertIsNone(gaps)
        self.assertIsNotNone(t_valid)
        self.assertIn("anio", t_valid)
        self.assertEqual(t_valid["anio"], 2026)
        self.assertEqual(t_valid["mes"], 10)
        self.assertEqual(t_valid["dia"], 1)
        self.assertEqual(t_valid["hora"], 15)
        self.assertEqual(t_valid["segundo"], 0)

        # ts_inicio debe estar intacto a pesar de que la cola física esté rota
        self.assertIsNotNone(ts_i)
        self.assertEqual(ts_i.hour, 15)
        self.assertEqual(ts_i.minute, 0)
        self.assertEqual(ts_i.second, 0)

        # 2. Verificar que obtenerTraza genera la traza sin KeyError
        traza = obtenerTraza("HN", 1, datos_np[0], t_valid, gaps, CONFIG_MSEED_TEST)
        self.assertEqual(traza.stats.npts, 1250)
        self.assertEqual(traza.stats.starttime.year, 2026)
        self.assertEqual(traza.stats.starttime.month, 10)
        self.assertEqual(traza.stats.starttime.day, 1)

        # 3. Verificar conversión completa con procesar_archivo_individual
        out_dir = os.path.join(self.temp_dir, "output_t12")
        os.makedirs(out_dir, exist_ok=True)
        ok, msg, info = procesar_archivo_individual(
            dat_path, out_dir, "DEV00", CONFIG_MSEED_TEST, self.logger, usar_fecha_filename=False
        )

        self.assertTrue(ok, f"Falló conversión de archivo truncado: {msg}")
        self.assertIsNotNone(info)
        self.assertTrue(os.path.exists(info["mseed_path"]))

        # Verificar integridad del miniSEED generado en disco
        st = read(info["mseed_path"])
        self.assertEqual(len(st), 3)
        for tr in st:
            self.assertEqual(tr.stats.npts, 1250)
            self.assertEqual(tr.stats.starttime.year, 2026)
            self.assertEqual(tr.stats.starttime.month, 10)
            self.assertEqual(tr.stats.starttime.day, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)

