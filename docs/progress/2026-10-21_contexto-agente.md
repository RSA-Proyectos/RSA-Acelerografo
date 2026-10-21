# Resumen de Sesión: Diseño del Plan de Contingencia de Conversión Offline, Empaquetado Diario y Exportación SFTP

**Fecha**: 2026-10-21  
**Repositorio**: `acelerografo-DEV00` (`RSA-Acelerografo`)  
**Agente de IA**: Antigravity  
**Usuario**: Milton  

---

## 🎯 Objetivo de la Sesión

Formular, diseñar y formalizar la arquitectura de un plan de contingencia y transporte sismológico para evacuar con seguridad y sin desbordamiento de almacenamiento los datos retenidos en las estaciones acelerográficas remotas `CHA01` y `CHA02`. Estas estaciones presentan una retención crítica de archivos debido a bloqueos en el servidor DNS corporativo (`192.170.100.250`) que impiden la resolución de dominios de Google Drive, previa a la implementación definitiva del sistema de ingesta HTTPS por VPS.

Se establecieron los requerimientos de refactorización para `procesar_y_subir_pendientes.py` (unificación de flags en inglés, desacople del DNS en conversión, purga autónoma de binarios por existencia local de MiniSEED y guarda preventiva de espacio libre), se diseñó la herramienta dedicada de empaquetado diario `empaquetar_archivos.py` (con wrapper `/usr/local/bin/empaquetar`, cerrojo atómico `flock`, checksums SHA-256 estándar y soporte para `crontab` en estaciones offline), y se redactó el blueprint formal de implementación en `docs/blueprints/`.

---

## 📂 Estructura del Repositorio Implementada

```text
acelerografo-DEV00/
├── docs/
│   ├── blueprints/
│   │   ├── 2026-10-20_plan_arquitectura_ingesta_egress_https_vps.md          # Arquitectura definitiva a VPS revisada
│   │   └── 2026-10-21_plan_contingencia_conversion_y_empaquetado_sftp.md     # [NUEVO] Blueprint formal de contingencia
│   └── progress/
│       ├── 2026-10-19_contexto-agente.md                                     # Contexto de sesión previa
│       └── 2026-10-21_contexto-agente.md                                     # [NUEVO] Documento de transición técnica
```

---

## ⚙️ Configuración del Entorno Virtual (`.venv`)

- **Ruta del entorno**: `/home/rsa/projects/acelerografo/.venv/` (Python 3.9+ en Raspberry Pi OS ARM).
- **Módulos y Primitivas del Sistema Involucradas**:
  - `tarfile`: Creación, compresión `gzip` y verificación de integridad previa a la eliminación de fuentes.
  - `hashlib`: Generación de sumas de verificación SHA-256 en formato estándar GNU (`.sha256`).
  - `fcntl.flock`: Bloqueo atómico no bloqueante (`LOCK_EX | LOCK_NB`) para exclusión mutua de procesos en ejecuciones manuales o por `crontab`.
  - `shutil.disk_usage`: Verificación y estimación de cuotas de disco libre previo a la compresión y conversión.
- **Rutas de Cerrojado y Paquetes**:
  - Lockfile de empaquetado: `$PROJECT_LOCAL_ROOT/tmp-files/empaquetar.lock` (fallback `/tmp/empaquetar_${USER}.lock`).
  - Directorio destino de paquetes: `/home/rsa/data/packages/` (propiedad `rsa:rsa`).

---

## 🛠️ Modificaciones de Código y Decisiones Técnicas

### 1. Diagnóstico de Bloqueos en `CHA01` / `CHA02` y Análisis de Flags Actuales
- Se inspeccionaron en profundidad los 8 flags actuales de `procesar_y_subir_pendientes.py` (`--solo-convertir`, `--solo-subir`, `--batch-size`, `--all`, `--delete-uploaded`, `--purge-dat`, `--dry-run`, `--verbosity`).
- **Hallazgo 1**: El *fail-fast* DNS inicial evaluaba `verificar_dns_drive()` antes de la Fase 1, abortando todo el proceso en `CHA01`/`CHA02` si no se pasaba explícitamente `--solo-convertir`.
- **Hallazgo 2**: `--purge-dat` requería que el archivo figurara como subido en `uploaded_files_registry.json`. Como Drive no respondía, no se purgaba ningún `.dat`, acumulando gigabytes brutos en la partición `/dev/root`.

### 2. Decisiones de Refactorización en `procesar_y_subir_pendientes.py`
- **Unificación a Inglés**: Sustitución de `--solo-convertir` por `--convert-only` y `--solo-subir` por `--upload-only` (con alias retrocompatibles para scripts existentes).
- **Desacople DNS**: La comprobación DNS se mueve exclusivamente al inicio de la Fase 2 (subida). La Fase 1 (conversión) siempre se ejecuta de forma local y offline.
- **Purga Local de Binarios (`--purge-dat`)**: Se elimina el `.dat` en cuanto su `.mseed` equivalente exista en local con tamaño $> 0$ y no esté en grabación activa. Esto genera una **ganancia neta de $\approx 9.8\text{ MB}$ por hora procesada** (~14 MB liberados vs ~4.2 MB del MiniSEED).
- **Guarda Preventiva de Espacio**: Si el disco tiene $< 10\%$ libre (< 500 MB) y no está activa la purga simultánea, se aborta la conversión para proteger el pipeline de adquisición SPI de C.

### 3. Decisiones de Diseño para el Módulo de Empaquetado (`empaquetar_archivos.py`)
- **Procesamiento de Tipo Único**: Mutuamente excluyente (`--mseed` [default], `--continuous`, `--events`, `--logs`) para máxima simplicidad, control y trazabilidad.
- **Destino Centralizado**: Directorio predeterminado `/home/rsa/data/packages/`.
- **Filtro Temporal**: `--date YYYY-MM-DD`, `--start-date`/`--end-date`, o `--all-days`.
- **Protección de Día en Curso**: En `--all-days`, se omite automáticamente el día actual para no generar paquetes incompletos, salvo uso explícito de `--include-today`.
- **Aptitud para `crontab` en Estaciones Offline**: Se integró cerrojo `fcntl.flock` no bloqueante, permitiendo automatizar la compresión diaria nocturna en estaciones desatendidas para ahorrar un 40% de almacenamiento y reducir inodos.
- **Salvaguardas Críticas**:
  - **Bloqueo estricto de `--delete-source` en `--logs`**: Protege incondicionalmente los archivos de log abiertos por Supervisor y los daemons activos.
  - **Verificación de Integridad Previa**: Antes de borrar archivos con `--delete-source`, el script prueba la apertura y lectura del `.tar.gz` con `tarfile`.
  - **Estimación de Espacio**: Aborta la compresión de un día si el espacio libre no cubre el estimado más un margen de seguridad de 500 MB.
- **Formato Checksum GNU**: Emisión de `<paquete>.tar.gz.sha256` estandarizado para validación masiva con `sha256sum -c *.sha256`.

### 4. Integración de Ayuda en `ayuda.sh`
- Se planificó la incorporación de las instrucciones de uso para `procesarpendientes` y `empaquetar` en [`scripts/task/ayuda.sh`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/task/ayuda.sh).

---

## 📋 Pasos Sugeridos para el Siguiente Agente

Para iniciar la ejecución del plan en la siguiente sesión:

1. **Crear y Conmutar a la Rama Git de Trabajo**:
   ```bash
   cd /home/rsa/git/montajes/acelerografo-DEV00
   git checkout -b feature/contingencia-extraccion-sftp
   ```

2. **Ejecutar la Fase 1 (Refactorización de `procesar_y_subir_pendientes.py`)**:
   - Renombrar flags a `--convert-only` y `--upload-only` manteniendo compatibilidad.
   - Reubicar la llamada `verificar_dns_drive()` exclusivamente al bloque de subida.
   - Actualizar `purgar_binarios_respaldados()` para comprobar existencia local del `.mseed`.
   - Incorporar la función de guarda de espacio libre (`shutil.disk_usage`).
   - Ejecutar suite unitaria `test_procesar_y_subir_pendientes.py` y actualizar casos de prueba.

3. **Ejecutar la Fase 2 (Implementación de `empaquetar_archivos.py`)**:
   - Crear [`scripts/operation/export/empaquetar_archivos.py`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/export/empaquetar_archivos.py).
   - Implementar el cerrojo `flock`, la CLI con `argparse`, filtros por fecha y reglas de seguridad.
   - Crear y certificar la suite de pruebas unitarias [`scripts/operation/export/test_empaquetar_archivos.py`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/export/test_empaquetar_archivos.py).

4. **Ejecutar la Fase 3 y 4 (Wrapper del Sistema y Ayuda)**:
   - Crear [`scripts/task/empaquetar.sh`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/task/empaquetar.sh) e integrarlo en [`scripts/setup/deploy.sh`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/setup/deploy.sh).
   - Actualizar [`scripts/task/ayuda.sh`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/task/ayuda.sh).

5. **Validar y Desplegar en Campo (`CHA01` / `CHA02`)**:
   - Guiar al usuario con la secuencia copy-paste de evacuación manual y descarga SFTP documentada en la Fase 5 del blueprint.
