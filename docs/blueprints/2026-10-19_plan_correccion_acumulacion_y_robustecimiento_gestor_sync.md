# Plan de Implementación: Corrección de Acumulación de Archivos y Robustecimiento del Gestor de Sincronización

**Fecha**: 2026-10-19  
**Proyecto**: `acelerografo-DEV00` (`RSA-Acelerografo`)  
**Documento de Diagnóstico Base**: [`docs/analysis/2026-10-19_diagnostico_acumulacion_archivos_y_fallo_control_espacio_drive.md`](file:///home/rsa/git/montajes/acelerografo-DEV00/docs/analysis/2026-10-19_diagnostico_acumulacion_archivos_y_fallo_control_espacio_drive.md)  
**Objetivo**: Erradicar el riesgo inminente de parada del sistema y corrupción del almacenamiento por saturación de disco (actualmente en 96% de uso / 3.92% libre en `DEV00`). El plan repara el bug crítico `NameError` en el control de espacio, implementa desalojo dinámico iterativo para garantizar la recuperación del umbral de seguridad (10%), robustece el cerrojo atómico `adquirir_cerrojo` diferenciando concurrencia legítima de fallos de permisos (`PermissionError`), ajusta la política de retención de binarios continuos `.dat` de 30 a 7 días y certifica la solución mediante pruebas unitarias automatizadas y validación en `DEV00`.

---

## Prerrequisitos y Restricciones Operativas

1. **Restricción SSHFS Activa**: La estación de desarrollo `DEV00` está montada bajo `montajes/acelerografo-DEV00`. Todos los comandos que afecten servicios de sistema o interactúen con el hardware real deben proporcionarse al usuario formateados para copy-paste.
2. **Entorno Virtual**: Todas las pruebas unitarias y ejecuciones Python deben realizarse con el intérprete del entorno virtual:
   `$PROJECT_LOCAL_ROOT/.venv/bin/python3` (o `python3` dentro del `.venv` activo).
3. **Protección del Pipeline SPI**: La adquisición C (`registro_continuo_4.5.0.c`, servicio `rsa-acelerografo.service`) debe mantenerse intacta. Ningún archivo reportado en `NombreArchivoRegistroContinuo.tmp` o con `mtime < 2h` puede ser eliminado bajo ninguna circunstancia.
4. **Espacio Crítico Actual**: La partición `/dev/root` cuenta únicamente con **572 MB libres**. Las pruebas unitarias deben ejecutarse en directorios temporales aislados (`tempfile.mkdtemp`) para no agotar el almacenamiento remanente de la estación.

---

## Diagrama de Flujo del Gestor Blindado

```mermaid
flowchart TD
    A["Inicio gestor_archivos_acq.py"] --> B["adquirir_cerrojo()"]
    B -->|Concurrencia detectada (BlockingIOError)| C["Aviso: Otra instancia en ejecución -> Exit 0"]
    B -->|Fallo de permisos (PermissionError)| D["Error crítico de permisos en stderr -> Exit 1"]
    B -->|Cerrojo adquirido con éxito| E["Cargar configuración y autenticar Drive"]
    
    E --> F{"¿Conectividad DNS y Drive?"}
    F -->|Sí| G["Subir archivos pendientes (Lote)"]
    F -->|No| H["Modo online sin conexión"]
    
    G --> I["Evaluar Retención Temporal por Edad"]
    H --> I
    
    I --> J["Evaluar Espacio en Disco"]
    J --> K{"free_space < umbral_minimo (10%)"}
    K -->|No| L["Saneamiento de uploaded_files_registry.json"]
    K -->|Sí| M["Eliminar .dat no activos (iterativo hasta >= 10%)"]
    
    M --> N{"¿Aún < 10%?"}
    N -->|No| L
    N -->|Sí| O["Eliminar .mseed subidos FIFO (iterativo hasta >= 10%)"]
    
    O --> P{"free_space < umbral_critico (5%)"}
    P -->|Sí| Q["Desalojo de emergencia (.dat, .mseed, eventos)"]
    P -->|No| L
    Q --> L
    
    L --> R["Resumen final y liberar_cerrojo()"]
```

---

## Fase 1: Corrección del Bug Crítico `NameError` y Unificación de Salvaguarda Activa

**Objetivo**: Eliminar la excepción no manejada que provoca el crasheo del gestor cada hora al entrar en zona de advertencia de espacio libre (< 10%).

### Archivos Afectados
- [`scripts/operation/drive/gestor_archivos_acq.py`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/drive/gestor_archivos_acq.py)

### Acciones Detalladas
1. **Línea 830 (Modo Online — Umbral Mínimo)**:
   - *Actual*:
     ```python
     if (archivo_mas_reciente_continuous and ruta_archivo == archivo_mas_reciente_continuous) or (os.path.basename(ruta_archivo) in archivos_activos_continuous):
         continue
     ```
   - *Reemplazo*:
     ```python
     if os.path.basename(ruta_archivo) in archivos_activos_continuous:
         continue
     ```
2. **Línea 864 (Modo Online — Umbral Crítico)**:
   - *Actual*:
     ```python
     if (archivo_mas_reciente_continuous and ruta_archivo == archivo_mas_reciente_continuous) or (os.path.basename(ruta_archivo) in archivos_activos_continuous):
         continue
     ```
   - *Reemplazo*:
     ```python
     if os.path.basename(ruta_archivo) in archivos_activos_continuous:
         continue
     ```
3. **Línea 930 (Modo Online sin Conectividad — Umbral Mínimo)**:
   - *Actual*:
     ```python
     if (archivo_mas_reciente_continuous and ruta_archivo == archivo_mas_reciente_continuous) or (os.path.basename(ruta_archivo) in archivos_activos_continuous):
         continue
     ```
   - *Reemplazo*:
     ```python
     if os.path.basename(ruta_archivo) in archivos_activos_continuous:
         continue
     ```

### Comprobación (Checkpoint 1)
- Verificación sintáctica con `python3 -m py_compile scripts/operation/drive/gestor_archivos_acq.py`.
- Búsqueda estricta en el archivo para comprobar que la variable inexistente `archivo_mas_reciente_continuous` ha sido totalmente erradicada:
  ```bash
  grep "archivo_mas_reciente_continuous" scripts/operation/drive/gestor_archivos_acq.py
  # Resultado esperado: 0 coincidencias
  ```

---

## Fase 2: Desalojo Dinámico Iterativo y Blindaje por Excepciones en Control de Espacio

**Objetivo**: Sustituir el desalojo unitario insuficiente (`break` incondicional tras borrar solo 4 MB) por un bucle iterativo que recupere efectivamente el umbral de seguridad, y blindar el bloque contra excepciones no controladas.

### Archivos Afectados
- [`scripts/operation/drive/gestor_archivos_acq.py`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/drive/gestor_archivos_acq.py)

### Acciones Detalladas
1. **Desalojo iterativo en eliminación de continuous por umbral mínimo (líneas 829–838)**:
   - Tras cada llamada exitosa a `eliminar_archivo_con_verificacion()`, re-evaluar `free_space = get_free_space_percentage(mseed_directory)`.
   - Si `free_space >= umbral_minimo`, interrumpir el bucle (`break`).
2. **Desalojo iterativo en eliminación de miniSEED por umbral mínimo (líneas 849–856)**:
   - *Actual*:
     ```python
     for ruta_archivo in archivos_mseed_ordenados:
         eliminado = eliminar_archivo_con_verificacion(
             ruta_archivo, "mseed", log_directory, logger, dry_run
         )
         if eliminado and not dry_run:
             logger.delete_space("mseed", os.path.basename(ruta_archivo), "min_threshold")
             break  # Eliminar solo uno a la vez
     ```
   - *Reemplazo*:
     ```python
     for ruta_archivo in archivos_mseed_ordenados:
         eliminado = eliminar_archivo_con_verificacion(
             ruta_archivo, "mseed", log_directory, logger, dry_run
         )
         if eliminado and not dry_run:
             logger.delete_space("mseed", os.path.basename(ruta_archivo), "min_threshold")
             free_space = get_free_space_percentage(mseed_directory)
             if free_space >= umbral_minimo:
                 break
     ```
3. **Mismo patrón para modo online sin conectividad (líneas 929–946)**:
   - Re-evaluar espacio tras cada archivo borrado en lugar de hacer `break` ciego en el primer elemento.
4. **Envoltura Protectora `try...except` en Bloque de Control de Espacio**:
   - Envolver las líneas 817 a 975 en un bloque `try...except Exception as e:` con registro estructurado:
     ```python
     try:
         # ========== POLÍTICA DE CONTROL DE ESPACIO ==========
         ...
     except Exception as e:
         logger.error("control_espacio", f"Excepción durante control de espacio: {str(e)}")
     ```
   - Esto garantiza que cualquier error de disco (ej. I/O transitorio) no impida que el gestor ejecute la fase de saneamiento de registros (`limpiar_archivos_inexistentes`) ni emita el `[SUMMARY] | ejecucion="completada"`.

### Comprobación (Checkpoint 2)
- Simular en un entorno de pruebas con 10 archivos `.dat` y 10 archivos `.mseed` bajo un espacio simulado de 4% y umbral del 10%, comprobando que el bucle borra los archivos necesarios hasta alcanzar el umbral y se detiene inmediatamente al recuperarlo.

---

## Fase 3: Robustecimiento de `adquirir_cerrojo` y Saneamiento en Tareas de Sistema

**Objetivo**: Erradicar el deadlock operativo provocado por el enmascaramiento de `PermissionError` como colisión de concurrencia cuando el lockfile fue manipulado por `root`, aislar la ubicación del lockfile y asegurar limpieza de cerrojos en `registrocontinuo stop`.

### Archivos Afectados
- [`scripts/operation/drive/gestor_archivos_acq.py`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/drive/gestor_archivos_acq.py)
- [`scripts/task/registrocontinuo.sh`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/task/registrocontinuo.sh)

### Acciones Detalladas

1. **Determinación Resiliente de la Ruta del Cerrojo en `gestor_archivos_acq.py`**:
   - Implementar función auxiliar `obtener_ruta_cerrojo(lock_path_param=None)`:
     ```python
     def obtener_ruta_cerrojo(lock_path=None):
         if lock_path:
             return lock_path
         # Prioridad 1: Directorio tmp-files del proyecto local con permisos rsa:rsa
         project_root = os.environ.get("PROJECT_LOCAL_ROOT", "")
         if project_root:
             tmp_dir = os.path.join(project_root, "tmp-files")
             if os.path.isdir(tmp_dir):
                 return os.path.join(tmp_dir, "gestor_archivos_acq.lock")
         # Prioridad 2: Fallback a /tmp con sufijo del usuario ejecutor
         usuario = os.environ.get("USER", "rsa")
         return f"/tmp/gestor_archivos_acq_{usuario}.lock"
     ```
2. **Refactorización de `adquirir_cerrojo()`**:
   - Desacoplar `BlockingIOError` de fallos de permisos o de apertura:
     ```python
     def adquirir_cerrojo(lock_path=None):
         """
         Intenta adquirir un cerrojo exclusivo no bloqueante a nivel de sistema operativo.
         Retorna (lock_fd, "ok") si tuvo éxito.
         Retorna (None, "ocupado") si otra instancia ya posee el cerrojo.
         Retorna (None, "error_permiso") si no se tiene permiso de acceso/escritura.
         Retorna (None, "error_io") ante otras fallas del sistema de archivos.
         """
         ruta = obtener_ruta_cerrojo(lock_path)
         try:
             # Modo 'a' no trunca el archivo y no genera contención de metadatos
             lock_fd = open(ruta, "a")
             fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
             return lock_fd, "ok"
         except BlockingIOError:
             try:
                 lock_fd.close()
             except Exception:
                 pass
             return None, "ocupado"
         except PermissionError as e:
             sys.stderr.write(f"Error crítico de permisos en lockfile '{ruta}': {e}\n")
             return None, "error_permiso"
         except (IOError, OSError) as e:
             sys.stderr.write(f"Error de E/S al abrir lockfile '{ruta}': {e}\n")
             return None, "error_io"
     ```
3. **Manejo en `main()`**:
   - Actualizar `main()` para reaccionar adecuadamente según la causa devuelta:
     ```python
     def main():
         lock_fd, estado = adquirir_cerrojo()
         if not lock_fd:
             if estado == "ocupado":
                 print("Aviso: Otra instancia de gestor_archivos_acq.py ya se encuentra en ejecución. Saliendo de forma limpia.")
                 return
             elif estado == "error_permiso":
                 print("ERROR: Permiso denegado al acceder al archivo de cerrojo. Verifique la propiedad del archivo.")
                 sys.exit(1)
             else:
                 print(f"ERROR: No se pudo adquirir el cerrojo del sistema ({estado}).")
                 sys.exit(1)
     ```
4. **Saneamiento Proactivo en `scripts/task/registrocontinuo.sh`**:
   - En la opción `stop)`, agregar la eliminación explícita de cerrojos residuales tras terminar los procesos:
     ```bash
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
     ```

### Comprobación (Checkpoint 3)
- Ejecutar test unitario simulando cerrojo en uso (`BlockingIOError`) $\to$ retorna `(None, "ocupado")`.
- Ejecutar test unitario con archivo sin permisos de escritura $\to$ retorna `(None, "error_permiso")` y no oculta la falla.
- Ejecutar `registrocontinuo stop` y confirmar que no quedan lockfiles en `/tmp` ni en `tmp-files/`.

---

## Fase 4: Ajuste de la Política Institucional de Retención de Binarios `.dat`

**Objetivo**: Alinear la política de retención de archivos continuous `.dat` con la capacidad física real de las tarjetas microSD de 16 GB (~11.5 GB útiles), evitando que el disco opere perpetuamente en el límite de saturación.

### Archivos Afectados
- [`configuration/configuracion_dispositivo.json.template`](file:///home/rsa/git/montajes/acelerografo-DEV00/configuration/configuracion_dispositivo.json.template)

### Dimensionamiento de Almacenamiento Comparativo

| Parámetro | Configuración Anterior (30d) | Nueva Configuración Propuesta (7d) | Ahorro en Disco |
|---|---|---|---|
| **Retención `.dat` (continuous)** | 30 días (720 archivos) = **~10.1 GB** | 7 días (168 archivos) = **~2.4 GB** | **~7.7 GB liberados** |
| **Retención `.mseed`** | 30 días (720 archivos) = **~3.0 GB** | 30 días (720 archivos) = **~3.0 GB** | 0 GB (mantiene mes completo) |
| **Retención Eventos** | 30 días = **~0.15 GB** | 30 días = **~0.15 GB** | 0 GB |
| **Total Almacenamiento Sísmico** | **~13.25 GB** (excede capacidad) | **~5.55 GB** (~48% de partición) | Margen libre permanente $\ge 40\%$ |

### Acciones Detalladas
1. Modificar `configuration/configuracion_dispositivo.json.template`:
   ```json
   "gestion_almacenamiento": {
       "umbrales": {
           "minimo": 10,
           "critico": 5
       },  
       "politicas": {
           "online": {
               "subir": ["mseed", "event"],
               "retener_dias": {"continuous": 7, "mseed": 30, "event": 30}
           },
           "offline": {
               "subir": [],
               "retener_dias": {"continuous": 7, "event": 30}
           }
       }
   }
   ```
2. Proveer instrucción explícita y scriptable para actualizar `configuracion_dispositivo.json` en `DEV00` y demás estaciones activas.

### Comprobación (Checkpoint 4)
- Validar formato JSON del template con `python3 -m json.tool configuration/configuracion_dispositivo.json.template > /dev/null`.

---

## Fase 5: Ampliación y Certificación de la Suite de Pruebas Unitarias

**Objetivo**: Blindar la suite automatizada para garantizar que ninguna regresión futura pase inadvertida.

### Archivos Afectados
- [`scripts/operation/drive/test_gestor_archivos_acq.py`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/drive/test_gestor_archivos_acq.py)

### Nuevos Casos de Prueba a Implementar
1. **`test_control_espacio_sin_name_error_y_desalojo_dinamico`**:
   - Crear entorno mock con disco al 4% (bajo umbral de 10%).
   - Poblar directorio binario con 5 archivos `.dat` (1 activo en `NombreArchivoRegistroContinuo.tmp` y 4 antiguos).
   - Poblar directorio mseed con 5 archivos `.mseed` ya registrados como subidos.
   - Ejecutar la lógica de control de espacio y validar que:
     - No se produce `NameError`.
     - El archivo `.dat` activo jamás es borrado.
     - Se eliminan los archivos necesarios hasta que la función mock de espacio libre reporta $\ge 10\%$.
2. **`test_adquirir_cerrojo_permisos_y_concurrencia`**:
   - Probar adquisición nominal $\to$ retorna descriptor y `"ok"`.
   - Probar segundo intento simultáneo $\to$ retorna `None` y `"ocupado"`.
   - Simular `PermissionError` en `open()` $\to$ retorna `None` y `"error_permiso"`.
3. **`test_main_reacciona_a_error_permiso`**:
   - Comprobar que ante `"error_permiso"`, `main()` finaliza con `sys.exit(1)` y emite mensaje de error claro en lugar del aviso de otra instancia en ejecución.

### Comprobación (Checkpoint 5)
- Ejecutar la suite completa en el entorno virtual de `DEV00`:
  ```bash
  $PROJECT_LOCAL_ROOT/.venv/bin/python3 scripts/operation/drive/test_gestor_archivos_acq.py
  ```
- **Criterio de Aprobación**: 100% de tests aprobados (`Ran N tests in 0.XXs - OK`).

---

## Fase 6: Despliegue en DEV00 y Plan de Validación en Campo

**Objetivo**: Aplicar la solución en la estación `DEV00`, liberar los ~13 GB de backlog acumulado y recuperar el margen de disco nominal $\ge 10\%$.

### Procedimiento Copy-Paste para el Operador

#### 1. Respaldar archivos modificados en la estación
```bash
cp /home/rsa/projects/acelerografo/scripts/drive/gestor_archivos_acq.py /home/rsa/projects/acelerografo/scripts/drive/gestor_archivos_acq.py.bak
cp /home/rsa/projects/acelerografo/configuracion/configuracion_dispositivo.json /home/rsa/projects/acelerografo/configuracion/configuracion_dispositivo.json.bak
```

#### 2. Detener servicio y purgar lockfiles residuales
```bash
registrocontinuo stop
sudo rm -f /tmp/gestor_archivos_acq*.lock /home/rsa/projects/acelerografo/tmp-files/gestor_archivos_acq.lock
```

#### 3. Aplicar nueva retención de continuous a 7 días en `configuracion_dispositivo.json`
```bash
sed -i 's/"retener_dias": {"continuous": 30/"retener_dias": {"continuous": 7/g' /home/rsa/projects/acelerografo/configuracion/configuracion_dispositivo.json
```

#### 4. Ejecución de Simulación (`--dry-run`)
```bash
cd /home/rsa/projects/acelerografo
.venv/bin/python3 scripts/drive/gestor_archivos_acq.py --dry-run
```
*Verificar en log que lee `continuous: 7`, adquiere el cerrojo sin error de permisos, detecta espacio bajo umbral y lista los archivos `.dat` y `.mseed` a eliminar sin crashear.*

#### 5. Ejecución Real de Puesta al Día
```bash
.venv/bin/python3 scripts/drive/gestor_archivos_acq.py
```
*Verificar que finaliza con:*
`[SUMMARY] | ejecucion="completada" | modo=online`

#### 6. Reiniciar Pipeline de Producción y Validar Espacio
```bash
registrocontinuo start
sleep 5
df -h /
```
*Criterio de éxito: Espacio libre en `/dev/root` superior al 10% (> 1.5 GB libres) y `registro_continuo` activo escribiendo tramas.*

---

## Matriz de Trazabilidad y Riesgos Mitigados

| Defecto / Riesgo Diagnosticado | Fase de Mitigación | Resultado Esperado |
|---|---|---|
| `NameError` en línea 830, 864, 930 | **Fase 1** | Cero excepciones; protección estricta del archivo activo SPI. |
| Borrado de solo 1 archivo (4 MB) en alarma de espacio | **Fase 2** | Bucle iterativo recupera la meta de espacio $\ge 10\%$ en un único ciclo. |
| Crasheo del gestor ante excepciones en control de espacio | **Fase 2** | `try...except` estructurado preserva el saneamiento del registro JSON. |
| Falso positivo de concurrencia por `PermissionError` en lockfile | **Fase 3** | Lockfile aislado en `tmp-files/` y reporte inequívoco de causas en `main()`. |
| Desacople dimensional entre SD 16 GB y retención de 30d `.dat` | **Fase 4** | Retención de 7d en `.dat` libera ~7.7 GB de margen operativo permanente. |
| Regresión inadvertida en futuros cambios | **Fase 5** | Suite unitaria ampliada con cobertura completa de cerrojos y umbrales. |
