# Conciliación Bancaria WLG — App Streamlit

App para que el equipo de administración concilie SAP vs extracto bancario
sin necesitar Claude en el momento. El motor es Python puro (sin IA);
el aprendizaje pasa por el archivo de feedback, que se revisa periódicamente.

## Estructura

```
conciliacion_app/
├── app.py              # Interfaz Streamlit (4 pestañas)
├── motor.py             # Motor de conciliación (toda la lógica de cruce)
├── excel_export.py       # Generador del Excel de 7 solapas
├── feedback.py           # Registro y aplicación de correcciones manuales
├── requirements.txt
├── feedback/
│   └── feedback.jsonl   # Se crea solo — acá quedan las correcciones
└── data/                # Vacío, por si querés guardar CSVs de referencia
```

## Correr localmente

```bash
pip install -r requirements.txt
streamlit run app.py
```

Abre en `http://localhost:8501`.

## Cómo se usa (para el equipo)

1. **Cargar** — subir MAYOR.csv, EXTRACTO.csv, indicar el saldo banco de cierre
2. **Resultado** — ver el cuadro de control y las estadísticas de cruce
3. **Corregir** — para cada caso MÚLTIPLE, elegir el banco correcto o confirmar
   que no tiene contraparte; también se puede marcar un cruce automático
   como incorrecto, o reportar un patrón nuevo
4. **Descargar** — bajar el Excel de 7 solapas (RESUMEN, PENDIENTES, CRUCES,
   REVERSIONES, GASTOS BANCARIOS, MAYOR, EXTRACTO)

Cada corrección de la pestaña 3 queda en `feedback/feedback.jsonl`. La
próxima vez que alguien concilie, esas correcciones específicas
(cruces manuales confirmados y "sin cruce" confirmados) se aplican
automáticamente antes de correr el motor.

## Cómo mejora el motor con el tiempo

La app **no llama a ninguna IA en tiempo real** — todo el cruce es determinístico.
El feedback es la materia prima para mejorar `motor.py`:

1. Cada tanto (semanal o mensual), revisá `feedback/feedback.jsonl`
2. Fijate especialmente los registros de tipo `cruce_incorrecto` y `patron_nuevo`
   — esos son los que señalan algo que el motor hizo mal o no sabía manejar
3. Traé ese archivo a una conversación con Claude (o a Claude Code) y decile
   qué encontraste. Claude te ayuda a traducir eso en una regla nueva o una
   corrección al motor
4. Actualizás `motor.py` con la mejora, la subís, y desde ahí todo el equipo
   se beneficia sin que nadie tenga que volver a corregir el mismo caso

Los registros de tipo `cruce_manual` y `sin_cruce_confirmado` ya se aplican
solos (son correcciones puntuales, caso por caso). Los de `cruce_incorrecto`
y `patron_nuevo` necesitan ojo humano/IA porque normalmente implican
generalizar una regla, no solo resolver un caso.

## Desplegar para que el equipo lo use sin instalar nada

**Opción rápida — Streamlit Community Cloud (gratis):**
1. Subí esta carpeta a un repo de GitHub (puede ser privado)
2. Entrá a share.streamlit.io, conectá el repo, apuntá a `app.py`
3. Te da una URL pública (o restringida por login de Google) que el equipo
   abre desde el navegador — nada que instalar

**Opción Microsoft 365 — Azure App Service o Azure Container Apps:**
Si preferís quedarte en el ecosistema que ya usa WLG, se puede containerizar
esta app (un Dockerfile simple) y desplegarla en Azure, con autenticación
vía Entra ID si hace falta restringir el acceso al equipo de administración.

**Nota sobre feedback.jsonl en producción:** si desplegás en la nube, ese
archivo necesita vivir en un storage persistente (no en el filesystem
efímero del contenedor) — por ejemplo un Azure Blob, o una base de datos
simple (SQLite en un volumen persistente, o una tabla en SharePoint/Excel
Online vía Microsoft Graph si querés mantenerlo 100% dentro de M365).
