#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ingesta de las exportaciones XML de ITACA a la zona raw (Práctica P1).

Por cada curso académico de ``data/exportaciones/<curso>/`` lee las cuatro
tablas (alumnos, cursos, contenidos y calificaciones), extrae solo los campos
del paso 3 del enunciado, anonimiza el NIA y guarda un JSON por tabla:

    data/raw/<curso>/<tabla>_<fecha>.json      p. ej. data/raw/2025-26/alumnos_2026-11-18.json

- ``Anyo`` se toma del atributo ``curso`` de la cabecera ``<centro>`` del XML.
- El NIA (``alumnos.NIA`` y ``calificaciones.alumno``) se sustituye por
  ``sha256(nia)[:10]``, el mismo valor en ambas tablas.
- Al terminar se validan las claves entre tablas y se comprueba que en la
  salida no queda ningún dato identificativo.

Uso:
    python ingesta/ingesta_itaca_xml.py
    python ingesta/ingesta_itaca_xml.py --entrada data/exportaciones --salida data/raw
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import xml.etree.ElementTree as ET
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Set

RAIZ_PROYECTO = Path(__file__).resolve().parents[1]
DIRECTORIO_EXPORTACIONES = RAIZ_PROYECTO / "data" / "exportaciones"
DIRECTORIO_RAW = RAIZ_PROYECTO / "data" / "raw"

TABLAS = ["alumnos", "cursos", "contenidos", "calificaciones"]

# Campos del paso 3 del enunciado. "Anyo" no está en los nodos: sale de la cabecera.
CAMPOS: Dict[str, List[str]] = {
    "cursos": ["Anyo", "codigo", "nombre_cas", "nombre_val", "abreviatura",
               "ensenanza", "padre"],
    "alumnos": ["Anyo", "NIA", "sexo", "nacionalidad", "provincia", "municipio",
                "ensenanza", "curso", "grupo", "turno", "linea", "modalidad",
                "repite", "estado_matricula", "tipo_matricula",
                "fecha_matricula", "fecha_ingreso_centro"],
    "contenidos": ["Anyo", "codigo", "nombre_cas", "nombre_val", "ensenanza",
                   "curso"],
    "calificaciones": ["Anyo", "evaluacion", "alumno", "ensenanza", "curso",
                       "contenido", "bloque_contenido", "nota_numerica",
                       "tipo_nota"],
}

# Campo que contiene el NIA en cada tabla.
CAMPO_NIA = {"alumnos": "NIA", "calificaciones": "alumno"}

# Atributos identificativos del XML de alumnos que nunca deben llegar a raw.
CAMPOS_PROHIBIDOS = {
    "nombre", "apellido1", "apellido2", "fecha_nac", "tipo_doc", "documento",
    "telefono1", "telefono2", "telefono3", "email1", "email2", "sip",
    "expediente", "cod_postal", "localidad", "pais_nac", "municipio_nac",
}

logger = logging.getLogger("ingesta_itaca")


@lru_cache(maxsize=None)
def anonimizar_nia(nia: str) -> str:
    """SHA-256 truncado a 10 caracteres. Se calcula una vez por NIA."""
    return hashlib.sha256(str(nia).encode()).hexdigest()[:10]


def parsear_xml(ruta: Path, tabla: str) -> List[Dict[str, str]]:
    """Lee un XML de ITACA y devuelve sus registros con los campos de CAMPOS.

    Lanza FileNotFoundError, ET.ParseError o ValueError si el fichero falta,
    está mal formado o no tiene la estructura esperada.
    """
    raiz = ET.parse(ruta).getroot()

    anyo = raiz.attrib.get("curso")
    if not anyo:
        raise ValueError("la cabecera <centro> no tiene el atributo 'curso'")

    contenedor = raiz.find(tabla)
    if contenedor is None:
        raise ValueError(f"no contiene el nodo <{tabla}>")

    campo_nia = CAMPO_NIA.get(tabla)
    registros = []
    for nodo in contenedor:
        registro = {"Anyo": anyo}
        for campo in CAMPOS[tabla][1:]:
            registro[campo] = nodo.attrib.get(campo, "").strip()
        if campo_nia and registro[campo_nia]:
            registro[campo_nia] = anonimizar_nia(registro[campo_nia])
        registros.append(registro)
    return registros


def guardar(datos: List[Dict[str, str]], directorio: Path, tabla: str) -> Path:
    """Guarda los registros en <directorio>/<tabla>_<fecha>.json."""
    directorio.mkdir(parents=True, exist_ok=True)
    ruta = directorio / f"{tabla}_{date.today().isoformat()}.json"
    with ruta.open("w", encoding="utf-8") as fichero:
        json.dump(datos, fichero, ensure_ascii=False, indent=2)
    logger.info("Guardado %s (%d registros)", ruta, len(datos))
    return ruta


def leer_codigos_grupos(ruta: Path) -> Optional[Set[str]]:
    """Códigos de grupos.xml, solo para validar (grupos no se ingesta).

    Devuelve None si el fichero no se puede leer; la ingesta sigue igualmente.
    """
    try:
        contenedor = ET.parse(ruta).getroot().find("grupos")
        if contenedor is None:
            raise ValueError("no contiene el nodo <grupos>")
        return {n.attrib.get("codigo", "").strip() for n in contenedor} - {""}
    except FileNotFoundError:
        logger.warning("No se encuentra %s: no se validan los grupos", ruta)
    except (ET.ParseError, ValueError, OSError) as exc:
        logger.warning("No se puede leer %s (%s): no se validan los grupos", ruta, exc)
    return None


def validar_grupos(curso: str, alumnos: List[Dict[str, str]], grupos: Set[str]) -> int:
    """Paso 5: todo grupo de alumnos existe en grupos. Devuelve nº de avisos."""
    sin_grupo = sum(1 for a in alumnos if not a["grupo"])
    inexistentes = sorted({a["grupo"] for a in alumnos
                           if a["grupo"] and a["grupo"] not in grupos})
    if inexistentes:
        logger.warning("%s: %d grupos de alumnos no están en grupos.xml: %s",
                       curso, len(inexistentes), ", ".join(inexistentes))
        return 1
    logger.info("%s: todos los grupos de alumnos existen en grupos.xml "
                "(%d alumnos sin grupo asignado)", curso, sin_grupo)
    return 0


def validar_claves(curso: str, datos: Dict[str, List[Dict[str, str]]]) -> int:
    """Paso 5: comprueba las referencias de calificaciones. Devuelve nº de avisos."""
    if not {"alumnos", "contenidos", "calificaciones"} <= datos.keys():
        logger.warning("%s: faltan tablas, no se validan las claves", curso)
        return 1

    nias = {a["NIA"] for a in datos["alumnos"]}
    contenidos = {(c["codigo"], c["curso"]) for c in datos["contenidos"]}
    calificaciones = datos["calificaciones"]

    sin_alumno = [c for c in calificaciones if c["alumno"] not in nias]
    sin_contenido = [c for c in calificaciones
                     if (c["contenido"], c["curso"]) not in contenidos]

    avisos = 0
    if sin_alumno:
        avisos += 1
        logger.warning(
            "%s: %d calificaciones (%d alumnos distintos) referencian alumnos "
            "que no están en alumnos", curso, len(sin_alumno),
            len({c["alumno"] for c in sin_alumno}))
    if sin_contenido:
        avisos += 1
        logger.warning(
            "%s: %d calificaciones referencian contenidos que no están en "
            "contenidos", curso, len(sin_contenido))
    if not avisos:
        logger.info("%s: claves coherentes (alumnos y contenidos de "
                    "calificaciones existen)", curso)
    return avisos


def comprobar_sin_datos_reales(ficheros: List[Path], nias_reales: set) -> bool:
    """Paso 5: verifica que la salida no contiene datos identificativos."""
    correcto = True
    for ruta in ficheros:
        try:
            registros = json.loads(ruta.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            correcto = False
            logger.error("No se puede comprobar %s: %s", ruta, exc)
            continue
        claves = set().union(*(r.keys() for r in registros)) if registros else set()
        prohibidas = claves & CAMPOS_PROHIBIDOS
        if prohibidas:
            correcto = False
            logger.error("%s contiene campos identificativos: %s",
                         ruta, ", ".join(sorted(prohibidas)))
        for campo in CAMPO_NIA.values():
            filtrados = {r[campo] for r in registros if r.get(campo) in nias_reales}
            if filtrados:
                correcto = False
                logger.error("%s contiene %d NIA sin anonimizar en '%s'",
                             ruta, len(filtrados), campo)
    if correcto:
        logger.info("Comprobado: ningún fichero de salida contiene datos "
                    "identificativos ni NIA reales")
    return correcto


def nias_originales(ruta: Path) -> set:
    """NIA reales de un alumnos.xml, solo para comprobar que no se filtran."""
    contenedor = ET.parse(ruta).getroot().find("alumnos")
    return {n.attrib["NIA"] for n in contenedor if n.attrib.get("NIA")}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ingesta de los XML de ITACA a JSON anonimizado en data/raw.")
    parser.add_argument("--entrada", type=Path, default=DIRECTORIO_EXPORTACIONES,
                        help="Directorio con una carpeta por curso (por defecto: %(default)s).")
    parser.add_argument("--salida", type=Path, default=DIRECTORIO_RAW,
                        help="Directorio raw de destino (por defecto: %(default)s).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")

    if not args.entrada.is_dir():
        logger.error("No existe el directorio de entrada %s", args.entrada)
        return 1
    cursos = sorted(p for p in args.entrada.iterdir() if p.is_dir())
    if not cursos:
        logger.error("No hay carpetas de curso en %s", args.entrada)
        return 1

    errores = avisos = 0
    generados: List[Path] = []
    nias_reales: set = set()

    for carpeta in cursos:
        logger.info("Procesando curso %s", carpeta.name)
        datos: Dict[str, List[Dict[str, str]]] = {}
        for tabla in TABLAS:
            ruta = carpeta / f"{tabla}.xml"
            try:
                datos[tabla] = parsear_xml(ruta, tabla)
                if tabla == "alumnos":
                    nias_reales |= nias_originales(ruta)
                generados.append(guardar(datos[tabla], args.salida / carpeta.name, tabla))
            except FileNotFoundError:
                errores += 1
                logger.error("No se encuentra el fichero %s", ruta)
            except ET.ParseError as exc:
                errores += 1
                logger.error("XML mal formado en %s: %s", ruta, exc)
            except (ValueError, OSError) as exc:
                errores += 1
                logger.error("Error procesando %s: %s", ruta, exc)
        avisos += validar_claves(carpeta.name, datos)
        if "alumnos" in datos:
            grupos = leer_codigos_grupos(carpeta / "grupos.xml")
            if grupos is None:
                avisos += 1
            else:
                avisos += validar_grupos(carpeta.name, datos["alumnos"], grupos)

    if not comprobar_sin_datos_reales(generados, nias_reales):
        errores += 1

    logger.info("Ingesta terminada: %d ficheros generados, %d errores, %d avisos",
                len(generados), errores, avisos)
    return 1 if errores else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        logger.warning("Ingesta interrumpida por el usuario: los ficheros de "
                       "data/raw pueden estar incompletos, vuelve a ejecutarla")
        raise SystemExit(130)
