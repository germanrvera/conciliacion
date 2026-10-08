"""
historial.py — Historial acumulativo de cruces y partidas abiertas
===================================================================
Persiste dos cosas entre conciliaciones mensuales:

1. CRUCES CONFIRMADOS (cruces.jsonl):
   SAP entries ya cruzadas → no se re-procesan el mes siguiente.

2. PARTIDAS ABIERTAS (pendientes_sap.jsonl / pendientes_bco.jsonl):
   Partidas que no cruzaron en su período → se arrastran al mes siguiente
   y participan en el cruce junto con los movimientos nuevos.

Flujo mensual:
  1. Correr la conciliación → el motor incluye automáticamente las
     partidas abiertas de períodos anteriores.
  2. Revisar y corregir.
  3. "Cerrar período" → guarda cruces confirmados y actualiza pendientes
     (elimina los que cruzaron, agrega los nuevos sin cruce).
"""

import json
import os
from datetime import datetime, date

HISTORIAL_DIR      = os.path.join(os.path.dirname(__file__), 'historial')
HISTORIAL_FILE     = os.path.join(HISTORIAL_DIR, 'cruces.jsonl')
PENDIENTES_SAP     = os.path.join(HISTORIAL_DIR, 'pendientes_sap.jsonl')
PENDIENTES_BCO     = os.path.join(HISTORIAL_DIR, 'pendientes_bco.jsonl')


def _ensure_dir():
    os.makedirs(HISTORIAL_DIR, exist_ok=True)


def cargar_historial():
    """
    Carga todos los cruces confirmados de períodos anteriores.
    Retorna dict: sap_key → {'bco_key', 'nivel', 'periodo', 'timestamp'}
    """
    if not os.path.exists(HISTORIAL_FILE):
        return {}
    historico = {}
    with open(HISTORIAL_FILE, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                reg = json.loads(line)
                if reg.get('sap_key'):
                    historico[reg['sap_key']] = reg
            except json.JSONDecodeError:
                pass
    return historico


def guardar_periodo(cruces_list, periodo, usuario=''):
    """
    Guarda los cruces del período cerrado.

    cruces_list: lista de dicts con {sap_key, bco_key, nivel, sap_desc, bco_desc}
    periodo: str tipo '2026-05' (año-mes)
    """
    _ensure_dir()
    timestamp = datetime.now().isoformat()
    with open(HISTORIAL_FILE, 'a', encoding='utf-8') as f:
        for c in cruces_list:
            reg = {
                'timestamp':  timestamp,
                'periodo':    periodo,
                'usuario':    usuario,
                'sap_key':    c.get('sap_key', ''),
                'bco_key':    c.get('bco_key', ''),
                'nivel':      c.get('nivel', ''),
                'sap_desc':   c.get('sap_desc', ''),
                'bco_desc':   c.get('bco_desc', ''),
            }
            f.write(json.dumps(reg, ensure_ascii=False) + '\n')


def listar_periodos():
    """Retorna lista de períodos ya cerrados (sin duplicados, ordenados)."""
    h = cargar_historial()
    periodos = sorted({v['periodo'] for v in h.values() if v.get('periodo')})
    return periodos


def borrar_periodo(periodo):
    """Elimina todos los cruces de un período (útil para re-hacer el cierre)."""
    if not os.path.exists(HISTORIAL_FILE):
        return 0
    lineas_ok = []
    eliminadas = 0
    with open(HISTORIAL_FILE, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                reg = json.loads(line)
                if reg.get('periodo') == periodo:
                    eliminadas += 1
                else:
                    lineas_ok.append(line)
            except json.JSONDecodeError:
                lineas_ok.append(line)
    with open(HISTORIAL_FILE, 'w', encoding='utf-8') as f:
        for line in lineas_ok:
            f.write(line + '\n')
    return eliminadas


# ── PARTIDAS ABIERTAS ──────────────────────────────────────────────────

def _leer_jsonl(path):
    if not os.path.exists(path):
        return []
    resultado = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    resultado.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return resultado


def _escribir_jsonl(path, registros):
    _ensure_dir()
    with open(path, 'w', encoding='utf-8') as f:
        for r in registros:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + '\n')


def cargar_pendientes_sap():
    """
    Carga partidas SAP abiertas de períodos anteriores.
    Retorna lista de dicts con los mismos campos que leer_mayor(),
    más 'periodo_origen'. Los idx son negativos para no colisionar.
    """
    registros = _leer_jsonl(PENDIENTES_SAP)
    resultado = []
    for i, r in enumerate(registros):
        entrada = {k: v for k, v in r.items()}
        # idx negativo para no colisionar con el mayor del mes actual
        entrada['idx'] = -(i + 1)
        # restaurar fecha si viene como string
        if isinstance(entrada.get('fecha'), str):
            try:
                entrada['fecha'] = date.fromisoformat(entrada['fecha'])
            except ValueError:
                pass
        resultado.append(entrada)
    return resultado


def cargar_pendientes_bco():
    """
    Carga movimientos banco sin cruce de períodos anteriores.
    Retorna lista de dicts con los mismos campos que leer_extracto(),
    más 'periodo_origen'. Los idx son negativos para no colisionar.
    """
    registros = _leer_jsonl(PENDIENTES_BCO)
    resultado = []
    for i, r in enumerate(registros):
        entrada = {k: v for k, v in r.items()}
        entrada['idx'] = -(i + 10000)
        if isinstance(entrada.get('fecha'), str):
            try:
                entrada['fecha'] = date.fromisoformat(entrada['fecha'])
            except ValueError:
                pass
        resultado.append(entrada)
    return resultado


def actualizar_pendientes(sin_sap, sin_bco, sap_keys_cruzadas, bco_keys_cruzadas,
                           periodo, sap_key_fn, bco_key_fn):
    """
    Actualiza los archivos de partidas abiertas al cerrar un período:
    - Elimina los que ya cruzaron (sap_keys_cruzadas / bco_keys_cruzadas).
    - Agrega los nuevos sin cruce del período que se cierra.
    """
    _ensure_dir()
    timestamp = datetime.now().isoformat()

    # SAP pendientes: filtra los ya cruzados y agrega los nuevos
    existentes_sap = [r for r in _leer_jsonl(PENDIENTES_SAP)
                      if r.get('sap_key') not in sap_keys_cruzadas]
    claves_ya = {r['sap_key'] for r in existentes_sap}
    for s in sin_sap:
        sk = sap_key_fn(s)
        if sk not in claves_ya:
            entrada = {k: v for k, v in s.items() if k != 'idx'}
            entrada['sap_key'] = sk
            entrada['periodo_origen'] = periodo
            entrada['timestamp'] = timestamp
            existentes_sap.append(entrada)
    _escribir_jsonl(PENDIENTES_SAP, existentes_sap)

    # Banco pendientes: filtra los ya cruzados y agrega los nuevos
    existentes_bco = [r for r in _leer_jsonl(PENDIENTES_BCO)
                      if r.get('bco_key') not in bco_keys_cruzadas]
    claves_ya_b = {r['bco_key'] for r in existentes_bco}
    for e in sin_bco:
        bk = bco_key_fn(e)
        if bk not in claves_ya_b:
            entrada = {k: v for k, v in e.items() if k != 'idx'}
            entrada['bco_key'] = bk
            entrada['periodo_origen'] = periodo
            entrada['timestamp'] = timestamp
            existentes_bco.append(entrada)
    _escribir_jsonl(PENDIENTES_BCO, existentes_bco)

    return len(existentes_sap), len(existentes_bco)


def contar_pendientes():
    """Retorna (n_sap, n_bco) de partidas abiertas acumuladas."""
    return len(_leer_jsonl(PENDIENTES_SAP)), len(_leer_jsonl(PENDIENTES_BCO))
