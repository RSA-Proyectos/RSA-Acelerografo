#!/bin/bash

# Cargar las variables de entorno
source /usr/local/bin/project_paths

# Definir el Python del entorno virtual
VENV_PYTHON="$PROJECT_LOCAL_ROOT/.venv/bin/python3"

# Función para verificar si un proceso está ejecutándose
is_running() {
  pgrep -f "$1" > /dev/null 2>&1
  return $?
}

# Dependiendo de los parámetros que se le pasen al programa se usa una opción u otra
case "$1" in
  start)
    # 1. Verificar/iniciar registro_continuo via systemd
    if systemctl is-active --quiet rsa-acelerografo.service; then
      echo "registro_continuo ya está ejecutándose (systemd)"
    else
      echo "Iniciando registro_continuo via systemd..."
      sudo systemctl start rsa-acelerografo.service
      sleep 3
    fi
    
    # 2. Ejecutar conversión binary_to_mseed (esperar a que termine)
    echo "Ejecutando conversión a miniSEED..."
    "$VENV_PYTHON" "$PROJECT_LOCAL_ROOT/scripts/mseed/binary_to_mseed.py" 1
    
    # 3. Ejecutar gestor de archivos (con guarda de concurrencia)
    if is_running "gestor_archivos_acq.py"; then
      echo "Aviso: gestor_archivos_acq.py ya se encuentra en ejecución. Omitiendo lanzamiento concurrente."
    else
      echo "Ejecutando gestor de archivos..."
      "$VENV_PYTHON" "$PROJECT_LOCAL_ROOT/scripts/drive/gestor_archivos_acq.py" &
    fi
    ;;
  
  stop)
    echo "Deteniendo sistema de registro continuo..."
    sudo systemctl stop rsa-acelerografo.service
    pkill -f binary_to_mseed.py 2>/dev/null
    pkill -f gestor_archivos_acq.py 2>/dev/null
    # Limpieza proactiva de cerrojos huérfanos
    rm -f /tmp/gestor_archivos_acq*.lock 2>/dev/null
    rm -f "$PROJECT_LOCAL_ROOT/tmp-files/gestor_archivos_acq.lock" 2>/dev/null
    sudo "$PROJECT_LOCAL_ROOT/scripts/acelerografo/ejecutables/reset_master"
    ;;

  restart)
    echo "Reiniciando sistema de registro continuo..."
    $0 stop && $0 start
    ;;
  
  *)
    echo "Modo de uso: registrocontinuo start|stop|restart"
    exit 1
    ;;
esac

exit 0


