#!/bin/bash

# Definir la raíz del proyecto en Git y el proyecto local
#USER_HOME=$(eval echo ~$SUDO_USER)  # Obtiene el home del usuario original cuando se usa sudo
#PROJECT_GIT_ROOT="$USER_HOME/git/Acelerografo-RSA"
#PROJECT_LOCAL_ROOT="$USER_HOME/projects/acelerografo-rsa"

echo "Usando la ruta del repositorio Git: $PROJECT_GIT_ROOT"
echo "Usando la ruta del proyecto local: $PROJECT_LOCAL_ROOT"

# Función para actualizar archivos si han cambiado, ignorando la versión en el nombre de los archivos de destino
function update_files_if_changed {
    local src_dir=$(echo "$1" | sed 's:/*$::')
    local dest_dir=$(echo "$2" | sed 's:/*$::')
    local changes_detected=false

    for src_file in $(find $src_dir -type f); do
        # Eliminar la versión del nombre del archivo de origen para construir el nombre de destino
        base_name=$(basename "$src_file")
        base_name_no_version=$(echo "$base_name" | sed -r 's/_[0-9]+\.[0-9]+\.[0-9]+//')
        dest_file="$dest_dir/$base_name_no_version"

        if [ ! -f "$dest_file" ] || [ "$src_file" -nt "$dest_file" ]; then
            echo "Actualizando: $dest_file"
            cp "$src_file" "$dest_file"
            changes_detected=true
        fi
    done

    if [ "$changes_detected" = false ]; then
        echo "No se detectaron cambios en $dest_dir"
    fi
}

# Función para actualizar el crontab si se detectan cambios en el archivo de origen
function update_crontab_if_changed {
    local src_file="$PROJECT_LOCAL_ROOT/configuracion/crontab.txt"
    local backup_file="$PROJECT_LOCAL_ROOT/tmp-files/crontab_backup.txt"

    # Verificar si el archivo de origen existe
    if [ ! -f "$src_file" ]; then
        echo "Error: El archivo de origen '$src_file' no existe."
        return 1
    fi

    # Si el archivo de respaldo no existe, crear uno inicial
    if [ ! -f "$backup_file" ]; then
        echo "Creando archivo de respaldo inicial: $backup_file"
        cp "$src_file" "$backup_file"
    fi

    # Comparar el archivo de origen con el archivo de respaldo
    if ! cmp -s "$src_file" "$backup_file"; then
        # Si los archivos son diferentes, actualizar el crontab y el archivo de respaldo
        echo "Detectados cambios en el archivo de crontab. Actualizando..."
        sudo crontab "$src_file"
        cp "$src_file" "$backup_file"
        echo "Crontab actualizado exitosamente."
    else
        echo "No se detectaron cambios en el archivo de crontab."
    fi
}

# Función para actualizar task-scripts en /usr/local/bin
function update_task_scripts {
    local src_dir=$1
    local dest_dir="/usr/local/bin"
    local task_changes_detected=false

    for script in $(find $src_dir -name "*.sh" -type f); do
        script_name=$(basename "$script" .sh)
        dest_file="$dest_dir/$script_name"
        if [ ! -f "$dest_file" ] || [ "$script" -nt "$dest_file" ]; then
            echo "Actualizando: $dest_file"
            sudo cp "$script" "$dest_file"
            sudo chmod +x "$dest_file"
            task_changes_detected=true
        fi
    done

    if [ "$task_changes_detected" = false ]; then
        echo "No se detectaron cambios en los task-scripts"
    fi
}

# Revisar y actualizar el crontab
update_crontab_if_changed

# Revisar y actualizar archivos en configuración, mqtt, mseed, drive
# Copiar y actualizar plantillas sin sobreescribir configuracion_maestra.json local si ya existe
cp $PROJECT_GIT_ROOT/configuration/*.template $PROJECT_LOCAL_ROOT/configuracion/
if [ ! -f "$PROJECT_LOCAL_ROOT/configuracion/configuracion_maestra.json" ]; then
    echo "Instalando configuración maestra inicial..."
    cp $PROJECT_GIT_ROOT/configuration/configuracion_maestra.json $PROJECT_LOCAL_ROOT/configuracion/
fi
# Re-hidratar configuraciones para aplicar posibles cambios en plantillas
PROJECT_GIT_ROOT=$PROJECT_GIT_ROOT PROJECT_LOCAL_ROOT=$PROJECT_LOCAL_ROOT python3 $PROJECT_GIT_ROOT/scripts/setup/hidratar_configuracion.py

update_files_if_changed "$PROJECT_GIT_ROOT/scripts/operation/mqtt/" "$PROJECT_LOCAL_ROOT/scripts/mqtt/"
update_files_if_changed "$PROJECT_GIT_ROOT/scripts/operation/mseed/" "$PROJECT_LOCAL_ROOT/scripts/mseed/"
update_files_if_changed "$PROJECT_GIT_ROOT/scripts/operation/drive/" "$PROJECT_LOCAL_ROOT/scripts/drive/"
mkdir -p "$PROJECT_LOCAL_ROOT/scripts/acelerografo"
update_files_if_changed "$PROJECT_GIT_ROOT/scripts/operation/acelerografo/" "$PROJECT_LOCAL_ROOT/scripts/acelerografo/"

# Actualizar nuevos módulos core y streaming
mkdir -p "$PROJECT_LOCAL_ROOT/scripts/core"
update_files_if_changed "$PROJECT_GIT_ROOT/scripts/operation/core/" "$PROJECT_LOCAL_ROOT/scripts/core/"

mkdir -p "$PROJECT_LOCAL_ROOT/scripts/streaming"
update_files_if_changed "$PROJECT_GIT_ROOT/scripts/operation/streaming/" "$PROJECT_LOCAL_ROOT/scripts/streaming/"

# Actualizar servidor web de configuración (se copia siempre para reflejar cambios en templates/static)
echo "Actualizando servidor web..."
mkdir -p $PROJECT_LOCAL_ROOT/scripts/web
cp -r $PROJECT_GIT_ROOT/scripts/operation/web/. $PROJECT_LOCAL_ROOT/scripts/web/

# Actualizar StructuredLogger (ubicado en la base de operation)
if [ -f "$PROJECT_GIT_ROOT/scripts/operation/structured_logger.py" ]; then
    cp "$PROJECT_GIT_ROOT/scripts/operation/structured_logger.py" "$PROJECT_LOCAL_ROOT/scripts/structured_logger.py"
    echo "Actualizando: $PROJECT_LOCAL_ROOT/scripts/structured_logger.py"
fi


# Revisar y actualizar task-scripts en /usr/local/bin
update_task_scripts "$PROJECT_GIT_ROOT/scripts/task/"

# Revisar si hay cambios en acelerografo o libraries y ejecutar make
if [ "$(find $PROJECT_GIT_ROOT/scripts/operation/acelerografo/ -type f -newer $PROJECT_LOCAL_ROOT/scripts/acelerografo/ejecutables/registro_continuo 2>/dev/null)" ] || \
   [ "$(find $PROJECT_GIT_ROOT/scripts/operation/acelerografo/libraries/ -type f -newer $PROJECT_LOCAL_ROOT/scripts/acelerografo/ejecutables/registro_continuo 2>/dev/null)" ]; then
    echo "Se detectaron cambios en acelerografo o libraries, ejecutando make..."
    cd $PROJECT_GIT_ROOT/scripts/setup/
    make
else
    echo "No se detectaron cambios en los acelerografo-scripts o sus librerias."
fi

# Función para actualizar servicio systemd de registro_continuo
function update_systemd_service {
    local src="$PROJECT_GIT_ROOT/scripts/task/rsa-acelerografo.service.template"
    local temp="$PROJECT_LOCAL_ROOT/tmp-files/rsa-acelerografo.service.tmp"
    local dest="/etc/systemd/system/rsa-acelerografo.service"

    if [ ! -f "$src" ]; then
        echo "Advertencia: No se encontró la plantilla del servicio systemd."
        return
    fi

    sed "s|{{PROJECT_LOCAL_ROOT}}|$PROJECT_LOCAL_ROOT|g" "$src" > "$temp"

    if [ ! -f "$dest" ] || ! cmp -s "$temp" "$dest"; then
        echo "Actualizando servicio systemd: rsa-acelerografo.service"
        sudo cp "$temp" "$dest"
        sudo systemctl daemon-reload
        sudo systemctl enable rsa-acelerografo.service
        sudo systemctl restart rsa-acelerografo.service
    else
        echo "No se detectaron cambios en el servicio systemd (rsa-acelerografo)."
    fi

    # Garantizar que el servicio esté siempre habilitado para auto-arranque tras reboot
    if ! systemctl is-enabled --quiet rsa-acelerografo.service 2>/dev/null; then
        echo "Habilitando auto-arranque en boot para rsa-acelerografo.service..."
        sudo systemctl enable rsa-acelerografo.service
    fi
}

# Revisar y actualizar servicio systemd
update_systemd_service

# Función para actualizar configuración de Supervisor
function update_supervisor_config {
    # --- mqtt_coordinator ---
    local src_mqtt="$PROJECT_GIT_ROOT/scripts/task/mqtt_coordinator.conf"
    local temp_mqtt="$PROJECT_LOCAL_ROOT/tmp-files/mqtt_coordinator.conf.tmp"
    local dest_mqtt="/etc/supervisor/conf.d/mqtt_coordinator.conf"

    sed "s|{{PROJECT_LOCAL_ROOT}}|$PROJECT_LOCAL_ROOT|g" "$src_mqtt" > "$temp_mqtt"

    if [ ! -f "$dest_mqtt" ] || ! cmp -s "$temp_mqtt" "$dest_mqtt"; then
        echo "Actualizando configuración de Supervisor: $dest_mqtt"
        sudo cp "$temp_mqtt" "$dest_mqtt"
        sudo supervisorctl reread
        sudo supervisorctl update
    else
        echo "No se detectaron cambios en la configuración de Supervisor (mqtt_coordinator)."
    fi

    # --- config_server ---
    local src_web="$PROJECT_GIT_ROOT/scripts/task/config_server.conf"
    local temp_web="$PROJECT_LOCAL_ROOT/tmp-files/config_server.conf.tmp"
    local dest_web="/etc/supervisor/conf.d/config_server.conf"

    sed -e "s|{{PROJECT_LOCAL_ROOT}}|$PROJECT_LOCAL_ROOT|g" \
        -e "s|{{PROJECT_GIT_ROOT}}|$PROJECT_GIT_ROOT|g" \
        "$src_web" > "$temp_web"

    if [ ! -f "$dest_web" ] || ! cmp -s "$temp_web" "$dest_web"; then
        echo "Actualizando configuración de Supervisor: $dest_web"
        sudo cp "$temp_web" "$dest_web"
        sudo supervisorctl reread
        sudo supervisorctl update
    else
        echo "No se detectaron cambios en la configuración de Supervisor (config_server)."
    fi

    # --- stream_processor ---
    local src_stream="$PROJECT_GIT_ROOT/scripts/task/stream_processor.conf"
    local temp_stream="$PROJECT_LOCAL_ROOT/tmp-files/stream_processor.conf.tmp"
    local dest_stream="/etc/supervisor/conf.d/stream_processor.conf"

    sed "s|{{PROJECT_LOCAL_ROOT}}|$PROJECT_LOCAL_ROOT|g" "$src_stream" > "$temp_stream"

    if [ ! -f "$dest_stream" ] || ! cmp -s "$temp_stream" "$dest_stream"; then
        echo "Actualizando configuración de Supervisor: $dest_stream"
        sudo cp "$temp_stream" "$dest_stream"
        sudo supervisorctl reread
        sudo supervisorctl update
    else
        echo "No se detectaron cambios en la configuración de Supervisor (stream_processor)."
    fi

    # --- gpd_worker ---
    local src_gpd="$PROJECT_GIT_ROOT/scripts/task/gpd_worker.conf"
    local temp_gpd="$PROJECT_LOCAL_ROOT/tmp-files/gpd_worker.conf.tmp"
    local dest_gpd="/etc/supervisor/conf.d/gpd_worker.conf"

    sed "s|{{PROJECT_LOCAL_ROOT}}|$PROJECT_LOCAL_ROOT|g" "$src_gpd" > "$temp_gpd"

    if [ ! -f "$dest_gpd" ] || ! cmp -s "$temp_gpd" "$dest_gpd"; then
        echo "Actualizando configuración de Supervisor: $dest_gpd"
        sudo cp "$temp_gpd" "$dest_gpd"
        sudo supervisorctl reread
        sudo supervisorctl update
    else
        echo "No se detectaron cambios en la configuración de Supervisor (gpd_worker)."
    fi
}


# Llamar a la función
update_supervisor_config

# Función para actualizar el entorno virtual si requirements.txt ha cambiado
function update_venv_if_changed {
    local src_file="$PROJECT_GIT_ROOT/requirements.txt"
    local venv_dir="$PROJECT_LOCAL_ROOT/.venv"
    local backup_file="$PROJECT_LOCAL_ROOT/tmp-files/requirements_backup.txt"

    if [ ! -d "$venv_dir" ]; then
        echo "Entorno virtual no encontrado. Creando..."
        bash "$PROJECT_GIT_ROOT/scripts/setup/crear_entorno_virtual.sh"
        cp "$src_file" "$backup_file" 2>/dev/null || true
        return
    fi

    if [ ! -f "$backup_file" ] || ! cmp -s "$src_file" "$backup_file"; then
        echo "Detectados cambios en requirements.txt. Actualizando entorno virtual..."
        "$venv_dir/bin/pip" install --upgrade -r "$src_file"
        cp "$src_file" "$backup_file"
    else
        echo "No se detectaron cambios en requirements.txt"
    fi
}

# Copiar modelo TFLite para GPD
mkdir -p "$PROJECT_LOCAL_ROOT/models"
if [ -f "$PROJECT_GIT_ROOT/models/gpd.tflite" ]; then
    cp "$PROJECT_GIT_ROOT/models/gpd.tflite" "$PROJECT_LOCAL_ROOT/models/"
    echo "Modelo GPD TFLite copiado a producción."
else
    echo "Advertencia: No se encontró el modelo $PROJECT_GIT_ROOT/models/gpd.tflite para copiar."
fi

# Revisar y actualizar el entorno virtual
update_venv_if_changed

echo "Actualización completada con éxito."
