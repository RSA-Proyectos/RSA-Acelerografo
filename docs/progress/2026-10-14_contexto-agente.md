# Resumen de Sesión: Herramienta Desacoplada de Puesta al Día, Control de Flujo con Retro-Presión y Releases v4.5.3 / v4.5.4

**Fecha**: 2026-10-14  
**Repositorio**: `RSA-Acelerografo` (`montajes/acelerografo-DEV00`)  
**Agente de IA**: Antigravity  
**Usuario**: Milton  

---

## 🎯 Objetivo de la Sesión

Resolver la acumulación de archivos binarios `.dat` y miniSEED pendientes generados tras la desactivación preventiva del crontab en estaciones acelerográficas remotas (`CHA01`, `CHA02`), provocada por fallas del DNS corporativo que causaban colapso de memoria RAM en el viejo `gestor_archivos_acq.py`. 

Se diseñó, planificó e implementó una solución modular, desacoplada e idempotente (`procesar_y_subir_pendientes.py` junto a su wrapper `/usr/local/bin/procesarpendientes`), dotada de cerrojo atómico anti-livelock, verificación temprana de DNS, control de flujo por cupo dinámico (retro-presión) y políticas de liberación de almacenamiento (`--delete-uploaded`, `--purge-dat`). El ciclo concluyó con la certificación de pruebas en `DEV00`, el despliegue y evacuación exitosa del backlog en la estación piloto `CHA01`, y la publicación de los releases semánticos oficiales **`v4.5.3`** y **`v4.5.4`**.

---

## 📂 Estructura del Repositorio Implementada

```text
RSA-Acelerografo/
├── docs/
│   ├── blueprints/
│   │   └── 2026-10-13_plan_conversion_y_subida_pendientes.md   # Arquitectura y blueprint de contingencia
│   ├── context/
│   │   └── procesar_y_subir_pendientes_context.md              # Contexto técnico estandarizado para agentes IA
│   └── progress/
│       └── 2026-10-14_contexto-agente.md                       # Documento de transición técnica (este archivo)
├── scripts/
│   ├── operation/
│   │   └── drive/
│   │       ├── procesar_y_subir_pendientes.py                  # Script principal con fases 1, 2 y 3 desacopladas
│   │       └── test_procesar_y_subir_pendientes.py             # Suite de 9 pruebas unitarias automatizadas (DEV0)
│   ├── setup/
│   │   └── update.sh                                           # Blindaje incondicional de permisos rsa:rsa
│   └── task/
│       └── procesarpendientes.sh                               # Wrapper bash de sistema para /usr/local/bin
└── CHANGELOG.md                                                # Sellado de versiones v4.5.3 y v4.5.4
```

---

## ⚙️ Configuración del Entorno Virtual (`.venv`)

- **Ruta del entorno**: `$PROJECT_LOCAL_ROOT/.venv` (`/home/rsa/projects/acelerografo/.venv`).
- **Versión de Python**: Python 3.9+ (arquitectura ARM en Debian Bullseye / Bookworm).
- **Dependencias científicas y de nube**:
  - `obspy`: Conversión y decodificación de datos sísmicos a miniSEED estándar.
  - `google-api-python-client`, `google-auth`, `google-auth-oauthlib`: Autenticación OAuth2 y subida por trozos a Google Drive API v3.
- **Decisión arquitectónica sobre entornos**:
  - Se descartó explícitamente la auto-reinvocación interna de Python (`_asegurar_entorno_virtual`) dentro de los scripts para evitar complejidad innecesaria y procesos hijos ocultos.
  - Siguiendo el estándar de la RSA, los scripts Python se mantienen puros e independientes, delegando la activación del entorno virtual al wrapper de sistema `/usr/local/bin/procesarpendientes` (`procesarpendientes.sh`).

---

## 🛠️ Modificaciones de Código y Decisiones Técnicas

### 1. Desacoplamiento en Tres Fases Independientes
- **Fase 1 (Conversión Local Garantizada)**:
  - Escanea archivos `.dat` acumulados en `data/registro-continuo/`.
  - Salvaguarda incondicionalmente el archivo activo en grabación SPI por el dsPIC33 (reportado en `NombreArchivoRegistroContinuo.tmp` y mtime < 2 horas).
  - Idempotencia total: Comprueba si el archivo `.mseed` ya existe con tamaño > 0; si es así, se omite de inmediato sin consumir CPU.
  - Ejecuta recolección forzada de memoria (`gc.collect()`) tras cada archivo procesado.
- **Fase 2 (Subida Resiliente a Google Drive)**:
  - Comprobación estricta de DNS hacia `oauth2.googleapis.com:443` con timeout de 5s (*fail-fast* inicial). Si el DNS falla, aborta antes de intentar transferencias, impidiendo el bucle de saturación de RAM.
  - Dosificación por lotes (`--batch-size`) con pausas de 1.5s anti-bufferbloat para no saturar enlaces móviles/satelitales.
  - Prioridad de subida estricta: Eventos sísmicos disparados primero, registros continuos después.
  - Registro atómico de archivos subidos en `uploaded_files_registry.json`.
- **Fase 3 (Purga Selectiva de Binarios Respaldados)**:
  - Activada con la bandera `--purge-dat`.
  - Escanea los archivos `.dat` locales y elimina únicamente aquellos cuyo `.mseed` equivalente ya consta confirmado como subido en `uploaded_files_registry.json`. El archivo activo nunca se toca.

### 2. Control de Flujo por Retro-Presión (Backpressure)
- Para evitar que la estación sature su tarjeta SD generando cientos de `.mseed` de golpe ante caídas de red, se implementó el cálculo de **cupo dinámico**:
  $$\text{cupo} = \max(0, \text{batch\_size} - \text{archivos\_mseed\_en\_disco})$$
- Si ya existen suficientes archivos `.mseed` o eventos esperando en disco para llenar el lote, **se omite completamente la conversión de nuevos `.dat`**, forzando al sistema a drenar primero el almacenamiento antes de generar más datos derivados.

### 3. Políticas de Liberación de Almacenamiento
- `--delete-uploaded`: Elimina el `.mseed` local inmediatamente después de recibir confirmación HTTP 200 de Google Drive.
- `--purge-dat`: Elimina el `.dat` original cuando su `.mseed` ya está respaldado en Drive.
- La combinación `--delete-uploaded --purge-dat` permite operar estaciones desatendidas con almacenamiento limitado sin riesgo de llenado de disco.

### 4. Cerrojo Atómico Exclusivo (`flock`)
- Control de concurrencia mediante `fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)` sobre `/tmp/procesar_y_subir_pendientes.lock`.
- Si una ejecución previa aún continúa activa cuando se dispara el cron, la nueva llamada finaliza limpiamente con `exit 0` emitiendo un aviso, sin consumir memoria ni generar procesos zombi.
- Los bloqueos de archivo son liberados automáticamente por el kernel de Linux en caso de fallos o reinicios abruptos.

### 5. Blindaje de Permisos en `update.sh`
- Se agregó saneamiento incondicional `chown -R rsa:rsa` y `chmod -R u+rwX` sobre `$PROJECT_LOCAL_ROOT` y `log-files/` al final de `update.sh`, erradicando bloqueos de `PermissionError` provocados por ejecuciones intermedias con `sudo`.

### 6. Publicación de Versiones Semánticas (SemVer)
- **`v4.5.3` (PATCH)**: Creación de `procesar_y_subir_pendientes.py`, wrapper `procesarpendientes.sh` y blindaje de permisos en `update.sh`.
- **`v4.5.4` (PATCH)**: Incorporación de control de flujo por cupo dinámico, fail-fast DNS previo y banderas `--delete-uploaded` y `--purge-dat`.
- Ambas versiones se promovieron a `main` aislando estrictamente la carpeta `docs/` para no ocupar memoria flash en los equipos de campo.

---

## 📊 Validación Operativa en Campo (`CHA01`)

La ejecución en la estación piloto arrojó resultados concluyentes:
- **Total .dat en disco**: 1,019 archivos.
- **Omitidos por idempotencia**: 745 archivos (ya convertidos).
- **Archivo activo protegido**: `CHA1_261013-170000.dat` (sin interrupción de la adquisición).
- **Archivos convertidos exitosamente**: 270 archivos en el primer lote.
- **Subida inicial a Google Drive**: 30 archivos sincronizados nominalmente.
- **Comprobación de retro-presión**: La segunda ejecución con `--batch-size 10` omitió conversiones y subió exactamente 10 archivos.
- **Automatización en Crontab**: Configurado al minuto 05 de cada hora (`5 * * * *`) con lote seguro de 30 archivos.

---

## 📋 Pasos Sugeridos para el Siguiente Agente

1. **Auditoría de Evacuación en `CHA01`**:
   - Inspeccionar el log de crontab en `CHA01`:
     ```bash
     tail -n 30 /home/rsa/projects/acelerografo/log-files/procesarpendientes_cron.log
     ```
   - Verificar la reducción del número de archivos pendientes en cola hacia Drive hasta alcanzar 0.

2. **Despliegue en la Estación de Producción `CHA02`**:
   - Conectarse por SSH a `CHA02`:
     ```bash
     ssh rsa@CHA02
     cd ~/git/RSA-Acelerografo
     git fetch --tags
     git checkout v4.5.4
     ./menu.sh    # Opción 3 (Actualizar el proyecto)
     ```
   - Ejecutar una simulación inicial:
     ```bash
     procesarpendientes --batch-size 30 --dry-run
     ```
   - Configurar la tarea horaria en crontab (`crontab -e`):
     ```cron
     5 * * * * /usr/local/bin/procesarpendientes --batch-size 30 >> /home/rsa/projects/acelerografo/log-files/procesarpendientes_cron.log 2>&1
     ```

3. **Volcado de Bitácora Personal**:
   - Ejecutar la skill `volcado_bitacora` para registrar formalmente la sesión en [`institucional/RSA-Bitacora-LLM-Milton`](file:///home/rsa/git/institucional/RSA-Bitacora-LLM-Milton/) e indexar los temas en el índice federado.
