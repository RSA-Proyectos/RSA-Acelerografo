# Plan de Implementación: Script Resiliente para Conversión y Subida de Archivos Pendientes (`procesar_y_subir_pendientes.py`)

**Fecha**: 2026-10-13  
**Proyecto**: `acelerografo-DEV00`  
**Objetivo**: Implementar una herramienta de recuperación y sincronización desacoplada (`procesar_y_subir_pendientes.py`) con su wrapper `/usr/local/bin/procesarpendientes` que permita convertir archivos binarios `.dat` acumulados a formato miniSEED (.mseed) de manera 100% local e idempotente, y subirlos a Google Drive de forma controlada mediante exclusión mutua (`fcntl.flock`), validación real de resolución DNS (`socket.getaddrinfo`), mecanismo de aborto temprano (*fail-fast*), dosificación por lotes y espaciado anti-bufferbloat.

---

## Prerequisitos

1. **Entorno de ejecución**: Raspberry Pi con entorno virtual activo en `/home/rsa/projects/acelerografo/.venv/` (Python 3.12/Debian 12 Bookworm).
2. **Archivos de configuración**:
   - `$PROJECT_LOCAL_ROOT/configuracion/configuracion_dispositivo.json`
   - `$PROJECT_LOCAL_ROOT/configuracion/configuracion_mseed.json`
   - `$PROJECT_LOCAL_ROOT/configuracion/drive_credentials.json` y `drive_token.json`
3. **Módulos existentes reutilizados**:
   - `scripts/operation/mseed/binary_to_mseed.py` (`procesar_archivo_individual`, `nombrar_archivo_mseed`, `extraer_tiempo_binario`)
   - `scripts/operation/drive/subir_archivo.py` (`Try_Autenticar_Drive`, `subir_archivo_con_reintentos`)
   - `scripts/operation/drive/drive_status_manager.py` (`ya_fue_subido`, `marcar_como_exitoso`)
   - `scripts/operation/structured_logger.py` (`StructuredLogger`)

---

## Fase 1: Implementación del Módulo Principal `procesar_y_subir_pendientes.py`

**Objetivo**: Desarrollar la lógica completa de conversión idempotente y subida resiliente a Google Drive.

### Componentes y Estructura Lógica

1. **Cerrojo atómico (`fcntl.flock`)**:
   - Bloqueo exclusivo no bloqueante sobre `/tmp/procesar_y_subir_pendientes.lock`.
   - Si otra instancia está activa, sale inmediatamente con código 0 y aviso limpio.

2. **Detección y protección del archivo activo**:
   - Lectura de `$PROJECT_LOCAL_ROOT/tmp-files/NombreArchivoRegistroContinuo.tmp` (línea 1).
   - Identificación del archivo con `mtime` más reciente en `/home/rsa/data/registro-continuo/`.
   - Exclusión estricta de dicho archivo para evitar lecturas de tramas incompletas del SPI.

3. **Conversión local garantizada (Fase 1)**:
   - Escaneo de todos los `.dat` en el directorio de registro continuo.
   - Cálculo del nombre `.mseed` esperado para cada binario.
   - Si el archivo `.mseed` ya existe en disco, se salta instantáneamente (idempotencia estricta, 0 consumo de CPU/I/O).
   - Si falta convertir, se procesa secuencialmente con `procesar_archivo_individual` y se fuerza `gc.collect()`.

4. **Validación DNS y Subida a Drive (Fase 2)**:
   - Prueba real: `socket.getaddrinfo("www.googleapis.com", 443, timeout=5)`.
   - Si falla: aviso informativo y aborto limpio (*fail-fast*), dejando los archivos resguardados localmente (>90 días de autonomía).
   - Si responde: autentica en Drive, prioriza eventos (`eventos_extraidos`) y continuos (`archivos_mseed`), verifica `ya_fue_subido()`, sube con pausas de 1.5s (*anti-bufferbloat*) y dosifica en lotes de tamaño configurable (`--batch-size 20`).

---

## Fase 2: Implementación del Wrapper Shell `procesarpendientes.sh`

**Objetivo**: Facilitar la invocación desatendida o manual desde `/usr/local/bin/procesarpendientes`.

### Acciones
1. Crear `montajes/acelerografo-DEV00/scripts/task/procesarpendientes.sh`.
2. Cargar variables de entorno (`project_paths`).
3. Invocar `$PROJECT_LOCAL_ROOT/.venv/bin/python3 $PROJECT_LOCAL_ROOT/scripts/drive/procesar_y_subir_pendientes.py "$@"`.
4. Integración automática con `deploy.sh` (instalación con permisos de ejecución en `/usr/local/bin/procesarpendientes`).

---

## Fase 3: Pruebas Unitarias Automatizadas

**Objetivo**: Garantizar el comportamiento esperado mediante una suite completa de pruebas unitarias.

### Acciones
1. Crear `montajes/acelerografo-DEV00/scripts/operation/drive/test_procesar_y_subir_pendientes.py`.
2. Probar:
   - Exclusión del archivo activo.
   - Idempotencia en conversión (salto de `.mseed` preexistentes).
   - Fail-fast ante fallo de resolución DNS.
   - Detección de exclusión mutua (`flock`).
   - Modo `--dry-run`.
