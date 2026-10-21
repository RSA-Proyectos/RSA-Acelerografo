#!/bin/bash
set -euo pipefail  # Modo seguro: salir en error, variables no definidas, errores en pipes

# Validar que las variables requeridas estén definidas
if [ -z "${PROJECT_GIT_ROOT:-}" ] || [ -z "${PROJECT_LOCAL_ROOT:-}" ]; then
    echo "ERROR: Las variables PROJECT_GIT_ROOT y PROJECT_LOCAL_ROOT deben estar definidas"
    exit 1
fi

echo "Usando la ruta del repositorio Git: $PROJECT_GIT_ROOT"
echo "Usando la ruta del proyecto local: $PROJECT_LOCAL_ROOT"

# Crear los directorios del proyecto local si no existen (sin sudo)
mkdir -p $PROJECT_LOCAL_ROOT
mkdir -p $PROJECT_LOCAL_ROOT/configuracion
mkdir -p $PROJECT_LOCAL_ROOT/log-files
mkdir -p $PROJECT_LOCAL_ROOT/tmp-files
mkdir -p $PROJECT_LOCAL_ROOT/resultados/eventos-detectados
mkdir -p $PROJECT_LOCAL_ROOT/resultados/eventos-extraidos
mkdir -p $PROJECT_LOCAL_ROOT/resultados/registro-continuo
mkdir -p $PROJECT_LOCAL_ROOT/resultados/mseed
mkdir -p $PROJECT_LOCAL_ROOT/scripts/acelerografo/ejecutables
mkdir -p $PROJECT_LOCAL_ROOT/scripts/acelerografo/libraries
mkdir -p $PROJECT_LOCAL_ROOT/scripts/mseed
mkdir -p $PROJECT_LOCAL_ROOT/scripts/mqtt
mkdir -p $PROJECT_LOCAL_ROOT/scripts/drive
mkdir -p $PROJECT_LOCAL_ROOT/scripts/export
mkdir -p $PROJECT_LOCAL_ROOT/scripts/task
mkdir -p $PROJECT_LOCAL_ROOT/scripts/web
mkdir -p /home/rsa/data/packages

# Asegurar que los directorios creados tengan la propiedad correcta (sin sudo)
chown -R $USER:$USER $PROJECT_LOCAL_ROOT
chown -R $USER:$USER /home/rsa/data/packages 2>/dev/null || true

# Crea los archivos necesarios
echo $(date) > $PROJECT_LOCAL_ROOT/resultados/registro-continuo/nueva-estacion.txt
echo 'nueva-estacion.txt' > $PROJECT_LOCAL_ROOT/tmp-files/NombreArchivoRegistroContinuo.tmp

# Crea los archivos log
touch $PROJECT_LOCAL_ROOT/log-files/drive.log
touch $PROJECT_LOCAL_ROOT/log-files/gestor_acq.log 
touch $PROJECT_LOCAL_ROOT/log-files/mqtt.log
touch $PROJECT_LOCAL_ROOT/log-files/mseed.log
touch $PROJECT_LOCAL_ROOT/log-files/registro_continuo.log
touch $PROJECT_LOCAL_ROOT/log-files/empaquetar.log

# Copiar la configuración maestra y las plantillas al proyecto local
cp $PROJECT_GIT_ROOT/configuration/configuracion_maestra.json $PROJECT_LOCAL_ROOT/configuracion/
cp $PROJECT_GIT_ROOT/configuration/*.template $PROJECT_LOCAL_ROOT/configuracion/

# Ejecutar la hidratación de la configuración
PROJECT_GIT_ROOT=$PROJECT_GIT_ROOT PROJECT_LOCAL_ROOT=$PROJECT_LOCAL_ROOT python3 $PROJECT_GIT_ROOT/scripts/setup/hidratar_configuracion.py

# Copiar los scripts de Python del proyecto en Git al proyecto local
cp $PROJECT_GIT_ROOT/scripts/operation/structured_logger.py $PROJECT_LOCAL_ROOT/scripts/structured_logger.py
cp $PROJECT_GIT_ROOT/scripts/operation/mqtt/*.py $PROJECT_LOCAL_ROOT/scripts/mqtt/
cp $PROJECT_GIT_ROOT/scripts/operation/mseed/*.py $PROJECT_LOCAL_ROOT/scripts/mseed/
cp $PROJECT_GIT_ROOT/scripts/operation/drive/*.py $PROJECT_LOCAL_ROOT/scripts/drive/
cp $PROJECT_GIT_ROOT/scripts/operation/export/*.py $PROJECT_LOCAL_ROOT/scripts/export/
cp -r $PROJECT_GIT_ROOT/scripts/operation/web/. $PROJECT_LOCAL_ROOT/scripts/web/

# Crear el entorno virtual e instalar dependencias Python
echo "Configurando entorno virtual Python..."
bash $PROJECT_GIT_ROOT/scripts/setup/crear_entorno_virtual.sh

# Copiar el task-script crontab.txt al directorio de proyectos
cp $PROJECT_GIT_ROOT/scripts/task/crontab.txt $PROJECT_LOCAL_ROOT/configuracion/
cp $PROJECT_GIT_ROOT/scripts/task/crontab.txt $PROJECT_LOCAL_ROOT/tmp-files/crontab_backup.txt 

# Copiar los archivos de configuracion de Supervisor al directorio de configuracion (procesando placeholders)
sed "s|{{PROJECT_LOCAL_ROOT}}|$PROJECT_LOCAL_ROOT|g" $PROJECT_GIT_ROOT/scripts/task/mqtt_coordinator.conf > $PROJECT_LOCAL_ROOT/tmp-files/mqtt_coordinator.conf
sudo cp $PROJECT_LOCAL_ROOT/tmp-files/mqtt_coordinator.conf /etc/supervisor/conf.d/

sed -e "s|{{PROJECT_LOCAL_ROOT}}|$PROJECT_LOCAL_ROOT|g" \
    -e "s|{{PROJECT_GIT_ROOT}}|$PROJECT_GIT_ROOT|g" \
    $PROJECT_GIT_ROOT/scripts/task/config_server.conf > $PROJECT_LOCAL_ROOT/tmp-files/config_server.conf
sudo cp $PROJECT_LOCAL_ROOT/tmp-files/config_server.conf /etc/supervisor/conf.d/

# Actualizar Supervisor
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl start mqtt_coordinator
sudo supervisorctl start config_server

# Copiar los task-scripts al directorio /usr/local/bin sin la extensión .sh
for script in $PROJECT_GIT_ROOT/scripts/task/*.sh; do
    script_name=$(basename "$script" .sh)
    sudo cp "$script" "/usr/local/bin/$script_name"
    # Conceder permisos de ejecución solo al script copiado
    sudo chmod +x "/usr/local/bin/$script_name"
done

# Crear un crontab con permiso de superusuario usando el contenido del archivo crontab.txt
sudo crontab $PROJECT_GIT_ROOT/scripts/task/crontab.txt

# Navegar al directorio donde está el Makefile y ejecutar make
cd $PROJECT_GIT_ROOT/scripts/setup/
make

# Instalar servicio systemd para registro_continuo
echo "Configurando servicio systemd rsa-acelerografo..."
sed "s|{{PROJECT_LOCAL_ROOT}}|$PROJECT_LOCAL_ROOT|g" \
    "$PROJECT_GIT_ROOT/scripts/task/rsa-acelerografo.service.template" \
    > "$PROJECT_LOCAL_ROOT/tmp-files/rsa-acelerografo.service"
sudo cp "$PROJECT_LOCAL_ROOT/tmp-files/rsa-acelerografo.service" \
    /etc/systemd/system/rsa-acelerografo.service
sudo systemctl daemon-reload
sudo systemctl enable rsa-acelerografo.service

# Asegurar que los archivos de log tengan permisos correctos
# (necesario porque el crontab de root puede crearlos después)
sudo chown $USER:$USER $PROJECT_LOCAL_ROOT/log-files/*.log 2>/dev/null || true
chmod 664 $PROJECT_LOCAL_ROOT/log-files/*.log 2>/dev/null || true

echo "Despliegue completado con éxito."
