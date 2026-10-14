"""
Script para subir archivos a Google Drive con sistema de reintentos configurable

EJEMPLOS DE USO:

python3 subir_archivo.py --continuous archivo.dat      # Sube archivo de registro continuo
python3 subir_archivo.py --mseed archivo.mseed          # Sube archivo miniSEED procesado
python3 subir_archivo.py --event evento.dat             # Sube archivo de evento extraído
python3 subir_archivo.py --tmp temporal.tmp             # Sube archivo temporal
python3 subir_archivo.py --log sistema.log              # Sube archivo de log
python3 subir_archivo.py --mseed archivo.mseed --noauth_local_webserver # Autenticación en remoto

PARÁMETROS OPCIONALES:

--delete    : Borra el archivo local después de confirmar la subida exitosa a Google Drive
              Ejemplo: python3 subir_archivo.py --mseed archivo.mseed --delete

--test      : Activa modo de prueba que simula fallos de red (90% de probabilidad de fallo)
              Útil para verificar el sistema de reintentos y registro de archivos fallidos
              Ejemplo: python3 subir_archivo.py --mseed archivo.mseed --test
              Ejemplo: python3 subir_archivo.py --event evento.dat --delete --test

MODOS DISPONIBLES:

--continuous : Archivos de registro continuo (.dat)
               Directorio: directorios.registro_continuo
               Drive ID: drive.carpetas.continuos_id

--mseed      : Archivos miniSEED procesados (.mseed)
               Directorio: directorios.archivos_mseed
               Drive ID: drive.carpetas.mseed_id

--event      : Archivos de eventos extraídos (.dat)
               Directorio: directorios.eventos_extraidos
               Drive ID: drive.carpetas.events_id

--tmp        : Archivos temporales (.tmp)
               Directorio: directorios.archivos_temporales
               Drive ID: drive.carpetas.tmp_id

--log        : Archivos de log (.log)
               Directorio: log-files/ (hardcodeado)
               Drive ID: drive.carpetas.logs_id

SISTEMA DE REINTENTOS:

El script implementa reintentos automáticos en caso de fallos durante la subida:
- Número de reintentos: Configurado en drive.config.max_reintentos (por defecto: 3)
- Tiempo de espera entre reintentos: Configurado en drive.config.tiempo_espera (por defecto: 2 segundos)
- Logging detallado de cada intento
- Espera exponencial entre reintentos para evitar sobrecarga

REQUISITOS:

- Variable de entorno PROJECT_LOCAL_ROOT debe estar definida
- Archivos de configuración necesarios:
    * configuracion_dispositivo.json (con estructura drive.carpetas y drive.config)
    * drive_credentials.json
    * drive_token.json

ESTRUCTURA JSON REQUERIDA:

{
  "drive": {
    "carpetas": {
      "continuos_id": "...",
      "mseed_id": "...",
      "events_id": "...",
      "tmp_id": "...",
      "logs_id": "..."
    },
    "config": {
      "max_reintentos": 5,
      "tiempo_espera": 2
    }
  }
}
"""

######################################### ~Librerias~ #################################################
from __future__ import print_function
from googleapiclient import errors
from googleapiclient.http import MediaFileUpload
from googleapiclient.discovery import build
from httplib2 import Http
from oauth2client import file, client, tools
import os
from datetime import datetime
import time
import sys
import json
import logging

# Importar el gestor de estado de subidas
from drive_status_manager import marcar_como_fallido, marcar_como_exitoso
#######################################################################################################


##################################### ~Variables globales~ ############################################
loggers = {}
isConecctedDrive = False
SCOPES = 'https://www.googleapis.com/auth/drive'
TEST_MODE = False  # Modo de prueba para simular fallos
TEST_FAILURE_PROBABILITY = 0.9  # Probabilidad de fallo en modo test
#######################################################################################################


######################################### ~Funciones~ #################################################

def subir_archivo_con_reintentos(service, nombre_archivo, path_completo_archivo, drive_id,
                                  max_reintentos, tiempo_espera, logger, borrar_despues=False,
                                  tipo_archivo=None, log_directory=None):
    """
    Función reutilizable para subir un archivo a Google Drive con sistema de reintentos.

    Parámetros:
        service: Objeto de servicio autenticado de Google Drive
        nombre_archivo: Nombre del archivo (sin ruta)
        path_completo_archivo: Ruta completa del archivo local
        drive_id: ID de la carpeta de Drive destino
        max_reintentos: Número máximo de intentos
        tiempo_espera: Segundos de espera entre reintentos
        logger: Objeto logger para registro
        borrar_despues: Si True, borra el archivo local después de subida exitosa
        tipo_archivo: Tipo de archivo ("continuous", "mseed", "event", "tmp", "log") para el registro de fallos
        log_directory: Directorio de logs donde guardar el JSON de fallos

    Retorna:
        True si la subida fue exitosa, False en caso contrario
    """
    intento = 0
    archivo_subido = False

    while intento < max_reintentos and not archivo_subido:
        intento += 1
        try:
            if intento == 1:
                logger.info(f'Subiendo el archivo: {nombre_archivo}')
                print(f'Subiendo el archivo: {path_completo_archivo}')
            else:
                logger.info(f'Reintento {intento}/{max_reintentos} para subir el archivo: {nombre_archivo}')
                print(f'Reintento {intento}/{max_reintentos}...')

            file_uploaded = insert_file(service, nombre_archivo, nombre_archivo, drive_id, 'text/plain', path_completo_archivo)

            if file_uploaded:
                archivo_subido = True
                logger.info(f'Archivo {nombre_archivo} subido correctamente a Google Drive en el intento {intento}')
                print(f'Archivo {nombre_archivo} subido correctamente a Google Drive')

                # Si se especificó borrar_despues, borrar el archivo local
                if borrar_despues:
                    try:
                        os.remove(path_completo_archivo)
                        logger.info(f'Archivo local {nombre_archivo} borrado exitosamente después de la subida')
                        print(f'Archivo local {nombre_archivo} borrado exitosamente')
                    except Exception as e:
                        logger.error(f'Error al borrar el archivo local {nombre_archivo}: {str(e)}')
                        print(f'ADVERTENCIA: No se pudo borrar el archivo local: {str(e)}')
            else:
                logger.warning(f'Intento {intento} fallido: No se recibió confirmación de subida')
                if intento < max_reintentos:
                    logger.info(f'Esperando {tiempo_espera} segundos antes del siguiente intento...')
                    print(f'Esperando {tiempo_espera} segundos antes de reintentar...')
                    time.sleep(tiempo_espera)

        except Exception as e:
            logger.error(f'Error en intento {intento}/{max_reintentos} subiendo {nombre_archivo}. Codigo: {str(e)}')
            print(f'Error en intento {intento}: {str(e)}')

            if intento < max_reintentos:
                logger.info(f'Esperando {tiempo_espera} segundos antes del siguiente intento...')
                print(f'Esperando {tiempo_espera} segundos antes de reintentar...')
                time.sleep(tiempo_espera)

    # Verificar resultado final y actualizar registro de fallos
    if not archivo_subido:
        logger.error(f'No se pudo subir el archivo {nombre_archivo} después de {max_reintentos} intentos')
        print(f'ERROR: No se pudo subir el archivo después de {max_reintentos} intentos')

        # Marcar archivo como fallido en el JSON
        if tipo_archivo and log_directory:
            try:
                marcar_como_fallido(log_directory, nombre_archivo, tipo_archivo, max_reintentos, logger)
            except Exception as e:
                logger.error(f'Error al marcar archivo como fallido en JSON: {str(e)}')
    else:
        # Marcar archivo como exitoso (registrar en exitosos y remover de fallidos si existía)
        if tipo_archivo and log_directory:
            try:
                # Obtener tamaño del archivo
                size_bytes = os.path.getsize(path_completo_archivo) if os.path.exists(path_completo_archivo) else 0
                marcar_como_exitoso(log_directory, nombre_archivo, tipo_archivo, drive_id, size_bytes, logger)
            except Exception as e:
                logger.error(f'Error al marcar archivo como exitoso en JSON: {str(e)}')

    return archivo_subido


# Lee un archivo de configuración en formato JSON y devuelve su contenido como un diccionario.
def read_fileJSON(nameFile):

    try:
        with open(nameFile, 'r') as f:
            data = json.load(f)
        return data
    except FileNotFoundError:
        print(f"Archivo {nameFile} no encontrado.")
        return None
    except json.JSONDecodeError:
        print(f"Error al decodificar el archivo {nameFile}.")
        return None
  
# Metodo que permite realizar la autenticacion a Google Drive
def get_authenticated(SCOPES, credential_file, token_file, service_name = 'drive', api_version = 'v3'):
    # The file token.json stores the user's access and refresh tokens, and is
    # created automatically when the authorization flow completes for the first
    # time.
    store = file.Storage(token_file)
    creds = store.get()
    if not creds or creds.invalid:
        flow = client.flow_from_clientsecrets(credential_file, SCOPES)
        # Se utiliza parse_known_args para permitir que el script acepte banderas de autenticación
        # (como --noauth_local_webserver) sin interferir con otros argumentos locales.
        flags, _ = tools.argparser.parse_known_args()
        creds = tools.run_flow(flow, store, flags)
    
    # Configurar timeout para redes inestables (60s previene que un socket colgado congele el proceso)
    http = Http(timeout=60)
    service = build(service_name, api_version, http = creds.authorize(http))

    return service

# Metodo que permite subir un archivo a la cuenta de Drive
def insert_file(service, name, description, parent_id, mime_type, filename):
    # MODO TEST: Simular fallos si está activado
    global TEST_MODE, TEST_FAILURE_PROBABILITY
    if TEST_MODE:
        import random
        if random.random() < TEST_FAILURE_PROBABILITY:
            raise Exception("Simulación de error de red")

    media_body = MediaFileUpload(filename, mimetype = mime_type, chunksize=-1, resumable = True)
    body = {
        'name': name,
        'description': description,
        'mimeType': mime_type
    }

    # Si se recibe la ID de la carpeta superior, la coloca
    if parent_id:
        body['parents'] = [parent_id]

    # Realiza la carga del archivo en la carpeta respectiva de Drive
    try:
        #print("punto de control")
        file = service.files().create(
            body = body,
            media_body = media_body,
            fields='id').execute()

        return file

    except errors.HttpError as error:
        print('An error occurred: %s' % error)
        return None


# Metodo para intentar conectarse a Google Drive y activar la bandera de conexion
def Try_Autenticar_Drive(SCOPES, credentials_file, token_file, logger):
    global isConecctedDrive
    # Llama al metodo para realizar la autenticacion, la primera vez se
    # abrira el navegador, pero desde la segunda ya no
    try:
        service = get_authenticated(SCOPES, credentials_file, token_file)
        isConecctedDrive = True
        print("Inicio Drive Ok")
        logger.info("Inicio Drive Ok")
        return service
    except Exception as e:
        isConecctedDrive = False
        print("********** Error Inicio Drive ********")
        logger.error("Error Inicio Drive: %s", str(e))
        return 0


# Función para inicializar y obtener el logger de un cliente
def obtener_logger(id_estacion, log_directory, log_filename):
    global loggers
    if id_estacion not in loggers:
        # Crear un logger para el cliente
        logger = logging.getLogger(id_estacion)
        logger.setLevel(logging.DEBUG)
        # Verificar si el directorio de logs existe, si no, crearlo
        if not os.path.isdir(log_directory):
            try:
                os.makedirs(log_directory)
                logger.info(f"Directorio de logs creado: {log_directory}")
            except Exception as e:
                logger.error(f"Error al crear el directorio de logs {log_directory}: {e}")
        # Ruta completa del archivo de log
        log_path = os.path.join(log_directory, log_filename)
        # Crear manejador de archivo, apuntando al archivo existente
        file_handler = logging.FileHandler(log_path)
        file_handler.setLevel(logging.DEBUG)
        # Crear formato de logging y añadirlo al manejador
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(formatter)
        # Añadir el manejador al logger
        logger.addHandler(file_handler)
        loggers[id_estacion] = logger
    return loggers[id_estacion]

#######################################################################################################


############################################ ~Main~ ###################################################
def main():

    # Mapeo de modos de archivo
    MODOS = {
        'continuous': {
            'dir_key': 'registro_continuo',
            'drive_key': 'continuos_id',
            'descripcion': 'Archivos de registro continuo',
            'tipo_archivo': 'continuous'
        },
        'mseed': {
            'dir_key': 'archivos_mseed',
            'drive_key': 'mseed_id',
            'descripcion': 'Archivos miniSEED procesados',
            'tipo_archivo': 'mseed'
        },
        'event': {
            'dir_key': 'eventos_extraidos',
            'drive_key': 'events_id',
            'descripcion': 'Archivos de eventos extraídos',
            'tipo_archivo': 'event'
        },
        'tmp': {
            'dir_key': 'archivos_temporales',
            'drive_key': 'tmp_id',
            'descripcion': 'Archivos temporales',
            'tipo_archivo': 'tmp'
        },
        'log': {
            'dir_key': 'log_directory',
            'drive_key': 'logs_id',
            'descripcion': 'Archivos de log',
            'tipo_archivo': 'log'
        }
    }

    # Buscar el modo y el nombre del archivo en los argumentos (más flexible que posiciones fijas)
    modo_arg = None
    nombre_archivo = None
    for i, arg in enumerate(sys.argv):
        if arg.startswith('--') and arg[2:] in MODOS:
            modo_arg = arg
            if i + 1 < len(sys.argv) and not sys.argv[i+1].startswith('--'):
                nombre_archivo = sys.argv[i+1]
            break

    # Validar argumentos
    if not modo_arg or not nombre_archivo:
        print("Uso: subir_archivo.py --<modo> <nombre_archivo> [--delete] [--test] [--noauth_local_webserver]")
        print("\nModos disponibles:")
        for modo, info in MODOS.items():
            print(f"  --{modo:<12} {info['descripcion']}")
        print("\nParámetros opcionales:")
        print("  --delete      Borra el archivo local después de subirlo exitosamente")
        print("  --test        Activa modo de prueba (simula fallos de red con 90% de probabilidad)")
        print("  --noauth_local_webserver  Usa autenticación manual (copiar/pegar código) para sesiones remotas")
        print("\nEjemplos:")
        print("  python3 subir_archivo.py --continuous archivo.dat")
        print("  python3 subir_archivo.py --mseed archivo.mseed --delete")
        print("  python3 subir_archivo.py --mseed archivo.mseed --noauth_local_webserver")
        return

    # Verificar parámetros opcionales
    borrar_despues = "--delete" in sys.argv

    # Activar modo test si se especifica
    global TEST_MODE
    test_mode_activado = "--test" in sys.argv
    if test_mode_activado:
        TEST_MODE = True
        print("\n" + "="*70)
        print("MODO TEST ACTIVADO")
        print("="*70)
        print(f"Simulando fallos de red con {TEST_FAILURE_PROBABILITY*100:.0f}% de probabilidad")
        print("Este modo es útil para probar el sistema de reintentos")
        print("="*70 + "\n")

    # Validar que el argumento comience con --
    if not modo_arg.startswith('--'):
        print(f"Error: El modo debe comenzar con '--'. Recibido: {modo_arg}")
        return

    # Extraer el modo sin el prefijo --
    modo = modo_arg[2:]

    # Validar que el modo sea válido
    if modo not in MODOS:
        print(f"Error: Modo '{modo}' no válido.")
        print(f"Modos disponibles: {', '.join(['--' + m for m in MODOS.keys()])}")
        return

    # Obtiene la variable de entorno para definir la ruta del archivo de configuración
    project_local_root = os.getenv("PROJECT_LOCAL_ROOT")
    if not project_local_root:
        print("La variable de entorno PROJECT_LOCAL_ROOT no está definida.")
        return

    # Definir rutas de archivos y directorios
    config_dispositivo_path = os.path.join(project_local_root, "configuracion", "configuracion_dispositivo.json")
    credentials_file = os.path.join(project_local_root, "configuracion", "drive_credentials.json")
    token_file = os.path.join(project_local_root, "configuracion", "drive_token.json")
    log_directory = os.path.join(project_local_root, "log-files")

    # Lee el archivo de configuración del dispositivo
    config_dispositivo = read_fileJSON(config_dispositivo_path)
    if config_dispositivo is None:
        print("No se pudo leer el archivo de configuración del dispositivo. Terminando el programa.")
        return

    # Obtener ID de la estación
    id_estacion = config_dispositivo.get("dispositivo", {}).get("id", "Unknown")

    # Obtener configuración de reintentos y tiempo de espera
    max_reintentos = config_dispositivo.get("drive", {}).get("config", {}).get("max_reintentos", 3)
    tiempo_espera = config_dispositivo.get("drive", {}).get("config", {}).get("tiempo_espera", 2)

    # Obtener información del modo seleccionado
    modo_info = MODOS[modo]
    dir_key = modo_info['dir_key']
    drive_key = modo_info['drive_key']

    # Obtener ruta del directorio
    if dir_key == 'log_directory':
        # log_directory es hardcodeado
        path_file = log_directory
    else:
        path_file = config_dispositivo.get("directorios", {}).get(dir_key, "")
        if not path_file:
            print(f"Error: No se encontró la ruta '{dir_key}' en configuracion_dispositivo.json")
            return

    # Obtener ID de carpeta de Drive
    drive_id = config_dispositivo.get("drive", {}).get("carpetas", {}).get(drive_key, "")
    if not drive_id:
        print(f"Error: No se encontró el ID de Drive '{drive_key}' en configuracion_dispositivo.json")
        return

    # Construir ruta completa del archivo
    path_completo_archivo = os.path.join(path_file, nombre_archivo)
        
    # Obtiene el directorio de logs y lo crea si no existe
    if not os.path.exists(log_directory):
        os.makedirs(log_directory)

    # Inicializa el logger
    logger = obtener_logger(id_estacion, log_directory, "drive.log")

    # Registrar si el modo test está activado
    if test_mode_activado:
        logger.warning(f"MODO TEST ACTIVADO: Se simularán fallos de red con {TEST_FAILURE_PROBABILITY*100:.0f}% de probabilidad")

    # Verifica si el archivo existe
    if not os.path.isfile(path_completo_archivo):
        print("El archivo %s no existe. Terminando el programa." % path_completo_archivo)
        logger.error("El archivo %s no existe. Terminando el programa." % path_completo_archivo)
        return

    # Llama al metodo para intentar conectarse a Google Drive
    service = Try_Autenticar_Drive(SCOPES, credentials_file, token_file, logger)

    if isConecctedDrive == True:
        # Obtener tipo de archivo del modo seleccionado
        tipo_archivo = modo_info.get('tipo_archivo', None)

        # Llama a la función reutilizable para subir el archivo con reintentos
        subir_archivo_con_reintentos(
            service=service,
            nombre_archivo=nombre_archivo,
            path_completo_archivo=path_completo_archivo,
            drive_id=drive_id,
            max_reintentos=max_reintentos,
            tiempo_espera=tiempo_espera,
            logger=logger,
            borrar_despues=borrar_despues,
            tipo_archivo=tipo_archivo,
            log_directory=log_directory
        )
    else:
        logger.error("No se pudo conectar a Google Drive. Verifica las credenciales.")
        print("ERROR: No se pudo conectar a Google Drive")
    

#######################################################################################################


#######################################################################################################
if __name__ == '__main__':
    main()
#######################################################################################################