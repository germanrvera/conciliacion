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
import historial as hist
import notas as nt
from datetime import date as _date_cls

st.set_page_config(page_title="Conciliación Bancaria WLG", layout="wide", page_icon="🏦")

# ══════════════════════════════════════════════════════════════════════
# AUTENTICACIÓN — lee desde st.secrets (Streamlit Cloud) o users.yaml (local)
# ══════════════════════════════════════════════════════════════════════
def _cargar_config_auth():
    # 1. Streamlit Cloud: leer desde st.secrets
    try:
        if 'credentials' in st.secrets:
            return {
                'credentials': st.secrets['credentials'].to_dict(),
                'cookie': st.secrets['cookie'].to_dict(),
            }
    except Exception:
        pass
    # 2. Desarrollo local: leer desde users.yaml
    _users_file = os.path.join(os.path.dirname(__file__), 'users.yaml')
    if os.path.exists(_users_file):
        with open(_users_file) as f:
            return yaml.load(f, Loader=SafeLoader)
    # 3. Sin configuración: mostrar instrucciones
    return None

_config = _cargar_config_auth()

if _config is None:
    st.error("⚙️ **La app no tiene credenciales configuradas.**")
    st.info(
        "Si estás en **Streamlit Cloud**, andá a **Settings → Secrets** y pegá el contenido "
        "del archivo `.streamlit/secrets.toml.example` del repositorio, reemplazando los valores. "
        "\n\nSi estás en local, creá el archivo `users.yaml` basándote en `users.yaml.example`."
    )
    st.stop()

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

# El usuario que concilió viene del login
_usuario_logueado = st.session_state.get('name', st.session_state.get('username', ''))

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

    # Auto-leer saldo de cierre desde el extracto (última fila con saldo != 0)
    _saldo_auto = 0.0
    if archivo_extracto:
        _cache_key = f"saldo_auto_{archivo_extracto.name}_{archivo_extracto.size}"
        if _cache_key not in st.session_state:
            try:
                _ext_preview = motor.leer_extracto(archivo_extracto)
                archivo_extracto.seek(0)
                st.session_state[_cache_key] = next(
                    (e['saldo'] for e in reversed(_ext_preview) if e.get('saldo', 0) != 0),
                    0.0
                )
            except Exception:
                st.session_state[_cache_key] = 0.0
        _saldo_auto = st.session_state[_cache_key]

    col3, col4 = st.columns(2)
    with col3:
        banco_nombre = st.text_input("Nombre del banco", value=st.session_state.banco_nombre)
    with col4:
        saldo_banco = st.number_input(
            "Saldo banco de cierre ($)",
            min_value=0.0, value=_saldo_auto, step=0.01, format="%.2f",
            help="Se completa automáticamente desde el extracto. Podés editarlo si el período no coincide."
        )
        if _saldo_auto > 0 and saldo_banco == _saldo_auto:
            st.caption("📋 Auto-leído del extracto")

    if st.button("🔄 Hacer conciliación", type="primary",
                  disabled=not (archivo_mayor and archivo_extracto)):
        with st.spinner("Cruzando movimientos..."):
            mayor    = motor.leer_mayor(archivo_mayor)
            extracto = motor.leer_extracto(archivo_extracto)
            # Agregar partidas abiertas de períodos anteriores
            pend_sap = hist.cargar_pendientes_sap()
            pend_bco = hist.cargar_pendientes_bco()
            mayor_completo    = mayor    + pend_sap
            extracto_completo = extracto + pend_bco
            reglas   = fb.reglas_desde_feedback()
            historico = hist.cargar_historial()
            resultado = motor.conciliar(
                mayor_completo, extracto_completo, saldo_banco,
                feedback_reglas=reglas,
                cruces_historicos=historico,
            )
            resultado['_diag_dp']   = motor.diagnostico_dp(mayor_completo, extracto_completo)
            resultado['_pend_sap']  = len(pend_sap)
            resultado['_pend_bco']  = len(pend_bco)
            resultado['_mayor_base'] = mayor
            st.session_state.resultado = resultado
            st.session_state.banco_nombre = banco_nombre
            st.session_state.usuario = _usuario_logueado
        n_pend = len(pend_sap) + len(pend_bco)
        msg = "Conciliación completa. Mirá la pestaña Resultado."
        if n_pend:
            msg += f" (incluye {len(pend_sap)} SAP y {len(pend_bco)} banco de períodos anteriores)"
        st.success(msg)

# ══════════════════════════════════════════════════════════════════════
# TAB 2 — RESULTADO
# ══════════════════════════════════════════════════════════════════════
with tab_resultado:
    r = st.session_state.resultado
    if not r:
        st.info("Todavía no corriste ninguna conciliación. Andá a la pestaña Cargar.")
    else:
        if r['SALDO_BCO'] == 0:
            st.info(
                "ℹ El saldo banco de cierre es $0 — el cuadro de control no puede verificarse. "
                "Si necesitás la diferencia exacta, ingresá el saldo en la pestaña **Cargar** y volvé a correr."
            )

        if r.get('_pend_sap', 0) or r.get('_pend_bco', 0):
            st.info(
                f"📂 Se incluyeron **{r.get('_pend_sap',0)} partidas SAP** y "
                f"**{r.get('_pend_bco',0)} movimientos banco** de períodos anteriores."
            )

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
        cols = st.columns(7)
        etiquetas = [
            ('HISTORICO', '📂 Historial'), ('EXACTO', '✅ Exacto'), ('FECHA', '📅 Fecha'),
            ('DP', '🏦 DP'), ('MULTI_SAP', '➕ Multi-SAP'), ('MANUAL', '✋ Manual'),
            ('MULTIPLES', '⚠️ Múltiples'),
        ]
        for col, (key, lbl) in zip(cols, etiquetas):
            col.metric(lbl, niveles.get(key, 0))

        if niveles.get('DP', 0) == 0 and r.get('_diag_dp'):
            with st.expander("🔍 Diagnóstico DP (por qué cruza 0)", expanded=False):
                st.code(r['_diag_dp'])

        st.divider()

        # ── Cerrar período ─────────────────────────────────────────────
        with st.expander("💾 Cerrar período — guardar cruces en el historial", expanded=False):
            periodos_guardados = hist.listar_periodos()
            if periodos_guardados:
                st.caption(f"Períodos ya cerrados: {', '.join(periodos_guardados)}")

            from datetime import date as _date
            _hoy = _date.today()
            periodo_default = f"{_hoy.year}-{_hoy.month:02d}"
            col_p1, col_p2 = st.columns([2, 1])
            with col_p1:
                periodo_input = st.text_input(
                    "Período a cerrar (AAAA-MM)",
                    value=periodo_default, key="periodo_cierre"
                )
            with col_p2:
                reemplazar = st.checkbox("Reemplazar si ya existe", key="reemplazar_periodo")

            if st.button("💾 Cerrar período y guardar", type="primary", key="btn_cerrar_periodo"):
                if reemplazar and periodo_input in periodos_guardados:
                    eliminados = hist.borrar_periodo(periodo_input)
                    st.info(f"Se eliminaron {eliminados} cruces previos del período {periodo_input}.")
                # Armar lista de cruces a guardar
                cruces_a_guardar = []
                for s in r['mayor']:
                    if s['idx'] not in r['cruces']:
                        continue
                    info = r['cruces'][s['idx']]
                    if info['nivel'] in ('REVERSION', 'CANCELACION', 'HISTORICO'):
                        continue
                    bco = info.get('bco')
                    cruces_a_guardar.append({
                        'sap_key':  r['sap_key_fn'](s),
                        'bco_key':  r['bco_key_fn'](bco) if bco else '',
                        'nivel':    info['nivel'],
                        'sap_desc': s['nombre'] or s['comentario'],
                        'bco_desc': bco['concepto'] if bco else '',
                    })
                hist.guardar_periodo(cruces_a_guardar, periodo_input, usuario=_usuario_logueado)

                # Actualizar partidas abiertas: quitar cruzadas, agregar nuevas pendientes
                sap_keys_cruzadas = {c['sap_key'] for c in cruces_a_guardar if c['sap_key']}
                bco_keys_cruzadas = {c['bco_key'] for c in cruces_a_guardar if c['bco_key']}
                n_pend_sap, n_pend_bco = hist.actualizar_pendientes(
                    r['sin_sap'], r['sin_bco'],
                    sap_keys_cruzadas, bco_keys_cruzadas,
                    periodo_input,
                    r['sap_key_fn'], r['bco_key_fn'],
                )
                st.success(
                    f"✅ Período {periodo_input} cerrado — "
                    f"{len(cruces_a_guardar)} cruces guardados. "
                    f"Partidas abiertas para el próximo mes: "
                    f"{n_pend_sap} SAP / {n_pend_bco} banco."
                )

        st.divider()

        # ── Cuadro de conciliación formal ──────────────────────────────
        st.subheader("📋 Cuadro de conciliación")
        _saldo_bco  = r['SALDO_BCO']
        _saldo_sap  = r['SALDO_SAP']
        _db_c       = r['db_c']   # débitos SAP sin banco (negativo)
        _cr_c       = r['cr_c']   # créditos SAP sin banco (positivo)
        _db_nc      = r['db_nc']  # débitos banco sin SAP (negativo)
        _cr_nc      = r['cr_nc']  # créditos banco sin SAP (positivo)
        _sc_bco     = r['sc_bco']
        _sc_sap     = r['sc_sap']
        _cierra     = abs(r['dif']) < 1

        col_bco, col_sap = st.columns(2)
        with col_bco:
            st.markdown("**Desde el extracto banco**")
            rows_bco = [
                ("Saldo extracto banco",                     _saldo_bco),
                ("(+) Créditos SAP no acreditados en banco", _cr_c),
                ("(+) Débitos SAP no debitados en banco",    _db_c),
                ("= **Saldo ajustado banco**",               _sc_bco),
            ]
            df_bco = pd.DataFrame(rows_bco, columns=["Concepto", "Importe"])
            df_bco["Importe"] = df_bco["Importe"].apply(lambda x: f"${x:,.2f}")
            st.table(df_bco.set_index("Concepto"))

        with col_sap:
            st.markdown("**Desde el Mayor SAP**")
            rows_sap = [
                ("Saldo Mayor SAP",                          _saldo_sap),
                ("(+) Créditos banco no contabilizados",     _cr_nc),
                ("(+) Débitos banco no contabilizados",      _db_nc),
                ("= **Saldo ajustado libros**",              _sc_sap),
            ]
            df_sap = pd.DataFrame(rows_sap, columns=["Concepto", "Importe"])
            df_sap["Importe"] = df_sap["Importe"].apply(lambda x: f"${x:,.2f}")
            st.table(df_sap.set_index("Concepto"))

        if _cierra:
            st.success(f"✅ La conciliación cierra — diferencia: ${r['dif']:,.2f}")
        else:
            st.error(f"⚠ Diferencia sin explicar: ${r['dif']:,.2f} — revisá las partidas pendientes")

        st.divider()

        # ── Tabla de cruces automáticos ────────────────────────────────
        st.subheader("✅ Cruces automáticos")
        cruces_tabla = []
        for s in r['mayor']:
            if s['idx'] not in r['cruces']:
                continue
            info = r['cruces'][s['idx']]
            nivel = info['nivel']
            if nivel in ('REVERSION', 'CANCELACION', 'SIN_CRUCE_CONFIRMADO'):
                continue
            bco = info.get('bco')
            bco_list = info.get('bco_list') or ([bco] if bco else [])
            bco_fecha  = bco['fecha']    if bco else '—'
            bco_conc   = (bco['concepto'] if len(bco_list) == 1 else
                          f"({len(bco_list)} líneas) " + (bco['concepto'] if bco else ''))
            bco_imp    = sum(e['importe'] for e in bco_list) if bco_list else 0
            dif_imp = round(s['importe'] - bco_imp, 2)
            cruces_tabla.append({
                'Nivel':          nivel,
                'Nº SAP':         s['ndoc'],
                'Fecha SAP':      s['fecha'],
                'Desc SAP':       (s['nombre'] or s['comentario'])[:40],
                'Importe SAP':    s['importe'],
                'Fecha banco':    bco_fecha,
                'Concepto banco': str(bco_conc)[:50],
                'Importe banco':  bco_imp,
                'Diferencia':     dif_imp,
                'Días dif':       info.get('dias', 0),
            })
        if cruces_tabla:
            df_cruces = pd.DataFrame(cruces_tabla)
            col_f1, col_f2 = st.columns([2, 1])
            with col_f1:
                filtro_nivel = st.multiselect(
                    "Filtrar por nivel",
                    options=sorted(df_cruces['Nivel'].unique()),
                    default=sorted(df_cruces['Nivel'].unique()),
                    key="filtro_nivel_cruces"
                )
            with col_f2:
                solo_dif = st.checkbox("Solo con diferencia de importe", key="filtro_dif_cruces")
            df_show = df_cruces[df_cruces['Nivel'].isin(filtro_nivel)]
            if solo_dif:
                df_show = df_show[df_show['Diferencia'].abs() > 1.0]
            st.dataframe(df_show, use_container_width=True, hide_index=True)

            # Totales de control
            tot_sap = df_show['Importe SAP'].sum()
            tot_bco = df_show['Importe banco'].sum()
            tot_dif = tot_sap - tot_bco
            c1, c2, c3 = st.columns(3)
            c1.metric("Total SAP cruzado", f"${tot_sap:,.2f}")
            c2.metric("Total banco cruzado", f"${tot_bco:,.2f}")
            c3.metric("Diferencia total", f"${tot_dif:,.2f}",
                      delta="✓ Cuadra" if abs(tot_dif) < 1 else "⚠ Revisar", delta_color="off")
            st.caption(f"{len(df_show)} cruces mostrados de {len(df_cruces)} totales")
        else:
            st.info("No hay cruces automáticos aún.")

        st.divider()
        st.subheader(f"⚠ Pendientes — {len(r['sin_sap'])} SAP / {len(r['sin_bco'])} banco")

        _hoy        = _date_cls.today()
        _notas_dict = nt.cargar_notas()

        def _edad_label(fecha):
            if not fecha or not hasattr(fecha, 'toordinal'):
                return 0
            return (_hoy - fecha).days

        def _semaforo(dias):
            if dias <= 30:  return "🟢"
            if dias <= 60:  return "🟡"
            return "🔴"

        sub1, sub2, sub3 = st.tabs(["Múltiples (revisar)", "SAP sin cruce", "Banco sin cruce"])

        with sub1:
            mult = [(s, r['cruces'][s['idx']]) for s in r['mayor']
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
                filas = []
                for s in r['sin_sap']:
                    sk    = r['sap_key_fn'](s)
                    dias  = _edad_label(s['fecha'])
                    nota  = _notas_dict.get(sk, {}).get('nota', '')
                    filas.append({
                        '': _semaforo(dias),
                        'Tipo': s.get('tipo_doc', ''),
                        'Nº SAP': s['ndoc'],
                        'Origen': s.get('periodo_origen', ''),
                        'Fecha': s['fecha'],
                        'Vcto': s.get('fecha_vcto', '') or '',
                        'Días': dias,
                        'Descripción': (s['nombre'] or s['comentario'])[:45],
                        'Importe': s['importe'],
                        'Nota': nota,
                    })
                filas.sort(key=lambda x: x['Días'], reverse=True)
                st.dataframe(pd.DataFrame(filas), use_container_width=True, hide_index=True)

                st.markdown("**Agregar nota a una partida SAP**")
                opciones_sap = {
                    f"{s['ndoc']} · {s['fecha']} · ${s['importe']:,.2f}": r['sap_key_fn'](s)
                    for s in r['sin_sap']
                }
                sel_sap = st.selectbox("Partida", ["-- elegir --"] + list(opciones_sap), key="nota_sap_sel")
                txt_nota_sap = st.text_input("Nota", key="nota_sap_txt",
                                              placeholder="Ej: cheque entregado, pendiente de presentación al banco")
                if st.button("💬 Guardar nota", key="btn_nota_sap",
                              disabled=sel_sap == "-- elegir --" or not txt_nota_sap.strip()):
                    nt.guardar_nota(opciones_sap[sel_sap], txt_nota_sap, usuario=_usuario_logueado)
                    st.success("Nota guardada.")
                    st.rerun()
            else:
                st.success("Todo el Mayor SAP está cruzado.")

        with sub3:
            if r['sin_bco']:
                filas = []
                for e in r['sin_bco']:
                    bk   = r['bco_key_fn'](e)
                    dias = _edad_label(e['fecha'])
                    nota = _notas_dict.get(bk, {}).get('nota', '')
                    filas.append({
                        '': _semaforo(dias),
                        'Fecha': e['fecha'],
                        'Origen': e.get('periodo_origen', ''),
                        'Días': dias,
                        'Tipo': 'Débito' if e['importe'] < 0 else 'Crédito',
                        'Importe': e['importe'],
                        'Concepto': e['concepto'][:50],
                        'Nota': nota,
                    })
                filas.sort(key=lambda x: x['Días'], reverse=True)
                st.dataframe(pd.DataFrame(filas), use_container_width=True, hide_index=True)

                st.markdown("**Agregar nota a un movimiento banco**")
                opciones_bco = {
                    f"{e['fecha']} · ${e['importe']:,.2f} · {e['concepto'][:40]}": r['bco_key_fn'](e)
                    for e in r['sin_bco']
                }
                sel_bco = st.selectbox("Movimiento", ["-- elegir --"] + list(opciones_bco), key="nota_bco_sel")
                txt_nota_bco = st.text_input("Nota", key="nota_bco_txt",
                                              placeholder="Ej: comisión bancaria del mes, ya contabilizada")
                if st.button("💬 Guardar nota", key="btn_nota_bco",
                              disabled=sel_bco == "-- elegir --" or not txt_nota_bco.strip()):
                    nt.guardar_nota(opciones_bco[sel_bco], txt_nota_bco, usuario=_usuario_logueado)
                    st.success("Nota guardada.")
                    st.rerun()
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

        usuario = _usuario_logueado

        # ── A. Resolver MÚLTIPLES: asignar manualmente o confirmar sin cruce ──
        st.markdown("### 1. Casos MÚLTIPLES sin decidir")
        mult = [(s, r['cruces'][s['idx']]) for s in r['mayor']
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
        cruces_reales = [(s, r['cruces'][s['idx']]) for s in r['mayor']
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
