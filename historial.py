"""
historial.py — Historial acumulativo de cruces confirmados
===========================================================
Persiste los cruces de cada período cerrado para que las conciliaciones
futuras no re-procesen lo ya confirmado.

Flujo mensual:
  1. Operador corre la conciliación del mes.
  2. Revisa los cruces automáticos y corrige los dudosos.
  3. Hace clic en "Cerrar período" → se guardan todos los cruces en el historial.
  4. El mes siguiente, esos cruces aparecen como "HISTORICO" y no se re-procesan.
"""

import json
import os
from datetime import datetime

HISTORIAL_DIR  = os.path.join(os.path.dirname(__file__), 'historial')
HISTORIAL_FILE = os.path.join(HISTORIAL_DIR, 'cruces.jsonl')


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
