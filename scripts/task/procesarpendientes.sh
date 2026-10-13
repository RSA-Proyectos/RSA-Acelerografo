#!/bin/bash
# Wrapper ejecutable para procesar y subir archivos pendientes
# Se instala automáticamente como /usr/local/bin/procesarpendientes mediante deploy.sh

# Cargar las variables de entorno institucionales
if [ -f /usr/local/bin/project_paths ]; then
  source /usr/local/bin/project_paths
elif [ -f "$HOME/projects/acelerografo/scripts/env/project_paths.sh" ]; then
  source "$HOME/projects/acelerografo/scripts/env/project_paths.sh"
fi

PROJECT_LOCAL_ROOT="${PROJECT_LOCAL_ROOT:-$HOME/projects/acelerografo}"
VENV_PYTHON="$PROJECT_LOCAL_ROOT/.venv/bin/python3"
SCRIPT_PYTHON="$PROJECT_LOCAL_ROOT/scripts/drive/procesar_y_subir_pendientes.py"

if [ ! -f "$VENV_PYTHON" ]; then
  echo "ERROR: Entorno virtual no encontrado en $VENV_PYTHON"
  exit 1
fi

if [ ! -f "$SCRIPT_PYTHON" ]; then
  # Fallback a la ruta del repositorio si se ejecuta antes del deploy
  SCRIPT_PYTHON="$(dirname "$(dirname "$(readlink -f "$0")")")/operation/drive/procesar_y_subir_pendientes.py"
fi

exec "$VENV_PYTHON" "$SCRIPT_PYTHON" "$@"
