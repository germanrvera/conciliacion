"""
feedback.py — Registro de correcciones del operador
=====================================================
Cada vez que alguien confirma un cruce dudoso, hace un cruce manual,
o confirma que algo no tiene contraparte, queda guardado acá.

Este archivo es lo que se trae a una sesión de Claude (o Claude Code)
periódicamente para decir "mirá lo que corrigió el equipo esta semana,
actualicemos las reglas del motor". No hay IA corriendo en la app en
sí — el feedback es la materia prima para mejorar motor.py a mano.
"""

import json
import os
from datetime import datetime

FEEDBACK_DIR = os.path.join(os.path.dirname(__file__), 'feedback')
FEEDBACK_FILE = os.path.join(FEEDBACK_DIR, 'feedback.jsonl')


def _ensure_dir():
    os.makedirs(FEEDBACK_DIR, exist_ok=True)


def registrar_correccion(tipo, sap_key=None, bco_key=None, sap_desc='',
                          bco_desc='', motivo_operador='', usuario=''):
    """
    Guarda una corrección hecha por el operador.

    tipo: 'cruce_manual' | 'sin_cruce_confirmado' | 'cruce_incorrecto' | 'patron_nuevo'
    sap_key / bco_key: las claves que genera motor.py (sap_key_fn / bco_key_fn)
    motivo_operador: texto libre — por qué el operador hizo esta corrección
    """
    _ensure_dir()
    registro = {
        'timestamp': datetime.now().isoformat(),
        'tipo': tipo,
        'sap_key': sap_key,
        'bco_key': bco_key,
        'sap_desc': sap_desc,
        'bco_desc': bco_desc,
        'motivo_operador': motivo_operador,
        'usuario': usuario,
    }
    with open(FEEDBACK_FILE, 'a', encoding='utf-8') as f:
        f.write(json.dumps(registro, ensure_ascii=False) + '\n')
    return registro


def cargar_feedback():
    """Lee todo el feedback acumulado y lo devuelve como lista de dicts."""
    if not os.path.exists(FEEDBACK_FILE):
        return []
    registros = []
    with open(FEEDBACK_FILE, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    registros.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return registros


def reglas_desde_feedback():
    """
    Convierte el feedback acumulado en las reglas que motor.py puede
    aplicar directamente (cruces_manuales, sin_cruce_confirmados).
    Solo toma el feedback de tipo 'cruce_manual' y 'sin_cruce_confirmado' —
    'cruce_incorrecto' y 'patron_nuevo' quedan para revisión humana/IA,
    no se aplican automáticamente porque pueden requerir una regla más
    general, no solo ese caso puntual.
    """
    registros = cargar_feedback()
    cruces_manuales = []
    sin_cruce_confirmados = []
    for r in registros:
        if r['tipo'] == 'cruce_manual' and r.get('sap_key') and r.get('bco_key'):
            cruces_manuales.append({
                'sap_key': r['sap_key'], 'bco_key': r['bco_key'],
                'motivo_operador': r.get('motivo_operador', ''),
            })
        elif r['tipo'] == 'sin_cruce_confirmado' and r.get('sap_key'):
            sin_cruce_confirmados.append({
                'sap_key': r['sap_key'],
                'motivo_operador': r.get('motivo_operador', ''),
            })
    return {'cruces_manuales': cruces_manuales, 'sin_cruce_confirmados': sin_cruce_confirmados}


def resumen_feedback_pendiente_revision():
    """
    Devuelve los registros que un humano (o Claude) todavía no revisó
    para convertir en regla general del motor — típicamente los de tipo
    'cruce_incorrecto' o 'patron_nuevo', que señalan algo que el motor
    hizo mal o un caso que no sabía manejar.
    """
    registros = cargar_feedback()
    return [r for r in registros if r['tipo'] in ('cruce_incorrecto', 'patron_nuevo')]
