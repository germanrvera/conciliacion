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
    """Tolerancia de días según el tipo de concepto bancario"""
    c = concepto.lower()
    if re.search(r'debito.*(automatico|directo)', c): return 90
    if re.search(r'transf.*igual.*(tit|titular)', c): return 90
    if re.search(r'echeq|echq', c): return 90
    if re.search(r'debito.?credito.automatico', c): return 90
    if re.search(r'deposito por caja', c): return 45
    if re.search(r'acreditacion de valores.*camara', c): return 45
    if re.search(r'credito inmediato.*debin', c): return 30
    if re.search(r'pago de obligaciones.*arca', c): return 30
    if re.search(r'gestion de documentos diferidos', c): return 10
    if re.search(r'transf.*interbanking', c): return 25
    if re.search(r'transf.*inmediata.*dist.*tit', c): return 5
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
        mayor.append({
            'idx': i, 'fecha': fecha,
            'ndoc': cols[3].strip() if len(cols) > 3 else '',
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


# ══════════════════════════════════════════════════════════════════════
# MOTOR DE CRUCE
# ══════════════════════════════════════════════════════════════════════
def conciliar(mayor, extracto, saldo_banco, feedback_reglas=None):
    """
    Corre el motor completo de conciliación.

    feedback_reglas: dict opcional con reglas aprendidas de feedback previo:
        {'cruces_manuales': [{'sap_key':..., 'bco_key':...}, ...],
         'sin_cruce_confirmados': [{'sap_key':...}, ...]}
    Estas reglas se aplican ANTES del motor automático, con máxima prioridad.

    Retorna un dict con todo el resultado: cruces, pendientes, estadísticas.
    """
    feedback_reglas = feedback_reglas or {}
    SALDO_SAP = mayor[-1]['saldo'] if mayor else 0
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

    # ── FASE -1: aplicar feedback manual previo (máxima prioridad) ────
    sap_by_key = {sap_key(s): s for s in sap_real}
    bco_by_key = {bco_key(e): e for e in extracto}

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
    # en caja, gestión de documentos diferidos). Se cruzan por fecha ± 15d
    # y por suma de importes, sin requerir número de lote coincidente.
    _pat_acred = re.compile(
        r'echq|acreditac.*valores|gestion de documentos diferidos|deposito por caja',
        re.IGNORECASE
    )
    acred_bco = [e for e in extracto
                 if not e['gasto'] and e['importe'] > 0 and _pat_acred.search(e['concepto'])]

    dp_sap = [s for s in sap_real if s['ndoc'].startswith('DP') and s['idx'] not in excluir_idx]

    for s in dp_sap:
        imp_sap = round(abs(s['importe']), 2)
        disponibles = [e for e in acred_bco
                       if e['idx'] not in used_bco
                       and abs((e['fecha'] - s['fecha']).days) <= 15]
        if not disponibles:
            continue

        # Intento 1: todos los disponibles suman exacto
        suma = round(sum(e['importe'] for e in disponibles), 2)
        found = disponibles if abs(suma - imp_sap) <= 1.0 else None

        # Intento 2: subconjunto (hasta 8 elementos)
        if not found:
            for r in range(1, min(len(disponibles), 8) + 1):
                for combo in combinations(disponibles, r):
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
        if not s['ndoc'].startswith('DP') and s['idx'] not in excluir_idx
    )
    imp_cnt_bco = Counter(round(abs(e['importe']), 2) for e in extracto if not e['gasto'])

    bco_by_imp = defaultdict(list)
    for e in extracto:
        if e['gasto'] or e['idx'] in used_bco:
            continue
        bco_by_imp[round(abs(e['importe']), 2)].append(e)

    for s in sap_real:
        if s['ndoc'].startswith('DP') or s['idx'] in excluir_idx:
            continue
        imp_abs = round(abs(s['importe']), 2)
        cands = [e for e in bco_by_imp.get(imp_abs, [])
                 if e['idx'] not in used_bco and abs(abs(e['importe']) - imp_abs) <= 1.0]
        if not cands:
            continue

        scored = []
        for e in cands:
            tol = tolerancia(e['concepto'])
            dias = abs((s['fecha'] - e['fecha']).days)
            if dias > tol:
                continue
            ds = (s['comentario'] + ' ' + s['nombre']).lower()
            de = (e['concepto'] + ' ' + e['info']).lower()
            cuit_s = extraer_cuit(ds)
            cuit_e = extraer_cuit(de)
            match_cuit = bool(cuit_s and cuit_e and cuit_s == cuit_e)
            sim_d = sim_palabras(s['nombre'], de)
            es_sueldo = 'sueldos a pagar' in ds and es_hab(e['concepto'])
            signo_ok = (s['importe'] > 0 and e['importe'] > 0) or (s['importe'] < 0 and e['importe'] < 0)

            score = max(0.3, 0.6 - dias * 0.003)
            if match_cuit: score += 0.35
            if sim_d >= 0.15: score += 0.2 + sim_d * 0.2
            if signo_ok: score += 0.1
            if dias <= 2: score += 0.15
            if es_sueldo: score += 0.3

            nivel = 'EXACTO' if (match_cuit or sim_d >= 0.15 or es_sueldo) else 'FECHA'
            partes = ['importe', f'fecha±{dias}d']
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
