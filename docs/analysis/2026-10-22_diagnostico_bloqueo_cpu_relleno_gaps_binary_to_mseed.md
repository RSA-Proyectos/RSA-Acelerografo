---
proyecto: RSA-Acelerografo
tipo: diagnostico_tecnico
resolucion: pendiente
temas: [mseed, conversion, binary_to_mseed, rendimiento_cpu, livelock]
fecha: 2026-10-22
---

# Diagnóstico Técnico: Bloqueo de CPU y Livelock en Conversión de Binarios con Gaps Temporales (binary_to_mseed)

**Fecha**: 2026-10-22  
**Proyecto / Repositorio**: `RSA-Acelerografo`  
**Componente(s) afectado(s)**: `scripts/operation/mseed/binary_to_mseed.py` (`obtenerTraza`), `scripts/operation/drive/procesar_y_subir_pendientes.py`  
**Estado**: Diagnosticado (mitigado con cuarentena de archivos afectados)  
**Severidad**: Alta  

---

## 1. Resumen Ejecutivo

Durante la ejecución del proceso de conversión masiva offline en la estación acelerográfica `CHA02` (`procesarpendientes --convert-only --all`), el flujo de procesamiento se detuvo indefinidamente en el primer archivo de la serie (`CHA2_261001-000000.dat`), superando con creces el tiempo nominal de conversión (habitualmente $< 5$ segundos por archivo) sin arrojar errores ni excepciones.

Al interrumpir el proceso mediante señal `SIGINT` (`Ctrl+C`), la traza de ejecución expuso que el hilo se encontraba atrapado en la función `obtenerTraza()` de `binary_to_mseed.py`, específicamente en una llamada a `np.insert()` dentro de un bucle iterativo que intenta rellenar con ceros los segundos faltantes (*gaps* temporales) del archivo binario.

La complejidad algorítmica $O(M \cdot N)$ de reasignar y clonar en memoria arreglos de cientos de miles de muestras en una CPU ARM con recursos limitados (Raspberry Pi con 0.9 GB de RAM) generó un livelock de CPU. El aislamiento provisional del día con *gaps* confirmó que el resto de archivos continuos procesan nominalmente.

---

## 2. Estado Actual

| Componente | Estado | Observaciones |
|---|---|---|
| `procesar_y_subir_pendientes.py` | ✅ Operativo | Flujo de control, cerrojos flock y detección offline funcionan nominalmente. |
| `binary_to_mseed.py` (Archivos continuos sin gaps) | ✅ Operativo | Convierte en $< 5$ s cuando no existen segundos faltantes (`segundos_faltantes is None`). |
| `binary_to_mseed.py` (Archivos con gaps) | ❌ Inoperativo / Bloqueante | Congela la CPU al ejecutar `np.insert()` en bucle cuando hay segundos faltantes. |
| Archivos del día 2026-10-01 (`CHA02`) | ⚠️ En Cuarentena | Aislados temporalmente en carpeta de cuarentena para permitir el avance del backlog. |

---

## 3. Evidencia y Análisis

* **Ruta de los recursos analizados**:
  - `scripts/operation/mseed/binary_to_mseed.py` (Líneas 444–464)
  - `scripts/operation/drive/procesar_y_subir_pendientes.py` (Líneas 298–306)
  - Registro de terminal en `logs.tmp`

* **Extracto del Traceback de Interrupción**:
  ```text
  rsa@CHA02:~/data/registro-continuo $ procesarpendientes --convert-only --all
  ...
  [FASE 1] Conversión Local de Archivos Binarios a miniSEED:
    Archivo(s) activo(s) en grabación protegidos: CHA2_261021-210000.dat
    [CONVIRTIENDO] CHA2_261001-000000.dat ──> CHA2_20261001_000000.mseed ...^CTraceback (most recent call last):
    File "/home/rsa/projects/acelerografo/scripts/drive/procesar_y_subir_pendientes.py", line 828, in <module>
      main()
    File "/home/rsa/projects/acelerografo/scripts/drive/procesar_y_subir_pendientes.py", line 740, in main
      res_conv = convertir_archivos_pendientes(
    File "/home/rsa/projects/acelerografo/scripts/drive/procesar_y_subir_pendientes.py", line 298, in convertir_archivos_pendientes
      exito, mensaje, info = procesar_archivo_individual(
    File "/home/rsa/projects/acelerografo/scripts/mseed/binary_to_mseed.py", line 497, in procesar_archivo_individual
      conversion_mseed_digital(nombre_archivo_mseed, path_archivo_salida, tiempo_binario, datos_archivo_binario, segundos_faltantes, config_mseed, logger)
    File "/home/rsa/projects/acelerografo/scripts/mseed/binary_to_mseed.py", line 391, in conversion_mseed_digital
      trazaCH1 = obtenerTraza(nombre, 1, datos_archivo_binario[0], tiempo_binario, segundos_faltantes, parametros_mseed)
    File "/home/rsa/projects/acelerografo/scripts/mseed/binary_to_mseed.py", line 457, in obtenerTraza
      data_completo = np.insert(data_completo, indice_muestra_faltante, lista_ceros)
  KeyboardInterrupt
  ```

* **Código Defectuoso Identificado en `binary_to_mseed.py` (Líneas 445–458)**:
  ```python
  if segundos_faltantes is not None:
      segundo_inicio = (horas * 3600) + (minutos * 60) + segundos
      muestras_por_segundo = fsample
      lista_ceros = np.zeros(muestras_por_segundo, dtype=np.int32)
      npts_completo = len(data) + int(len(segundos_faltantes) * muestras_por_segundo)
      data_completo = np.zeros(npts_completo, dtype=np.int32)
      data_completo[:len(data)] = data
      stats['npts'] = npts_completo

      for segundo_faltante in segundos_faltantes:
          tiempo_muestra_faltante = int(segundo_faltante - segundo_inicio)
          indice_muestra_faltante = tiempo_muestra_faltante * muestras_por_segundo
          data_completo = np.insert(data_completo, indice_muestra_faltante, lista_ceros)
  ```

---

## 4. Hallazgos y Causa Raíz

### Hallazgo 1: Inserción no vectorizada y reasignación cuadrática en NumPy
* **Descripción técnica**: En NumPy, la función `np.insert()` **no opera *in-place*** sobre la memoria existente; en cada llamada aloca un arreglo completamente nuevo y copia todos los elementos previos y posteriores al punto de inserción.
* **Causa**: Al ejecutarse dentro del bucle `for segundo_faltante in segundos_faltantes:`, si un archivo contiene por ejemplo $M = 2,500$ segundos faltantes sobre una traza de $N = 360,000$ muestras a 100 Hz, el código ejecuta 2,500 reasignaciones completas de memoria por cada canal. Como la conversión procesa 3 canales (`trazaCH1`, `trazaCH2`, `trazaCH3`), se realizan $7,500$ reasignaciones masivas de arreglos.
* **Condiciones de activación**: Se manifiesta exclusivamente cuando una estación experimenta pérdida de paquetes, reinicios a mitad de hora o desfases de reloj, generando una lista poblada en `segundos_faltantes`.
* **Impacto**: La CPU de la Raspberry Pi queda saturada al 100% copiando buffers, lo que aparenta un congelamiento permanente del sistema e impide que el gestor de contingencia o el cron procesen los días subsiguientes.

### Hallazgo 2: Inconsistencia dimensional en la pre-asignación
* **Descripción técnica**: En la línea 450 ya se pre-asigna `data_completo` con el tamaño final previsto (`npts_completo = len(data) + len(segundos_faltantes) * fsample`).
* **Causa**: Al hacer luego `np.insert()`, el arreglo crece dinámicamente en 100 muestras adicionales en cada iteración por encima del tamaño ya expandido, desvirtuando el cálculo de `stats['npts']` y desfasando los índices temporales de las muestras siguientes.

---

## 5. Mitigaciones Aplicadas

1. **Aislamiento en Cuarentena**:
   - Se movieron los archivos `.dat` del día 2026-10-01 (que contenían los paquetes con *gaps*) a un directorio de cuarentena fuera de `registro-continuo/`.
   - Con esta acción, el gestor continuó con los días posteriores (2026-10-02 en adelante) sin congelamientos, completando la conversión de más de 450 archivos con éxito.

---

## 6. Backlog de Opciones de Solución para la Siguiente Sesión

Para abordar a profundidad este problema en una sesión posterior, se identifican las siguientes alternativas de refactorización:

1. **Opción A (Asignación Directa por Rebanadas / Slicing $O(N)$ - Recomendada)**:
   - Mantener el pre-dimensionamiento de `data_completo` a `npts_completo` lleno de ceros (`np.zeros(..., dtype=np.int32)`).
   - Escribir los datos válidos en los bloques temporales calculados o mapear directamente los índices sin invocar jamás `np.insert()`.
   - Complejidad: lineal $O(N)$, tiempo de ejecución estimado $< 1$ segundo incluso con miles de segundos faltantes.

2. **Opción B (Manejo Canónico en ObsPy Stream)**:
   - En lugar de rellenar con ceros artificiales forzando una traza única contigua, generar trazas independientes por segmento continuo dentro del mismo `Stream` de ObsPy (`Stream(traces=[...])`).
   - El estándar MiniSEED soporta nativamente múltiples registros con gaps sin necesidad de sintetizar ceros que puedan distorsionar el procesamiento sismológico.

---

## 7. Plan de Validación Futuro

| # | Checkpoint | Criterio de Éxito |
|---|---|---|
| CP-1 | Prueba unitaria con gaps sintéticos | Crear un test unitario con 1,800 segundos faltantes y verificar que `obtenerTraza` complete en $< 0.5$ s. |
| CP-2 | Conversión de archivo real de cuarentena | Procesar `CHA2_261001-000000.dat` con el código refactorizado y comprobar la generación válida de `CHA2_20261001_000000.mseed`. |
