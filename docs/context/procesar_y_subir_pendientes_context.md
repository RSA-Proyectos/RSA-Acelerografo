---
proyecto: acelerografo
tipo: contexto_tecnico
archivo: scripts/operation/drive/procesar_y_subir_pendientes.py
temas: [drive, conversion, mseed, dns, resiliencia, retro-presion, contingencias, flock, almacenamiento]
generado: 2026-10-14
---
# procesar_y_subir_pendientes.py — Contexto para Agentes IA

> Herramienta desacoplada, secuencial e idempotente para conversión local (.dat a .mseed), subida resiliente a Google Drive, control de flujo por cupo dinámico y políticas de liberación de almacenamiento (--delete-uploaded, --purge-dat) en acelerógrafos RSA.

**Ruta**: `scripts/operation/drive/procesar_y_subir_pendientes.py` (ejecutable mediante wrapper `/usr/local/bin/procesarpendientes`)  
**LOC**: ~710 | **Lenguaje**: Python 3.9+ | **Dependencias**: `obspy`, `google-api-python-client`, `google-auth`, `fcntl`, `socket`  
**Proceso**: Invocado bajo demanda vía SSH, por crontab horario desatendido o remotamente por MQTT. Opera bajo cerrojo atómico no bloqueante `fcntl.flock` en `/tmp/procesar_y_subir_pendientes.lock`.

---

## 🏛️ Arquitectura y Flujo de Ejecución

El script está diseñado bajo el principio de **Defensa en Profundidad** y desacoplamiento para resolver contingencias operativas (como caídas prolongadas de DNS corporativo o acumulaciones de miles de archivos binarios) sin saturar la memoria RAM ni provocar desborde de almacenamiento en disco:

```mermaid
flowchart TD
    Inicio([Inicio /usr/local/bin/procesarpendientes]) --> Lock{¿Adquirir flock?}
    Lock -- "No (Ocupado)" --> SalirLock["[AVISO] Instancia previa en curso.<br/>Salida limpia exit 0 (Anti-livelock)"]
    Lock -- "Sí (Exclusivo)" --> CheckDNS{¿--solo-convertir?}
    
    CheckDNS -- "No" --> DNSProbe{¿DNS a Google OK?<br/>oauth2.googleapis.com}
    CheckDNS -- "Sí" --> Fase1
    
    DNSProbe -- "Fallo DNS" --> FailFast["[FAIL-FAST DNS]<br/>Aborta preventivamente.<br/>Protege 100% disco y RAM"]
    DNSProbe -- "Nominal" --> ColaDisco["Escanear .mseed y eventos en disco<br/>obtener_cola_subida_drive()"]
    
    ColaDisco --> CalcCupo["Cálculo Cupo Dinámico:<br/>cupo = max(0, batch_size - en_disco)"]
    CalcCupo --> Fase1["[FASE 1] Conversión Local .dat ──> .mseed<br/>(Idempotente, protege archivo activo SPI,<br/>acotado por max_convertir=cupo)"]
    
    Fase1 --> Fase2["[FASE 2] Subida Dosificada a Google Drive<br/>(Lote de batch_size, pausas anti-bufferbloat,<br/>opción --delete-uploaded)"]
    
    Fase2 --> CheckPurge{¿Flag --purge-dat?}
    CheckPurge -- "Sí" --> Fase3["[FASE 3] Purga de Binarios .dat<br/>(Elimina solo los .dat cuyo .mseed consta<br/>como subido en uploaded_files_registry.json)"]
    CheckPurge -- "No" --> Fin([Fin y liberación flock])
    Fase3 --> Fin
```

---

## ⚙️ Configuraciones y Variables de Entorno

- **`PROJECT_LOCAL_ROOT`**: Ruta base del proyecto en la Raspberry Pi (por defecto `/home/rsa/projects/acelerografo`).
- **`LOCK_FILE_PATH`**: Archivo de cerrojo exclusivo `/tmp/procesar_y_subir_pendientes.lock`.
- **`configuracion_dispositivo.json`**:
  - `drive.carpetas.mseed_id`: ID de carpeta en Google Drive para registros continuos.
  - `drive.carpetas.events_id`: ID de carpeta en Google Drive para eventos sísmicos disparados.
  - `directorios.registro_continuo`: Ruta local de archivos `.dat` (`/home/rsa/data/registro-continuo/`).
  - `directorios.archivos_mseed`: Ruta local de archivos `.mseed` (`/home/rsa/data/mseed/`).
  - `directorios.eventos_extraidos`: Ruta local de eventos extraídos (`/home/rsa/data/eventos/`).
- **`configuracion_mseed.json`**: Configuración de cabeceras miniSEED, código de estación (`CODIGO(1)`) y formato temporal de archivo.
- **`uploaded_files_registry.json`**: Registro JSON de archivos confirmados en Google Drive para trazabilidad e idempotencia en la nube.

---

## 🧩 Componentes y Funciones Clave

| Elemento / Función | Responsabilidad Técnica |
|---|---|
| `adquirir_cerrojo()` / `liberar_cerrojo()` | Gestiona el bloqueo atómico no bloqueante mediante `fcntl.flock(lock_fd, fcntl.LOCK_EX \| fcntl.LOCK_NB)`. Si otra instancia corre, aborta instantáneamente (`exit 0`), erradicando duplicación de procesos en crontab. |
| `verificar_dns_drive(host, port, timeout)` | Comprueba resolución DNS real (`socket.getaddrinfo`) hacia `www.googleapis.com:443` con timeout de 5s. Evita falsos positivos de conectividad IP pura sin DNS. |
| `obtener_archivos_activos(local_root, path_rc)` | Identifica el archivo `.dat` en grabación activa continua (inspeccionando `NombreArchivoRegistroContinuo.tmp` y mtime reciente) para salvaguardarlo incondicionalmente de conversiones o purgas. |
| `obtener_cola_subida_drive(cfg, log_dir)` | Construye la lista de archivos pendientes de subir a Drive inspeccionando `uploaded_files_registry.json`, otorgando prioridad estricta a eventos sísmicos sobre continuos. |
| `convertir_archivos_pendientes(...)` | **Fase 1**: Convierte secuencialmente archivos `.dat` a `.mseed` con ObsPy. Es 100% idempotente (omite si el `.mseed` ya existe) y respeta el cupo estricto `max_convertir` con `gc.collect()` tras cada conversión. |
| `subir_archivos_pendientes(...)` | **Fase 2**: Autenticación OAuth2 y subida dosificada por lotes (`--batch-size`) con pausas anti-bufferbloat (1.5s), fail-fast si se cae la red a mitad de lote y soporte para `--delete-uploaded`. |
| `purgar_binarios_respaldados(...)` | **Fase 3**: Escanea archivos `.dat` y elimina físicamente solo aquellos cuyo `.mseed` equivalente ya consta confirmado en `uploaded_files_registry.json`. Protege el archivo activo. |
| `main()` | CLI con banderas `--solo-convertir`, `--solo-subir`, `--dry-run`, `--batch-size`, `--all`, `--delete-uploaded`, `--purge-dat`, `--verbosity`. |

---

## 🕹️ Parámetros por Línea de Comandos (CLI)

```bash
# Ejecución estándar (cupo dinámico de 20 por defecto):
procesarpendientes

# Ejecución recomendada en crontab (lote de 30 con liberación total de espacio):
procesarpendientes --batch-size 30 --delete-uploaded --purge-dat

# Solo conversión local (sin tocar red ni exigir DNS):
procesarpendientes --solo-convertir --batch-size 30

# Solo subida de archivos ya convertidos en disco:
procesarpendientes --solo-subir --batch-size 30

# Simulación completa sin alterar archivos en disco ni subir a Drive:
procesarpendientes --batch-size 30 --delete-uploaded --purge-dat --dry-run
```

---

## ⚠️ Limitaciones Conocidas y Buenas Prácticas

1. **Dependencia de CPU ARM**: La conversión miniSEED en Raspberry Pi toma ~0.4 a 1.0 segundos por archivo horario. En crontab se recomienda dosificar en lotes de 20 a 30 archivos (`--batch-size 30`) para no sobrecargar el SoC.
2. **Desfase en Crontab**: Se recomienda programar en el minuto 05 de cada hora (`5 * * * *`) para evitar colisiones de E/S en el segundo exacto `00:00` en que el proceso C rota el archivo continuo.
3. **Persistencia del Wrapper**: El script debe ejecutarse siempre a través de `/usr/local/bin/procesarpendientes` para asegurar la carga del entorno virtual Python (`$PROJECT_LOCAL_ROOT/.venv`).
