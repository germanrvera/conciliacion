"""
notas.py — Notas por partida (SAP o banco)
==========================================
Permite que el operador deje un comentario en cualquier partida
pendiente o dudosa. Las notas persisten entre sesiones y se muestran
en las tablas de pendientes.
"""

import json
import os
from datetime import datetime

NOTAS_DIR  = os.path.join(os.path.dirname(__file__), 'feedback')
NOTAS_FILE = os.path.join(NOTAS_DIR, 'notas.jsonl')


def _ensure_dir():
    os.makedirs(NOTAS_DIR, exist_ok=True)


def cargar_notas():
    """Retorna dict: clave → {'nota', 'usuario', 'timestamp'}"""
    if not os.path.exists(NOTAS_FILE):
        return {}
    notas = {}
    with open(NOTAS_FILE, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    r = json.loads(line)
                    if r.get('clave'):
                        notas[r['clave']] = r
                except json.JSONDecodeError:
                    pass
    return notas


def guardar_nota(clave, nota, usuario=''):
    """Guarda o actualiza una nota para la clave dada (sap_key o bco_key)."""
    _ensure_dir()
    registro = {
        'clave':     clave,
        'nota':      nota.strip(),
        'usuario':   usuario,
        'timestamp': datetime.now().isoformat(),
    }
    # Reescribir el archivo actualizando la clave si ya existe
    existentes = cargar_notas()
    existentes[clave] = registro
    with open(NOTAS_FILE, 'w', encoding='utf-8') as f:
        for r in existentes.values():
            f.write(json.dumps(r, ensure_ascii=False) + '\n')


def borrar_nota(clave):
    existentes = cargar_notas()
    if clave in existentes:
        del existentes[clave]
        with open(NOTAS_FILE, 'w', encoding='utf-8') as f:
            for r in existentes.values():
                f.write(json.dumps(r, ensure_ascii=False) + '\n')
