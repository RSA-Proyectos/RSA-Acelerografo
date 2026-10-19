---
id: ADR-025
titulo: Robustecimiento de Cerrojos de Proceso, Desalojo Dinámico Iterativo y Gobernanza de Almacenamiento en Disco
estado: Aceptado
fecha: 2026-10-19
temas: [flock, cerrojo, espacio-disco, almacenamiento, retencion, resiliencia, acelerografo, raspberry-pi, permissions]
entorno: acelerografo
---

# ADR-025: Robustecimiento de Cerrojos de Proceso, Desalojo Dinámico Iterativo y Gobernanza de Almacenamiento en Disco

## Estado

**Aceptado** | Fecha: 2026-10-19

## Contexto

Tras la adopción de [ADR-024](024_blindaje_pipeline_drive_livelock_memoria_concurrencia.md), que incorporó cerrojos atómicos a nivel de sistema operativo (`fcntl.flock`) y dosificación por lotes para erradicar el livelock de memoria en estaciones Raspberry Pi 3B+, las pruebas de validación en campo en las estaciones acelerográficas `DEV00` y `TES00` evidenciaron una acumulación anómala y progresiva de datos en disco. En `DEV00`, la partición raíz `/dev/root` alcanzó un **96% de ocupación (14 GB usados, solo 572 MB libres / 3.92% de espacio libre)**, acumulando más de 744 archivos MiniSEED y cientos de binarios `.dat`, ignorando las alertas de espacio mínimo (10%) y crítico (5%).

El análisis técnico forense documentado en [`docs/analysis/2026-10-19_diagnostico_acumulacion_archivos_y_fallo_control_espacio_drive.md`](../analysis/2026-10-19_diagnostico_acumulacion_archivos_y_fallo_control_espacio_drive.md) identificó cuatro defectos interconectados en el diseño y en la implementación del gestor de sincronización:

1. **Defecto de Código Crítico (`NameError`)**: En cada ciclo horario, el gestor evaluaba la retención temporal, detectaba correctamente el espacio bajo el umbral mínimo (`3.92% < 10%`) e intentaba el desalojo; sin embargo, en las líneas 830, 864 y 930 de `gestor_archivos_acq.py`, evaluaba la variable no declarada `archivo_mas_reciente_continuous`, provocando el aborto intempestivo del proceso Python sin registrar el error en el log estructurado, sin borrar ningún archivo y sin ejecutar el saneamiento de `uploaded_files_registry.json`.
2. **Desalojo Insuficiente Unitario (`break` incondicional)**: En la evaluación del umbral mínimo, el bucle de miniSEED contenía un `break` incondicional tras eliminar un único archivo (~4.2 MB). Como la estación genera ~4.5 MB de datos por hora (MiniSEED continuo más eventos sísmicos GPD), la tasa de generación superaba la tasa de liberación, imposibilitando que la estación recuperara el margen de seguridad del 10%.
3. **Deadlock Operativo por Falso Positivo de Concurrencia en Cerrojo (`adquirir_cerrojo`)**: Al intentar modificar la retención temporal en `configuracion_dispositivo.json` y reiniciar el servicio (`registrocontinuo stop && registrocontinuo start`), el gestor abortó en la primera línea de `main()` con el aviso: `"Otra instancia de gestor_archivos_acq.py ya se encuentra en ejecución. Saliendo de forma limpia"`. La función `adquirir_cerrojo()` abría `/tmp/gestor_archivos_acq.lock` en modo `"w"` capturando genéricamente `(BlockingIOError, IOError)`. Si el lockfile había sido manipulado previamente por `root` (vía `sudo` o systemd), el intento de apertura por el usuario no privilegiado `rsa` generaba un `PermissionError` (subclase de `IOError`), el cual era enmascarado silenciosamente como colisión de concurrencia, congelando el gestor de forma permanente.
4. **Desacople Dimensional de Almacenamiento**: La configuración institucional por defecto establecía `"retener_dias": {"continuous": 30, "mseed": 30, "event": 30}`. 30 días de binarios `.dat` demandan ~10.1 GB y 30 días de `.mseed` ~3.0 GB (~13.1 GB de datos sismológicos), excediendo la capacidad física real disponible (~11.5 GB útiles tras SO) en tarjetas microSD de 16 GB.

---

## Opciones Evaluadas

### Decisión 1: Salvaguarda de Archivos Activos en Control de Espacio
- **Opción A (Seleccionada)**: Unificar la verificación con `archivos_activos_continuous` (obtenidos mediante `obtener_archivos_activos_continuous()`), eliminando definitivamente la variable huérfana `archivo_mas_reciente_continuous` en todos los modos (online min, online crit, online sin conexión).
  - *Ventajas*: Coherente con el modo offline probado, reutiliza la función testeada que protege el archivo activo por `NombreArchivoRegistroContinuo.tmp` y por `mtime < 2h`.
  - *Desventajas*: Ninguna.
- **Opción B**: Definir y asignar `archivo_mas_reciente_continuous = obtener_archivo_mas_reciente(...)` antes de cada bloque.
  - *Ventajas*: Conserva la sintaxis anterior.
  - *Desventajas*: Redundante, genera complejidad innecesaria y no previene inconsistencias si cambian los criterios de protección de archivos activos.

### Decisión 2: Mecanismo de Desalojo ante Espacio Bajo Umbral
- **Opción A (Seleccionada)**: Bucle Dinámico Iterativo con Reevaluación de Umbral. Reemplazar los cortes incondicionales (`break`) por la reevaluación de `free_space = get_free_space_percentage()` tras cada eliminación de `.dat` y `.mseed`, deteniéndose en cuanto `free_space >= umbral_minimo`. Envolver toda la política de control de espacio en bloques `try...except Exception as e:` con registro estructurado (`logger.error`).
  - *Ventajas*: Garantiza que la estación recupere el margen de seguridad ($\ge 10\%$) en un solo ciclo horario. Si una excepción de I/O ocurre en un archivo, no colapsa el gestor ni impide el saneamiento de `uploaded_files_registry.json`.
  - *Desventajas*: Puede borrar múltiples archivos continuos antiguos en ráfaga en estaciones con alto déficit acumulado.
- **Opción B**: Cuota fija de $N$ archivos por ciclo (ej. borrar hasta 10 archivos por hora).
  - *Ventajas*: Desalojo gradual.
  - *Desventajas*: En situaciones críticas (como 3.77% vs 10%), tomaría días salir de la zona de alarma y se corre el riesgo de saturación total antes de recuperar el control.

### Decisión 3: Robustecimiento del Mecanismo de Cerrojo y Aislamiento de Permisos
- **Opción A (Seleccionada)**: Estrategia de Defensa en Profundidad:
  1. *Aislamiento de Ruta (`obtener_ruta_cerrojo`)*: Priorizar `$PROJECT_LOCAL_ROOT/tmp-files/gestor_archivos_acq.lock` (directorio con permisos garantizados `rsa:rsa`), con fallback a `/tmp/gestor_archivos_acq_${USER}.lock`.
  2. *Desacoplamiento Estricto de Excepciones*: Capturar `BlockingIOError` (concurrencia legítima $\to$ estado `"ocupado"`), distinguiéndolo de `PermissionError` (emisión explícita en `sys.stderr` y estado `"error_permiso"`).
  3. *Manejo en `main()`*: Emitir aviso limpio y salir con código 0 ante `"ocupado"`; emitir mensaje de error crítico y salir con código 1 ante `"error_permiso"`.
  4. *Saneamiento Proactivo*: Añadir `rm -f /tmp/gestor_archivos_acq*.lock` y `rm -f "$PROJECT_LOCAL_ROOT/tmp-files/gestor_archivos_acq.lock"` dentro de `registrocontinuo.sh stop`.
  - *Ventajas*: Erradica completamente los deadlocks por permisos entre `root` y `rsa`. Diagnóstico inequívoco ante problemas de acceso al disco.
  - *Desventajas*: Modifica la firma interna de `adquirir_cerrojo` (mitigado con bandera `return_status=False` para retrocompatibilidad).
- **Opción B**: Forzar únicamente la limpieza del lockfile en Bash mediante `sudo rm -f` en `registrocontinuo.sh`.
  - *Ventajas*: No requiere cambios en la lógica interna de Python.
  - *Desventajas*: Frágil ante ejecuciones manuales por terminal, caídas abruptas de energía o reinicios de sistema donde el lockfile de `root` permanezca en `/tmp`.

### Decisión 4: Gobernanza de Retención de Binarios `.dat`
- **Opción A (Seleccionada)**: Reducir la retención de binarios `continuous` a 7 días en la política `online` dentro de `configuracion_dispositivo.json.template` (manteniendo 30 días para `mseed` y `event`).
  - *Ventajas*: Reduce en ~7.7 GB la huella en disco. 7 días de binarios brutos es margen suficiente para contingencias de conversión a miniSEED, mientras que el formato sismológico estándar miniSEED preserva el registro de 30 días con una huella de solo ~3.0 GB.
  - *Desventajas*: Menor histórico local de archivos binarios propietarios `.dat`.
- **Opción B**: Mantener 30 días de binarios y delegar la poda exclusivamente al control de espacio.
  - *Ventajas*: Mantiene 1 mes de archivos binarios originales.
  - *Desventajas*: La estación operaría perpetuamente en el límite de saturación (90% - 95% de uso de disco), eliminando el margen de seguridad ante desconexiones prolongadas.

---

## Decisión

Se adopta de forma integral la **Opción A** en los cuatro componentes evaluados:

1. **Corrección de Salvaguarda**: Se eliminó la referencia a `archivo_mas_reciente_continuous` en las líneas 830, 864 y 930 de `gestor_archivos_acq.py`, utilizando exclusivamente `if os.path.basename(ruta_archivo) in archivos_activos_continuous: continue`.
2. **Desalojo Dinámico Iterativo**: Se sustituyó el `break` incondicional en el umbral mínimo por reevaluación dinámica tras cada archivo borrado (`if free_space >= umbral_minimo: break`) y se blindó el bloque con captura estructurada de excepciones (`try...except Exception as e:`).
3. **Robustecimiento del Cerrojo de Proceso**:
   - Se implementó `obtener_ruta_cerrojo()` ubicando el lockfile en `$PROJECT_LOCAL_ROOT/tmp-files/gestor_archivos_acq.lock` con fallback por usuario en `/tmp/`.
   - Se segregó `BlockingIOError` de `PermissionError`, adaptando `main()` para finalizar con código 1 y advertencia inequívoca si se deniegan permisos.
   - Se añadió purga proactiva de lockfiles en `registrocontinuo.sh stop`.
4. **Gobernanza de Retención**: Se estandarizó la retención de archivos `continuous` a 7 días en la política `online` de `configuracion_dispositivo.json.template`.
5. **Certificación y Cobertura Automatizada**: Se amplió la suite unitaria en `test_gestor_archivos_acq.py` a 10 pruebas automatizadas, cubriendo exclusión mutua, discriminación de permisos en flock, resolución de rutas y simulación de desalojo dinámico.

---

## Consecuencias

### Positivas
- **Inmunidad ante Deadlock Operativo**: La apertura y adquisición del lockfile es inmune a colisiones de permisos generadas por ejecuciones accidentales con `sudo`.
- **Estabilidad de Espacio en Almacenamiento**: Las estaciones acelerográficas recuperan de forma reactiva y determinista el umbral de seguridad institucional ($\ge 10\%$ libre / > 1.5 GB disponibles) ante cualquier acumulación.
- **Protección Garantizada de la Adquisición SPI**: El archivo en grabación activa por el dsPIC33 nunca es eliminado durante el desalojo de emergencia.
- **Dimensionamiento Sostenible**: El uso promedio de almacenamiento para datos sismológicos se reduce de ~13.25 GB a ~5.55 GB (~48% de la partición `/dev/root`), permitiendo a las estaciones operar más de 60 días sin conexión sin riesgo de llenado de disco.
- **Validación Exitosa en Producción**: Certificado en `DEV00`, logrando evacuar 106 binarios `.dat` antiguos, recuperando el disco de 3.77% a 10.0% libre y purgando 297 registros huérfanos de `uploaded_files_registry.json`.

### Negativas / Deuda Técnica
- **Ajuste Manual en Estaciones Existentes**: Las estaciones ya desplegadas en campo (`CHA01`, `CHA02`, `TES00`) requieren actualizar manualmente `configuracion_dispositivo.json` a 7 días de `continuous` o aplicar el script de actualización `update.sh`.

---

## Referencias

- Diagnóstico técnico de origen: [`docs/analysis/2026-10-19_diagnostico_acumulacion_archivos_y_fallo_control_espacio_drive.md`](../analysis/2026-10-19_diagnostico_acumulacion_archivos_y_fallo_control_espacio_drive.md)
- Blueprint de arquitectura e implementación: [`docs/blueprints/2026-10-19_plan_correccion_acumulacion_y_robustecimiento_gestor_sync.md`](../blueprints/2026-10-19_plan_correccion_acumulacion_y_robustecimiento_gestor_sync.md)
- ADR Precursor: [ADR-024: Blindaje del Pipeline de Subida a Google Drive contra Livelock de Memoria, Concurrencia Descontrolada y Bufferbloat](024_blindaje_pipeline_drive_livelock_memoria_concurrencia.md)
