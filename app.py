"""
app.py — Conciliación Bancaria WLG
====================================
App Streamlit para que el equipo de administración haga la conciliación
sin necesidad de usar Claude directamente. Todo el motor es Python puro.

Flujo: Cargar archivos -> Ver resultado -> Corregir dudosos -> Descargar Excel
Cada corrección queda guardada en feedback/feedback.jsonl para revisión
periódica y mejora del motor (esa parte sí se hace con ayuda de Claude,
en una sesión aparte, no en tiempo real dentro de la app).

Correr con: streamlit run app.py
"""

import io
import os
import sys
from datetime import datetime

import streamlit as st
import pandas as pd
import yaml
import streamlit_authenticator as stauth
from yaml.loader import SafeLoader

sys.path.insert(0, os.path.dirname(__file__))
import motor
import excel_export
import feedback as fb

st.set_page_config(page_title="Conciliación Bancaria WLG", layout="wide", page_icon="🏦")

# ══════════════════════════════════════════════════════════════════════
# AUTENTICACIÓN — lee desde st.secrets (Streamlit Cloud) o users.yaml (local)
# ══════════════════════════════════════════════════════════════════════
def _cargar_config_auth():
    # En Streamlit Cloud los secrets se cargan desde el dashboard
    try:
        if 'credentials' in st.secrets:
            return {
                'credentials': st.secrets['credentials'].to_dict(),
                'cookie': st.secrets['cookie'].to_dict(),
            }
    except Exception:
        pass
    # Fallback: archivo local para desarrollo
    _users_file = os.path.join(os.path.dirname(__file__), 'users.yaml')
    with open(_users_file) as f:
        return yaml.load(f, Loader=SafeLoader)

_config = _cargar_config_auth()

authenticator = stauth.Authenticate(
    _config['credentials'],
    _config['cookie']['name'],
    _config['cookie']['key'],
    _config['cookie']['expiry_days'],
)

authenticator.login()

if st.session_state.get('authentication_status') is False:
    st.error('Usuario o contraseña incorrectos.')
    st.stop()
elif st.session_state.get('authentication_status') is None:
    st.warning('Ingresá tu usuario y contraseña para continuar.')
    st.stop()

# ══════════════════════════════════════════════════════════════════════
# ESTADO DE SESIÓN
# ══════════════════════════════════════════════════════════════════════
if 'resultado' not in st.session_state:
    st.session_state.resultado = None
if 'banco_nombre' not in st.session_state:
    st.session_state.banco_nombre = 'Credicoop'

st.title("🏦 Conciliación Bancaria")
st.caption("SAP (Mayor) vs Extracto bancario — motor automático + corrección manual")

tab_cargar, tab_resultado, tab_corregir, tab_descargar = st.tabs(
    ["📤 1. Cargar", "📊 2. Resultado", "✏️ 3. Corregir", "⬇️ 4. Descargar"]
)

# ══════════════════════════════════════════════════════════════════════
# TAB 1 — CARGAR
# ══════════════════════════════════════════════════════════════════════
with tab_cargar:
    st.subheader("Subí los dos archivos del período")

    col1, col2 = st.columns(2)
    with col1:
        archivo_mayor = st.file_uploader("MAYOR.csv (export de SAP)", type=['csv'])
    with col2:
        archivo_extracto = st.file_uploader("EXTRACTO.csv (extracto del banco)", type=['csv'])

    col3, col4 = st.columns(2)
    with col3:
        banco_nombre = st.text_input("Nombre del banco", value=st.session_state.banco_nombre)
    with col4:
        saldo_banco = st.number_input(
            "Saldo banco de cierre ($)",
            min_value=0.0, step=0.01, format="%.2f",
            help="Este dato no sale del CSV — es el saldo de cierre que usás para el cuadro de control."
        )

    usuario = st.text_input("Tu nombre (para el registro de quién concilió)", value="")

    if st.button("🔄 Hacer conciliación", type="primary", disabled=not (archivo_mayor and archivo_extracto)):
        with st.spinner("Cruzando movimientos..."):
            mayor = motor.leer_mayor(archivo_mayor)
            extracto = motor.leer_extracto(archivo_extracto)
            reglas = fb.reglas_desde_feedback()
            resultado = motor.conciliar(mayor, extracto, saldo_banco, feedback_reglas=reglas)
            st.session_state.resultado = resultado
            st.session_state.banco_nombre = banco_nombre
            st.session_state.usuario = usuario
        st.success("Conciliación completa. Mirá la pestaña Resultado.")

    if archivo_mayor and archivo_extracto and saldo_banco == 0:
        st.warning("⚠ Falta indicar el saldo banco de cierre.")

# ══════════════════════════════════════════════════════════════════════
# TAB 2 — RESULTADO
# ══════════════════════════════════════════════════════════════════════
with tab_resultado:
    r = st.session_state.resultado
    if not r:
        st.info("Todavía no corriste ninguna conciliación. Andá a la pestaña Cargar.")
    else:
        dif = r['dif']
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Saldo banco", f"${r['SALDO_BCO']:,.2f}")
        c2.metric("Saldo SAP", f"${r['SALDO_SAP']:,.2f}")
        c3.metric("Diferencia conciliada", f"${dif:,.2f}",
                   delta="✓ Cierra" if abs(dif) < 1 else "⚠ Revisar", delta_color="off")
        total_cruzado = sum(v for k, v in r['niveles'].items()
                             if k not in ('REVERSION', 'CANCELACION', 'SIN_CRUCE_CONFIRMADO'))
        c4.metric("Cruzados automáticamente", total_cruzado)

        st.divider()
        niveles = r['niveles']
        cols = st.columns(6)
        etiquetas = [
            ('EXACTO', '✅ Exacto'), ('FECHA', '📅 Fecha'), ('DP', '🏦 DP'),
            ('MULTI_SAP', '➕ Multi-SAP'), ('MANUAL', '✋ Manual'), ('MULTIPLES', '⚠️ Múltiples'),
        ]
        for col, (key, lbl) in zip(cols, etiquetas):
            col.metric(lbl, niveles.get(key, 0))

        st.divider()
        st.subheader(f"⚠ Pendientes — {len(r['sin_sap'])} SAP / {len(r['sin_bco'])} banco")

        sub1, sub2, sub3 = st.tabs(["Múltiples (revisar)", "SAP sin cruce", "Banco sin cruce"])

        with sub1:
            mult = [(s, r['cruces'][s['idx']]) for s in r['mayor'][1:]
                    if s['idx'] in r['cruces'] and r['cruces'][s['idx']]['nivel'] == 'MULTIPLES']
            if mult:
                df = pd.DataFrame([{
                    'Nº SAP': s['ndoc'], 'Fecha': s['fecha'],
                    'Descripción': s['nombre'] or s['comentario'],
                    'Importe': s['importe'], 'Motivo': info['motivo'],
                } for s, info in mult])
                st.dataframe(df, use_container_width=True, hide_index=True)
            else:
                st.success("No hay casos múltiples pendientes.")

        with sub2:
            if r['sin_sap']:
                df = pd.DataFrame([{
                    'Nº SAP': s['ndoc'], 'Fecha': s['fecha'],
                    'Descripción': s['nombre'] or s['comentario'], 'Importe': s['importe'],
                } for s in r['sin_sap']])
                st.dataframe(df, use_container_width=True, hide_index=True)
            else:
                st.success("Todo el Mayor SAP está cruzado.")

        with sub3:
            if r['sin_bco']:
                df = pd.DataFrame([{
                    'Fecha': e['fecha'], 'Importe': e['importe'],
                    'Concepto': e['concepto'],
                    'Tipo': 'Débito' if e['importe'] < 0 else 'Crédito',
                } for e in r['sin_bco']])
                st.dataframe(df, use_container_width=True, hide_index=True)
            else:
                st.success("Todo el extracto está cruzado.")

# ══════════════════════════════════════════════════════════════════════
# TAB 3 — CORREGIR (el corazón del feedback loop)
# ══════════════════════════════════════════════════════════════════════
with tab_corregir:
    r = st.session_state.resultado
    if not r:
        st.info("Primero hacé una conciliación en la pestaña Cargar.")
    else:
        st.subheader("Corregí lo que el motor no resolvió solo")
        st.caption(
            "Cada corrección que hagas acá queda guardada. Periódicamente se revisa "
            "ese historial (con ayuda de Claude) para mejorar las reglas del motor."
        )

        usuario = st.session_state.get('usuario', '')

        # ── A. Resolver MÚLTIPLES: asignar manualmente o confirmar sin cruce ──
        st.markdown("### 1. Casos MÚLTIPLES sin decidir")
        mult = [(s, r['cruces'][s['idx']]) for s in r['mayor'][1:]
                if s['idx'] in r['cruces'] and r['cruces'][s['idx']]['nivel'] == 'MULTIPLES']

        if not mult:
            st.success("No hay MÚLTIPLES pendientes de decisión.")
        for s, info in mult:
            with st.expander(f"{s['ndoc']} · {s['fecha']} · ${s['importe']:,.2f} · {s['nombre'] or s['comentario']}"):
                st.caption(info['motivo'])
                imp_abs = round(abs(s['importe']), 2)
                candidatos = [e for e in r['extracto']
                              if not e['gasto'] and abs(abs(e['importe']) - imp_abs) < 1.0]
                if candidatos:
                    opciones = {f"{e['fecha']} · ${e['importe']:,.2f} · {e['concepto'][:60]}": e
                                for e in candidatos}
                    elegido = st.selectbox(
                        "¿Cuál es el movimiento banco correcto?",
                        options=["-- elegir --"] + list(opciones.keys()),
                        key=f"sel_{s['idx']}"
                    )
                    motivo = st.text_input("¿Por qué? (queda registrado)", key=f"mot_{s['idx']}")
                    col_a, col_b = st.columns(2)
                    if col_a.button("✅ Confirmar este cruce", key=f"btn_ok_{s['idx']}",
                                     disabled=elegido == "-- elegir --"):
                        e = opciones[elegido]
                        fb.registrar_correccion(
                            tipo='cruce_manual',
                            sap_key=r['sap_key_fn'](s), bco_key=r['bco_key_fn'](e),
                            sap_desc=s['nombre'] or s['comentario'], bco_desc=e['concepto'],
                            motivo_operador=motivo, usuario=usuario,
                        )
                        st.success("Guardado. Se aplicará en la próxima conciliación.")
                if st.button("🚫 No tiene contraparte (confirmar sin cruce)", key=f"btn_no_{s['idx']}"):
                    motivo2 = st.session_state.get(f"mot_{s['idx']}", "")
                    fb.registrar_correccion(
                        tipo='sin_cruce_confirmado',
                        sap_key=r['sap_key_fn'](s),
                        sap_desc=s['nombre'] or s['comentario'],
                        motivo_operador=motivo2, usuario=usuario,
                    )
                    st.success("Guardado como confirmado sin contraparte.")

        st.divider()

        # ── B. Marcar cruces del motor que están MAL ──────────────────
        st.markdown("### 2. Marcar un cruce automático como incorrecto")
        st.caption("Si ves un EXACTO/FECHA/DP mal hecho, marcalo acá para que se revise la regla.")
        cruces_reales = [(s, r['cruces'][s['idx']]) for s in r['mayor'][1:]
                          if s['idx'] in r['cruces']
                          and r['cruces'][s['idx']]['nivel'] not in ('REVERSION', 'CANCELACION', 'SIN_CRUCE_CONFIRMADO')]
        if cruces_reales:
            opciones_c = {f"{s['ndoc']} · {s['fecha']} · ${s['importe']:,.2f} · {info['nivel']}": (s, info)
                          for s, info in cruces_reales}
            elegido_c = st.selectbox("Elegí el cruce a marcar como incorrecto",
                                      options=["-- elegir --"] + list(opciones_c.keys()))
            if elegido_c != "-- elegir --":
                s_sel, info_sel = opciones_c[elegido_c]
                st.write(f"Cruzado con: {info_sel['bco']['concepto'] if info_sel.get('bco') else '(múltiples líneas)'}")
                motivo3 = st.text_area("¿Por qué está mal? ¿Cuál sería el correcto?")
                if st.button("🚩 Marcar como incorrecto"):
                    fb.registrar_correccion(
                        tipo='cruce_incorrecto',
                        sap_key=r['sap_key_fn'](s_sel),
                        bco_key=r['bco_key_fn'](info_sel['bco']) if info_sel.get('bco') else None,
                        sap_desc=s_sel['nombre'] or s_sel['comentario'],
                        motivo_operador=motivo3, usuario=usuario,
                    )
                    st.success("Guardado para revisión.")

        st.divider()

        # ── C. Patrón nuevo / comentario libre ─────────────────────────
        st.markdown("### 3. Reportar un patrón nuevo")
        st.caption("Por ejemplo: un tipo de gasto bancario que no se detectó, un proveedor con lógica especial, etc.")
        patron_txt = st.text_area("Describí el patrón nuevo", key="patron_libre")
        if st.button("📝 Guardar patrón nuevo"):
            fb.registrar_correccion(tipo='patron_nuevo', motivo_operador=patron_txt, usuario=usuario)
            st.success("Guardado para revisión.")

# ══════════════════════════════════════════════════════════════════════
# TAB 4 — DESCARGAR
# ══════════════════════════════════════════════════════════════════════
with tab_descargar:
    r = st.session_state.resultado
    if not r:
        st.info("Primero hacé una conciliación.")
    else:
        st.subheader("Descargar Excel de resultado")
        st.caption("7 solapas: RESUMEN, PENDIENTES, CRUCES, REVERSIONES, GASTOS BANCARIOS, MAYOR, EXTRACTO")

        wb = excel_export.generar_excel(r, banco_nombre=st.session_state.banco_nombre)
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        nombre_archivo = f"{st.session_state.banco_nombre}_CONCILIACION_{datetime.now().strftime('%Y%m%d')}.xlsx"
        st.download_button(
            "⬇️ Descargar Excel", data=buf, file_name=nombre_archivo,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
        )

        if abs(r['dif']) < 1:
            st.success(f"✓ La conciliación cierra en cero.")
        else:
            st.warning(f"⚠ Diferencia de ${r['dif']:,.2f} — revisá los pendientes antes de cerrar el período.")

# ══════════════════════════════════════════════════════════════════════
# SIDEBAR — resumen de feedback acumulado
# ══════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.write(f"👤 **{st.session_state.get('name', '')}**")
    authenticator.logout("Cerrar sesión", "sidebar")
    st.divider()
    st.header("📋 Feedback acumulado")
    registros = fb.cargar_feedback()
    st.metric("Total de correcciones registradas", len(registros))
    pendiente_revision = fb.resumen_feedback_pendiente_revision()
    st.metric("Pendientes de revisar (patrones/errores)", len(pendiente_revision))
    st.caption(
        "Este archivo (`feedback/feedback.jsonl`) es lo que se lleva a una "
        "sesión de Claude cada tanto para mejorar las reglas del motor. "
        "La app en sí no usa IA — es reglas fijas + corrección manual."
    )
    if registros:
        with st.expander("Ver últimas 10 correcciones"):
            for reg in registros[-10:]:
                st.text(f"[{reg['tipo']}] {reg.get('motivo_operador','')[:80]}")
