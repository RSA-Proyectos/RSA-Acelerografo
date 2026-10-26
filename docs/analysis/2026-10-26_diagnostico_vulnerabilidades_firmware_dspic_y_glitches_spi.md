---
proyecto: acelerografo
tipo: diagnostico_tecnico
resolucion: pendiente
temas: [firmware_dspic, glitches_spi, condicion_carrera, ausencia_dma, sincronizacion_rtc, adxl355]
fecha: 2026-10-26
---

# Diagnóstico Técnico: Vulnerabilidades de Concurrencia en Firmware dsPIC33EP y Causa Raíz Física de Glitches SPI en Adquisición Acelerográfica

**Fecha**: 2026-10-26  
**Proyecto / Repositorio**: `RSA-Acelerografo` (`montajes/acelerografo-DEV00`)  
**Componente(s) afectado(s)**: `scripts/firmware/firmware_dspic.c`, `scripts/firmware/TIEMPO_RTC.c`, `scripts/operation/mseed/binary_to_mseed.py`  
**Estado**: Diagnosticado  
**Severidad**: Alta  

---

## 1. Resumen Ejecutivo

Durante el desarrollo y certificación de la **Fase 6** de refactorización del motor de conversión `binary_to_mseed.py` (acelerógrafo de la estación `CHA02`), se identificaron anomalías sistemáticas en los archivos binarios crudos:
1. Glitches de bus SPI con repetición idéntica de bytes en cabeceras de tiempo (`0x04 0x04 0x04 0x04 0x04 0x04`), generando marcas de tiempo inválidas del tipo `04:04:04` en archivos de la tarde (ej. hora 17 en `CHA2_260928-170000.dat`).
2. Outliers cronológicos transitorios que provocaban falsos cruces de día y gaps artificiales de 24 horas (inflando archivos de 8.6 MB a más de 81 MB y generando livelocks de CPU).

Si bien la Fase 6 blindó exitosamente el software de procesamiento en la Raspberry Pi mediante filtros heurísticos vectorizados ($O(N)$), la causa raíz física permanecía sin documentar. 

La inspección exhaustiva del firmware del microcontrolador adquisidor (**dsPIC33EP256MC202** a 80 MHz) reveló que estas anomalías no son producto de fallos aleatorios de hardware, sino de **defectos arquitectónicos críticos de concurrencia y diseño de firmware**:
* **Ausencia de doble buffer (Ping-Pong)**: Un único buffer global de 2506 bytes es accedido concurrentemente por temporizadores, la señal SQW de 1 Hz y la lectura SPI esclavo de la RPi.
* **Sobrecarga de interrupciones SPI byte a byte sin DMA**: Transferir cada segundo requiere 2,506 interrupciones por software en modo esclavo, provocando sub-ejecuciones y desbordamientos del registro `SPI1BUF` bajo condiciones de jitter o contención de prioridad.
* **Invocación de E/S bloqueante y retardos dentro de ISRs**: Tareas pesadas de reordenamiento de memoria y accesos SPI al ADXL355 se ejecutan dentro de la interrupción externa INT1, mientras el bucle principal `main()` permanece 100% ocioso.
* **Congelamiento de fecha en cruce de medianoche**: La variable `horaSistema` cicla a 0 al alcanzar las 24 horas, pero `fechaSistema` nunca se incrementa en el bucle continuo del firmware.

Este diagnóstico formaliza la vinculación directa entre la arquitectura del firmware y las anomalías observadas en campo, estableciendo los requerimientos para una refactorización de grado sismológico institucional.

---

## 2. Estado Actual

| Componente | Estado Operativo | Observaciones |
|---|---|---|
| `scripts/firmware/firmware_dspic.c` | ⚠️ Degradado / Frágil | Arquitectura monolítica guiada 100% por interrupciones sin doble buffer ni DMA. |
| `scripts/firmware/TIEMPO_RTC.c` | ⚠️ Degradado | Función `IncrementarFecha()` implementada pero huérfana (nunca invocada en régimen continuo). |
| Bus SPI1 (dsPIC Esclavo $\to$ RPi Maestro) | ⚠️ Frágil | Modo byte a byte en interrupción `spi_1`; susceptible a desincronización de reloj y bytes duplicados. |
| Bus SPI2 (dsPIC Maestro $\to$ ADXL355 + DS3234) | ✅ Operativo | Lectura de FIFO a 100 ms y sincronización de 1 Hz funcionales, pero ejecutadas sincrónicamente en ISR. |
| `scripts/operation/mseed/binary_to_mseed.py` | ✅ Operativo (Mitigado) | Fase 6 filtra en software los glitches `0x04` y descarta outliers cronológicos. |

---

## 3. Evidencia y Análisis

### Rutas analizadas
* Código fuente de firmware:
  * [`scripts/firmware/firmware_dspic.c`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/firmware/firmware_dspic.c)
  * [`scripts/firmware/TIEMPO_RTC.c`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/firmware/TIEMPO_RTC.c)
  * [`scripts/firmware/ADXL355_SPI.c`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/firmware/ADXL355_SPI.c)
* Código fuente de procesamiento:
  * [`scripts/operation/mseed/binary_to_mseed.py`](file:///home/rsa/git/montajes/acelerografo-DEV00/scripts/operation/mseed/binary_to_mseed.py)
* Archivo patológico observado en campo: `/home/rsa/data/test-bin/CHA2_260928-170000.dat`
* Transición técnica previa: [`docs/progress/2026-10-26_contexto-agente.md`](file:///home/rsa/git/montajes/acelerografo-DEV00/docs/progress/2026-10-26_contexto-agente.md)

---

### Extractos Relevantes del Código y Correlación Física

#### A. Buffer Global Único sin Protección Atómica (Condición de Carrera)

En `firmware_dspic.c`:
```c
// firmware_dspic.c:32
unsigned char tramaCompleta[2506] = {0}; // Buffer único global
```

Este arreglo es mutado concurrentemente por:
1. `Timer1Int()` (prioridad 2, cada 100 ms): escribe muestras del ADXL355 en `tramaCompleta[contFIFO + contMuestras + x]`.
2. `Muestrear()` invocado desde `int_1()` (prioridad 2, cada 1 segundo): inyecta cabecera y escribe la estampa de tiempo en `tramaCompleta[2500..2505]`.
3. `spi_1()` (prioridad 3): lee de `tramaCompleta[i]` y coloca el byte en `SPI1BUF` a solicitud del maestro RPi.

**Correlación con el problema observado**:
Cuando la RPi atiende la interrupción `RP1` e inicia la transferencia de 2506 bytes, la lectura no es instantánea. Si el pulso de 1 Hz del RTC o el desbordamiento de Timer1 ocurren mientras la RPi lee la cola de la trama (bytes 2400 a 2505), el dsPIC sobreescribe las variables `tiempo` y las posiciones finales de `tramaCompleta` en pleno vuelo. Esto genera estampas de tiempo inconsistentes y tramas híbridas (parte del segundo $N$ y parte del segundo $N+1$).

---

#### B. Sobrecarga de Interrupciones en SPI1 Esclavo y Sub-ejecución del Buffer

En `firmware_dspic.c`:
```c
// firmware_dspic.c:408-418
// Rutina de lectura de los datos del acelerometro (C:0xA3   F:0xF3):
if ((banLec == 1) && (buffer == 0xA3))
{
   banLec = 2; // Activa la bandera de lectura
   i = 0;
   SPI1BUF = tramaCompleta[i];
}
if ((banLec == 2) && (buffer != 0xF3))
{
   SPI1BUF = tramaCompleta[i];
   i++;
}
```

* Cada byte transferido genera una interrupción de CPU `spi_1`. A 250 Hz, 2506 bytes equivalen a **2,506 interrupciones por segundo**.
* A un reloj SPI de 2 MHz generado por la Raspberry Pi, llega un pulso de clock cada $0.5\ \mu\text{s}$ (un byte cada $4\ \mu\text{s}$).
* Con una velocidad de 40 MIPS (ciclo de instrucción de $25\ \text{ns}$), $4\ \mu\text{s}$ equivalen a solo **160 ciclos de instrucción**.
* Si la interrupción `spi_1` se retrasa unos pocos ciclos debido al guardado de contexto o si coincide con la ejecución de `Timer1Int`, el registro de transmisión `SPI1BUF` **no se actualiza a tiempo** antes de que el shift-register de hardware empiece a desplazar datos.

**Correlación con los glitches `0x04 0x04 0x04...`**:
El valor `0x04` en el acelerógrafo corresponde a la constante `DRDY_OFF` (`0x04`) del registro `POWER_CTL` o a residuos en el bus cuando ocurre sub-ejecución (`underflow`) de la FIFO de hardware del módulo SPI. Al no haber cargado el nuevo byte de tiempo, el hardware repite el byte anterior en el bus MISO. Al repetirse en las 6 posiciones de tiempo, se produce la estampa sintomática:
$$\text{año}=04, \quad \text{mes}=04, \quad \text{día}=04, \quad \text{hora}=04, \quad \text{minuto}=04, \quad \text{segundo}=04$$

---

#### C. Código Bloqueante y Retardos dentro de Interrupciones

En `firmware_dspic.c`:
```c
// firmware_dspic.c:511-530
void int_1() org IVT_ADDR_INT1INTERRUPT // ISR de 1 Hz del RTC
{
   INT1IF_bit = 0;
   if (banSetReloj == 1)
   {
      horaSistema++;
      if (horaSistema == 86400) { horaSistema = 0; }
      if (banInicio == 1)
      {
         Muestrear(); // <-- LLAMADA PESADA DENTRO DE ISR
      }
   }
}
```
Y dentro de `Muestrear()`:
```c
// firmware_dspic.c:288-296
for (x = 0; x < numSetsFIFO; x++) {
   ADXL355_read_FIFO(datosLeidos); // Lecturas síncronas SPI2 bloqueantes
   ...
}
InterrupcionP1(0XB1); // Genera pulso con Delay_us(20) bloqueante
```

Mientras la CPU está atrapada ejecutando decenas de transacciones SPI síncronas en `int_1`, cualquier petición SPI proveniente de la Raspberry Pi queda bloqueada o desfasada.

---

#### D. Defecto de Manejo de Calendario en Cruces de Medianoche

En `firmware_dspic.c`:
```c
// firmware_dspic.c:521-524
if (horaSistema == 86400)
{
   horaSistema = 0; // Se reinicia a medianoche, pero fechaSistema NO cambia
}
```
En `TIEMPO_RTC.c` existe la función:
```c
// TIEMPO_RTC.c:169
unsigned long IncrementarFecha(unsigned long longFecha) { ... }
```
Sin embargo, **no existe ninguna llamada a `IncrementarFecha` en todo `firmware_dspic.c`**. 

**Correlación con desfases cronológicos**:
A las 00:00:00, el microcontrolador pasa a la hora 0 manteniendo congelada la fecha del día previo. Solo cambia de fecha si la Raspberry Pi envía explícitamente el comando `0xA4` o si se solicita forzosamente una relectura del RTC (`0xA6`). Si la RPi se retrasa en resincronizar, el archivo binario recibe tramas con fecha del día anterior mezcladas con hora 00, detonando la lógica de cruce de día en `binary_to_mseed.py`.

---

## 4. Hallazgos y Causa Raíz

### Hallazgo 10 (H10): Sobrecarga de interrupciones SPI esclavo y sub-ejecución del buffer de transmisión (Causa de Glitches `0x04`)
* **Descripción técnica**: La interfaz SPI1 en modo esclavo atiende cada byte entrante mediante una interrupción de software (`spi_1`). A frecuencias de bus estándar, la latencia de respuesta de la ISR supera la ventana temporal disponible bajo contención de interrupciones, provocando que el hardware repita bytes residuales (`0x04`) en la línea MISO.
* **Causa**: No utilizar el periférico DMA nativo del dsPIC33EP para descargar la transferencia SPI de la CPU.
* **Condiciones de activación**: Lecturas continuas de tramas de 2506 bytes a velocidades de SPI superiores a 1 MHz mientras concurren interrupciones del Timer1 o INT1.

### Hallazgo 11 (H11): Condición de carrera por buffer global único sin ping-pong
* **Descripción técnica**: `tramaCompleta[2506]` es un buffer único compartido donde coexisten escrituras periódicas del sensor y lecturas del host.
* **Causa**: Falta de arquitectura de doble buffer (buffer de trabajo vs buffer de transmisión).
* **Condiciones de activación**: Coincidencia temporal entre el pulso del RTC (1 Hz) y la lectura activa del maestro SPI.

### Hallazgo 12 (H12): Invocación de E/S bloqueante y bucles dentro de ISRs
* **Descripción técnica**: La ISR `int_1` ejecuta lectura de FIFO, transferencias SPI2 hacia el acelerómetro, formateo de 250 muestras y retardos artificiales (`Delay_us`), dejando el bucle principal desaprovechado.
* **Causa**: Diseño monolítico reactivo sin separación entre contexto de interrupción y contexto de ejecución de tareas.
* **Condiciones de activación**: Ocurre en cada segundo de adquisición.

### Hallazgo 13 (H13): Ausencia de verificación de integridad a nivel de enlace (Sin CRC)
* **Descripción técnica**: La trama binaria de 2506 bytes carece de checksum, paridad o CRC (CRC16/CRC32).
* **Causa**: Omisión de protocolo de control de redundancia en el diseño del formato de trama.
* **Condiciones de activación**: Cualquier ruido de acople electromagnético, rebote o desincronización de reloj pasa inadvertido al nivel de enlace físico y corrompe los datos crudos en disco.

### Hallazgo 14 (H14): Congelamiento de fecha por omisión de incremento en cruce de medianoche
* **Descripción técnica**: Al llegar a `horaSistema == 86400`, el reloj del día vuelve a 0 pero `fechaSistema` permanece estática.
* **Causa**: La función `IncrementarFecha()` quedó huérfana en `TIEMPO_RTC.c` y nunca fue conectada al reinicio de día en `firmware_dspic.c`.
* **Condiciones de activación**: Operación ininterrumpida que cruce las 23:59:59 sin comando externo de sincronización.

---

## 5. Evaluación de Riesgo

| # | Escenario de Riesgo | Probabilidad | Impacto | Mitigación Requerida |
|---|---|---|---|---|
| **R10** | Corrupción silenciosa de datos acelerográficos por carreras de memoria en `tramaCompleta`. | Alta | Alto | Implementar doble buffer (Ping-Pong) con intercambio atómico de punteros. |
| **R11** | Glitches recurrentes en cabeceras de tiempo que evadan filtros heurísticos de software. | Media | Crítico | Migrar la transmisión SPI1 a DMA o incorporar CRC32 en la trama. |
| **R12** | Colgado indefinido del microcontrolador por bloqueo en SPI2 sin Watchdog. | Baja | Crítico | Habilitar el Watchdog Timer (WDT) del dsPIC y timeouts por hardware en SPI. |
| **R13** | Inconsistencia de fechas en catálogos sismológicos por cruce no sincronizado de medianoche. | Media | Alto | Incrementar automáticamente `fechaSistema` en el desbordamiento de 86400 s. |

---

## 6. Opciones y Decisiones de Diseño

### Decisión: Estrategia de Mitigación y Evolución del Firmware

| Opción | Descripción | Ventajas | Desventajas |
|---|---|---|---|
| **A: Mitigación Exclusiva en Software (Raspberry Pi)** | Mantener el firmware actual en las estaciones desplegadas y continuar refinando los filtros heurísticos en `binary_to_mseed.py` (Fase 6 actual). | Cero intervención física en campo; no requiere programadores ni apertura de gabinetes. | La CPU de la RPi gasta ciclos en saneamiento; no resuelve la raíz física; datos gravemente dañados se pierden. |
| **B: Refactorización Parcial del Firmware Existente** | Modificar `firmware_dspic.c` para incorporar doble buffer ping-pong, incrementar fecha a medianoche y mover el I/O pesado al `main()`. | Resuelve las condiciones de carrera principales con bajo esfuerzo de desarrollo; mantiene la estructura actual. | Sigue usando interrupciones byte a byte en SPI1; no explota al 100% el hardware. |
| **C: Reescritura Arquitectónica Completa (Firmware v2.0)** | Rediseñar el firmware utilizando **SPI Slave con DMA**, doble buffer ping-pong, **CRC-32 al final de la trama** (2510 bytes), WDT activo y máquina de estados limpia. | Máxima robustez industrial y sismológica; transferencia SPI transparente con 0% carga de CPU; verificación instantánea de integridad. | Requiere campaña de reprogramación de microcontroladores (PicKit/ICD) y adaptación menor en el script receptor. |

> **Recomendación Estratégica**:
> 1. **Corto Plazo (Operación Actual)**: Mantener la **Opción A** (Fase 6 de `binary_to_mseed.py` ya certificada para 24/25 archivos) para garantizar la continuidad operativa de la red sin detener estaciones en campo.
> 2. **Mediano Plazo (Próxima Versión de Hardware / Mantenimiento Mayor)**: Implementar la **Opción C** (Firmware v2.0) en laboratorio, homologando una nueva versión de firmware libre de glitches por diseño.

---

## 7. Mitigaciones Aplicadas

* **En Firmware**: Ninguna aplicada aún — el código analizado permanece en su versión histórica para preservar compatibilidad con las estaciones en servicio.
* **En Software de Procesamiento (`binary_to_mseed.py`)**:
  * **Filtro de Glitches SPI (`0x04 0x04...`)**: Implementado en Fase 6 para purgar tramas con bytes repetidos e impedir su inyección en la traza sismológica.
  * **Filtro de Coherencia Cronológica**: Pasada vectorial que suprime saltos horarios anómalos aislados.
  * **Acotamiento de Medianoche**: Se restringe la suma de 86400 s a la ventana estricta de las 23h $\to$ 00h.

---

## 8. Backlog de Mejoras

1. **[F1] Implementar Doble Buffer (Ping-Pong) en dsPIC**:
   Declarar `unsigned char tramaBuffer[2][2506]`. Un buffer acumula los datos del segundo en curso mientras el otro permanece invariante para la lectura de la Raspberry Pi.
2. **[F2] Migrar Transmisión SPI1 a DMA**:
   Configurar el canal DMA0 del dsPIC33EP en modo transferencia de memoria a periférico (`SPI1BUF`), activado por el evento de inicio de lectura de la RPi, eliminando las 2506 interrupciones por segundo.
3. **[F3] Reestructurar Ejecución a Máquina de Estados en `main()`**:
   Reducir las rutinas de interrupción (`int_1`, `Timer1Int`) a banderas de un ciclo (`flag_segundo_listo = 1`), ejecutando la lectura de la FIFO y el formateo en el bucle principal.
4. **[F4] Vincular `IncrementarFecha()` al Cruce de 86400 s**:
   Llamar a `fechaSistema = IncrementarFecha(fechaSistema)` cuando `horaSistema == 86400`, eliminando el congelamiento de fecha en cruces de día autónomos.
5. **[F5] Integrar Checksum CRC-16 / CRC-32 en Trama**:
   Añadir campos de redundancia cíclica al final de la trama para validación hardware/software determinista.
6. **[F6] Activar Watchdog Timer (WDT)**:
   Configurar el fusible de WDT con refresco en el bucle principal para auto-recuperación ante bloqueos de bus SPI.

---

## 9. Dependencias y Prerrequisitos

| Prerrequisito | Estado | Acción Requerida |
|---|---|---|
| Entorno de compilación MPLAB X / XC16 o MikroC | ⬜ Por verificar | Confirmar compilador original utilizado para compilar `firmware_dspic.c`. |
| Programador hardware (Microchip PICkit 3 / 4 o ICD) | ⬜ Requerido | Necesario para flashear el dsPIC33EP en banco de pruebas. |
| Banco de pruebas en laboratorio con dsPIC + ADXL355 + DS3234 | ⬜ Requerido | Validar con osciloscopio / analizador lógico antes de desplegar a estaciones remotas. |

---

## 10. Plan de Validación para Firmware v2.0

| # | Checkpoint | Criterio de Éxito |
|---|---|---|
| **CP-1** | Inspección en Osciloscopio / Analizador Lógico | Clock SPI1 a 4 MHz sin sub-ejecución ni bytes residuales en MISO durante transferencias continuas. |
| **CP-2** | Carga de CPU con DMA | dsPIC33EP con utilización de CPU $< 10\%$ durante adquisición continua a 250 Hz. |
| **CP-3** | Cruce de Medianoche Continuo | Prueba sintética de 48 horas continuas en banco verificando incremento autónomo de fecha a las 00:00:00 sin comandos RPi. |
| **CP-4** | Integridad CRC en Receptor Python | Tasa de tramas corruptas recibidas por la RPi reducida a 0.00% en condiciones nominales. |
