#!/bin/bash

# ===================================================================
# diagnostico.sh - Script de Diagnóstico Automatizado de Telemetría
# Red Sísmica del Austro (RSA)
#
# Genera un reporte consolidado de salud del sistema, adquisición,
# sensor y almacenamiento en $PROJECT_LOCAL_ROOT/log-files/diagnostico_report.log
# ===================================================================

# Cargar variables de entorno del sistema si existen
if [ -f /usr/local/bin/project_paths ]; then
    source /usr/local/bin/project_paths
fi

# Si PROJECT_LOCAL_ROOT no está definido, usar la ruta estándar
if [ -z "$PROJECT_LOCAL_ROOT" ]; then
    PROJECT_LOCAL_ROOT="/home/rsa/projects/acelerografo"
fi

VENV_PYTHON="$PROJECT_LOCAL_ROOT/.venv/bin/python3"
LOG_DIR="$PROJECT_LOCAL_ROOT/log-files"
REPORT_FILE="$LOG_DIR/diagnostico_report.log"

# Asegurar existencia del directorio de logs
mkdir -p "$LOG_DIR"

# Parseo de argumentos
QUIET=false
RAW_REGISTRY=false
TARGET_CHANNEL="all"

for arg in "$@"; do
    case "$arg" in
        -q|--quiet)
            QUIET=true
            ;;
        --raw)
            RAW_REGISTRY=true
            ;;
        acquisition|sensor|drive|all)
            TARGET_CHANNEL="$arg"
            ;;
        -h|--help)
            echo "Uso: diagnostico [--quiet|-q] [--raw] [all|acquisition|sensor|drive]"
            echo "  all          : Ejecuta diagnóstico completo (por defecto)"
            echo "  acquisition  : Inspecciona servicio systemd, stream_processor, pipe y ring buffer"
            echo "  sensor       : Ejecuta wrapper y evalúa acelerómetro y reloj"
            echo "  drive        : Verifica sincronización, backlog MiniSEED y almacenamiento"
            echo "  --raw        : Muestra el volcado completo de subidas sin sintetizar"
            echo "  --quiet, -q  : Modo silencioso (no imprime a terminal, solo escribe a $REPORT_FILE)"
            exit 0
            ;;
        *)
            echo "Advertencia: Parámetro desconocido '$arg'. Opciones válidas: all, acquisition, sensor, drive, --quiet, --raw"
            ;;
    esac
done

# Función: Diagnóstico General del Sistema
diagnostico_general() {
    echo "--- SECCIÓN: ESTADO GENERAL DEL SISTEMA ---"
    echo "[Información del Sistema]"
    date --iso-8601=seconds 2>/dev/null || date -Iseconds 2>/dev/null || date
    hostname 2>/dev/null
    uptime 2>/dev/null
    echo ""

    echo "[Hardware Raspberry Pi]"
    if command -v vcgencmd >/dev/null 2>&1; then
        vcgencmd measure_temp 2>&1
        vcgencmd get_throttled 2>&1
    else
        echo "vcgencmd no disponible en este sistema"
    fi
    echo ""

    echo "[Espacio en Disco]"
    df -h
    echo ""

    echo "[Estado de Servicios Supervisor]"
    if command -v supervisorctl >/dev/null 2>&1; then
        sudo supervisorctl status 2>&1
    else
        echo "supervisorctl no disponible"
    fi
    echo ""

    echo "[Estado Servicio rsa-acelerografo.service]"
    if command -v systemctl >/dev/null 2>&1; then
        sudo systemctl status rsa-acelerografo.service --no-pager -l 2>&1
    else
        echo "systemctl no disponible"
    fi
    echo ""
}

# Función: Diagnóstico de Adquisición
diagnostico_acquisition() {
    echo "--- SECCIÓN: DIAGNÓSTICO ADQUISICIÓN ---"
    echo "[1. Estado del servicio systemd (registro_continuo en C)]"
    if command -v systemctl >/dev/null 2>&1; then
        sudo systemctl status rsa-acelerografo.service --no-pager -l 2>&1
        echo ""
        echo "[Últimos 30 logs de systemd]"
        sudo journalctl -u rsa-acelerografo.service --no-pager -n 30 2>&1
    else
        echo "systemctl no disponible"
    fi
    echo ""

    echo "[2. Estado del proceso stream_processor (Supervisor)]"
    if command -v supervisorctl >/dev/null 2>&1; then
        sudo supervisorctl status stream_processor 2>&1
    fi
    echo "--- Últimas 50 líneas de supervisor_stream_processor.log ---"
    if [ -f "$LOG_DIR/supervisor_stream_processor.log" ]; then
        tail -n 50 "$LOG_DIR/supervisor_stream_processor.log" 2>&1
    else
        echo "Archivo supervisor_stream_processor.log no encontrado"
    fi
    echo "--- Últimas 20 líneas de supervisor_stream_processor.err ---"
    if [ -f "$LOG_DIR/supervisor_stream_processor.err" ]; then
        tail -n 20 "$LOG_DIR/supervisor_stream_processor.err" 2>&1
    else
        echo "Archivo supervisor_stream_processor.err no encontrado"
    fi
    echo ""

    echo "[3. Inspección del Named Pipe /tmp/my_pipe]"
    ls -la /tmp/my_pipe 2>&1
    stat /tmp/my_pipe 2>&1
    echo ""

    echo "[4. Estado del Ring Buffer]"
    if [ -d /home/rsa/data/ring-buffer ]; then
        echo "Listado de /home/rsa/data/ring-buffer/ (10 más recientes):"
        ls -lt /home/rsa/data/ring-buffer/ 2>&1 | head -10
        echo "Últimos 5 archivos ring_*.bin por fecha de modificación:"
        find /home/rsa/data/ring-buffer/ -name "ring_*.bin" -printf "%T+ %p\n" 2>/dev/null | sort -r | head -5
    else
        echo "Directorio /home/rsa/data/ring-buffer no existe"
    fi
    echo ""

    echo "[5. Verificación de proceso stream_processor y descriptores de pipe]"
    PROC_PIDS=$(pgrep -f stream_processor.py 2>/dev/null)
    if [ -n "$PROC_PIDS" ]; then
        echo "PID(s) stream_processor: $PROC_PIDS"
        for pid in $PROC_PIDS; do
            echo "Descriptores de archivo para PID $pid:"
            ls -la "/proc/$pid/fd/" 2>/dev/null | grep pipe || echo "No se detectaron descriptores de pipe abiertos"
        done
    else
        echo "stream_processor.py no se encuentra en ejecución"
    fi
    echo ""

    echo "[6. Entradas de adquisición en mqtt_coordinator.log]"
    if [ -f "$LOG_DIR/mqtt_coordinator.log" ]; then
        grep -i -E "ACQUISITION|STALE|PIPE_RECONNECT|PIPE_RECONNECTED" "$LOG_DIR/mqtt_coordinator.log" 2>/dev/null | tail -20 || echo "Sin entradas de adquisición recientes"
    else
        echo "mqtt_coordinator.log no encontrado"
    fi
    echo ""
}

# Función: Diagnóstico de Sensor
diagnostico_sensor() {
    echo "--- SECCIÓN: DIAGNÓSTICO SENSOR ---"
    echo "[1. Ejecución directa del wrapper comprobar_registro_wrapper.py]"
    WRAPPER_PATH="$PROJECT_LOCAL_ROOT/scripts/acelerografo/comprobar_registro_wrapper.py"
    if [ ! -f "$WRAPPER_PATH" ]; then
        if [ -f "$PROJECT_LOCAL_ROOT/scripts/operation/acelerografo/comprobar_registro_wrapper.py" ]; then
            WRAPPER_PATH="$PROJECT_LOCAL_ROOT/scripts/operation/acelerografo/comprobar_registro_wrapper.py"
        fi
    fi

    if [ -f "$WRAPPER_PATH" ]; then
        if [ -x "$VENV_PYTHON" ]; then
            "$VENV_PYTHON" "$WRAPPER_PATH" 2>&1
        elif command -v python3 >/dev/null 2>&1; then
            python3 "$WRAPPER_PATH" 2>&1
        else
            echo "Error: Intérprete Python no encontrado ($VENV_PYTHON)"
        fi
    else
        echo "Error: Wrapper no encontrado en $WRAPPER_PATH"
    fi
    echo ""

    echo "[2. Detalle de archivo del wrapper]"
    ls -la "$WRAPPER_PATH" 2>&1
    echo ""

    echo "[3. Entradas de sensor en mqtt_coordinator.log]"
    if [ -f "$LOG_DIR/mqtt_coordinator.log" ]; then
        grep -i -E "SENSOR|ANOMALY|clock|wrapper" "$LOG_DIR/mqtt_coordinator.log" 2>/dev/null | tail -20 || echo "Sin entradas de sensor recientes"
    else
        echo "mqtt_coordinator.log no encontrado"
    fi
    echo ""

    echo "[4. Estado del servicio de adquisición (rsa-acelerografo)]"
    if command -v systemctl >/dev/null 2>&1; then
        sudo systemctl is-active rsa-acelerografo.service 2>&1
    else
        echo "systemctl no disponible"
    fi
    echo ""
}

# Función: Diagnóstico de Drive y Almacenamiento
diagnostico_drive() {
    echo "--- SECCIÓN: DIAGNÓSTICO DRIVE ---"
    echo "[1. Registro de subidas a Google Drive]"
    if [ "$RAW_REGISTRY" = true ]; then
        if [ -f "$LOG_DIR/uploaded_files_registry.json" ]; then
            cat "$LOG_DIR/uploaded_files_registry.json"
        elif [ -f "$LOG_DIR/drive_status.json" ]; then
            cat "$LOG_DIR/drive_status.json"
        else
            echo "No se encontró archivo de registro de subidas"
        fi
        echo ""
    else
        # Modo sintético estructurado
        if [ -f "$LOG_DIR/uploaded_files_registry.json" ] || [ -f "$LOG_DIR/drive_status.json" ]; then
            PYTHON_EXEC=""
            if [ -x "$VENV_PYTHON" ]; then
                PYTHON_EXEC="$VENV_PYTHON"
            elif command -v python3 >/dev/null 2>&1; then
                PYTHON_EXEC="python3"
            fi

            if [ -n "$PYTHON_EXEC" ]; then
                "$PYTHON_EXEC" - "$PROJECT_LOCAL_ROOT" "$LOG_DIR" << 'EOF'
import sys, os
project_root = sys.argv[1]
log_dir = sys.argv[2]
for p in [os.path.join(project_root, "scripts", "drive"), os.path.join(project_root, "scripts", "operation", "drive")]:
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)
try:
    import drive_status_manager as dsm
    resumen = dsm.obtener_resumen_diagnostico(log_dir, max_ultimos=5)
    tot = resumen["totales"]
    print(f"Estado de registro     : {log_dir}/uploaded_files_registry.json")
    print(f"Totales indexados      : {tot['exitosos']} exitosos | {tot['fallidos']} fallidos")
    print("")
    print("Métricas por Categoría:")
    for tipo, info in resumen["detalle_por_tipo"].items():
        if info["exitosos"] > 0 or info["fallidos"] > 0:
            print(f"  • {tipo:<11}: {info['exitosos']:>4} subidos | {info['fallidos']:>2} fallidos")
    print("")
    print("Últimos Archivos Subidos:")
    hay_ultimos = False
    for tipo, info in resumen["detalle_por_tipo"].items():
        if info["ultimos"]:
            hay_ultimos = True
            print(f"  [{tipo}]")
            for f_name, f_time in info["ultimos"]:
                print(f"    - {f_name} ({f_time})")
    if not hay_ultimos:
        print("  (Sin registros de subida recientes)")
    print("")
    print("Estado de Fallos Retenidos:")
    if resumen["fallidos_activos"]:
        for item in resumen["fallidos_activos"]:
            print(f"  ⚠️ [{item['tipo']}] {item['archivo']} (falló: {item['fecha']})")
    else:
        print("  ✓ Sin archivos fallidos retenidos.")
except Exception as e:
    print(f"Aviso: No se pudo generar resumen con drive_status_manager ({e}). Mostrando extracto:")
    status_file = os.path.join(log_dir, "uploaded_files_registry.json")
    if not os.path.isfile(status_file):
        status_file = os.path.join(log_dir, "drive_status.json")
    if os.path.isfile(status_file):
        with open(status_file, "r") as f:
            for _ in range(25):
                line = f.readline()
                if not line: break
                sys.stdout.write(line)
        print("... [usa --raw para ver el archivo completo]")
EOF
            else
                echo "Intérprete Python no disponible para sintetizar registro. Extracto inicial:"
                head -n 25 "$LOG_DIR/uploaded_files_registry.json" 2>/dev/null || head -n 25 "$LOG_DIR/drive_status.json" 2>/dev/null
                echo "... [usa --raw para ver el archivo completo]"
            fi
        else
            echo "No se encontró archivo de registro de subidas (uploaded_files_registry.json ni drive_status.json)"
        fi
        echo ""
    fi

    echo "[2. Archivos MiniSEED en cola de subida]"
    if [ -d /home/rsa/data/mseed ]; then
        COUNT_MSEED=$(ls -1 /home/rsa/data/mseed/*.mseed 2>/dev/null | wc -l)
        echo "Cantidad de archivos MiniSEED en disco: $COUNT_MSEED"
        echo "Últimos 10 archivos MiniSEED:"
        ls -lt /home/rsa/data/mseed/*.mseed 2>/dev/null | head -10
    else
        echo "Directorio /home/rsa/data/mseed no existe"
    fi
    echo ""

    echo "[3. Espacio en disco de almacenamiento de datos]"
    df -h /home/rsa/data/ 2>&1 || df -h
    echo ""

    echo "[4. Últimas 30 líneas de gestor_acq.log]"
    if [ -f "$LOG_DIR/gestor_acq.log" ]; then
        tail -n 30 "$LOG_DIR/gestor_acq.log" 2>&1
    else
        echo "Log del gestor no encontrado ($LOG_DIR/gestor_acq.log)"
    fi
    echo ""

    echo "[5. Entradas de Drive en mqtt_coordinator.log]"
    if [ -f "$LOG_DIR/mqtt_coordinator.log" ]; then
        grep -i -E "DRIVE|UPLOAD|SYNC|upload_retry|pending_backlog" "$LOG_DIR/mqtt_coordinator.log" 2>/dev/null | tail -20 || echo "Sin entradas de Drive recientes"
    else
        echo "mqtt_coordinator.log no encontrado"
    fi
    echo ""

    echo "[6. Conectividad con Google APIs]"
    ping -c 2 -W 3 www.googleapis.com 2>&1 || echo "Sin acceso a www.googleapis.com"
    echo ""
}

# Generador del reporte completo
generar_reporte() {
    echo "==================================================================="
    echo "REPORTE DE DIAGNÓSTICO AUTOMATIZADO - ESTACIÓN ACELEROGRÁFICA RSA"
    echo "==================================================================="
    echo "Fecha de generación : $(date --iso-8601=seconds 2>/dev/null || date -Iseconds 2>/dev/null || date)"
    echo "Estación            : $(hostname 2>/dev/null || echo 'desconocida')"
    echo "Canal diagnosticado : $TARGET_CHANNEL"
    echo "==================================================================="
    echo ""

    # Siempre incluir estado general del sistema
    diagnostico_general

    case "$TARGET_CHANNEL" in
        acquisition)
            diagnostico_acquisition
            ;;
        sensor)
            diagnostico_sensor
            ;;
        drive)
            diagnostico_drive
            ;;
        all)
            diagnostico_acquisition
            diagnostico_sensor
            diagnostico_drive
            ;;
    esac

    echo "==================================================================="
    echo "FIN DEL REPORTE"
    echo "==================================================================="
}

# Ejecución y redirección
if [ "$QUIET" = true ]; then
    generar_reporte > "$REPORT_FILE" 2>&1
else
    generar_reporte 2>&1 | tee "$REPORT_FILE"
fi

exit 0
