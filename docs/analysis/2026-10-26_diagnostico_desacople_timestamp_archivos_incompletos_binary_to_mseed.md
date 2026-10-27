---
proyecto: acelerografo
tipo: diagnostico_tecnico
resolucion: resuelto
temas: [binary_to_mseed, desacople_timestamp, corrupcion_trama, archivos_truncados, sincronizacion_sismica]
fecha: 2026-10-26
---

# Diagnóstico Técnico: Desacople de Timestamp y Pérdida de Metadatos de Fecha por Truncamiento en `binary_to_mseed.py`

**Fecha**: 2026-10-26  
**Proyecto / Repositorio**: `RSA-Acelerografo` (`montajes/acelerografo-DEV00`)  
**Componente(s) afectado(s)**: `scripts/operation/mseed/binary_to_mseed.py` (`leer_archivo_binario`, `extraer_timestamp_completo_binario`, `procesar_archivo_individual`, `obtenerTraza`)  
**Estado**: Diagnosticado  
**Severidad**: Alta  

---

## 1. Resumen Ejecutivo

Durante la campaña de validación masiva en lote sobre los 25 archivos binarios reales de la estación `CHA02` almacenados en `/home/rsa/data/test-bin` (tras la implementación de las Fases 1 a 6 de refactorización de gaps, dispersión $O(N)$ y purga de glitches SPI), 24 archivos fueron procesados con total éxito. Dichas conversiones exhibieron tiempos de ejecución sub-segundo (~0.9 s a 1.0 s por hora nominal en Raspberry Pi 3B+), duraciones exactas de 1.00 h ($3,599.996$ s) con $900{,}000$ muestras, y una erradicación completa de livelocks de CPU e inflaciones dimensionales.

No obstante, se identificó un fallo aislado en el archivo `CHA2_261001-150002.dat`:
```text
Procesando /home/rsa/data/test-bin/CHA2_261001-150002.dat...
Error: Error inesperado al procesar CHA2_261001-150002.dat: 'anio'
```

El análisis forense determinó que este archivo coincide con una caída o corte abrupto de energía en la estación sísmica durante la hora 15 (la estación cesó de registrar y recién reinició a las 16:06:48). El apagón provocó que la última trama del archivo quedara truncada en disco. La función auxiliar `extraer_timestamp_completo_binario` intentó leer la última trama mediante un salto ciego al final del archivo (`f.seek(-2506, 2)`), fallando con una excepción interna en `datetime(...)` que devolvió `(None, None)` para `(ts_inicio, ts_final)`. 

Al recibir `ts_inicio is None`, `leer_archivo_binario` omitió inyectar las llaves `'anio'`, `'mes'` y `'dia'` en el diccionario `tiempo_binario_validado`, a pesar de que cientos de tramas válidas con fecha completa estampada por hardware estaban disponibles en memoria. Al propagarse este diccionario incompleto hacia `obtenerTraza`, se detonó un `KeyError: 'anio'`.

Desde la perspectiva sismológica institucional, el principio rector es tajante: **es mejor no tener datos a tener datos erróneos o con tiempo aproximado**. Una pérdida de sincronización de tiempo destruye la coherencia de fase, la relocalización hipocentral y el cálculo de fuentes sísmicas. Este diagnóstico establece las bases técnicas para desacoplar la validación de tiempo de la trama final rota y extraer la fecha legítima directamente de las tramas válidas del digitalizador sin suposiciones artificiales.

---

## 2. Estado Actual

| Componente / Archivo | Estado Operativo | Observaciones |
|---|---|---|
| `binary_to_mseed.py` (motor $O(N)$) | ✅ Operativo | 24/25 archivos de CHA02 convierten en ~0.9s sin livelock ni gaps artificiales de 24h. |
| `leer_archivo_binario()` | ⚠️ Degradado | Valida tramas en memoria, pero delega la fecha de `tiempo_binario_validado` a `ts_inicio`. |
| `extraer_timestamp_completo_binario()` | ❌ Frágil | Asume que la última trama siempre existe y es válida; falla ante cortes de luz y retorna `None`. |
| `procesar_archivo_individual()` | ⚠️ Degradado | Sobrescribe `tiempo_para_conversion` con un diccionario que puede carecer de fecha. |
| `obtenerTraza()` | ⚠️ Bloqueado | Detona `KeyError: 'anio'` si el diccionario de tiempo no contiene los campos de fecha. |

---

## 3. Evidencia y Análisis

### Rutas analizadas
* Logs de ejecución en DEV00: `/home/rsa/git/logs.tmp` (L156-157).
* Archivo patológico: `/home/rsa/data/test-bin/CHA2_261001-150002.dat`.
* Código fuente: `scripts/operation/mseed/binary_to_mseed.py` (L270-295, L390-460, L512-520, L620-630).

### Extractos relevantes de código

#### A. La omisión condicional de fecha en `leer_archivo_binario`
```python
# scripts/operation/mseed/binary_to_mseed.py:279-291
tiempo_binario_validado = {
    "hora": h_val, "hora_s": str(h_val).zfill(2),
    "minuto": m_val, "minuto_s": str(m_val).zfill(2),
    "segundo": s_val, "segundo_s": str(s_val).zfill(2),
    "n_segundo": primer_tiempo_valido
}
# Si ts_inicio es None, NO se agregan anio, mes, dia:
if ts_inicio is not None:
    tiempo_binario_validado.update({
        "anio": ts_inicio.year, "anio_s": str(ts_inicio.year),
        "mes": ts_inicio.month, "mes_s": str(ts_inicio.month).zfill(2),
        "dia": ts_inicio.day, "dia_s": str(ts_inicio.day).zfill(2),
    })
```

#### B. La fragilidad de `extraer_timestamp_completo_binario` ante tramas finales truncadas
```python
# scripts/operation/mseed/binary_to_mseed.py:430-459
f.seek(-2506, 2)  # Seek desde el final del archivo
ultima_trama = np.fromfile(f, dtype=np.uint8, count=2506)
if ultima_trama.size < 2506:
    return None, None

# Si la estación se apagó a mitad de escritura, los bytes 2500-2505 contienen basura:
timestamp_final = datetime.datetime(anio_final, mes_final, dia_final,
                                   hora_final, minuto_final, segundo_final)
# Cualquier valor fuera de rango lanza ValueError:
except Exception:
    return None, None  # <-- Retorna (None, None), anulando también ts_inicio
```

#### C. El detonante en `procesar_archivo_individual` y `obtenerTraza`
```python
# scripts/operation/mseed/binary_to_mseed.py:626
# Como tiempo_binario_validado es un diccionario con hora/minuto/segundo, evalúa a True:
tiempo_para_conversion = tiempo_binario_validado if tiempo_binario_validado else tiempo_binario

# scripts/operation/mseed/binary_to_mseed.py:513
def obtenerTraza(..., tiempo_binario, ...):
    anio = tiempo_binario["anio"]  # <-- KeyError: 'anio'
```

### Cronología del Evento
1. **2026-10-01 15:00:02**: La estación `CHA02` inicia la grabación del archivo horario `CHA2_261001-150002.dat`.
2. **2026-10-01 ~15:xx:xx**: Ocurre un corte abrupto de energía en la estación. El archivo en disco queda con un tamaño no múltiplo exacto de 2506 bytes o con la última trama incompleta.
3. **2026-10-01 16:06:48**: Se restablece la energía; la estación arranca y genera `CHA2_261001-160648.dat` (segmento de 39 segundos).
4. **2026-10-23**: Durante el procesamiento en lote en DEV00, 24 archivos completan la conversión. El archivo `CHA2_261001-150002.dat` aborta con `KeyError: 'anio'`.

---

## 4. Hallazgos y Causa Raíz

### Hallazgo 8 (H8): Desacople y dependencia de `ts_inicio` externo para la fecha validada
* **Descripción técnica**: `leer_archivo_binario` lee y valida rigurosamente cada una de las tramas del archivo (verificando que año, mes y día estén en rangos válidos), pero no extrae los campos de fecha de las tramas válidas en memoria (`chunk_valido[0]`). En su lugar, condiciona la existencia de `'anio'`, `'mes'` y `'dia'` a que la función auxiliar `extraer_timestamp_completo_binario` retorne un objeto `datetime` no nulo.
* **Causa**: Acoplamiento innecesario entre la extracción de fecha canónica y una función secundaria diseñada para metadatos de inicio/fin.
* **Condiciones de activación**: Ocurre siempre que un archivo binario finalice abruptamente por corte de energía o caída del proceso adquisidor, dejando la última trama física corrupta o truncada.

### Hallazgo 9 (H9): Captura silenciosa global y fallo destructivo en `extraer_timestamp_completo_binario`
* **Descripción técnica**: La función `extraer_timestamp_completo_binario` ejecuta un bloque `try...except Exception` que, ante un fallo en la última trama, descarta también la información de la primera trama (que frecuentemente es 100% válida), retornando `(None, None)` en bloque.
* **Causa**: Falta de aislamiento entre la lectura de la primera trama y la lectura de la última trama.
* **Condiciones de activación**: Archivos incompletos con cabecera inicial válida pero cola corrupta.

---

## 5. Evaluación de Riesgo

| # | Escenario de Riesgo | Probabilidad | Impacto | Mitigación Requerida |
|---|---|---|---|---|
| **R7** | Pérdida de eventos sísmicos legítimos por descarte de archivos truncados tras cortes de luz. | Media | Alto | Extraer la fecha y hora de la primera trama válida en memoria sin depender de la cola del archivo. |
| **R8** | Corrupción de catálogos sismológicos por asignación de tiempo erróneo o aproximado. | Baja | Crítico | **Principio de no aproximación**: si una trama tiene cabecera inválida, se purga. Si el archivo no tiene tramas válidas, se rechaza. |
| **R9** | Generación de MiniSEED con timestamps de medianoche o año desfasado por reloj RTC descargado. | Baja | Crítico | Implementar validación cruzada (sanity check) entre estampa de hardware y nombre de archivo. |

---

## 6. Opciones y Decisiones de Diseño

### Decisión: Fuente de Fecha y Criterio de Aceptación/Rechazo de Tiempo

| Opción | Descripción | Ventajas | Desventajas |
|---|---|---|---|
| **A: Extracción Estricta desde Primera Trama Válida** | Extraer `año, mes, día` directamente de `chunk_valido[0, 2500:2503]`. Descartar tramas individuales corruptas al inicio o fin. | 100% basado en hardware real. Preserva los datos válidos previos a un corte de energía sin suposiciones. | Requiere que al menos 1 trama tenga fecha válida. |
| **B: Validación Cruzada (Hardware vs Nombre de Archivo)** | Usar Opción A para el tiempo, pero comparar la fecha obtenida contra la fecha del nombre del archivo como *sanity check*. | Detecta fallos de reloj RTC de hardware (ej. RTC reseteado al año 2000). Máxima seguridad sismológica. | Requiere estandarización estricta del nombre del archivo en la red. |
| **C: Tolerancia Cero (Rechazo Total de Archivos Truncados)** | Rechazar cualquier archivo que tenga tramas corruptas al inicio o al final. | Pureza matemática absoluta en la base de datos. | Se pierden todos los datos sísmicos previos a un corte eléctrico (pérdida de eventos sísmicos reales). |

> **Recomendación consensuada**: Implementar la **Opción A** complementada con la regla de validación de la **Opción B**:
> 1. El tiempo de conversión se toma **únicamente** de la primera trama que haya pasado los filtros de integridad (`chunk_valido[0]`).
> 2. Las tramas corruptas (iniciales o finales) se descartan estrictamente; nunca se inventan ni aproximan muestras de tiempo.
> 3. Si no existe ninguna trama válida en el archivo, se rechaza el archivo (`status: FAIL - sin_tramas_validas`).
> 4. Si la fecha de la trama discrepa del nombre del archivo (ej. RTC desincronizado con año fuera de rango), se rechaza con alerta de desincronización de reloj.

---

## 7. Mitigaciones Aplicadas

* **Autonomía total de fecha en `leer_archivo_binario`**: Se extrae `anio`, `mes` y `dia` de forma autónoma desde el nombre del archivo (si `usar_fecha_filename=True`) o de la primera trama física válida (`chunk_valido[0]`), asegurando que `tiempo_binario_validado` siempre contenga la estructura completa requerida por `obtenerTraza`.
* **Aislamiento en `extraer_timestamp_completo_binario`**: Se separaron las lecturas de la primera y última trama en bloques `try/except` independientes. Un truncamiento abrupto en la cola por corte de energía no invalida el timestamp de inicio ni la conversión del segmento válido.
* **Resiliencia en `procesar_archivo_individual` y CLI**: Se eliminó el rechazo prematuro de archivos por ruido en la trama cero física y se corrigió el enrutamiento de salida en el modo `--dir`.
* **Certificación con Test T12 y Lote Real**: Se añadió el test unitario `test_archivo_truncado_por_apagon_conserva_fecha_t12` en `test_binary_to_mseed.py` (13/13 tests OK) y se certificó el 100% de conversión exitosa (30/30 archivos) sobre el dataset `/home/rsa/data/test-bin`.

---

## 8. Backlog de Mejoras

1. **[M1] Autonomía de fecha en `leer_archivo_binario`**:
   Construir `tiempo_binario_validado` extrayendo `anio`, `mes` y `dia` directamente de la primera trama válida (`chunk_valido[0]`), eliminando la dependencia de `ts_inicio`.
2. **[M2] Desacople y aislamiento en `extraer_timestamp_completo_binario`**:
   Separar la obtención de `ts_inicio` y `ts_final` en bloques independientes para que un fallo en la última trama truncada no invalide el inicio del archivo.
3. **[M3] Purga estricta de tramas truncadas de cola**:
   Garantizar que si el archivo termina con bytes residuales ($< 2506$ bytes) por corte de energía, estos se descarten limpiamente sin afectar la conversión del segmento válido.
4. **[M4] Test Unitario T12 en `test_binary_to_mseed.py`**:
   Crear un test que inyecte un archivo sintético con tramas válidas pero con una última trama deliberadamente cortada/corrupta, verificando que la conversión retorne las trazas válidas con fecha completa y sin excepciones `KeyError`.

---

## 9. Dependencias y Prerrequisitos

| Prerrequisito | Estado | Acción Requerida |
|---|---|---|
| Fases 1 a 6 de `binary_to_mseed.py` | ✅ Completadas | Base de código refactorizada y funcional en DEV00. |
| Archivo patológico `CHA2_261001-150002.dat` | ✅ Disponible | Disponible en `/home/rsa/data/test-bin` para validación. |
| Suite de tests sintéticos (T1 a T11) | ✅ Operativa | Base para incorporar el test T12. |

---

## 10. Plan de Validación Futuro

| # | Checkpoint | Criterio de Éxito |
|---|---|---|
| **CP-1** | Test sintético T12 | Ejecutar `.venv/bin/python3 scripts/mseed/test_binary_to_mseed.py` y validar 13 tests OK. |
| **CP-2** | Conversión de `CHA2_261001-150002.dat` | El archivo convierte sin `KeyError: 'anio'` y genera MiniSEED con las tramas registradas hasta el corte de luz. |
| **CP-3** | Procesamiento del lote completo (25/25) | Todos los archivos de `/home/rsa/data/test-bin` convierten exitosamente (100% de tasa de éxito). |
