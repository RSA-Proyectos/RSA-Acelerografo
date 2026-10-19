---
proyecto: acelerografo-DEV00
tipo: diagnostico_tecnico
resolucion: resuelto
adr: ADR-025
blueprint: docs/blueprints/2026-10-19_plan_correccion_acumulacion_y_robustecimiento_gestor_sync.md
temas: [drive, almacenamiento, espacio-disco, nameerror, retencion, acelerografo, dev00, tes00, flock, cerrojo, permisos]
fecha: 2026-10-19
---

# Diagnóstico Técnico: Acumulación de Archivos en Disco y Falla Crítica del Control de Espacio en el Gestor de Adquisición

**Fecha**: 2026-10-19  
**Proyecto / Repositorio**: `acelerografo-DEV00` (`RSA-Acelerografo`)  
**Componente(s) afectado(s)**: `scripts/operation/drive/gestor_archivos_acq.py`, `configuration/configuracion_dispositivo.json`, `scripts/task/registrocontinuo.sh`  
**Estación(es) bajo análisis**: `DEV00` (desarrollo) y `TES00` (testeo)  
**Estado**: Resuelto (implementado y validado en producción en DEV00; documentado en ADR-025)  
**Severidad**: Alta (riesgo inminente de parada de adquisición SPI y corrupción de sistema por llenado al 100% de la partición raíz `/dev/root`)  

---

## 1. Resumen Ejecutivo

Durante las pruebas de validación en las estaciones acelerográficas `DEV00` y `TES00`, se identificó una acumulación anómala y progresiva de archivos en almacenamiento local que ignora los límites de seguridad de la estación. En `DEV00`, la partición raíz `/dev/root` alcanzó un **96% de ocupación (14 GB usados, solo 572 MB libres / 3.92% de espacio libre)** con **744 archivos MiniSEED acumulados** en `/home/rsa/data/mseed/` y cientos de archivos binarios `.dat` en `/home/rsa/data/registro-continuo/`.

A pesar de que la configuración establece un umbral mínimo de espacio libre del 10% y un umbral crítico del 5%, el gestor no libera espacio en disco. El análisis forense de [`logs.tmp`](file:///home/rsa/git/logs.tmp) y del código fuente reveló dos defectos de código críticos concurrentes:
1. **Defecto de Código Crítico (`NameError`)**: En cada ejecución horaria, el script evalúa la retención temporal, detecta correctamente que el disco está bajo el umbral mínimo (`3.92% < 10%`) e intenta ejecutar el control de espacio; sin embargo, en la línea 830 de `gestor_archivos_acq.py`, evalúa la variable no declarada `archivo_mas_reciente_continuous`, provocando la muerte instantánea del proceso Python sin registrar el error en el log estructurado ni ejecutar el desalojo de archivos ni el saneamiento de registros.
2. **Falso Positivo de Concurrencia y Enmascaramiento de Permisos en el Cerrojo (`adquirir_cerrojo`)**: En una prueba posterior documentada en [`logs.tmp`](file:///home/rsa/git/logs.tmp), el usuario modificó `configuracion_dispositivo.json` para reducir el tiempo de retención de archivos continuos (`.dat`) y reinició el servicio mediante `registrocontinuo stop` y `registrocontinuo start`. Sin embargo, la nueva retención **nunca llegó a ejecutarse**. El script abortó en la primera línea de `main()` con el mensaje: `Aviso: Otra instancia de gestor_archivos_acq.py ya se encuentra en ejecución. Saliendo de forma limpia.`. Este comportamiento ocurre porque la función `adquirir_cerrojo()` captura de forma genérica `(BlockingIOError, IOError)` al intentar abrir `/tmp/gestor_archivos_acq.lock` en modo `"w"`. Si dicho archivo fue creado previamente por `root` (vía `sudo` o `systemd`), el intento de apertura por el usuario `rsa` produce un `PermissionError` (subclase de `IOError`), el cual es capturado silenciosamente y tratado como si otra instancia estuviera corriendo, provocando un **deadlock operativo** que bloquea la ejecución del gestor.

Adicionalmente, se evidenció un desacople arquitectónico entre la política de retención temporal (`"retener_dias": 30`), que exige ~13.2 GB de almacenamiento para un mes de datos brutos y miniSEED, y la capacidad física real de las tarjetas microSD de 16 GB (~11.5 GB útiles tras SO). Al estar inoperativos tanto el control de espacio por el crasheo como el arranque del gestor por el bloqueo de cerrojo, la estación satura la partición de forma inevitable.

---

## 2. Estado Actual de la Estación `DEV00`

| Componente / Parámetro | Estado | Métrica Observada | Observaciones Técnicas |
|---|---|---|---|
| **Partición Raíz (`/dev/root`)** | ⚠️ Crítico | `14G / 15G` (96% uso, 572 MB libres) | Espacio libre real en **3.92%**, muy por debajo del umbral mínimo (10%) y crítico (5%). |
| **Pipeline SPI (`registro_continuo`)** | ✅ Operativo | PID 1727, uptime 19 días | Adquisición C activa; escribe binarios `.dat` horarios continuamente. |
| **Daemons Supervisor** | ✅ Operativo | 4/4 `RUNNING` | `stream_processor`, `gpd_worker`, `mqtt_coordinator`, `config_server` nominales. |
| **Cola MiniSEED en Disco** | ⚠️ Sobredimensionada | **744 archivos** en disco | 744 horas continuas (~31 días completos de datos). Todos con subida exitosa confirmada. |
| **Registro de Subidas (`registry.json`)** | ✅ Consistente | 4,899 exitosos / 0 fallidos | 840 mseed y 4,059 eventos sincronizados con Google Drive; sin fallos retenidos. |
| **Conectividad Google APIs** | ✅ Nominal | 0% packet loss, RTT 80.6 ms | Resolución DNS y enlace HTTPS funcionando plenamente. |
| **Subida Horaria (`upload_resumen`)** | ✅ Nominal | `1/1 status=success` | Sube con éxito el nuevo MiniSEED de la hora en curso cada 60 minutos (cuando no es bloqueado por el cerrojo). |
| **Cerrojo de Proceso (`/tmp/gestor_archivos_acq.lock`)** | ❌ Falso Bloqueo | Aborta en `main()` | Captura genérica de `IOError` enmascara `PermissionError` como colisión de concurrencia, abortando la ejecución sin ejecutar trabajo. |
| **Retención Temporal por Edad** | ❌ Inoperativa | Bloqueada en arranque | Cambios en `retener_dias` (ej. prueba `.dat`) no se leen ni aplican porque el gestor aborta en el cerrojo antes de leer el JSON. |
| **Control de Espacio en Disco** | ❌ Inoperativo | **Crashea en cada ciclo** | Aborta en línea 830 por `NameError`; no borra ningún archivo ni llega a umbral crítico. |

---

## 3. Evidencia y Análisis

### 3.1. Interrupción Abrupta en `DEV0_gestor_acq.log`
En el extracto de logs de `logs.tmp` (líneas 106 a 135), se comprueba que en **todas las horas registradas** (11:00, 12:00, 13:00, 14:00, 15:00 UTC), la ejecución finaliza de golpe en la línea de advertencia de espacio, sin emitir el resumen de finalización `[SUMMARY] | ejecucion="completada"`:

```text
2026-10-19 14:00:34,530 - DEV0_gestor_acq.log - INFO - [INIT] | modo=online | id_estacion=DEV0
2026-10-19 14:00:34,530 - DEV0_gestor_acq.log - INFO - [SUMMARY] | config_cargada=True | modo=online | umbral_min=10 | umbral_crit=5 | retener_dias={'continuous': 30, 'mseed': 30, 'event': 30}
2026-10-19 14:01:05,692 - DEV0_gestor_acq.log - INFO - [SUMMARY] | archivos_omitidos=4547 | razon=ya_subidos
2026-10-19 14:01:11,413 - DEV0_gestor_acq.log - INFO - [SUMMARY] | upload_resumen=1/1 | status=success
2026-10-19 14:01:11,527 - DEV0_gestor_acq.log - INFO - [SUMMARY] | retencion_evaluada=True | modo_simulacion=False | continuous_expirados=0 | mseed_expirados=1 | event_expirados=0
2026-10-19 14:01:11,528 - DEV0_gestor_acq.log - WARNING - Espacio bajo umbral mínimo | online | 4.04% < 10%
# --> FIN ANÓMALO DEL CICLO: No hay logs de desalojo, ni de saneamiento, ni cierre de ciclo.
```

### 3.2. Línea de Código Causante de la Falla
En `scripts/operation/drive/gestor_archivos_acq.py`, líneas 822–835:

```python
# Umbral mínimo
if free_space < umbral_minimo:
    logger.warning(f"Espacio bajo umbral mínimo | online | {free_space:.2f}% < {umbral_minimo}%")

    # Eliminar archivos continuous antiguos (excepto el más reciente)
    archivos_continuous_paths = [os.path.join(binary_directory, f) for f in os.listdir(binary_directory) if f.endswith(".dat")]
    archivos_continuous_ordenados = sorted(archivos_continuous_paths, key=os.path.getmtime)

    for ruta_archivo in archivos_continuous_ordenados:
        # LÍNEA 830: ERROR CRÍTICO
        if (archivo_mas_reciente_continuous and ruta_archivo == archivo_mas_reciente_continuous) or (os.path.basename(ruta_archivo) in archivos_activos_continuous):
            continue
```

- La variable `archivo_mas_reciente_continuous` **no está definida** en el ámbito local ni global de la función `_ejecutar_gestor()`.
- Al existir archivos `.dat` en `binary_directory`, el bucle `for` itera y evalúa `archivo_mas_reciente_continuous`, disparando instantáneamente un `NameError`.
- La llamada a `_ejecutar_gestor()` en `main()` carece de captura de excepciones en este nivel, delegando al bloque `finally:` que libera el cerrojo `/tmp/gestor_archivos_acq.lock` y termina el script con código de error.

### 3.3. Nueva Evidencia: Prueba de Cambio de Retención en `DEV00` y Bloqueo de Cerrojo
Durante una nueva prueba realizada por el usuario en `DEV00`, registrada en [`logs.tmp`](file:///home/rsa/git/logs.tmp) (líneas 1 a 41), se ejecutó la siguiente secuencia para modificar la retención de binarios continuos `.dat`:

```text
rsa@DEV00:~ $ registrocontinuo stop
Deteniendo sistema de registro continuo...
rsa@DEV00:~ $ registrocontinuo start
Iniciando registro_continuo via systemd...
Ejecutando conversión a miniSEED...
[01/01] ── DEV0_261019-150000.dat ────────────────────────────────────────
tiempos_np : [54001, 56611]
binary time: 2026-10-19 15:00:01 ──> 2026-10-19 15:43:31
mseed time : 2026-10-19 15:00:01 ──> 2026-10-19 15:43:31
output     : /home/rsa/data/mseed/DEV0_20261019_150001.mseed
status     : OK (0.73s)

Ejecutando gestor de archivos...
rsa@DEV00:~ $ ... [Warnings de deprecación google-auth Python 3.9] ...
Aviso: Otra instancia de gestor_archivos_acq.py ya se encuentra en ejecución. Saliendo de forma limpia.

rsa@DEV00:~ $ cd projects/acelerografo/configuracion
rsa@DEV00:~/projects/acelerografo/configuracion $ nano configuracion_dispositivo.json
rsa@DEV00:~/projects/acelerografo/configuracion $ registrocontinuo stop
Deteniendo sistema de registro continuo...
rsa@DEV00:~/projects/acelerografo/configuracion $ registrocontinuo start
Iniciando registro_continuo via systemd...
Ejecutando conversión a miniSEED...
[01/01] ── DEV0_261019-154338.dat ────────────────────────────────────────
tiempos_np : [56620, 56746]
binary time: 2026-10-19 15:43:40 ──> 2026-10-19 15:45:46
mseed time : 2026-10-19 15:43:40 ──> 2026-10-19 15:45:46
output     : /home/rsa/data/mseed/DEV0_20261019_154340.mseed
status     : OK (0.04s)

Ejecutando gestor de archivos...
rsa@DEV00:~/projects/acelerografo/configuracion $ ... [Warnings google-auth] ...
Aviso: Otra instancia de gestor_archivos_acq.py ya se encuentra en ejecución. Saliendo de forma limpia.
```

#### Análisis Técnico de la Secuencia de Prueba:
1. **La nueva configuración de retención NUNCA se evaluó**: Tras editar `configuracion_dispositivo.json` con `nano` y relanzar el pipeline con `registrocontinuo start`, el proceso `gestor_archivos_acq.py` abortó de inmediato en `main()`. El código jamás llegó a llamar a `_ejecutar_gestor()`, por lo que **no leyó el archivo JSON, no evaluó la retención de `.dat`, no evaluó el espacio en disco ni intentó borrar ningún archivo**.
2. **Discrepancia entre la Guarda Bash (`registrocontinuo.sh`) y el Cerrojo Python**:
   - En [`scripts/task/registrocontinuo.sh`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/task/registrocontinuo.sh#L32-L37):
     ```bash
     if is_running "gestor_archivos_acq.py"; then
       echo "Aviso: gestor_archivos_acq.py ya se encuentra en ejecución. Omitiendo lanzamiento concurrente."
     else
       echo "Ejecutando gestor de archivos..."
       "$VENV_PYTHON" "$PROJECT_LOCAL_ROOT/scripts/drive/gestor_archivos_acq.py" &
     fi
     ```
   - La función `is_running` ejecuta `pgrep -f "gestor_archivos_acq.py"`. Dado que el script imprimió `"Ejecutando gestor de archivos..."`, **se confirma que a nivel de sistema operativo no existía ningún proceso `gestor_archivos_acq.py` en ejecución** (había sido terminado por `pkill` en `stop`).
   - Sin embargo, milisegundos después, el proceso Python recién nacido emitió:
     `"Aviso: Otra instancia de gestor_archivos_acq.py ya se encuentra en ejecución. Saliendo de forma limpia."`
3. **Mecanismo de Falla en `adquirir_cerrojo()`**:
   - En [`scripts/operation/drive/gestor_archivos_acq.py`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/drive/gestor_archivos_acq.py#L79-L93):
     ```python
     def adquirir_cerrojo(lock_path=LOCK_FILE_PATH):
         try:
             lock_fd = open(lock_path, "w")
             fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
             return lock_fd
         except (BlockingIOError, IOError):
             try:
                 lock_fd.close()
             except Exception:
                 pass
             return None
     ```
   - Si el archivo `/tmp/gestor_archivos_acq.lock` fue creado previamente por `root` (por ejemplo, si en algún momento se ejecutó `sudo registrocontinuo` o fue invocado por un servicio/cron administrativo con permisos de superusuario), el archivo queda con propietario `root:root` y permisos `0644`.
   - Cuando el usuario sin privilegios `rsa` ejecuta `registrocontinuo start`, la llamada `open('/tmp/gestor_archivos_acq.lock', 'w')` falla de inmediato con `PermissionError: [Errno 13] Permission denied`.
   - En Python 3, `PermissionError` es subclase de `OSError` (e `IOError` es un alias de `OSError`). Por lo tanto, el bloque `except (BlockingIOError, IOError):` **captura el error de permisos**, descarta silenciosamente la excepción y retorna `None`.
   - La función `main()` interpreta ese `None` como colisión de concurrencia y aborta limpiamente, ocultando la falla real de permisos.

---

## 4. Hallazgos y Causa Raíz

### Hallazgo 1: `NameError` en Condiciones de Exclusión de Binarios Continuos (Causa Raíz Directa)
* **Descripción técnica**: El intérprete Python aborta por excepción `NameError: name 'archivo_mas_reciente_continuous' is not defined`.
* **Causa**: Durante la refactorización para implementar `obtener_archivos_activos_continuous()` (que protege de forma robusta los archivos en uso por el adquisidor C vía `NombreArchivoRegistroContinuo.tmp` y `mtime < 2h`), se eliminó la asignación previa de `archivo_mas_reciente_continuous`, pero se conservó la referencia directa a esa variable en tres ubicaciones del código:
  1. **Línea 830**: Control de espacio bajo umbral mínimo (modo online).
  2. **Línea 864**: Control de espacio bajo umbral crítico (modo online).
  3. **Línea 930**: Control de espacio bajo umbral mínimo (modo online sin conexión).
* **Condiciones de activación**: Se manifiesta de forma determinista en cuanto el espacio libre cae por debajo de `umbral_minimo` (10%) y existe al menos un archivo `.dat` en disco.
* **Impacto**: Anulación total de las políticas de control de espacio. Ningún archivo se borra por espacio, el umbral crítico nunca se ejecuta y la partición avanza hacia el llenado al 100%.

### Hallazgo 2: Desalojo Unitario Insuficiente en Umbral Mínimo (`break` incondicional)
* **Descripción técnica**: En la línea 855 de `gestor_archivos_acq.py`:
  ```python
  for ruta_archivo in archivos_mseed_ordenados:
      eliminado = eliminar_archivo_con_verificacion(ruta_archivo, "mseed", log_directory, logger, dry_run)
      if eliminado and not dry_run:
          logger.delete_space("mseed", os.path.basename(ruta_archivo), "min_threshold")
          break  # <--- Salida tras un solo archivo
  ```
* **Causa**: Diseño de eliminación de "un solo archivo a la vez".
* **Condiciones de activación**: Ocurriría si se superara el Hallazgo 1.
* **Impacto**: Cada archivo `.mseed` horario libera ~4.2 MB. La estación genera ~4.5 MB de MiniSEED más eventos en cada ciclo. Al borrar únicamente 1 archivo por ciclo horario, la tasa de liberación es superada por la tasa de generación, impidiendo que el disco recupere el umbral mínimo del 10% y forzando la degradación hacia el umbral crítico.

### Hallazgo 3: Conflicto de Dimensionamiento entre `retener_dias: 30` y Capacidad de Tarjeta MicroSD
* **Descripción técnica**: La política de retención temporal por días mantiene legítimamente los 744 archivos miniSEED en disco.
* **Causa**:
  - `retener_dias` está fijado en 30 días para `continuous`, `mseed` y `event`.
  - 30 días de `.dat` = 720 h × 14 MB = **~10.1 GB**.
  - 30 días de `.mseed` = 720 h × 4.2 MB = **~3.0 GB**.
  - Total datos sismológicos = **~13.1 GB**.
  - Capacidad útil de la partición `/dev/root` de 15 GB (descontando SO y venv): **~11.5 GB**.
* **Condiciones de activación**: Estaciones con tarjetas microSD de 16 GB operando por más de 20-25 días continuos.
* **Impacto**: La retención por días satura el disco por diseño si el control de espacio por umbrales (10% y 5%) no recorta dinámicamente el backlog. Al estar inoperativo el control de espacio por el Hallazgo 1, la saturación es inevitable.

### Hallazgo 4: Falso Positivo de Concurrencia y Enmascaramiento de `PermissionError` en `adquirir_cerrojo()` (Bloqueo Operativo)
* **Descripción técnica**: `gestor_archivos_acq.py` aborta en el arranque emitiendo `"Aviso: Otra instancia de gestor_archivos_acq.py ya se encuentra en ejecución"` a pesar de que ningún otro proceso del gestor existe en la tabla de procesos del sistema.
* **Causa**: Enmascaramiento de excepciones en `adquirir_cerrojo()`. La función engloba indistintamente `BlockingIOError` (verdadero conflicto de concurrencia `flock`) e `IOError` (que en Python 3 abarca `PermissionError`, `FileNotFoundError`, etc.). Al intentar abrir en modo escritura (`"w"`) una ruta compartida en `/tmp` (`/tmp/gestor_archivos_acq.lock`) que pertenece a `root:root`, el usuario `rsa` recibe `PermissionError` (Errno 13). La función lo captura, asume erróneamente que otra instancia posee el cerrojo y retorna `None`.
* **Condiciones de activación**: Se manifiesta de forma permanente e irreversible en cualquier estación donde `/tmp/gestor_archivos_acq.lock` haya sido creado con privilegios elevados (`sudo`, systemd como root) y posteriormente el gestor sea invocado por el usuario estándar `rsa`.
* **Impacto**: **Deadlock operativo total**. Ningún cambio de configuración en `configuracion_dispositivo.json` (como la reducción del tiempo de retención de `.dat` ensayada en la prueba) llega a procesarse, ni se ejecutan las subidas programadas de archivos ni el control de espacio, dejando el sistema en un estado vegetativo donde los archivos se acumulan sin límite alguno.

---

## 5. Evaluación de Riesgo

| # | Escenario de Riesgo | Probabilidad | Impacto | Mitigación Requerida |
|---|---|---|---|---|
| **R1** | **Colapso Total de Partición Raíz (100% de Disco)**: Se agotan los 572 MB restantes en menos de 48 horas debido a la generación continua de `.dat` (14 MB/h), `.mseed` (4.2 MB/h) y eventos GPD. | Alta | Crítico | Reparar de inmediato el bug de control de espacio para habilitar el desalojo FIFO de archivos ya respaldados. |
| **R2** | **Corrupción del Pipeline de Adquisición SPI**: Al llenarse el disco al 100%, la función `fwrite()` en `registro_continuo_4.5.0.c` fallará, provocando el cierre intempestivo del servicio de adquisición o la corrupción del sistema de archivos `ext4`. | Alta | Crítico | Evacuar/purgar de emergencia archivos `.dat` y `.mseed` antiguos ya confirmados en Google Drive. |
| **R3** | **Degradación Sistémica en Estaciones de Campo**: Si este código se despliega a `CHA01` o `CHA02`, reproducirá el colapso de almacenamiento en pocas semanas. | Alta | Alto | Certificar la corrección en `DEV00` y `TES00` antes de generar el release a producción. |
| **R4** | **Parálisis Permanente del Subsistema de Sincronización y Retención**: Al fallar el cerrojo con un falso positivo de concurrencia (`PermissionError`), la estación queda completamente ciega y congelada respecto a subidas y purga local, ignorando cualquier ajuste en los tiempos de retención y acumulando datos indefinidamente. | Alta | Crítico | Desacoplar `BlockingIOError` de `PermissionError`, garantizar que el lock pertenezca al usuario de ejecución o reubicar el archivo de cerrojo a un directorio gestionado con permisos apropiados. |

---

## 6. Opciones y Decisiones

### Decisión 1: Corrección de Salvaguarda de Archivos Activos en Control de Espacio

| Opción | Descripción | Ventajas | Desventajas |
|---|---|---|---|
| **Opción A (Recomendada)** | **Unificar con `archivos_activos_continuous`**: Eliminar `archivo_mas_reciente_continuous` y usar exclusivamente `if os.path.basename(ruta_archivo) in archivos_activos_continuous: continue` (idéntico al modo offline). | Código limpio, coherente con el modo offline, reutiliza la función testeada `obtener_archivos_activos_continuous()`. | Ninguna identificada. |
| **Opción B** | **Definir la variable**: Asignar `archivo_mas_reciente_continuous = obtener_archivo_mas_reciente(...)` antes de los bloques de control de espacio. | Mantiene la doble condición. | Redundante, pues `obtener_archivos_activos_continuous()` ya incluye el archivo más reciente si tiene `mtime < 2h` o está en el `.tmp`. |

> **Recomendación**: Implementar la **Opción A**, alineando la lógica del modo online con el modo offline que ya se encuentra validado.

---

### Decisión 2: Comportamiento de Desalojo en Umbral Mínimo (< 10%)

| Opción | Descripción | Ventajas | Desventajas |
|---|---|---|---|
| **Opción A (Recomendada)** | **Bucle Dinámico hasta Recuperar Umbral**: Iterar eliminando archivos antiguos ya respaldados hasta que `free_space >= umbral_minimo`. | Garantiza que la estación recupere efectivamente el margen de seguridad de disco en una sola corrida. | Elimina múltiples archivos de golpe si el déficit acumulado es amplio. |
| **Opción B** | **Cuota fija por ciclo (ej. 10 archivos)**: Borrar hasta un máximo de $N$ archivos por hora. | Desalojo gradual. | En situaciones de déficit severo (como el actual 3.92% vs 10%), tomaría días salir de la zona de alarma. |

> **Recomendación**: Implementar la **Opción A** tanto para binarios `.dat` como para `.mseed`, verificando tras cada eliminación si `free_space >= umbral_minimo`.

---

### Decisión 3: Política Institucional de Retención de Binarios `.dat`

| Opción | Descripción | Ventajas | Desventajas |
|---|---|---|---|
| **Opción A (Recomendada)** | **Reducir `retener_dias.continuous` a 7 días en `configuracion_dispositivo.json`**: Mantener 7 días de binarios `.dat` (igual que en modo offline) y 30 días de `mseed`. | Reduce en ~7.5 GB la huella en disco. 7 días de binarios es margen suficiente para contingencias de conversión a miniSEED. | Menor histórico local de archivos binarios puros (aunque el miniSEED preserva los datos al 100%). |
| **Opción B** | **Mantener 30 días y confiar plenamente en el control de espacio**: Dejar la configuración en 30 días y permitir que los umbrales recorten archivos constantemente. | No altera plantillas de configuración. | La estación vivirá permanentemente operando entre el 90% y 95% de uso de disco. |

> **Recomendación**: Implementar la **Opción A**, ajustando la plantilla a 7 días de `continuous` y 30 días de `mseed`.

---

### Decisión 4: Robustecimiento del Mecanismo de Cerrojo de Exclusión (`adquirir_cerrojo`)

| Opción | Descripción | Ventajas | Desventajas |
|---|---|---|---|
| **Opción A (Recomendada)** | **Desacoplar Excepciones y Reubicar Lockfile**: Distinguir `BlockingIOError` (concurrencia legítima) de `PermissionError` y otros `OSError` (reportando el error exacto en stderr/log). Reubicar el lockfile a `$PROJECT_LOCAL_ROOT/data/gestor_archivos_acq.lock` (o `/tmp/gestor_archivos_acq_${USER}.lock`) para aislarlo de conflictos de permisos entre usuarios. | Elimina de raíz las colisiones de permisos entre `root` y `rsa`. Diagnóstico claro si ocurre una falla de I/O genuina. | Requiere asegurar que el directorio exista (ya garantizado en `$PROJECT_LOCAL_ROOT/data/`). |
| **Opción B** | **Forzar Limpieza en `registrocontinuo stop`**: Añadir `sudo rm -f /tmp/gestor_archivos_acq.lock` dentro del caso `stop)` de `registrocontinuo.sh`. | Elimina el lockfile huérfano al reiniciar. | Si el proceso muere por otra vía (ej. crash o reinicio abrupto), el lockfile en `/tmp` con dueño `root` persistiría. |

> **Recomendación**: Implementar una estrategia de **Defensa en Profundidad** combinando **Opción A** (manejo explícito de excepciones y nombre/ruta de lock resiliente por usuario en Python) y **Opción B** (saneamiento proactivo del archivo de cerrojo en `registrocontinuo.sh stop`).

---

## 7. Mitigaciones Aplicadas

* **Ninguna aplicada aún en código fuente**: Pendiente de aprobación del presente diagnóstico para ejecutar las modificaciones en `scripts/operation/drive/gestor_archivos_acq.py`, `scripts/task/registrocontinuo.sh` y en la configuración.

---

## 8. Backlog de Mejoras

1. **Corrección de Bug `NameError`**: Reemplazar las comprobaciones de `archivo_mas_reciente_continuous` en las líneas 830, 864 y 930 por `os.path.basename(ruta_archivo) in archivos_activos_continuous`.
2. **Refactorización Resiliente de `adquirir_cerrojo()`**:
   - Separar la captura de `BlockingIOError` (otra instancia activa → salida limpia esperada) de `PermissionError` y `OSError` (problema de acceso a disco/permisos → registrar error explícito en `sys.stderr` y logger).
   - Utilizar una ruta de cerrojo con sufijo de usuario o ubicada en el directorio de datos de la aplicación (ej. `/tmp/gestor_archivos_acq_rsa.lock` o `$PROJECT_LOCAL_ROOT/data/gestor_archivos_acq.lock`).
3. **Saneamiento de Cerrojos en `registrocontinuo.sh`**:
   - En el comando `stop)`, asegurar la eliminación del archivo de cerrojo huérfano (`rm -f /tmp/gestor_archivos_acq*.lock`) tras detener los procesos.
4. **Desalojo Dinámico en Umbral Mínimo**: Sustituir el `break` incondicional de la línea 855 por re-verificación de `free_space >= umbral_minimo`.
5. **Manejo Robusto de Excepciones en Control de Espacio**: Envolver el bloque completo de control de espacio (`POLÍTICA DE CONTROL DE ESPACIO`) en un bloque `try/except Exception as e:` que registre el error en el log estructurado con `logger.error("control_espacio", str(e))` sin abortar la ejecución del gestor ni el saneamiento del registro.
6. **Ajuste de Plantilla de Configuración**: Modificar `configuration/configuracion_dispositivo.json.template` para que la política online contemple `"retener_dias": {"continuous": 7, "mseed": 30, "event": 30}`.
7. **Ampliación de Suite de Pruebas Unitarias**:
   - Añadir en `test_gestor_archivos_acq.py` un caso de prueba que simule disco bajo el umbral mínimo (`free_space = 4.0%`) con archivos `.dat` y `.mseed` presentes, verificando que no ocurra `NameError` y que los archivos se eliminen efectivamente.
   - Añadir pruebas unitarias reales sobre `adquirir_cerrojo()` evaluando el comportamiento ante `PermissionError` y ante concurrencia real vía subproceso.

---

## 9. Plan de Validación

| # | Checkpoint | Criterio de Éxito |
|---|---|---|
| **CP-1** | Ejecución de suite unitaria ampliada | `python3 test_gestor_archivos_acq.py` aprueba al 100% incluyendo la prueba de simulación de espacio bajo umbral mínimo y manejo de cerrojo. |
| **CP-2** | Validación del cerrojo bajo usuario `rsa` | Al ejecutar `registrocontinuo stop && registrocontinuo start` como usuario `rsa`, `gestor_archivos_acq.py` no emite falso positivo de otra instancia en ejecución y adquiere el cerrojo exitosamente. |
| **CP-3** | Ejecución en simulación (`--dry-run`) en `DEV00` | `python3 gestor_archivos_acq.py --dry-run` lee la nueva configuración de retención (`.dat`), no crashea en el warning de espacio y muestra los archivos que se borrarían hasta recuperar el 10%. |
| **CP-4** | Ejecución real en `DEV00` con nueva retención | `python3 gestor_archivos_acq.py` corre de principio a fin, emite `[SUMMARY] | ejecucion="completada"`, desaloja binarios `.dat` antiguos según el nuevo tiempo de retención y recupera el espacio libre a $\ge 10\%$. |
| **CP-5** | Verificación en `diagnostico drive` | El reporte `diagnostico drive` muestra `disco_libre >= 10.0%`, purga efectiva de binarios `.dat` y reducción del número de miniSEED en disco. |
