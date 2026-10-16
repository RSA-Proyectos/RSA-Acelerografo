# Resumen de Sesión: Blindaje del Pipeline de Subida a Google Drive contra Livelock de Memoria, Concurrencia Descontrolada y Bufferbloat

**Fecha**: 2026-10-16  
**Repositorio**: `RSA-Acelerografo` (`montajes/acelerografo-DEV00`)  
**Agente de IA**: Antigravity  
**Usuario**: Milton  

---

## 🎯 Objetivo de la Sesión

Resolver y blindar de forma definitiva la sincronización de archivos con Google Drive en el flujo continuo del acelerógrafo (`registrocontinuo.sh`, `gestor_archivos_acq.py`, `subir_archivo.py`), mitigando las condiciones de carrera, la contienda de memoria, el swap thrashing y los cuelgues de red (livelock / bufferbloat) que causaron caídas sistémicas en estaciones de campo de la RSA (`CHA01`, `CHA02`).

Se reutilizaron e integraron los mecanismos probados en la herramienta desacoplada `procesar_y_subir_pendientes.py` para dotar al gestor continuo de cerrojo atómico exclusivo `fcntl.flock`, resolución DNS real, procesamiento acotado por lotes (`batch_size=20`), colas prioritarias (`event` → `mseed` → `continuous`), pausas anti-bufferbloat, recolección explícita de basura `gc.collect()`, protección estricta de binarios activos y reducción del timeout HTTP a 60 segundos. El ciclo culminó con la certificación de la suite de pruebas unitarias automatizada (18 pruebas totales OK en `logs.tmp`), la generación de la documentación de contexto técnico, la formalización del `ADR-024` y la actualización del índice federado institucional, marcando el diagnóstico técnico del 2026-09-30 como resuelto.

---

## 📂 Estructura del Repositorio Implementada

```text
RSA-Acelerografo/
├── configuration/
│   └── configuracion_dispositivo.json.template             # Agregado parámetro batch_size: 20 en drive.config
├── docs/
│   ├── adr/
│   │   └── 024_blindaje_pipeline_drive_livelock_memoria_concurrencia.md # Formalización técnica de la decisión
│   ├── blueprints/
│   │   └── 2026-10-14_plan_blindaje_scripts_livelock_memoria.md         # Blueprint de arquitectura aprobado
│   ├── context/
│   │   ├── gestor_archivos_acq_context.md                  # Contexto actualizado con cerrojo, DNS y lotes
│   │   ├── registrocontinuo_context.md                     # Nuevo contexto técnico del orquestador bash
│   │   ├── subir_archivo_context.md                        # Nuevo contexto técnico del cliente Google Drive API
│   │   └── test_gestor_archivos_acq_context.md             # Nuevo contexto técnico de la suite unitaria
│   └── progress/
│       └── 2026-10-16_contexto-agente.md                   # Documento de transición técnica (este archivo)
└── scripts/
    ├── operation/
    │   └── drive/
    │       ├── gestor_archivos_acq.py                      # Gestor continuo blindado con flock, lotes y DNS
    │       ├── procesar_y_subir_pendientes.py              # Parche de prevención de ResourceWarning en cerrojo
    │       ├── subir_archivo.py                            # Reducción de timeout Http(timeout=60)
    │       └── test_gestor_archivos_acq.py                 # Suite de 6 pruebas unitarias aisladas
    └── task/
        └── registrocontinuo.sh                             # Guarda is_running "gestor_archivos_acq.py"
```

---

## ⚙️ Configuración del Entorno Virtual (`.venv`)

- **Ruta del entorno**: `$PROJECT_LOCAL_ROOT/.venv` (`/home/rsa/projects/acelerografo/.venv`).
- **Versión de Python**: Python 3.9+ en arquitectura ARM (Debian Bullseye / Bookworm).
- **Módulos del Sistema Operativo y Conectividad**:
  - `fcntl`: Para primitivas de cerrojo de archivos POSIX (`flock`).
  - `socket`: Para resolución de nombres mediante `socket.getaddrinfo`.
  - `httplib2`: Cliente HTTP subyacente para Google API Client con timeout configurado a 60 s.
- **Directrices de Ejecución**:
  - Toda ejecución programática y automatizada en crontab se delega a `$PROJECT_LOCAL_ROOT/.venv/bin/python3`, evitando invocar el Python global del sistema y garantizando aislamiento estricto de paquetes.

---

## 🛠️ Modificaciones de Código y Refactorización

### 1. Orquestador Bash (`scripts/task/registrocontinuo.sh`)
- Se implementó la guarda `is_running "gestor_archivos_acq.py"` previa al disparo del gestor en segundo plano (`&`).
- Si un proceso del gestor anterior se encuentra aún activo (debido a latencia de subida o drenaje de backlog), el script omite el lanzamiento concurrente y registra un mensaje informativo, evitando la proliferación de instancias en crontab.

### 2. Gestor Continuo Blindado (`scripts/operation/drive/gestor_archivos_acq.py`)
- **Cerrojo Atómico Exclusivo**: Integración de `adquirir_cerrojo("/tmp/gestor_archivos_acq.lock")` y `liberar_cerrojo()` usando `fcntl.flock(fcntl.LOCK_EX | fcntl.LOCK_NB)`. Toda la ejecución del gestor se canalizó a través de la función `_ejecutar_gestor()`, asegurando la liberación del candado en el bloque `finally:` de `main()`.
- **Verificación DNS Real**: `check_internet_connection()` fue reemplazado para ejecutar `socket.getaddrinfo("www.googleapis.com", 443, timeout=5)`. Elimina falsos positivos ante conexiones cautivas o caídas de DNS.
- **Colas Prioritarias Desacopladas**: Subida estructurada en orden de criticidad sismológica:
  1. Eventos extraídos (`event`, P1).
  2. MiniSEED continuo (`mseed`, P2).
  3. Archivos binarios (`continuous`, P3, opcional según configuración).
- **Procesamiento Acotado por Lotes (`batch_size=20`)**: Limita a un máximo de 20 archivos por categoría en cada invocación horaria. Interrumpe el ciclo de subida de inmediato (*fail-fast*) si se produce un fallo de red.
- **Pausa Anti-Bufferbloat y Gestión de Memoria**: Incorporación de `time.sleep(1.5)` tras cada subida para drenar buffers en enlaces móviles y llamada a `gc.collect()` tras cada lote para liberar memoria acumulada en peticiones SSL.
- **Protección de Archivo Activo C**: `obtener_archivos_activos_continuous()` previene que el binario `.dat` actualmente abierto por `registro_continuo_4.5.0.c` sea borrado o subido antes de tiempo.

### 3. Cliente de Subida a Google Drive (`scripts/operation/drive/subir_archivo.py`)
- Reducción del timeout de red en `get_authenticated()`: se reemplazó `Http(timeout=300)` por `Http(timeout=60)`. Ante pérdidas de paquetes en redes celulares, el socket se interrumpe tras 60 s en lugar de congelar el proceso durante 5 minutos por intento.

### 4. Plantilla de Configuración (`configuration/configuracion_dispositivo.json.template`)
- Incorporación del parámetro `"batch_size": 20` dentro del bloque `"drive": { "config": { ... } }`.

### 5. Parche en `scripts/operation/drive/procesar_y_subir_pendientes.py`
- En `adquirir_cerrojo()`, se aseguró el cierre explícito del descriptor de archivo (`lock_fd.close()`) en caso de colisión (`BlockingIOError` / `IOError`), eliminando advertencias de `ResourceWarning: unclosed file`.

### 6. Suite de Pruebas Unitarias (`scripts/operation/drive/test_gestor_archivos_acq.py`)
- Creación de suite `unittest` con 6 pruebas aisladas (tempfile):
  1. `test_check_internet_connection_exito` (mock DNS exitoso).
  2. `test_check_internet_connection_fallo_dns` (mock gaierror con detección de fallo).
  3. `test_adquirir_y_liberar_cerrojo` (exclusión mutua real de locks en disco).
  4. `test_obtener_archivos_activos_desde_tmp` (lectura de `NombreArchivoRegistroContinuo.tmp`).
  5. `test_obtener_archivos_activos_fallback_mtime` (protección por `mtime < 2h`).
  6. `test_main_cerrojo_ocupado_aborta_limpiamente` (aborto sin ejecución ante contienda).
- **Resultado de Validación**: Ejecutada la suite combinada de 18 pruebas (6 del gestor + 12 de pendientes), certificada con éxito en 1.816s (`logs.tmp`: `Ran 18 tests in 1.816s ... OK`).

---

## 🏛️ Gobernanza Institucional y Decisiones de Arquitectura

1. **ADR-024 Emitido y Aceptado**:
   - Título: *Blindaje del Pipeline de Subida a Google Drive contra Livelock de Memoria, Concurrencia Descontrolada y Bufferbloat*.
   - Ubicaciones: `montajes/acelerografo-DEV00/docs/adr/024_blindaje_pipeline_drive_livelock_memoria_concurrencia.md` y `rsa/RSA-Metodologias/decisiones/024_blindaje_pipeline_drive_livelock_memoria_concurrencia.md`.
2. **Índice Federado Sincronizado**:
   - Actualizado [indice_tematico.md](file:///home/rsa/git/rsa/RSA-Metodologias/indice/indice_tematico.md).
   - El diagnóstico técnico `2026-09-30` (*Saturación crítica de memoria, concurrencia en subidas a Drive y pérdida de acceso remoto en CHA02*) fue marcado formalmente como **resuelto** vinculándolo al ADR-024 y al blueprint de blindaje.

---

## 📋 Pasos Sugeridos para el Siguiente Agente

1. **Confirmar Integración Git**:
   - Verificar si el usuario ya realizó el merge de la rama `fix/livelock` a `develop` según las instrucciones provistas en la sesión.
2. **Preparación de Release Semántico (`v4.5.5`)**:
   - Si se decide desplegar a estaciones en campo, actualizar `CHANGELOG.md` documentando las mejoras de blindaje anti-livelock, cerrojo atómico, timeout de 60 s y pruebas unitarias.
3. **Despliegue a Estaciones Remotas (`CHA01`, `CHA02`)**:
   - Tras desplegar el release, verificar la reactivación limpia del crontab (`crontab -e`: `0 * * * * /bin/bash /home/rsa/projects/acelerografo/scripts/task/registrocontinuo.sh start`).
   - Monitorear durante 24-48 horas la memoria RAM y el drenaje ordenado de archivos en Google Drive sin presencia de duplicados.
4. **Volcado de Bitácora**:
   - Ejecutar la skill `volcado_bitacora` cuando el usuario dé por terminada la sesión para archivar la memoria de trabajo en la bitácora personal de Milton.
