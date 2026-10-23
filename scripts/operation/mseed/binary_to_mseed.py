"""
Script de conversión de archivos binarios (.dat) a formato miniSEED (.mseed)

EJEMPLOS DE USO:

1. Modo simple (sintaxis corta):
   python3 binary_to_mseed.py 1                                          # Registro continuo
   python3 binary_to_mseed.py 2                                          # Evento extraído
   python3 binary_to_mseed.py 3 archivo.dat                              # Conversión manual (nombre)
   python3 binary_to_mseed.py 3 /ruta/completa/archivo.dat              # Conversión manual (ruta absoluta)

2. Modo con flags (sintaxis descriptiva):
   python3 binary_to_mseed.py --continuous                               # Registro continuo
   python3 binary_to_mseed.py --event                                    # Evento extraído
   python3 binary_to_mseed.py --file archivo.dat                         # Conversión manual (nombre)
   python3 binary_to_mseed.py --file /ruta/completa/archivo.dat         # Conversión manual (ruta absoluta)
   python3 binary_to_mseed.py --dir /ruta/completa/directorio          # Conversión por directorio

4. Modo con directorio:
   python3 binary_to_mseed.py --dir /ruta/completa/directorio    # Convierte todos los .dat del directorio

DESCRIPCIÓN DE MODOS:

- Modo 1 (--continuous):
    Lee el nombre del archivo desde NombreArchivoRegistroContinuo.tmp
    Busca en: directorios.registro_continuo (del JSON)
    Guarda en: directorios.archivos_mseed (del JSON)

- Modo 2 (--event):
    Lee el nombre del archivo desde NombreArchivoEventoExtraido.tmp
    Busca en: directorios.eventos_extraidos (del JSON)
    Guarda en: directorios.eventos_extraidos (del JSON)

- Modo 3 (--file):
    Especifica manualmente el archivo a convertir
    Si la ruta es absoluta, usa esa ruta directamente
    Si es solo el nombre, busca en: directorios.registro_continuo (del JSON)
    Guarda en: directorios.archivos_mseed (del JSON)

- Modo 4 (--dir):
    Convierte todos los archivos .dat encontrados en el directorio especificado
    Busca en: ruta proporcionada por el usuario
    Guarda en: directorios.archivos_mseed (del JSON)
    Procesa archivos secuencialmente y continúa aunque alguno falle

REQUISITOS:

- Variable de entorno PROJECT_LOCAL_ROOT debe estar definida
- Archivos de configuración necesarios:
    * configuracion_dispositivo.json
    * configuracion_mseed.json
"""

######################################### ~Librerias~ #################################################
import numpy as np
from obspy import UTCDateTime, read, Trace, Stream
import os
import subprocess
import time
import sys
import json
from time import time as timer
import logging
import datetime
import argparse
import glob
import re

# Insertar ruta para importar structured_logger desde el directorio superior (scripts/operation/)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from structured_logger import StructuredLogger

# ===== CONFIGURACIÓN DE LOGGING =====
VERBOSITY_LEVEL = "SUMMARY"  # Puede ser: DEBUG, INFO, SUMMARY
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
LOG_BACKUP_COUNT = 3
# ====================================
#######################################################################################################

##################################### ~Variables globales~ ############################################
loggers = {}
#######################################################################################################

######################################### ~Funciones~ #################################################
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
    

def leer_archivo_binario(archivo_binario, logger, usar_fecha_filename=False):
    start_time = timer()
    datos = [[], [], []]
    tiempos = []

    total_tramas_invalidas = 0
    chunk_size = 2506 * 60  # Leer en bloques de aproximadamente 2.5 MB
    with open(archivo_binario, "rb") as f:
        while True:
            chunk = np.fromfile(f, dtype=np.uint8, count=chunk_size)
            if chunk.size == 0:
                break

            num_tramas = len(chunk) // 2506
            if num_tramas == 0:
                continue

            chunk = chunk[:num_tramas * 2506].reshape((num_tramas, 2506))

            horas = chunk[:, 2503].astype(np.uint32)
            minutos = chunk[:, 2504].astype(np.uint32)
            segundos = chunk[:, 2505].astype(np.uint32)

            # Crear máscara de tramas con tiempos válidos
            mascara_valida = (horas <= 23) & (minutos <= 59) & (segundos <= 59)
            tramas_invalidas = (~mascara_valida).sum()

            for h, m, s, valido in zip(horas, minutos, segundos, mascara_valida):
                if not valido:
                    logger.data_warning(os.path.basename(archivo_binario), "trama_invalida", f"{h:02}:{m:02}:{s:02}")
                    total_tramas_invalidas += 1
                    continue
                tiempos.append(h * 3600 + m * 60 + s)

            # Filtrar datos crudos solo con tramas válidas
            chunk_valido = chunk[mascara_valida]
            datos_crudos = chunk_valido[:, :2500].reshape((-1, 250, 10))

            for j in range(3):
                dato_1 = datos_crudos[:, :, j * 3 + 1].flatten()
                dato_2 = datos_crudos[:, :, j * 3 + 2].flatten()
                dato_3 = datos_crudos[:, :, j * 3 + 3].flatten()

                xValue = ((dato_1.astype(np.uint32) << 12) & 0xFF000) + \
                         ((dato_2.astype(np.uint32) << 4) & 0xFF0) + \
                         ((dato_3.astype(np.uint32) >> 4) & 0xF)

                xValue = xValue.astype(np.int32)
                mask = xValue >= 0x80000
                xValue[mask] = -1 * ((~xValue[mask] + 1) & 0x7FFFF)

                datos[j].extend(xValue)

    if total_tramas_invalidas > 0:
        logger.data_warning(os.path.basename(archivo_binario), "tramas_descartadas", total_tramas_invalidas)

    # Filtrar tramas con timestamps duplicados (conservar primera ocurrencia)
    tiempos_np_raw = np.array(tiempos)
    datos_array = np.array(datos)
    fsample = 250  # constante de muestreo de la trama

    # Evitar IndexError si no hay tiempos válidos
    if tiempos_np_raw.size == 0:
        logger.convert_fail(os.path.basename(archivo_binario), "sin_tramas_validas")
        end_time = timer()
        return datos_array, None, end_time - start_time, None, None, None

    _, indices_unicos = np.unique(tiempos_np_raw, return_index=True)
    indices_unicos = np.sort(indices_unicos)  # mantener orden cronológico original

    if len(indices_unicos) < len(tiempos_np_raw):
        tramas_duplicadas = len(tiempos_np_raw) - len(indices_unicos)
        logger.data_warning(os.path.basename(archivo_binario), "tramas_duplicadas_filtradas", tramas_duplicadas)
        tiempos_np = tiempos_np_raw[indices_unicos]

        # Vectorización multidimensional directa O(1) de overhead:
        num_tramas_total = datos_array.shape[1] // fsample
        datos_reshaped = datos_array[:, :num_tramas_total * fsample].reshape((3, num_tramas_total, fsample))
        datos_np = datos_reshaped[:, indices_unicos, :].reshape((3, -1))
    else:
        tiempos_np = tiempos_np_raw
        datos_np = datos_array

    segundos_faltantes = []
    dif_segundos = np.diff(tiempos_np)

    # Corregir cruces de medianoche: si la diferencia es negativa,
    # sumar 86400 (un día completo en segundos)
    dif_segundos = np.where(dif_segundos < 0, dif_segundos + 86400, dif_segundos)

    # Validación de saltos anómalos
    saltos_grandes = dif_segundos[dif_segundos > 1]
    if len(saltos_grandes) > 0:
        top5 = [int(x) for x in sorted(saltos_grandes)[-5:]]
        total_faltantes = sum(int(x - 1) for x in saltos_grandes)
        logger.data_warning(os.path.basename(archivo_binario), "segundos_faltantes", f"count={total_faltantes}, saltos={len(saltos_grandes)}, top5={top5}")

    missing_indices = np.where(dif_segundos > 1)[0]
    for idx in missing_indices:
        t_ant = int(tiempos_np[idx])
        t_sig = int(tiempos_np[idx + 1])
        if t_sig < t_ant:
            # Cruce de medianoche (ej. 23:59:58 -> 00:00:02): t_sig pertenece al día siguiente
            t_sig_ajustado = t_sig + 86400
            segundos_faltantes.extend([(s % 86400) for s in range(t_ant + 1, t_sig_ajustado)])
        else:
            segundos_faltantes.extend(range(t_ant + 1, t_sig))

    tiempo_inicio = datetime.timedelta(seconds=int(tiempos_np[0]))
    tiempo_final = datetime.timedelta(seconds=int(tiempos_np[-1]))

    # Extraer timestamps completos de primera y última trama
    ts_inicio, ts_final = extraer_timestamp_completo_binario(archivo_binario, usar_fecha_filename)

    # Construir tiempo_binario desde la primera trama VÁLIDA (no la primera trama raw)
    primer_tiempo_valido = int(tiempos_np[0])
    h_val = primer_tiempo_valido // 3600
    m_val = (primer_tiempo_valido % 3600) // 60
    s_val = primer_tiempo_valido % 60

    tiempo_binario_validado = {
        "hora": h_val, "hora_s": str(h_val).zfill(2),
        "minuto": m_val, "minuto_s": str(m_val).zfill(2),
        "segundo": s_val, "segundo_s": str(s_val).zfill(2),
        "n_segundo": primer_tiempo_valido
    }
    # Fecha: se toma del ts_inicio ya calculado por extraer_timestamp_completo_binario
    if ts_inicio is not None:
        tiempo_binario_validado.update({
            "anio": ts_inicio.year, "anio_s": str(ts_inicio.year),
            "mes": ts_inicio.month, "mes_s": str(ts_inicio.month).zfill(2),
            "dia": ts_inicio.day, "dia_s": str(ts_inicio.day).zfill(2),
        })

    end_time = timer()
    return datos_np, segundos_faltantes if segundos_faltantes else None, end_time - start_time, ts_inicio, ts_final, tiempo_binario_validado


# Extrae y convierte valores de tiempo del archivo binario y los devuelve en un diccionario.
def extraer_tiempo_binario(archivo, usar_fecha_filename=False):
    # Abrir el archivo en modo de lectura binaria
    with open(archivo, "rb") as f:
        # Leer 2506 bytes del archivo y almacenarlos en un arreglo de numpy
        tramaDatos = np.fromfile(f, np.int8, 2506)
    
    if tramaDatos.size < 2506:
        print("Error: Tamaño de trama insuficiente. Archivo binario podría estar dañado o incompleto.")
        return None
    
    # Extraer HORA de las tramas (siempre de las tramas binarias)
    hora = int(tramaDatos[2503])
    minuto = int(tramaDatos[2504])
    segundo = int(tramaDatos[2505])
    n_segundo = hora * 3600 + minuto * 60 + segundo
    
    # Extraer FECHA según configuración
    if usar_fecha_filename:
        # Método nuevo: extraer fecha del nombre del archivo
        fecha_info = extraer_fecha_desde_nombre_archivo(archivo)
        if fecha_info is None:
            # Si falla, usar método tradicional como fallback
            print(f"Advertencia: No se pudo extraer fecha del nombre de '{os.path.basename(archivo)}', usando método tradicional")
            anio = int(tramaDatos[2500]) + 2000
            mes = int(tramaDatos[2501])
            dia = int(tramaDatos[2502])
        else:
            anio = fecha_info['anio']
            mes = fecha_info['mes']
            dia = fecha_info['dia']
    else:
        # Método tradicional: extraer fecha de las tramas binarias
        anio = int(tramaDatos[2500]) + 2000
        mes = int(tramaDatos[2501])
        dia = int(tramaDatos[2502])
       
    # Crear diccionario de resultados con valores numéricos y cadenas formateadas
    tiempo_binario = {
        "anio": anio,
        "anio_s": str(anio),
        "mes": mes,
        "mes_s": str(mes).zfill(2),
        "dia": dia,
        "dia_s": str(dia).zfill(2),
        "hora": hora,
        "hora_s": str(hora).zfill(2),
        "minuto": minuto,
        "minuto_s": str(minuto).zfill(2),
        "segundo": segundo,
        "segundo_s": str(segundo).zfill(2),
        "n_segundo": n_segundo
    }
    return(tiempo_binario)


def extraer_fecha_desde_nombre_archivo(archivo_path):
    """
    Extrae la fecha (año, mes, día) del nombre del archivo binario.
    """
    
    # Obtener solo el nombre del archivo sin la ruta
    nombre_archivo = os.path.basename(archivo_path)
    
    # Patrón: CODIGO_AAMMDD-HHMMSS.dat (ejemplo: DEV00_260105-174128.dat)
    patron = r'^[A-Z0-9]+_(\d{2})(\d{2})(\d{2})-\d{6}\.dat$'
    match = re.match(patron, nombre_archivo)
    
    if not match:
        return None
    
    # Extraer componentes de fecha
    anio_corto = int(match.group(1))  # 26
    mes = int(match.group(2))          # 01
    dia = int(match.group(3))          # 05
    
    # Convertir año de 2 dígitos a 4 dígitos (asume 2000+)
    anio = 2000 + anio_corto
    
    # Validar valores básicos
    if mes < 1 or mes > 12 or dia < 1 or dia > 31:
        return None
    
    return {
        "anio": anio,
        "anio_s": str(anio),
        "mes": mes,
        "mes_s": str(mes).zfill(2),
        "dia": dia,
        "dia_s": str(dia).zfill(2)
    }


def extraer_timestamp_completo_binario(archivo_binario, usar_fecha_filename=False):
    """
    Extrae las fechas completas de la primera y última trama del archivo binario.
    
    Args:
        archivo_binario: Ruta del archivo binario
        usar_fecha_filename: Si True, extrae la fecha del nombre del archivo
        
    Returns:
        tuple: (timestamp_inicio, timestamp_final) como objetos datetime, o (None, None) si hay error
    """
    try:
        with open(archivo_binario, "rb") as f:
            # Leer primera trama (2506 bytes)
            primera_trama = np.fromfile(f, dtype=np.uint8, count=2506)
            if primera_trama.size < 2506:
                return None, None
            
            # HORA de la primera trama (siempre de las tramas)
            hora_inicio = int(primera_trama[2503])
            minuto_inicio = int(primera_trama[2504])
            segundo_inicio = int(primera_trama[2505])
            
            # FECHA según configuración
            if usar_fecha_filename:
                fecha_info = extraer_fecha_desde_nombre_archivo(archivo_binario)
                if fecha_info:
                    anio_inicio = fecha_info['anio']
                    mes_inicio = fecha_info['mes']
                    dia_inicio = fecha_info['dia']
                else:
                    # Fallback a método tradicional
                    anio_inicio = int(primera_trama[2500]) + 2000
                    mes_inicio = int(primera_trama[2501])
                    dia_inicio = int(primera_trama[2502])
            else:
                anio_inicio = int(primera_trama[2500]) + 2000
                mes_inicio = int(primera_trama[2501])
                dia_inicio = int(primera_trama[2502])
            
            # Ir al final del archivo para leer última trama
            f.seek(-2506, 2)  # Seek desde el final del archivo
            ultima_trama = np.fromfile(f, dtype=np.uint8, count=2506)
            if ultima_trama.size < 2506:
                return None, None
            
            # HORA de la última trama (siempre de las tramas)
            hora_final = int(ultima_trama[2503])
            minuto_final = int(ultima_trama[2504])
            segundo_final = int(ultima_trama[2505])
            
            # FECHA según configuración (asume que el archivo no cruza medianoche si es por filename)
            if usar_fecha_filename:
                anio_final = anio_inicio
                mes_final = mes_inicio
                dia_final = dia_inicio
            else:
                anio_final = int(ultima_trama[2500]) + 2000
                mes_final = int(ultima_trama[2501])
                dia_final = int(ultima_trama[2502])
            
            # Crear objetos datetime
            timestamp_inicio = datetime.datetime(anio_inicio, mes_inicio, dia_inicio, 
                                                hora_inicio, minuto_inicio, segundo_inicio)
            timestamp_final = datetime.datetime(anio_final, mes_final, dia_final,
                                               hora_final, minuto_final, segundo_final)
            
            return timestamp_inicio, timestamp_final
    except Exception:
        return None, None


def extraer_timestamps_mseed(archivo_mseed):
    """
    Extrae los timestamps de inicio y fin de un archivo miniSEED.
    
    Returns:
        tuple: (timestamp_inicio, timestamp_final) como objetos datetime, o (None, None) si hay error
    """
    try:
        st = read(archivo_mseed)
        if len(st) == 0:
            return None, None
        # Obtener el timestamp más temprano y más tardío de todas las trazas
        start_times = [tr.stats.starttime.datetime for tr in st]
        end_times = [tr.stats.endtime.datetime for tr in st]
        
        timestamp_inicio = min(start_times)
        timestamp_final = max(end_times)
        
        return timestamp_inicio, timestamp_final
    except Exception:
        return None, None


# Genera el nombre del archivo Mini-SEED basado en el tipo de archivo, el código de estación y el tiempo extraído.
def nombrar_archivo_mseed(codigo_estacion,tiempo_binario):
    # Formatear fecha y hora como cadenas
    fecha_string = tiempo_binario["anio_s"] + tiempo_binario["mes_s"] + tiempo_binario["dia_s"]
    hora_string = tiempo_binario["hora_s"] + tiempo_binario["minuto_s"] + tiempo_binario["segundo_s"]
    fileName = f'{codigo_estacion}_{fecha_string}_{hora_string}.mseed'
    return fileName
    
    
# Convierte los datos procesados del archivo binario a formato Mini-SEED y los guarda con el nombre especificado.
def conversion_mseed_digital(fileName, path, tiempo_binario, datos_archivo_binario, segundos_faltantes, parametros_mseed, logger):
    nombre = parametros_mseed["SENSOR(2)"]

    # Crear trazas para cada canal
    trazaCH1 = obtenerTraza(nombre, 1, datos_archivo_binario[0], tiempo_binario, segundos_faltantes, parametros_mseed)
    trazaCH2 = obtenerTraza(nombre, 2, datos_archivo_binario[1], tiempo_binario, segundos_faltantes, parametros_mseed)        
    trazaCH3 = obtenerTraza(nombre, 3, datos_archivo_binario[2], tiempo_binario, segundos_faltantes, parametros_mseed)

    # Crear un objeto Stream con las trazas
    stData = Stream(traces=[trazaCH1, trazaCH2, trazaCH3])

    fileNameCompleto = os.path.join(path, fileName)
    
    stData.write(fileNameCompleto, format='MSEED', encoding='STEIM1', reclen=512)


# Crea una traza de datos con los parámetros especificados y ajusta los datos para incluir ceros en los segundos faltantes si es necesario.
def obtenerTraza(nombreCanal, num_canal, data, tiempo_binario, segundos_faltantes, parametros_mseed):
    anio = tiempo_binario["anio"]
    mes = tiempo_binario["mes"]
    dia = tiempo_binario["dia"]
    horas = tiempo_binario["hora"]
    minutos = tiempo_binario["minuto"]
    segundos = tiempo_binario["segundo"]
    microsegundos = 0  # Si siempre es 0, podemos establecerlo aquí directamente

    fsample = int(parametros_mseed["MUESTREO(20)"])
    calidad = parametros_mseed["CALIDAD(16)"]

    # Determinar el prefijo del nombre del canal basado en la frecuencia de muestreo
    if fsample > 80:
        nombreCanal = 'E'
    else:
        nombreCanal = 'S'

    # Añadir el sufijo basado en el tipo de sensor
    if parametros_mseed["SENSOR(2)"] == 'SISMICO':
        nombreCanal += 'L'
    else:
        nombreCanal += 'N'

    # Determinar el índice del canal
    num_canal = num_canal - 3 * (int((num_canal - 1) / 3))
    nombreCanal += parametros_mseed["CANAL(18)"][num_canal - 1:num_canal]

    # Crear diccionario de estadísticas
    stats = {
        'network': parametros_mseed["RED(19)"],
        'station': parametros_mseed["CODIGO(1)"],
        'location': str(parametros_mseed["UBICACION(17)"]),  # Convertir a cadena
        'channel': nombreCanal,
        'npts': len(data),
        'sampling_rate': fsample,
        'mseed': {'dataquality': calidad},
        'starttime': UTCDateTime(anio, mes, dia, horas, minutos, segundos, microsegundos)
    }

    # Si hay segundos faltantes, rellenar ceros con mapeo vectorizado directo O(N) por slicing
    if segundos_faltantes is not None:
        segundo_inicio = (horas * 3600) + (minutos * 60) + segundos
        muestras_por_segundo = fsample

        # 1. Calcular el rango temporal total del archivo
        num_segundos_datos = len(data) // muestras_por_segundo
        num_segundos_gaps = len(segundos_faltantes) if segundos_faltantes else 0
        duracion_total_seg = num_segundos_datos + num_segundos_gaps
        ultimo_segundo = segundo_inicio + duracion_total_seg - 1

        # 2. Pre-alocar arreglo de ceros con dimensión EXACTA
        npts_completo = duracion_total_seg * muestras_por_segundo
        data_completo = np.zeros(npts_completo, dtype=np.int32)

        # 3. Construir conjunto de gaps para lookup O(1)
        gaps_set = set(segundos_faltantes)

        # 4. Copiar bloques de datos válidos en sus posiciones cronológicas exactas
        idx_datos = 0  # cursor sobre data (datos compactados sin gaps)
        for seg in range(segundo_inicio, ultimo_segundo + 1):
            if seg in gaps_set:
                # Este segundo es un gap: la posición ya tiene ceros pre-alocados
                continue
            # Posición destino en el arreglo completo (offset temporal desde el inicio)
            idx_destino = (seg - segundo_inicio) * muestras_por_segundo
            bloque = data[idx_datos : idx_datos + muestras_por_segundo]
            if len(bloque) < muestras_por_segundo:
                # Datos incompletos para este segundo (trama parcial al final)
                data_completo[idx_destino : idx_destino + len(bloque)] = bloque
                idx_datos += len(bloque)
                break
            data_completo[idx_destino : idx_destino + muestras_por_segundo] = bloque
            idx_datos += muestras_por_segundo

        stats['npts'] = npts_completo
        traza = Trace(data=data_completo, header=stats)
    else:
        traza = Trace(data=data, header=stats)
   
    return traza


# Función para inicializar y obtener el logger estructurado
def obtener_logger_estructurado(id_estacion, log_directory, log_filename, verbosity="SUMMARY"):
    return StructuredLogger(
        id_estacion=id_estacion,
        log_directory=log_directory,
        log_filename=log_filename,
        verbosity=verbosity,
        max_bytes=LOG_MAX_BYTES,
        backup_count=LOG_BACKUP_COUNT
    )

# Procesa un archivo binario individual y lo convierte a miniSEED.
def procesar_archivo_individual(binary_file, path_archivo_salida, codigo_estacion, config_mseed, logger, usar_fecha_filename=False):
    try:
        binary_filename = os.path.basename(binary_file)
        
        # Extraer tiempo del archivo binario (con método configurable)
        tiempo_binario = extraer_tiempo_binario(binary_file, usar_fecha_filename)
        if tiempo_binario is None:
            mensaje = "Tamaño de trama insuficiente o archivo dañado"
            logger.convert_fail(binary_filename, mensaje)
            return False, mensaje, None

        # Generar nombre y leer datos
        nombre_archivo_mseed = nombrar_archivo_mseed(codigo_estacion, tiempo_binario)
        datos_archivo_binario, segundos_faltantes, tiempo_lectura, ts_bin_inicio, ts_bin_final, tiempo_binario_validado = leer_archivo_binario(binary_file, logger, usar_fecha_filename)
        
        logger.read_ok(binary_filename, tiempo_lectura)

        # Usar el timestamp validado como referencia canónica para la conversión.
        # Caer a tiempo_binario original (de extraer_tiempo_binario) solo si leer_archivo_binario no pudo validar.
        tiempo_para_conversion = tiempo_binario_validado if tiempo_binario_validado else tiempo_binario

        # Realizar conversión
        ruta_mseed_completa = os.path.join(path_archivo_salida, nombre_archivo_mseed)
        conversion_mseed_digital(nombre_archivo_mseed, path_archivo_salida, tiempo_para_conversion, datos_archivo_binario, segundos_faltantes, config_mseed, logger)
        
        logger.convert_ok(binary_filename, nombre_archivo_mseed, tiempo_lectura)
        
        # Extraer timestamps del archivo miniSEED generado
        ts_mseed_inicio, ts_mseed_final = extraer_timestamps_mseed(ruta_mseed_completa)
        
        # Crear diccionario con información detallada
        info = {
            'binary_filename': binary_filename,
            'mseed_filename': nombre_archivo_mseed,
            'mseed_path': ruta_mseed_completa,
            'tiempos_np': [int(tiempo_para_conversion['n_segundo']), None], # Timestamp canónico validado
            'ts_bin_inicio': ts_bin_inicio,
            'ts_bin_final': ts_bin_final,
            'ts_mseed_inicio': ts_mseed_inicio,
            'ts_mseed_final': ts_mseed_final,
            'tiempo_lectura': tiempo_lectura,
            'segundos_faltantes': segundos_faltantes
        }
        
        # Actualizar tiempos_np final si lo tenemos del ts_bin_final
        if ts_bin_final:
            info['tiempos_np'][1] = ts_bin_final.hour * 3600 + ts_bin_final.minute * 60 + ts_bin_final.second

        return True, "Éxito", info
    except Exception as e:
        mensaje = f"Error inesperado al procesar {os.path.basename(binary_file)}: {str(e)}"
        logger.convert_fail(os.path.basename(binary_file), str(e))
        return False, mensaje, None

#######################################################################################################

############################################ ~Main~ ###################################################
def main():

    start_time_total = timer()

    # Parser de argumentos
    parser = argparse.ArgumentParser(description="Conversor de binario a Mini-SEED")
    parser.add_argument("modo_simple", nargs="?", choices=["1", "2", "3"],
                        help="Modo simple (1: Registro continuo, 2: Evento extraído, 3: Conversión manual)")
    parser.add_argument("archivo_nombre", nargs="?",
                        help="Nombre del archivo binario (requerido para modo 3)")
    parser.add_argument("--continuous", action="store_true",
                        help="Modo registro continuo (equivalente a modo 1)")
    parser.add_argument("--event", action="store_true",
                        help="Modo evento extraído (equivalente a modo 2)")
    parser.add_argument("--file", metavar="ARCHIVO",
                        help="Modo conversión manual, especifica el archivo binario (equivalente a modo 3)")
    parser.add_argument("--dir", metavar="DIRECTORIO",
                        help="Modo conversión por directorio, convierte todos los archivos .dat del directorio especificado")
    parser.add_argument("--output-dir", "-o", metavar="DIRECTORIO",
                        help="Directorio de salida para los archivos miniSEED generados (opcional, por defecto usa archivos_mseed)")
    args = parser.parse_args()

    # Obtiene la variable de entorno para definir la ruta del archivo de configuración
    project_local_root = os.getenv("PROJECT_LOCAL_ROOT")
    if not project_local_root:
        print("La variable de entorno PROJECT_LOCAL_ROOT no está definida.")
        return

    # Definir rutas de archivos y directorios
    config_mseed_file = os.path.join(project_local_root, "configuracion", "configuracion_mseed.json")
    config_dispositivo_file = os.path.join(project_local_root, "configuracion", "configuracion_dispositivo.json")
    archivoNombresArchivosRC = os.path.join(project_local_root, "tmp-files", "NombreArchivoRegistroContinuo.tmp")
    archivoNombresArchivosEE = os.path.join(project_local_root, "tmp-files", "NombreArchivoEventoExtraido.tmp")
    log_directory = os.path.join(project_local_root, "log-files")

    # Lee el archivo de configuración de mseed
    config_mseed = read_fileJSON(config_mseed_file)
    if config_mseed is None:
        print("No se pudo leer el archivo de configuración mseed. Terminando el programa.")
        return

    # Lee el archivo de configuración del dispositivo
    config_dispositivo = read_fileJSON(config_dispositivo_file)
    if config_dispositivo is None:
        print("No se pudo leer el archivo de configuración del dispositivo. Terminando el programa.")
        return

    # Obtener rutas desde configuracion_dispositivo.json
    path_registro_continuo = config_dispositivo.get("directorios", {}).get("registro_continuo", "")
    path_eventos_extraidos = config_dispositivo.get("directorios", {}).get("eventos_extraidos", "")
    path_archivos_mseed = config_dispositivo.get("directorios", {}).get("archivos_mseed", "")

    # Obtiene el codigo de la estacion
    codigo_estacion = config_mseed.get("CODIGO(1)", "Unknown")
    if codigo_estacion == "Unknown":
        print("No se encontró 'CODIGO(1)' en configuracion_mseed.json")
        return

    # Método de extracción de fecha
    usar_fecha_filename = config_mseed.get("USAR_FECHA_FILENAME", False)

    # Obtiene el ID del dispositivo
    dispositivo_id = config_dispositivo.get("dispositivo", {}).get("id", "Unknown")
    if dispositivo_id == "Unknown":
        print("No se encontró 'id' del dispositivo en configuracion_dispositivo.json")
        return

    # Verificar que el directorio de logs existe, si no, crearlo
    if not os.path.isdir(log_directory):
        try:
            os.makedirs(log_directory)
            print(f"Directorio de logs creado: {log_directory}")
        except Exception as e:
            print(f"Error al crear el directorio de logs {log_directory}: {e}")
            return

    # Inicializa el logger estructurado
    logger = obtener_logger_estructurado(dispositivo_id, log_directory, "mseed.log", verbosity=VERBOSITY_LEVEL)
    
    logger.init(f"Método extracción fecha: {'Nombre archivo' if usar_fecha_filename else 'Tramas binarias'}")

    # Determinar tipo de archivo y ruta
    if args.modo_simple in ("1", "2", "3"):
        tipoArchivo = args.modo_simple
    elif args.continuous:
        tipoArchivo = "1"
    elif args.event:
        tipoArchivo = "2"
    elif args.file:
        tipoArchivo = "3"
    elif args.dir:
        tipoArchivo = "4"
    else:
        logger.config_error("main", "No se especificó un modo válido.")
        print("Error: No se especificó un modo válido.")
        print("Uso:")
        print("  python3 binary_to_mseed.py 1               # Registro continuo")
        print("  python3 binary_to_mseed.py 2               # Evento extraído")
        print("  python3 binary_to_mseed.py 3 archivo.dat   # Conversión manual")
        print("  python3 binary_to_mseed.py --continuous    # Registro continuo")
        print("  python3 binary_to_mseed.py --event         # Evento extraído")
        print("  python3 binary_to_mseed.py --file archivo.dat  # Conversión manual")
        print("  python3 binary_to_mseed.py --dir /ruta/directorio  # Conversión por directorio")
        return

    if tipoArchivo == '1':
        # Archivos registro continuo
        if not path_registro_continuo:
            logger.config_error("config_dispositivo", "No se encontró la ruta 'registro_continuo'")
            print("Error: No se encontró la ruta 'registro_continuo' en configuracion_dispositivo.json")
            return
        if not path_archivos_mseed:
            logger.config_error("config_dispositivo", "No se encontró la ruta 'archivos_mseed'")
            print("Error: No se encontró la ruta 'archivos_mseed' en configuracion_dispositivo.json")
            return

        # Verificar que los directorios existen
        if not os.path.isdir(path_registro_continuo):
            logger.config_error("dirs", f"El directorio de registro continuo no existe: {path_registro_continuo}")
            print(f"Error: El directorio de registro continuo no existe: {path_registro_continuo}")
            return
        if not os.path.isdir(path_archivos_mseed):
            logger.config_error("dirs", f"El directorio de archivos mseed no existe: {path_archivos_mseed}")
            print(f"Error: El directorio de archivos mseed no existe: {path_archivos_mseed}")
            return

        try:
            with open(archivoNombresArchivosRC) as ficheroNombresArchivos:
                lineasFicheroNombresArchivos = ficheroNombresArchivos.readlines()
                if len(lineasFicheroNombresArchivos) < 2:
                    logger.convert_fail("RC_list", "insufficient_lines")
                    print("Error: El archivo de nombres de registro continuo no tiene suficientes líneas.")
                    return
                binary_filename = lineasFicheroNombresArchivos[1].rstrip('\n')
        except FileNotFoundError:
            logger.convert_fail("RC_list", "file_not_found")
            print(f"Error: No se encontró el archivo: {archivoNombresArchivosRC}")
            return

        binary_file = os.path.join(path_registro_continuo, binary_filename)
        path_archivo_salida = path_archivos_mseed
        logger.convert_start("RC", binary_filename)
        
    elif tipoArchivo == '2':
        # Archivos eventos extraidos
        if not path_eventos_extraidos:
            logger.config_error("config_dispositivo", "No se encontró la ruta 'eventos_extraidos'")
            print("Error: No se encontró la ruta 'eventos_extraidos' en configuracion_dispositivo.json")
            return

        # Verificar que el directorio existe
        if not os.path.isdir(path_eventos_extraidos):
            logger.config_error("dirs", f"El directorio de eventos extraídos no existe: {path_eventos_extraidos}")
            print(f"Error: El directorio de eventos extraídos no existe: {path_eventos_extraidos}")
            return

        try:
            with open(archivoNombresArchivosEE) as ficheroNombresArchivos:
                lineasFicheroNombresArchivos = ficheroNombresArchivos.readlines()
                if len(lineasFicheroNombresArchivos) < 1:
                    logger.convert_fail("EE_list", "insufficient_lines")
                    print("Error: El archivo de nombres de eventos extraidos no tiene suficientes líneas.")
                    return
                binary_filename = lineasFicheroNombresArchivos[0].rstrip('\n')
        except FileNotFoundError:
            logger.convert_fail("EE_list", "file_not_found")
            print(f"Error: No se encontró el archivo: {archivoNombresArchivosEE}")
            return

        binary_file = os.path.join(path_eventos_extraidos, binary_filename)
        path_archivo_salida = path_eventos_extraidos
        logger.convert_start("EE", binary_filename)
        
    elif tipoArchivo == '3':
        # Conversión manual de archivo específico
        # Determinar la ruta del archivo: puede venir como argumento posicional o con --file
        archivo_input = args.archivo_nombre or args.file

        if not archivo_input:
            logger.config_error("main", "missing_file_arg")
            print("Error: Se debe especificar el archivo binario.")
            print("Uso: python3 binary_to_mseed.py 3 <archivo.dat>")
            print("  o: python3 binary_to_mseed.py --file <archivo.dat>")
            return

        # Si la ruta es absoluta, usarla directamente; si no, buscar en path_registro_continuo
        if os.path.isabs(archivo_input):
            binary_file = archivo_input
            binary_filename = os.path.basename(archivo_input)
        else:
            if not path_registro_continuo:
                logger.config_error("config_dispositivo", "No se encontró la ruta 'registro_continuo'")
                print("Error: No se encontró la ruta 'registro_continuo' en configuracion_dispositivo.json")
                return
            binary_filename = archivo_input
            binary_file = os.path.join(path_registro_continuo, binary_filename)

        if args.output_dir:
            path_archivo_salida = args.output_dir
            if not os.path.isdir(path_archivo_salida):
                try:
                    os.makedirs(path_archivo_salida, exist_ok=True)
                except Exception as e:
                    logger.config_error("dirs", f"no_se_pudo_crear_output_dir: {e}")
                    print(f"Error al crear el directorio de salida: {e}")
                    return
        else:
            if not path_archivos_mseed:
                logger.config_error("config_dispositivo", "No se encontró la ruta 'archivos_mseed'")
                print("Error: No se encontró la ruta 'archivos_mseed' en configuracion_dispositivo.json")
                return

            # Verificar que el directorio de salida existe
            if not os.path.isdir(path_archivos_mseed):
                logger.config_error("dirs", f"mseed_dir_not_exists: {path_archivos_mseed}")
                print(f"Error: El directorio de archivos mseed no existe: {path_archivos_mseed}")
                return

            path_archivo_salida = path_archivos_mseed

        logger.convert_start("Manual", binary_filename)
        
    elif tipoArchivo == '4':
        # Conversión por directorio
        directorio_entrada = args.dir
        if not directorio_entrada:
            logger.config_error("main", "missing_dir_arg")
            print("Error: Se debe especificar el directorio.")
            return

        if not os.path.isdir(directorio_entrada):
            logger.config_error("dirs", f"input_dir_not_exists: {directorio_entrada}")
            print(f"Error: El directorio no existe: {directorio_entrada}")
            return

        if args.output_dir:
            path_archivo_salida = args.output_dir
            if not os.path.isdir(path_archivo_salida):
                try:
                    os.makedirs(path_archivo_salida, exist_ok=True)
                except Exception as e:
                    logger.config_error("dirs", f"no_se_pudo_crear_output_dir: {e}")
                    print(f"Error al crear el directorio de salida: {e}")
                    return
        else:
            if not path_archivos_mseed:
                logger.config_error("config_dispositivo", "No se encontró la ruta 'archivos_mseed'")
                print("Error: No se encontró la ruta 'archivos_mseed' en configuracion_dispositivo.json")
                return

            if not os.path.isdir(path_archivos_mseed):
                logger.config_error("dirs", f"mseed_dir_not_exists: {path_archivos_mseed}")
                print(f"Error: El directorio de archivos mseed no existe: {path_archivos_mseed}")
                return

            path_archivo_salida = path_archivos_mseed

        archivos_dat = sorted(glob.glob(os.path.join(directorio_entrada, "*.dat")))

        if not archivos_dat:
            logger.convert_fail("dir_scan", f"no_dat_files_found: {directorio_entrada}")
            print(f"Advertencia: No se encontraron archivos .dat en: {directorio_entrada}")
            return

        logger.convert_start("Directorio", directorio_entrada)
        logger.info(f"Archivos encontrados: {len(archivos_dat)}")
        print(f"\nEncontrados {len(archivos_dat)} archivos .dat en {directorio_entrada}")
        print(f"Directorio de salida: {path_archivo_salida}\n")

        exitosos = 0
        fallidos = 0
        archivos_fallidos = []

        for idx, binary_file in enumerate(archivos_dat, 1):
            binary_filename = os.path.basename(binary_file)
            
            # Formato compacto del índice con padding
            idx_str = f"[{idx:02d}/{len(archivos_dat):02d}]"
            separator = "─" * 40
            print(f"{idx_str} ── {binary_filename} {separator}")
            
            exito, mensaje, info = procesar_archivo_individual(
                binary_file, 
                path_archivos_mseed, 
                codigo_estacion, 
                config_mseed, 
                logger,
                usar_fecha_filename
            )
            
            if exito and info:
                # Formatear tiempos
                ts_bin_str = f"{info['ts_bin_inicio'].strftime('%Y-%m-%d %H:%M:%S')} ──> {info['ts_bin_final'].strftime('%Y-%m-%d %H:%M:%S')}" if info['ts_bin_inicio'] and info['ts_bin_final'] else "N/A"
                ts_mseed_str = f"{info['ts_mseed_inicio'].strftime('%Y-%m-%d %H:%M:%S')} ──> {info['ts_mseed_final'].strftime('%Y-%m-%d %H:%M:%S')}" if info['ts_mseed_inicio'] and info['ts_mseed_final'] else "N/A"
                
                # Acortar path si es muy largo
                output_path = info['mseed_path']
                if len(output_path) > 60:
                    output_path = "..." + output_path[-57:]
                
                print(f"tiempos_np : {info['tiempos_np']}")
                print(f"binary time: {ts_bin_str}")
                print(f"mseed time : {ts_mseed_str}")
                print(f"output     : {output_path}")
                print(f"status     : OK ({info['tiempo_lectura']:.2f}s)")
                print()
                exitosos += 1
            else:
                print(f"tiempos_np : N/A")
                print(f"binary time: ERROR")
                print(f"mseed time : ERROR")
                print(f"output     : N/A")
                print(f"status     : FAILED - {mensaje}")
                print()
                fallidos += 1
                archivos_fallidos.append((binary_filename, mensaje))

        print(f"\n{'='*60}")
        print(f"RESUMEN DE CONVERSIÓN")
        print(f"{'='*60}")
        print(f"Total de archivos: {len(archivos_dat)}")
        print(f"Exitosos: {exitosos}")
        print(f"Fallidos: {fallidos}")

        logger.summary(tipo="directorio", exitosos=exitosos, fallidos=fallidos)

        if archivos_fallidos:
            print(f"\nArchivos que fallaron:")
            for nombre, error in archivos_fallidos:
                print(f"  - {nombre}: {error}")
                logger.convert_fail(nombre, error)
        print(f"{'='*60}\n")
        
        return

    # Verificar que el archivo binario existe
    if not os.path.isfile(binary_file):
        logger.convert_fail(binary_file, "file_not_found")
        print(f"Error: El archivo binario no existe: {binary_file}")
        return

    # Procesar archivo individual para modos 1, 2 y 3
    exito, mensaje, info = procesar_archivo_individual(
        binary_file, 
        path_archivo_salida, 
        codigo_estacion, 
        config_mseed, 
        logger,
        usar_fecha_filename
    )
    
    if exito and info:
        # Formato compacto para un solo archivo
        idx_str = "[01/01]"
        separator = "─" * 40
        print(f"{idx_str} ── {os.path.basename(binary_file)} {separator}")
        
        ts_bin_str = f"{info['ts_bin_inicio'].strftime('%Y-%m-%d %H:%M:%S')} ──> {info['ts_bin_final'].strftime('%Y-%m-%d %H:%M:%S')}" if info['ts_bin_inicio'] and info['ts_bin_final'] else "N/A"
        ts_mseed_str = f"{info['ts_mseed_inicio'].strftime('%Y-%m-%d %H:%M:%S')} ──> {info['ts_mseed_final'].strftime('%Y-%m-%d %H:%M:%S')}" if info['ts_mseed_inicio'] and info['ts_mseed_final'] else "N/A"
        
        output_path = info['mseed_path']
        if len(output_path) > 60:
            output_path = "..." + output_path[-57:]
        
        print(f"tiempos_np : {info['tiempos_np']}")
        print(f"binary time: {ts_bin_str}")
        print(f"mseed time : {ts_mseed_str}")
        print(f"output     : {output_path}")
        print(f"status     : OK ({info['tiempo_lectura']:.2f}s)")
        print()
    else:
        print(f"Error: {mensaje}")

    logger.summary(ejecucion="completada", modo=tipoArchivo)

#######################################################################################################
if __name__ == '__main__':
    main()
#######################################################################################################

