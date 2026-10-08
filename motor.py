"""
motor.py — Motor de conciliación bancaria Credicoop
=====================================================
Consolida toda la lógica aprendida:
- Cruce estándar 1-a-1 (importe + fecha + CUIT/descripción/HAB)
- Cancelaciones contables (pares +/- que suman $0)
- Reversiones AS (asientos duplicados el mismo día)
- DP 1-a-varios (depósitos que cruzan con múltiples líneas banco)
- Multi-SAP-a-1-banco (varias partidas del mismo proveedor suman 1 movimiento)
- Regla de importes ,000 sin CUIT -> MULTIPLES

Este motor es puro Python/pandas — no depende de una IA en cada corrida.
El archivo feedback.jsonl es donde se acumulan las correcciones manuales
para revisarlas periódicamente y mejorar estas reglas.
"""

import re
from datetime import date, datetime
from collections import defaultdict, Counter
from itertools import combinations
from dataclasses import dataclass, field
from typing import Optional


# ══════════════════════════════════════════════════════════════════════
# PARSERS
# ══════════════════════════════════════════════════════════════════════
def parse_num(s):
    if s is None or str(s).strip() in ('', '-', 'None', 'nan'):
        return 0.0
    s = str(s).strip().replace('.', '').replace(',', '.').replace(' ', '')
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_fecha_sap(s):
    s = str(s).strip()
    for fmt in ('%d/%m/%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def parse_fecha_bco(s):
    s = str(s).strip()
    if len(s) == 8 and s.isdigit():
        try:
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        except ValueError:
            pass
    for fmt in ('%d/%m/%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


# ══════════════════════════════════════════════════════════════════════
# CLASIFICADORES
# ══════════════════════════════════════════════════════════════════════
GASTOS_PATRONES = [
    (r'iva.*(ali adic|alicuota|comis)', 'IVA bancario'),
    (r'sircreb', 'IIBB/SIRCREB'),
    (r'percepcion.*brutos', 'IIBB/SIRCREB'),
    (r'impuesto ley 25', 'Imp.Ley25.413'),
    (r'ali gral s/(debitos|creditos)', 'Imp.Ley25.413'),
    (r'(echq|echeq).*comis', 'Comisión bancaria'),
    (r'comision (por|e-cheq)', 'Comisión bancaria'),
    (r'^debito gestion de documentos', 'Gtos gestión docs'),
]


def es_gasto(concepto):
    c = concepto.lower()
    for pat, tipo in GASTOS_PATRONES:
        if re.search(pat, c):
            return tipo
    return None


def tolerancia(concepto):
    """Tolerancia de días según el tipo de concepto bancario.

    Muchos cobros (PR) son cheques diferidos: la empresa los recibe y registra en SAP
    en enero, pero el banco los acredita en mayo-julio cuando el cheque vence.
    Por eso los tipos de banco que corresponden a acreditaciones (ECHQ, depósito en
    caja, interbanking) tienen tolerancias amplias.
    """
    c = concepto.lower()
    if re.search(r'debito.*(automatico|directo)', c): return 90
    if re.search(r'transf.*igual.*(tit|titular)', c): return 90
    # ECHQ acreditaciones y pagos de cámara pueden demorar meses
    if re.search(r'echeq|echq', c): return 180
    if re.search(r'debito.?credito.automatico', c): return 90
    # Depósitos en caja: cheques diferidos entregados en sucursal, acreditación tardía
    if re.search(r'deposito por caja', c): return 180
    if re.search(r'acreditacion de valores.*camara', c): return 180
    if re.search(r'credito inmediato.*debin', c): return 30
    if re.search(r'pago de obligaciones.*arca', c): return 30
    if re.search(r'gestion de documentos diferidos', c): return 10
    # Interbanking puede usarse para cobros diferidos también
    if re.search(r'transf.*interbanking', c): return 90
    if re.search(r'transf.*inmediata.*dist.*tit', c): return 5
    # transferencia no-inmediata entre distinto titular
    if re.search(r'transfer.*e/cuenta|transf.*e/cta.*dist', c): return 45
    return 3


def extraer_cuit(texto):
    m = re.search(r'\b(\d{11})\b', str(texto))
    return m.group(1) if m else None


def sim_palabras(a, b):
    wa = set(re.findall(r'[a-z0-9]{3,}', a.lower()))
    wb = set(re.findall(r'[a-z0-9]{3,}', b.lower()))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))


def sim_ngrams_recall(a, b, n=4):
    """Recall de 4-gramas: fracción de los n-gramas del nombre SAP que aparecen
    en el texto del banco. Robusto cuando el banco abrevia o trunca el nombre
    (ej: 'MURESCO S.A.' vs 'MURESCO SOCIEDAD ANONIMA'). Retorna 0.0 cuando el
    banco no incluye el nombre (ECHEQ, Depósito en caja) — sin falsos positivos."""
    na = re.sub(r'[^a-z0-9]', '', a.lower())
    nb = re.sub(r'[^a-z0-9]', '', b.lower())
    if len(na) < n or len(nb) < n:
        return 0.0
    ga = set(na[i:i+n] for i in range(len(na) - n + 1))
    gb = set(nb[i:i+n] for i in range(len(nb) - n + 1))
    if not ga:
        return 0.0
    return len(ga & gb) / len(ga)


def es_redondo(importe):
    """Importe sin decimales significativos (termina en ,000)"""
    try:
        v = abs(float(importe))
        return v >= 1000 and round(v % 1, 2) == 0.0
    except (TypeError, ValueError):
        return False


def es_hab(concepto):
    return bool(re.search(r'-hab-|pago de haberes', concepto.lower()))


# ══════════════════════════════════════════════════════════════════════
# LECTURA DE ARCHIVOS
# ══════════════════════════════════════════════════════════════════════
def leer_mayor(file_bytes_or_path, encoding='latin-1'):
    """Lee MAYOR.csv (export SAP). Acepta path o objeto file-like."""
    if hasattr(file_bytes_or_path, 'read'):
        content = file_bytes_or_path.read()
        if isinstance(content, bytes):
            content = content.decode(encoding)
        lines = content.splitlines()
    else:
        with open(file_bytes_or_path, encoding=encoding) as f:
            lines = f.read().splitlines()

    mayor = []
    for i, line in enumerate(lines):
        if i == 0:
            continue
        cols = line.split(';')
        if all(c.strip() == '' for c in cols):
            continue
        fecha = parse_fecha_sap(cols[0])
        if not fecha:
            continue
        # SAP export real: 20 columnas
        # col[8]=Comentarios, col[12]=Nombre contrapartida,
        # col[13]=Cargo/Abono ML, col[14]=Saldo acumulado ML
        # col[1] = fecha de vencimiento (crítico para PP = cheques diferidos)
        fecha_vcto = parse_fecha_sap(cols[1]) if len(cols) > 1 else None
        if fecha_vcto == fecha:
            fecha_vcto = None  # sin sentido guardarla si es igual a fecha_contab
        if len(cols) >= 15:
            imp = parse_num(cols[13])
            saldo = parse_num(cols[14])
            comentario = cols[8].strip()
            nombre = cols[12].strip()
        else:
            # fallback para formato reducido (< 15 columnas)
            imp = parse_num(cols[9]) if len(cols) > 9 else 0
            saldo = parse_num(cols[10]) if len(cols) > 10 else 0
            comentario = cols[6].strip() if len(cols) > 6 else ''
            nombre = cols[8].strip() if len(cols) > 8 else ''
        if imp == 0:
            continue
        # col[2] = Serie (siempre "Primario"), col[3] = Nº doc con prefijo de tipo
        # El tipo real (PR, PP, DP, AS) es el prefijo de 2-3 letras del nro_doc
        nro_doc = cols[3].strip() if len(cols) > 3 else ''
        _tdoc_m = re.match(r'^([A-Z]{2,3})\b', nro_doc)
        tipo_doc = _tdoc_m.group(1) if _tdoc_m else (cols[2].strip() if len(cols) > 2 else '')
        ndoc = nro_doc  # nro_doc ya incluye el tipo; usamos tal cual como clave
        mayor.append({
            'idx': i, 'fecha': fecha,
            'fecha_vcto': fecha_vcto,  # None si no aplica o igual a fecha_contab
            'ndoc': ndoc,
            'nro_doc': nro_doc,
            'tipo_doc': tipo_doc,
            'folio': cols[4].strip() if len(cols) > 4 else '',
            'comentario': comentario,
            'nombre': nombre,
            'importe': imp,
            'saldo': saldo,
        })
    return mayor


def leer_extracto(file_bytes_or_path, encoding='latin-1'):
    """Lee EXTRACTO.csv (extracto bancario Credicoop)."""
    if hasattr(file_bytes_or_path, 'read'):
        content = file_bytes_or_path.read()
        if isinstance(content, bytes):
            content = content.decode(encoding)
        lines = content.splitlines()
    else:
        with open(file_bytes_or_path, encoding=encoding) as f:
            lines = f.read().splitlines()

    extracto = []
    for i, line in enumerate(lines):
        cols = line.split(';')
        if all(c.strip() == '' for c in cols):
            continue
        fecha = parse_fecha_bco(cols[0])
        if not fecha:
            continue
        conc = cols[1].strip() if len(cols) > 1 else ''
        if 'SALDO INICIAL' in conc:
            continue
        debito = parse_num(cols[3]) if len(cols) > 3 else 0
        credito = parse_num(cols[4]) if len(cols) > 4 else 0
        saldo = parse_num(cols[5]) if len(cols) > 5 else 0
        info = cols[6].strip() if len(cols) > 6 else ''
        imp = parse_num(cols[7]) if len(cols) > 7 else (credito - debito)
        if imp == 0 and debito == 0 and credito == 0:
            continue
        extracto.append({
            'idx': i, 'fecha': fecha, 'concepto': conc, 'info': info,
            'debito': debito, 'credito': credito, 'saldo': saldo,
            'importe': imp, 'gasto': es_gasto(conc),
        })
    return extracto


def diagnostico_dp(mayor, extracto):
    """Retorna texto de diagnóstico para entender por qué DP = 0."""
    _pat_acred = re.compile(
        r'echq|acreditac.*valores|gestion de documentos diferidos|deposito por caja',
        re.IGNORECASE
    )
    dp_sap = [s for s in mayor if s['nro_doc'].startswith('DP')]
    acred_bco = [e for e in extracto
                 if not e['gasto'] and e['importe'] > 0 and _pat_acred.search(e['concepto'])]

    lines = []
    lines.append(f"SAP total rows: {len(mayor)}")
    lines.append(f"SAP DP rows: {len(dp_sap)}")
    lines.append(f"Banco acreditaciones ECHQ/GDD: {len(acred_bco)}")

    if not dp_sap:
        sample_ndocs = list({s['ndoc'] for s in mayor[:30]})
        lines.append(f"Muestra ndoc SAP (primeros 30): {sample_ndocs}")
    else:
        for s in dp_sap[:5]:
            lines.append(f"  SAP DP: ndoc={s['ndoc']} fecha={s['fecha']} importe={s['importe']:,.2f}")

    if not acred_bco:
        sample_conceptos = list({e['concepto'] for e in extracto if not e['gasto'] and e['importe'] > 0}
                                 )[:20]
        lines.append(f"Conceptos banco (acreditaciones, muestra): {sample_conceptos}")
    else:
        for e in acred_bco[:5]:
            lines.append(f"  BCO acred: fecha={e['fecha']} importe={e['importe']:,.2f} concepto={e['concepto'][:50]}")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════
# MOTOR DE CRUCE
# ══════════════════════════════════════════════════════════════════════
def conciliar(mayor, extracto, saldo_banco, feedback_reglas=None, cruces_historicos=None):
    """
    Corre el motor completo de conciliación.

    feedback_reglas:   dict opcional con reglas aprendidas de feedback previo.
    cruces_historicos: dict sap_key→info de historial.cargar_historial().
                       Los SAP que ya aparecen ahí se marcan HISTORICO sin re-procesar.

    Retorna un dict con todo el resultado: cruces, pendientes, estadísticas.
    """
    feedback_reglas   = feedback_reglas   or {}
    cruces_historicos = cruces_historicos or {}
    # Saldo SAP: usar sólo entradas del período actual (idx >= 0).
    # Las entradas de historial (idx < 0) tienen saldo del mes original — no sirven.
    _curr = [s for s in mayor if s.get('idx', 0) >= 0]
    SALDO_SAP = _curr[-1]['saldo'] if _curr else (mayor[-1]['saldo'] if mayor else 0)
    SALDO_BCO = saldo_banco
    sap_real = mayor

    cruces = {}      # idx_sap -> info
    cruces_bco = {}  # idx_bco -> idx_sap
    used_bco = set()
    excluir_idx = set()  # SAP excluidos del cruce estándar (reversión/cancelación)

    def sap_key(s):
        return f"{s['ndoc']}|{s['fecha']}|{round(s['importe'],2)}"

    def bco_key(e):
        return f"{e['fecha']}|{round(e['importe'],2)}|{e['concepto'][:30]}"

    # ── FASE -2: marcar cruces ya confirmados en el historial ─────────
    sap_by_key = {sap_key(s): s for s in sap_real}
    bco_by_key = {bco_key(e): e for e in extracto}

    for sk, hist in cruces_historicos.items():
        s = sap_by_key.get(sk)
        if not s or s['idx'] in cruces:
            continue
        e = bco_by_key.get(hist.get('bco_key', ''))
        cruces[s['idx']] = {
            'nivel': 'HISTORICO',
            'bco': e,
            'dias': 0,
            'motivo': f"Confirmado en período {hist.get('periodo','?')} — {hist.get('nivel','?')}",
        }
        excluir_idx.add(s['idx'])
        if e:
            cruces_bco[e['idx']] = s['idx']
            used_bco.add(e['idx'])

    # ── FASE -1: aplicar feedback manual previo (máxima prioridad) ────

    for regla in feedback_reglas.get('cruces_manuales', []):
        s = sap_by_key.get(regla.get('sap_key'))
        e = bco_by_key.get(regla.get('bco_key'))
        if s and e and s['idx'] not in cruces and e['idx'] not in used_bco:
            cruces[s['idx']] = {
                'nivel': 'MANUAL', 'bco': e, 'dias': abs((s['fecha']-e['fecha']).days),
                'motivo': f"Cruce manual confirmado por el equipo: {regla.get('motivo_operador','')}"
            }
            cruces_bco[e['idx']] = s['idx']
            used_bco.add(e['idx'])
            excluir_idx.add(s['idx'])

    for regla in feedback_reglas.get('sin_cruce_confirmados', []):
        s = sap_by_key.get(regla.get('sap_key'))
        if s and s['idx'] not in cruces:
            cruces[s['idx']] = {
                'nivel': 'SIN_CRUCE_CONFIRMADO', 'bco': None, 'dias': 0,
                'motivo': f"Confirmado sin contraparte por el equipo: {regla.get('motivo_operador','')}"
            }
            excluir_idx.add(s['idx'])

    # ── FASE 0a: cancelaciones contables (par +/- mismo ndoc+fecha+prov) ──
    por_ndoc_fecha = defaultdict(list)
    for s in sap_real:
        if s['idx'] in excluir_idx:
            continue
        clave = (s['ndoc'], s['fecha'], (s['nombre'] or s['comentario']).lower().strip())
        por_ndoc_fecha[clave].append(s)

    for clave, items in por_ndoc_fecha.items():
        if len(items) != 2:
            continue
        a, b = items
        if abs(a['importe'] + b['importe']) < 0.02 and abs(a['importe']) > 0.01:
            for x in items:
                cruces[x['idx']] = {
                    'nivel': 'CANCELACION', 'bco': None, 'dias': 0,
                    'motivo': 'Par de cancelación contable — sin movimiento bancario propio'
                }
                excluir_idx.add(x['idx'])

    # ── FASE 0b: reversiones AS (duplicado exacto de un asiento) ──────
    grupos_ident = defaultdict(list)
    for s in sap_real:
        if s['idx'] in excluir_idx:
            continue
        grupos_ident[(s['fecha'], (s['nombre'] or s['comentario']).lower().strip())].append(s)

    for clave, items in grupos_ident.items():
        if len(items) < 2:
            continue
        por_ndoc = defaultdict(list)
        for s in items:
            por_ndoc[s['ndoc']].append(s)
        ndocs = list(por_ndoc.keys())
        if len(ndocs) < 2:
            continue
        for i in range(len(ndocs)):
            for j in range(i + 1, len(ndocs)):
                imp_i = sorted(round(x['importe'], 2) for x in por_ndoc[ndocs[i]])
                imp_j = sorted(round(x['importe'], 2) for x in por_ndoc[ndocs[j]])
                if imp_i == imp_j and len(imp_i) >= 2:
                    ndoc_marcar = max(ndocs[i], ndocs[j])
                    for x in por_ndoc[ndoc_marcar]:
                        cruces[x['idx']] = {
                            'nivel': 'REVERSION', 'bco': None, 'dias': 0,
                            'motivo': 'Reversión/duplicado contable — sin movimiento bancario propio'
                        }
                        excluir_idx.add(x['idx'])

    # ── FASE 1: DP — 1 SAP a varias acreditaciones banco ──────────────
    # Credicoop acredita depósitos en varias líneas (ECHQ/cámara, depósito
    # en caja, gestión de documentos diferidos). Los cheques diferidos se
    # registran en SAP meses antes de acreditarse; se amplía la ventana a
    # ±365 días para capturar ese desfasaje (ene SAP → may-dic banco).
    _pat_acred = re.compile(
        r'echq|acreditac.*valores|gestion de documentos diferidos|deposito por caja',
        re.IGNORECASE
    )
    acred_bco = [e for e in extracto
                 if not e['gasto'] and e['importe'] > 0 and _pat_acred.search(e['concepto'])]

    # Pre-agrupar acreditaciones ECHQ por número de depósito "Dep:XXXXXXXXXX"
    # para facilitar el cruce con DP (varias líneas de 1 mismo depósito)
    _re_dep = re.compile(r'\bDep:(\d+)', re.IGNORECASE)
    dep_grupos = defaultdict(list)
    for e in acred_bco:
        m = _re_dep.search(e['concepto'])
        if m:
            dep_grupos[m.group(1)].append(e)

    dp_sap = [s for s in sap_real if s['nro_doc'].startswith('DP') and s['idx'] not in excluir_idx]

    for s in dp_sap:
        imp_sap = round(abs(s['importe']), 2)
        found = None

        # Intento 0: grupo de ECHQ con mismo Dep: que suma exacto al DP
        for dep_id, grupo in dep_grupos.items():
            grupo_disp = [e for e in grupo if e['idx'] not in used_bco
                          and abs((e['fecha'] - s['fecha']).days) <= 365]
            if not grupo_disp:
                continue
            suma_grupo = round(sum(e['importe'] for e in grupo_disp), 2)
            if abs(suma_grupo - imp_sap) <= 1.0:
                found = grupo_disp
                break

        # Solo candidatos dentro de ±365 días y cuyo importe no supere el SAP
        disponibles = [e for e in acred_bco
                       if e['idx'] not in used_bco
                       and abs((e['fecha'] - s['fecha']).days) <= 365
                       and e['importe'] <= imp_sap + 1.0]

        # Intento 1: todos los disponibles suman exacto
        if not found:
            suma = round(sum(e['importe'] for e in disponibles), 2)
            found = disponibles if abs(suma - imp_sap) <= 1.0 else None

        # Intento 2: subconjunto — cap a 25 candidatos más cercanos al objetivo
        # para evitar explosión combinatoria
        if not found:
            candidatos = sorted(disponibles, key=lambda e: abs(e['importe'] - imp_sap / max(len(disponibles), 1)))[:25]
            for r in range(1, min(len(candidatos), 5) + 1):
                for combo in combinations(candidatos, r):
                    if abs(round(sum(e['importe'] for e in combo), 2) - imp_sap) <= 1.0:
                        found = list(combo)
                        break
                if found:
                    break

        if found:
            cruces[s['idx']] = {
                'nivel': 'DP', 'bco': found[0], 'bco_list': found, 'dias': 0,
                'motivo': f'DP: {len(found)} acreditaciones banco suman ${imp_sap:,.2f}'
            }
            for e in found:
                cruces_bco[e['idx']] = s['idx']
                used_bco.add(e['idx'])

    # ── FASE 2: motor estándar 1-a-1 ──────────────────────────────────
    imp_cnt_sap = Counter(
        round(abs(s['importe']), 2) for s in sap_real
        if not s['nro_doc'].startswith('DP') and s['idx'] not in excluir_idx
    )
    imp_cnt_bco = Counter(round(abs(e['importe']), 2) for e in extracto if not e['gasto'])

    bco_by_imp = defaultdict(list)
    for e in extracto:
        if e['gasto'] or e['idx'] in used_bco:
            continue
        bco_by_imp[round(abs(e['importe']), 2)].append(e)

    for s in sap_real:
        if s['nro_doc'].startswith('DP') or s['idx'] in excluir_idx:
            continue
        imp_abs = round(abs(s['importe']), 2)
        cands = [e for e in bco_by_imp.get(imp_abs, [])
                 if e['idx'] not in used_bco and abs(abs(e['importe']) - imp_abs) <= 1.0]
        if not cands:
            continue

        # PP = cheques diferidos: usar fecha_vcto como referencia de fecha si está disponible,
        # porque es la fecha de vencimiento del cheque (más cercana al débito bancario real).
        # El banco puede demorar hasta 90 días en acreditar, así que mantenemos la
        # tolerancia normal del concepto — el benefit es el menor distance, no acotar la ventana.
        usa_vcto = s.get('tipo_doc') == 'PP' and s.get('fecha_vcto')
        fecha_ref = s['fecha_vcto'] if usa_vcto else s['fecha']

        scored = []
        for e in cands:
            dias = abs((fecha_ref - e['fecha']).days)
            tol = tolerancia(e['concepto'])
            if dias > tol:
                continue
            ds = (s['comentario'] + ' ' + s['nombre']).lower()
            de = (e['concepto'] + ' ' + e['info']).lower()
            cuit_s = extraer_cuit(ds)
            cuit_e = extraer_cuit(de)
            match_cuit = bool(cuit_s and cuit_e and cuit_s == cuit_e)
            # Combinar similitud por palabras y por recall de 4-gramas.
            # sim_ngrams_recall captura abreviaciones (MURESCO S.A. → MURESCO SOCIEDAD ANONIMA)
            # sin generar falsos positivos cuando el banco no incluye el nombre.
            sim_d = max(sim_palabras(s['nombre'], de), sim_ngrams_recall(s['nombre'], de))
            es_sueldo = 'sueldos a pagar' in ds and es_hab(e['concepto'])
            signo_ok = (s['importe'] > 0 and e['importe'] > 0) or (s['importe'] < 0 and e['importe'] < 0)
            # PP = pago (negativo) → solo débito bancario (negativo)
            # PR = cobro (positivo) → solo crédito bancario (positivo)
            # Usar tipo_doc; si no está (historial antiguo) inferir de nro_doc/ndoc
            _tdoc = s.get('tipo_doc') or re.match(r'^([A-Z]{2,3})\b', s.get('nro_doc') or s.get('ndoc') or '')
            _tdoc = _tdoc.group(1) if hasattr(_tdoc, 'group') else _tdoc
            if _tdoc in ('PP', 'PR') and not signo_ok:
                continue

            score = max(0.3, 0.6 - dias * 0.003)
            if match_cuit: score += 0.35
            if sim_d >= 0.15: score += 0.2 + sim_d * 0.2
            if signo_ok: score += 0.1
            if dias <= 2: score += 0.15
            if es_sueldo: score += 0.3
            if usa_vcto: score += 0.25  # bonus por usar fecha exacta de vencimiento

            nivel = 'EXACTO' if (match_cuit or sim_d >= 0.15 or es_sueldo or usa_vcto) else 'FECHA'
            partes = ['importe', f'fecha±{dias}d']
            if usa_vcto: partes[1] = f'vcto±{dias}d'
            if match_cuit: partes.append('CUIT')
            if sim_d >= 0.15: partes.append(f'desc({sim_d:.0%})')
            if es_sueldo: partes.append('HAB')
            scored.append((score, dias, nivel, ' + '.join(partes), e, match_cuit))

        if not scored:
            continue
        scored.sort(key=lambda x: (-x[0], x[1]))
        best_score, best_dias, nivel, motivo, be, best_mc = scored[0]

        if len(scored) > 1 and scored[1][0] >= best_score * 0.88 and best_score > 0.4:
            nivel = 'MULTIPLES'
            motivo += ' (varios candidatos)'
        elif es_redondo(s['importe']) and not best_mc and 'HAB' not in motivo:
            n_s = imp_cnt_sap[imp_abs]
            n_b = imp_cnt_bco[imp_abs]
            if n_s > 1 or n_b > 1:
                nivel = 'MULTIPLES'
                motivo += f' (importe ,000 sin CUIT — SAP×{n_s} BCO×{n_b})'

        cruces[s['idx']] = {'nivel': nivel, 'motivo': motivo, 'bco': be, 'dias': best_dias}
        if nivel != 'MULTIPLES':
            cruces_bco[be['idx']] = s['idx']
            used_bco.add(be['idx'])

    # ── FASE 3: multi-SAP a 1 banco ───────────────────────────────────
    sin_cruce_ahora = [s for s in sap_real if s['idx'] not in cruces and s['idx'] not in excluir_idx]
    por_prov_fecha = defaultdict(list)
    for s in sin_cruce_ahora:
        clave = ((s['nombre'] or s['comentario']).lower().strip()[:25], s['fecha'])
        por_prov_fecha[clave].append(s)

    for clave, items in por_prov_fecha.items():
        if len(items) < 2:
            continue
        suma = round(sum(x['importe'] for x in items), 2)
        if abs(suma) < 1:
            continue
        imp_abs = round(abs(suma), 2)
        cands = [e for e in extracto if not e['gasto'] and e['idx'] not in used_bco
                 and abs(abs(e['importe']) - imp_abs) <= 1.0
                 and ((suma > 0 and e['importe'] > 0) or (suma < 0 and e['importe'] < 0))
                 and abs((e['fecha'] - clave[1]).days) <= 90]
        if len(cands) == 1:
            be = cands[0]
            for s in items:
                cruces[s['idx']] = {
                    'nivel': 'MULTI_SAP', 'bco': be, 'dias': abs((be['fecha'] - clave[1]).days),
                    'motivo': f'{len(items)} partidas SAP mismo proveedor suman ${suma:,.2f} = banco'
                }
            cruces_bco[be['idx']] = items[0]['idx']
            used_bco.add(be['idx'])

    # ── Resultado final ────────────────────────────────────────────────
    sin_sap = [s for s in sap_real if s['idx'] not in cruces]
    sin_bco = [e for e in extracto if e['idx'] not in cruces_bco and not e['gasto']]
    gastos = [e for e in extracto if e['gasto']]
    niveles = Counter(v['nivel'] for v in cruces.values())

    db_nc = sum(e['importe'] for e in sin_bco if e['importe'] < 0)
    cr_nc = sum(e['importe'] for e in sin_bco if e['importe'] > 0)
    db_c = sum(s['importe'] for s in sin_sap if s['importe'] < 0)
    cr_c = sum(s['importe'] for s in sin_sap if s['importe'] > 0)
    sc_bco = SALDO_BCO + db_c + cr_c
    sc_sap = SALDO_SAP + db_nc + cr_nc
    diferencia = sc_bco - sc_sap

    return {
        'mayor': mayor, 'extracto': extracto,
        'cruces': cruces, 'cruces_bco': cruces_bco,
        'sin_sap': sin_sap, 'sin_bco': sin_bco, 'gastos': gastos,
        'niveles': dict(niveles),
        'SALDO_SAP': SALDO_SAP, 'SALDO_BCO': SALDO_BCO,
        'db_nc': db_nc, 'cr_nc': cr_nc, 'db_c': db_c, 'cr_c': cr_c,
        'sc_bco': sc_bco, 'sc_sap': sc_sap, 'dif': diferencia,
        'sap_key_fn': sap_key, 'bco_key_fn': bco_key,
    }
