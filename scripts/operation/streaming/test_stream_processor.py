"""
Tests unitarios para streaming/stream_processor.py

Ejecutar:
    cd /home/rsa/git/montajes/acelerografo-DEV00
    python3 scripts/operation/streaming/test_stream_processor.py

o con pytest:
    python3 -m pytest scripts/operation/streaming/test_stream_processor.py -v

No requiere hardware real. Usa un FIFO temporal en /tmp para simular el pipe
del sistema C. Las tramas se construyen con build_test_frame() del módulo
core/frame_decoder.py.

Estrategia de tests:
    - Se crea un FIFO real con os.mkfifo() en /tmp para cada test que necesita I/O.
    - El escritor envía bytes al FIFO en un hilo separado para evitar deadlock.
    - StreamProcessor se ejecuta en modo dry_run o con RingBufferStore temporal.
    - Se utiliza stop() + join() para terminar el daemon limpiamente en los tests.
"""

import os
import sys
import time
import signal
import tempfile
import threading
import datetime
import traceback

# Agregar el directorio scripts/operation al path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.frame_decoder import (
    FRAME_SIZE,
    build_test_frame,
    decode_timestamp,
)
from streaming.stream_processor import StreamProcessor
from streaming.ring_buffer_store import RingBufferStore


# ---------------------------------------------------------------------------
# Infraestructura de test (sin dependencia de pytest)
# ---------------------------------------------------------------------------

_tests_run = 0
_tests_passed = 0
_tests_failed = 0
_failures = []


def _run_test(name: str, fn):
    global _tests_run, _tests_passed, _tests_failed
    _tests_run += 1
    try:
        fn()
        _tests_passed += 1
        print(f"  ✅ {name}")
    except AssertionError as e:
        _tests_failed += 1
        _failures.append((name, str(e)))
        print(f"  ❌ {name}: {e}")
    except Exception as e:
        _tests_failed += 1
        _failures.append((name, f"{type(e).__name__}: {e}"))
        print(f"  ❌ {name}: {type(e).__name__}: {e}")
        traceback.print_exc()


def _assert_eq(a, b, msg=""):
    assert a == b, f"{msg} → esperado={b!r}, obtenido={a!r}"


def _assert_raises(exc_type, fn, msg=""):
    try:
        fn()
        assert False, f"{msg} → se esperaba {exc_type.__name__} pero no se lanzó"
    except exc_type:
        pass


# ---------------------------------------------------------------------------
# Utilidades de test
# ---------------------------------------------------------------------------

def _make_frame(hour=14, minute=30, second=0, x=0, y=0, z=0) -> bytes:
    """Construye una trama de prueba con timestamp dado."""
    return build_test_frame(
        year=2026, month=6, day=16,
        hour=hour, minute=minute, second=second,
        x_value=x, y_value=y, z_value=z
    )


def _make_invalid_frame() -> bytes:
    """Construye una trama con timestamp inválido (hora=99, minuto=99, segundo=99)."""
    frame = bytearray(_make_frame())
    frame[2503] = 99   # hora inválida
    frame[2504] = 99   # minuto inválido
    frame[2505] = 99   # segundo inválido
    return bytes(frame)


def _make_temp_fifo() -> str:
    """Crea un FIFO temporal en /tmp y retorna su ruta."""
    fifo_path = tempfile.mktemp(prefix="rsa_test_pipe_", suffix=".fifo")
    os.mkfifo(fifo_path)
    return fifo_path


def _cleanup_fifo(fifo_path: str) -> None:
    """Elimina el FIFO si existe."""
    try:
        os.unlink(fifo_path)
    except OSError:
        pass


def _escribir_en_pipe(fifo_path: str, datos: bytes, delay_s: float = 0.05) -> threading.Thread:
    """
    Inicia un hilo que abre el FIFO como escritor y envía los datos.

    El delay permite que el lector (StreamProcessor) abra el pipe primero.
    Retorna el hilo (ya iniciado) para que el test pueda hacer join() si desea.
    """
    def _escritor():
        time.sleep(delay_s)
        try:
            fd = os.open(fifo_path, os.O_WRONLY)
            os.write(fd, datos)
            os.close(fd)
        except OSError:
            pass

    t = threading.Thread(target=_escritor, daemon=True)
    t.start()
    return t


def _run_processor_con_timeout(processor: StreamProcessor, timeout_s: float = 2.0) -> None:
    """
    Ejecuta processor.run() en un hilo y lo detiene después de timeout_s segundos.

    Garantiza que el test no se cuelgue si el processor no termina por sí solo.
    """
    def _runner():
        processor.run()

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    time.sleep(timeout_s)
    processor.stop()
    t.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Tests: inicialización y validación de argumentos
# ---------------------------------------------------------------------------

def test_init_valores_por_defecto():
    """StreamProcessor se inicializa con valores por defecto correctos."""
    p = StreamProcessor()
    _assert_eq(p._pipe_path, "/tmp/my_pipe", "pipe_path por defecto")
    _assert_eq(p._buffer_dir, "/home/rsa/data/ring-buffer/", "buffer_dir por defecto")
    _assert_eq(p._dry_run, False, "dry_run por defecto")
    _assert_eq(p.frames_procesados, 0, "frames_procesados inicial")
    _assert_eq(p.frames_invalidos, 0, "frames_invalidos inicial")
    _assert_eq(p.frames_error, 0, "frames_error inicial")


def test_init_valores_personalizados():
    """StreamProcessor acepta parámetros personalizados."""
    p = StreamProcessor(
        pipe_path="/tmp/otro_pipe",
        buffer_dir="/tmp/mi_ring/",
        max_size_mb=100,
        archivo_duracion_s=60,
        dry_run=True,
    )
    _assert_eq(p._pipe_path, "/tmp/otro_pipe", "pipe_path personalizado")
    _assert_eq(p._dry_run, True, "dry_run=True")
    _assert_eq(p._max_size_mb, 100, "max_size_mb personalizado")


# ---------------------------------------------------------------------------
# Tests: apertura del pipe
# ---------------------------------------------------------------------------

def test_abrir_pipe_no_existente_lanza_error():
    """_abrir_pipe() directo lanza FileNotFoundError si el pipe no existe."""
    p = StreamProcessor(pipe_path="/tmp/pipe_que_no_existe_rsa.fifo", dry_run=True)
    _assert_raises(
        FileNotFoundError,
        p._abrir_pipe,
        "pipe inexistente debe lanzar FileNotFoundError al llamar a _abrir_pipe"
    )


def test_abrir_pipe_retry_timeout_lanza_error():
    """_abrir_pipe_con_retry() lanza RuntimeError si el pipe no aparece tras timeout."""
    p = StreamProcessor(
        pipe_path="/tmp/pipe_que_no_existe_rsa.fifo",
        pipe_retry_max_s=0.1,
        dry_run=True
    )
    p._running = True
    _assert_raises(
        RuntimeError,
        p._abrir_pipe_con_retry,
        "_abrir_pipe_con_retry debe lanzar RuntimeError tras expirar el timeout"
    )


def test_run_pipe_inexistente_termina_limpiamente():
    """run() termina limpiamente registrando error sin propagar excepción fatal."""
    p = StreamProcessor(
        pipe_path="/tmp/pipe_que_no_existe_rsa.fifo",
        pipe_retry_max_s=0.1,
        dry_run=True
    )
    # run() no debe levantar excepción
    p.run()
    assert p._running is False, "El procesador debe quedar detenido tras fallo de pipe"


def test_abrir_pipe_existente():
    """_abrir_pipe() abre el FIFO correctamente y asigna un fd válido."""
    fifo_path = _make_temp_fifo()
    try:
        p = StreamProcessor(pipe_path=fifo_path, dry_run=True)
        # Abrir en un hilo para evitar bloqueo (O_RDWR no bloquea)
        p._abrir_pipe()
        assert p._fd is not None, "fd debe ser asignado tras abrir el pipe"
        assert p._fd >= 0, f"fd debe ser >= 0, obtenido: {p._fd}"
        p._cerrar_pipe()
        assert p._fd is None, "fd debe ser None tras cerrar"
    finally:
        _cleanup_fifo(fifo_path)


# ---------------------------------------------------------------------------
# Tests: procesamiento de tramas individuales
# ---------------------------------------------------------------------------

def test_procesar_trama_valida_dry_run():
    """_procesar_trama() en dry_run incrementa frames_procesados con trama válida."""
    p = StreamProcessor(dry_run=True)
    frame = _make_frame(hour=14, minute=30, second=0)
    p._procesar_trama(frame)
    _assert_eq(p.frames_procesados, 1, "1 trama procesada")
    _assert_eq(p.frames_invalidos, 0, "sin inválidas")


def test_procesar_trama_invalida_incrementa_contador():
    """_procesar_trama() con timestamp inválido incrementa frames_invalidos."""
    p = StreamProcessor(dry_run=True)
    frame = _make_invalid_frame()
    p._procesar_trama(frame)
    _assert_eq(p.frames_invalidos, 1, "1 trama inválida")
    _assert_eq(p.frames_procesados, 0, "0 tramas procesadas")


def test_procesar_multiples_tramas_dry_run():
    """Múltiples llamadas a _procesar_trama() acumulan correctamente los contadores."""
    p = StreamProcessor(dry_run=True)
    # 3 válidas + 2 inválidas
    for s in range(3):
        p._procesar_trama(_make_frame(hour=14, minute=30, second=s))
    for _ in range(2):
        p._procesar_trama(_make_invalid_frame())

    _assert_eq(p.frames_procesados, 3, "3 tramas válidas")
    _assert_eq(p.frames_invalidos, 2, "2 tramas inválidas")


# ---------------------------------------------------------------------------
# Tests: acumulación de lecturas parciales
# ---------------------------------------------------------------------------

def test_acumulador_vacio_al_inicio():
    """El acumulador interno está vacío al inicializar el processor."""
    p = StreamProcessor(dry_run=True)
    _assert_eq(len(p._acumulador), 0, "acumulador vacío inicial")


def test_acumulador_maneja_lectura_parcial():
    """
    El bucle de lectura maneja correctamente tramas que llegan en múltiples
    fragmentos (lecturas parciales).

    Simula una trama enviada en 3 chunks: el procesador debe acumularlos
    y emitir exactamente 1 trama completa.
    """
    fifo_path = _make_temp_fifo()
    try:
        p = StreamProcessor(pipe_path=fifo_path, dry_run=True)

        frame = _make_frame(hour=10, minute=0, second=0)
        # Dividir la trama en 3 partes
        parte1 = frame[:500]
        parte2 = frame[500:2000]
        parte3 = frame[2000:]

        def _escritor_fragmentado():
            time.sleep(0.1)
            fd = os.open(fifo_path, os.O_WRONLY)
            os.write(fd, parte1)
            time.sleep(0.05)
            os.write(fd, parte2)
            time.sleep(0.05)
            os.write(fd, parte3)
            os.close(fd)

        t = threading.Thread(target=_escritor_fragmentado, daemon=True)
        t.start()

        _run_processor_con_timeout(p, timeout_s=1.5)
        t.join(timeout=2.0)

        _assert_eq(p.frames_procesados, 1, "1 trama procesada desde 3 fragmentos")
        _assert_eq(p.frames_invalidos, 0, "sin inválidas")
    finally:
        _cleanup_fifo(fifo_path)


# ---------------------------------------------------------------------------
# Tests: flujo completo con FIFO real (dry_run)
# ---------------------------------------------------------------------------

def test_flujo_completo_una_trama():
    """El processor recibe 1 trama válida por el FIFO y la procesa correctamente."""
    fifo_path = _make_temp_fifo()
    try:
        p = StreamProcessor(pipe_path=fifo_path, dry_run=True)
        frame = _make_frame(hour=9, minute=0, second=0)

        _escribir_en_pipe(fifo_path, frame, delay_s=0.15)
        _run_processor_con_timeout(p, timeout_s=1.0)

        _assert_eq(p.frames_procesados, 1, "1 trama procesada")
        _assert_eq(p.frames_invalidos, 0, "sin inválidas")
    finally:
        _cleanup_fifo(fifo_path)


def test_flujo_completo_multiples_tramas():
    """El processor recibe N tramas consecutivas y las cuenta correctamente."""
    fifo_path = _make_temp_fifo()
    try:
        p = StreamProcessor(pipe_path=fifo_path, dry_run=True)

        N = 5
        datos = b"".join(_make_frame(hour=10, minute=0, second=s) for s in range(N))

        _escribir_en_pipe(fifo_path, datos, delay_s=0.15)
        _run_processor_con_timeout(p, timeout_s=1.5)

        _assert_eq(p.frames_procesados, N, f"{N} tramas procesadas")
        _assert_eq(p.frames_invalidos, 0, "sin inválidas")
    finally:
        _cleanup_fifo(fifo_path)


def test_flujo_invalidas_intercaladas():
    """
    El processor descarta tramas inválidas y procesa las válidas correctamente
    cuando están intercaladas.
    """
    fifo_path = _make_temp_fifo()
    try:
        p = StreamProcessor(pipe_path=fifo_path, dry_run=True)

        # válida, inválida, válida, inválida, válida
        datos = (
            _make_frame(hour=10, minute=0, second=0)
            + _make_invalid_frame()
            + _make_frame(hour=10, minute=0, second=2)
            + _make_invalid_frame()
            + _make_frame(hour=10, minute=0, second=4)
        )

        _escribir_en_pipe(fifo_path, datos, delay_s=0.15)
        _run_processor_con_timeout(p, timeout_s=1.5)

        _assert_eq(p.frames_procesados, 3, "3 tramas válidas procesadas")
        _assert_eq(p.frames_invalidos, 2, "2 tramas inválidas descartadas")
    finally:
        _cleanup_fifo(fifo_path)


# ---------------------------------------------------------------------------
# Tests: flujo completo con RingBufferStore real
# ---------------------------------------------------------------------------

def test_flujo_completo_con_ring_buffer():
    """
    Integración completa: el processor escribe tramas al RingBufferStore real
    y los datos son recuperables mediante query_raw().
    """
    fifo_path = _make_temp_fifo()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            N = 3
            tramas = [_make_frame(hour=11, minute=0, second=s, x=s * 100) for s in range(N)]
            datos = b"".join(tramas)

            p = StreamProcessor(
                pipe_path=fifo_path,
                buffer_dir=tmpdir,
                max_size_mb=10,
                archivo_duracion_s=3600,
                usar_fecha_filename=False,  # En tests usamos fecha desde trama
                dry_run=False,
            )

            _escribir_en_pipe(fifo_path, datos, delay_s=0.15)
            _run_processor_con_timeout(p, timeout_s=1.5)

            _assert_eq(p.frames_procesados, N, f"{N} tramas escritas al ring buffer")
            _assert_eq(p.frames_invalidos, 0, "sin inválidas")

            # Verificar que el ring buffer contiene los datos
            store = RingBufferStore(
                directorio=tmpdir,
                max_size_mb=10,
                archivo_duracion_s=3600,
                usar_fecha_filename=False,
            )
            results = store.query_raw(
                start=datetime.datetime(2026, 6, 16, 11, 0, 0),
                end=datetime.datetime(2026, 6, 16, 11, 0, 59),
            )
            store.close()

            _assert_eq(len(results), N, f"{N} tramas recuperadas del ring buffer")
    finally:
        _cleanup_fifo(fifo_path)


# ---------------------------------------------------------------------------
# Tests: señales y cierre limpio
# ---------------------------------------------------------------------------

def test_stop_detiene_el_bucle():
    """stop() detiene el bucle de lectura sin forzar el kill del hilo."""
    fifo_path = _make_temp_fifo()
    try:
        p = StreamProcessor(pipe_path=fifo_path, dry_run=True)

        t = threading.Thread(target=p.run, daemon=True)
        t.start()
        time.sleep(0.3)
        p.stop()
        t.join(timeout=3.0)

        assert not t.is_alive(), "El hilo del processor debe haber terminado tras stop()"
    finally:
        _cleanup_fifo(fifo_path)


def test_stop_antes_de_run_no_falla():
    """Llamar stop() antes de run() no causa errores."""
    p = StreamProcessor(dry_run=True)
    p.stop()  # No debe lanzar ninguna excepción
    _assert_eq(p._running, False, "_running debe ser False")


def test_signal_sigterm_detiene_processor():
    """
    El manejador de SIGTERM llama a stop(), lo que termina el bucle de lectura.
    Se simula llamando a _signal_handler() directamente desde el hilo principal.
    No se invoca _registrar_señales() para evitar restricción de hilo principal.
    """
    fifo_path = _make_temp_fifo()
    try:
        p = StreamProcessor(pipe_path=fifo_path, dry_run=True)
        # No llamar _registrar_señales(): signal.signal() solo funciona en el
        # hilo principal y el test no puede garantizarlo desde el runner.

        t = threading.Thread(target=p.run, daemon=True)
        t.start()
        time.sleep(0.2)

        # Llamar directamente al manejador (simula el efecto de recibir SIGTERM)
        p._signal_handler(signal.SIGTERM, None)

        t.join(timeout=3.0)
        assert not t.is_alive(), "El hilo debe terminar tras simular SIGTERM"
    finally:
        _cleanup_fifo(fifo_path)


# ---------------------------------------------------------------------------
# Tests: estadísticas
# ---------------------------------------------------------------------------

def test_estadisticas_iniciales_son_cero():
    """Los contadores de estadísticas comienzan en cero."""
    p = StreamProcessor(dry_run=True)
    _assert_eq(p.frames_procesados, 0, "frames_procesados")
    _assert_eq(p.frames_invalidos, 0, "frames_invalidos")
    _assert_eq(p.frames_error, 0, "frames_error")


def test_estadisticas_acumulan_correctamente():
    """Los contadores acumulan de forma monotónicamente creciente."""
    p = StreamProcessor(dry_run=True)
    for s in range(5):
        p._procesar_trama(_make_frame(hour=12, minute=0, second=s))
    for _ in range(3):
        p._procesar_trama(_make_invalid_frame())

    assert p.frames_procesados == 5, f"esperado 5, obtenido {p.frames_procesados}"
    assert p.frames_invalidos == 3, f"esperado 3, obtenido {p.frames_invalidos}"
    assert p.frames_error == 0, f"esperado 0, obtenido {p.frames_error}"


# ---------------------------------------------------------------------------
# Tests: auto-recuperación (Self-Healing) del pipe
# ---------------------------------------------------------------------------

def test_pipe_es_valido_detecta_eliminacion():
    """_pipe_es_valido() detecta cuando el archivo FIFO ha sido eliminado de disco."""
    fifo_path = _make_temp_fifo()
    try:
        p = StreamProcessor(pipe_path=fifo_path, dry_run=True)
        p._abrir_pipe()
        assert p._pipe_es_valido() is True, "El pipe recién abierto debe ser válido"

        # Eliminar el archivo del disco (simula rm -f)
        os.unlink(fifo_path)
        assert p._pipe_es_valido() is False, "El descriptor huérfano debe ser detectado como inválido"
        p._cerrar_pipe()
    finally:
        _cleanup_fifo(fifo_path)


def test_pipe_es_valido_detecta_cambio_inodo():
    """_pipe_es_valido() detecta cuando el FIFO fue borrado y recreado con nuevo inodo."""
    fifo_path = _make_temp_fifo()
    try:
        p = StreamProcessor(pipe_path=fifo_path, dry_run=True)
        p._abrir_pipe()
        assert p._pipe_es_valido() is True, "Pipe original debe ser válido"

        # Simular recreación del FIFO por reinicio de registro_continuo
        os.unlink(fifo_path)
        os.mkfifo(fifo_path)

        assert p._pipe_es_valido() is False, "El descriptor debe detectar cambio de inodo"
        p._cerrar_pipe()
    finally:
        _cleanup_fifo(fifo_path)


def test_autorrecuperacion_reconexion_tras_recrear_pipe():
    """
    StreamProcessor se reconecta automáticamente y reanuda la lectura sin reiniciar el proceso
    cuando el named pipe es borrado y recreado con un nuevo inodo en tiempo de ejecución.
    """
    fifo_path = _make_temp_fifo()
    try:
        p = StreamProcessor(pipe_path=fifo_path, dry_run=True)

        def _runner():
            p.run()

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        time.sleep(0.3)

        # 1. Enviar primera trama por el pipe original
        _escribir_en_pipe(fifo_path, _make_frame(hour=15, minute=0, second=0), delay_s=0.05)
        time.sleep(0.4)
        _assert_eq(p.frames_procesados, 1, "Primera trama procesada en pipe original")

        # 2. Simular reinicio de registro_continuo (rm -f /tmp/my_pipe y mkfifo)
        os.unlink(fifo_path)
        time.sleep(0.2)
        os.mkfifo(fifo_path)

        # Dar margen al bucle de lectura (chequeo cada 1s) para detectar y reconectar
        time.sleep(2.0)

        # 3. Enviar segunda trama por el nuevo pipe recreado
        _escribir_en_pipe(fifo_path, _make_frame(hour=15, minute=0, second=1), delay_s=0.05)
        time.sleep(0.6)

        p.stop()
        t.join(timeout=2.0)

        _assert_eq(p.frames_procesados, 2, "Debe haber procesado 2 tramas tras la auto-reconexión")
        _assert_eq(p.frames_invalidos, 0, "Sin tramas inválidas")
    finally:
        _cleanup_fifo(fifo_path)


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("\n" + "=" * 65)
    print("  Tests: streaming/stream_processor.py")
    print("=" * 65)

    grupos = [
        ("inicialización y argumentos", [
            test_init_valores_por_defecto,
            test_init_valores_personalizados,
        ]),
        ("apertura del pipe", [
            test_abrir_pipe_no_existente_lanza_error,
            test_abrir_pipe_retry_timeout_lanza_error,
            test_run_pipe_inexistente_termina_limpiamente,
            test_abrir_pipe_existente,
        ]),
        ("procesamiento de tramas individuales", [
            test_procesar_trama_valida_dry_run,
            test_procesar_trama_invalida_incrementa_contador,
            test_procesar_multiples_tramas_dry_run,
        ]),
        ("acumulación de lecturas parciales", [
            test_acumulador_vacio_al_inicio,
            test_acumulador_maneja_lectura_parcial,
        ]),
        ("flujo completo con FIFO real (dry_run)", [
            test_flujo_completo_una_trama,
            test_flujo_completo_multiples_tramas,
            test_flujo_invalidas_intercaladas,
        ]),
        ("flujo completo con RingBufferStore real", [
            test_flujo_completo_con_ring_buffer,
        ]),
        ("señales y cierre limpio", [
            test_stop_detiene_el_bucle,
            test_stop_antes_de_run_no_falla,
            test_signal_sigterm_detiene_processor,
        ]),
        ("estadísticas", [
            test_estadisticas_iniciales_son_cero,
            test_estadisticas_acumulan_correctamente,
        ]),
        ("auto-recuperación (Self-Healing) del pipe", [
            test_pipe_es_valido_detecta_eliminacion,
            test_pipe_es_valido_detecta_cambio_inodo,
            test_autorrecuperacion_reconexion_tras_recrear_pipe,
        ]),
    ]

    for grupo_nombre, fns in grupos:
        print(f"\n▶ {grupo_nombre}")
        for fn in fns:
            _run_test(fn.__doc__ or fn.__name__, fn)

    print("\n" + "=" * 65)
    print(f"  Resultado: {_tests_passed}/{_tests_run} tests pasados", end="")
    if _tests_failed > 0:
        print(f", {_tests_failed} fallidos")
        print("\nFallas:")
        for name, msg in _failures:
            print(f"  • {name}: {msg}")
    else:
        print(" — Todo OK ✅")
    print("=" * 65 + "\n")

    sys.exit(0 if _tests_failed == 0 else 1)
