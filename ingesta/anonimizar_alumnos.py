#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Convierte los XML de ITACA a JSON anonimizado.

El resultado replica la estructura de ``data/exportaciones`` dentro de
``data/raw``: por cada curso académico se generan los mismos ficheros, pero
con extensión ``.json``:

    data/raw/2021-22/alumnos.json
    data/raw/2021-22/calificaciones.json
    data/raw/2021-22/cursos.json
    data/raw/2021-22/grupos.json
    data/raw/2021-22/contenidos.json

El campo ``NIA`` de ``alumnos`` y la referencia ``alumno`` de
``calificaciones`` se anonimizan con el mismo SHA-256 truncado, para conservar
la relación entre ambos ficheros:

    hashlib.sha256(str(nia).encode()).hexdigest()[:10]

En ``alumnos.json`` solo se escriben ``anyo``, ``NIA``, ``nombre``, ``grupo``
y ``curso``. En los demás JSON se conservan los metadatos y campos del XML.

Uso:
    python ingesta/anonimizar_alumnos.py
    python ingesta/anonimizar_alumnos.py --entrada data/exportaciones --salida data/raw
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Tuple

RAIZ_PROYECTO = Path(__file__).resolve().parents[1]
DIRECTORIO_EXPORTACIONES = RAIZ_PROYECTO / "data" / "exportaciones"
DIRECTORIO_RAW = RAIZ_PROYECTO / "data" / "raw"

def anonimizar_nia(nia: str) -> str:
    """Devuelve el hash SHA-256 truncado a 10 caracteres del NIA."""
    return hashlib.sha256(str(nia).encode()).hexdigest()[:10]


def leer_xml(
    ruta: Path, nombre_contenedor: str
) -> Tuple[Dict[str, str], List[Dict[str, str]]]:
    """Lee un XML y devuelve los atributos del centro y sus registros."""
    if not ruta.exists():
        raise FileNotFoundError(f"No se encuentra el fichero: {ruta}")

    raiz = ET.parse(ruta).getroot()
    contenedor = raiz.find(nombre_contenedor)
    if contenedor is None:
        raise ValueError(
            f"El XML {ruta} no contiene el nodo <{nombre_contenedor}>"
        )

    metadatos = dict(raiz.attrib)
    registros = [dict(elemento.attrib) for elemento in contenedor]
    return metadatos, registros


def convertir_fichero(ruta: Path, nombre: str) -> Any:
    """Convierte un XML a JSON y anonimiza los identificadores de alumno."""
    metadatos, registros = leer_xml(ruta, nombre)

    if nombre == "alumnos":
        return [
            {
                "anyo": ruta.parent.name,
                "NIA": anonimizar_nia(registro["NIA"]),
                "nombre": registro.get("nombre", ""),
                "grupo": registro.get("grupo", ""),
                "curso": registro.get("curso", ""),
            }
            for registro in registros
        ]

    campo_nia = {"alumnos": "NIA", "calificaciones": "alumno"}.get(nombre)
    if campo_nia:
        for registro in registros:
            if registro.get(campo_nia):
                registro[campo_nia] = anonimizar_nia(registro[campo_nia])

    # Se conserva el nodo raíz <centro> y el nombre original del contenedor.
    return {
        "centro": metadatos,
        nombre: registros,
    }


def procesar_curso(
    directorio_origen: Path, directorio_destino: Path
) -> List[Tuple[str, int]]:
    """Convierte todos los XML de un curso académico."""
    directorio_destino.mkdir(parents=True, exist_ok=True)
    resultados: List[Tuple[str, int]] = []

    for ruta_origen in sorted(directorio_origen.glob("*.xml")):
        nombre = ruta_origen.stem
        datos = convertir_fichero(ruta_origen, nombre)
        ruta_destino = directorio_destino / f"{nombre}.json"
        with ruta_destino.open("w", encoding="utf-8") as fichero:
            json.dump(datos, fichero, ensure_ascii=False, indent=2)

        total_registros = len(datos) if isinstance(datos, list) else len(datos[nombre])
        resultados.append((nombre, total_registros))

    return resultados


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Convierte los XML de ITACA a JSON anonimizado manteniendo la "
            "estructura de carpetas y los nombres de fichero."
        )
    )
    parser.add_argument(
        "--entrada",
        type=Path,
        default=DIRECTORIO_EXPORTACIONES,
        help=f"Directorio de origen (por defecto: {DIRECTORIO_EXPORTACIONES}).",
    )
    parser.add_argument(
        "--salida",
        type=Path,
        default=DIRECTORIO_RAW,
        help=f"Directorio de destino (por defecto: {DIRECTORIO_RAW}).",
    )
    args = parser.parse_args()

    if not args.entrada.is_dir():
        print(f"ERROR: no existe el directorio {args.entrada}", file=sys.stderr)
        return 1

    cursos = sorted(
        (ruta for ruta in args.entrada.iterdir() if ruta.is_dir()),
        key=lambda ruta: ruta.name,
    )
    if not cursos:
        print(f"ERROR: no hay cursos en {args.entrada}", file=sys.stderr)
        return 1

    total_ficheros = 0
    for directorio_origen in cursos:
        print(f"Procesando {directorio_origen.name}...", file=sys.stderr)
        resultados = procesar_curso(
            directorio_origen, args.salida / directorio_origen.name
        )
        total_ficheros += len(resultados)
        for nombre, total_registros in resultados:
            print(
                f"  {directorio_origen.name}/{nombre}.json: {total_registros} registros",
                file=sys.stderr,
            )

    print(
        f"\nConversión completada: {total_ficheros} ficheros JSON en {args.salida}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
