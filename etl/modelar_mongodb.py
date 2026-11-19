#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Construye el modelo mixto de MongoDB a partir de la carga. Bloque 2, apartado 04.

Requiere haber cargado la base con ``etl/cargar_mongodb.py``. Hace dos pasos
y al final verifica que cada calificación encuentra su módulo:

1. ``aplanar_cursos()`` (7.1) → ``cursos_planos``
   En ``cursos`` cada nodo apunta a su padre (``padre`` → ``codigo``):
   Familia > Grado > Ciclo > Primero/Segundo. Se parte de los cursos
   Primero/Segundo y se sube de abajo arriba con tres ``$lookup`` de la
   colección sobre sí misma. Cada ``$lookup`` devuelve un array, que el
   ``$unwind`` convierte en objeto.

2. ``crear_modulos()`` (7.2) → ``modulos``
   Cada contenido (módulo) embebe los datos de su curso aplanado con un
   ``$lookup`` de dos campos (``curso`` + ``anyo``) y ``$replaceRoot`` /
   ``$mergeObjects``. Se añade ``turno`` como array con los turnos en que se
   imparte el curso, calculado a partir de las matrículas activas.

Cambios respecto al código del enunciado (ver README, Bloque 2):
- ``anyo`` en ``cursos_planos``, porque se cargan los cinco cursos académicos.
- El ``$lookup`` 7.2 cruza ``curso`` + ``anyo``. El del enunciado compara
  ``grado`` (un nombre, "GRADO MEDIO") con ``ensenanza`` (un código, "5"),
  que nunca coinciden.
- ``turno`` no existe en contenidos ni en cursos: lo tiene el grupo, y un mismo
  curso puede impartirse en turno diurno y semipresencial a la vez.

Tolerancia a errores: cada paso depende del anterior, así que si uno falla o
no pasa su validación, el script lo explica y se detiene (código 1) en vez de
construir un modelo incompleto. Los ``$out`` reemplazan la colección de forma
atómica: si la agregación falla, la versión anterior se conserva.

Uso:
    python etl/modelar_mongodb.py
"""

from __future__ import annotations

import logging
from collections import Counter

from pymongo import ASCENDING
from pymongo.database import Database
from pymongo.errors import ConnectionFailure, DuplicateKeyError, PyMongoError

from cargar_mongodb import conectar

logger = logging.getLogger("modelar_mongodb")

# Códigos de turno de ITACA (lo confirman los nombres de los grupos «... SEMI»).
TURNOS = {"D": "Diurno", "S": "Semipresencial"}


# --- 7.1 · cursos_planos -------------------------------------------------------

FILTRO_CURSOS = {"nombre_cas": {"$regex": "^(Primero|Segundo)"}}

PIPELINE_CURSOS_PLANOS = [
    {"$match": FILTRO_CURSOS},

    {"$lookup": {"from": "cursos", "localField": "padre",
                 "foreignField": "codigo", "as": "ciclo"}},
    {"$unwind": "$ciclo"},
    {"$lookup": {"from": "cursos", "localField": "ciclo.padre",
                 "foreignField": "codigo", "as": "grado"}},
    {"$unwind": "$grado"},
    {"$lookup": {"from": "cursos", "localField": "grado.padre",
                 "foreignField": "codigo", "as": "familia"}},
    {"$unwind": "$familia"},

    {"$project": {
        "_id": 0,
        "anyo": "$anyo",
        "familia": "$familia.nombre_cas",
        "grado": "$grado.nombre_cas",
        "ciclo": "$ciclo.nombre_cas",
        "ciclo_abreviatura": "$ciclo.abreviatura",
        "primseg": {"$arrayElemAt": [{"$split": ["$nombre_cas", " "]}, 0]},
        "curso_codigo": "$codigo",
        "curso_abreviatura": "$abreviatura",
    }},
    {"$out": "cursos_planos"},
]


def aplanar_cursos(db: Database) -> int:
    """Crea ``cursos_planos`` y devuelve su nº de documentos."""
    db.cursos.aggregate(PIPELINE_CURSOS_PLANOS)
    total = db.cursos_planos.count_documents({})
    logger.info("Colección cursos_planos creada: %d documentos", total)
    return total


def validar_cursos_planos(db: Database, total: int) -> bool:
    """Todo curso Primero/Segundo debe tener ciclo, grado y familia.

    Un ``$unwind`` descarta los cursos con la cadena de padres incompleta, así
    que si falta algún nivel, ``cursos_planos`` tendrá menos documentos.
    """
    esperados = db.cursos.count_documents(FILTRO_CURSOS)
    if total != esperados:
        logger.error("cursos_planos tiene %d documentos y hay %d cursos "
                     "Primero/Segundo: a %d les falta algún nivel de la jerarquía",
                     total, esperados, esperados - total)
        return False
    logger.info("Validado: los %d cursos Primero/Segundo tienen ciclo, grado y "
                "familia", esperados)
    return True


# --- 7.2 · modulos (modelo mixto) ---------------------------------------------

PIPELINE_MODULOS = [
    # Lookup con 2 campos: datos del curso aplanado del mismo curso y año.
    {"$lookup": {
        "from": "cursos_planos",
        "let": {"curso": "$curso", "anyo": "$anyo"},
        "pipeline": [
            {"$match": {"$expr": {"$and": [
                {"$eq": ["$curso_codigo", "$$curso"]},
                {"$eq": ["$anyo", "$$anyo"]},
            ]}}},
            {"$project": {"_id": 0, "anyo": 0, "curso_codigo": 0}},
        ],
        "as": "curso_datos",
    }},
    {"$unwind": {"path": "$curso_datos", "preserveNullAndEmptyArrays": True}},
    {"$replaceRoot": {"newRoot": {"$mergeObjects": ["$$ROOT", "$curso_datos"]}}},

    # Turnos en que se imparte el curso ese año, según las matrículas activas.
    {"$lookup": {
        "from": "alumnos",
        "let": {"curso": "$curso", "anyo": "$anyo"},
        "pipeline": [
            {"$match": {"$expr": {"$and": [
                {"$eq": ["$curso", "$$curso"]},
                {"$eq": ["$anyo", "$$anyo"]},
                {"$eq": ["$estado_matricula", "M"]},
                {"$ne": ["$turno", ""]},
            ]}}},
            {"$group": {"_id": "$turno"}},
        ],
        "as": "turnos_curso",
    }},
    {"$addFields": {"turno": {"$sortArray": {
        "input": {"$map": {
            "input": "$turnos_curso._id",
            "as": "t",
            "in": {"$switch": {
                "branches": [{"case": {"$eq": ["$$t", codigo]}, "then": nombre}
                             for codigo, nombre in TURNOS.items()],
                "default": "$$t",
            }},
        }},
        "sortBy": 1,
    }}}},

    # Documento final: campos del modelo del enunciado + abreviaturas.
    {"$project": {
        "_id": 0,
        "anyo": 1,
        "curso": 1,
        "modulo": 1,
        "nombre_modulo": 1,
        "ciclo": 1,
        "primseg": 1,
        "grado": 1,
        "familia": 1,
        "turno": 1,
        "ciclo_abreviatura": 1,
        "curso_abreviatura": 1,
    }},
    {"$out": "modulos"},
]


def crear_modulos(db: Database) -> int:
    """Crea ``modulos`` con su índice único y devuelve su nº de documentos."""
    db.contenidos.aggregate(PIPELINE_MODULOS)
    db.modulos.create_index(
        [("modulo", ASCENDING), ("anyo", ASCENDING), ("curso", ASCENDING)],
        unique=True)
    total = db.modulos.count_documents({})
    logger.info("Colección modulos creada: %d documentos (índice único "
                "modulo + anyo + curso)", total)
    return total


def validar_modulos(db: Database, total: int) -> bool:
    """Un módulo por contenido, todos con su curso, y recuento de turnos."""
    correcto = True
    esperados = db.contenidos.count_documents({})
    if total != esperados:
        correcto = False
        logger.error("modulos tiene %d documentos y contenidos %d", total, esperados)

    sin_curso = db.modulos.count_documents({"ciclo": {"$exists": False}})
    if sin_curso:
        correcto = False
        logger.error("%d módulos no tienen datos de curso (ciclo, grado, familia)",
                     sin_curso)

    if correcto:
        logger.info("Validado: los %d contenidos tienen su módulo con los datos "
                    "del curso embebidos", total)

    reparto = Counter(" + ".join(m["turno"]) or "(sin alumnos activos)"
                      for m in db.modulos.find({}, {"turno": 1}))
    for turnos, n in reparto.most_common():
        logger.info("  turno %-25s %5d módulos", turnos, n)
    return correcto


# --- Verificación del modelo completo -------------------------------------------

def validar_calificaciones(db: Database) -> bool:
    """Cada calificación debe encontrar exactamente un módulo (modulo + anyo + curso).

    Es el cruce que usarán los indicadores: calificaciones → modulos.
    """
    pipeline = [
        {"$lookup": {
            "from": "modulos",
            "localField": "modulo",
            "foreignField": "modulo",
            "let": {"anyo": "$anyo", "curso": "$curso"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$anyo", "$$anyo"]},
                    {"$eq": ["$curso", "$$curso"]},
                ]}}},
                {"$project": {"_id": 1}},
            ],
            "as": "modulo_datos",
        }},
        {"$group": {"_id": {"$size": "$modulo_datos"}, "n": {"$sum": 1}}},
    ]
    reparto = {r["_id"]: r["n"] for r in db.calificaciones.aggregate(pipeline)}
    total = sum(reparto.values())
    if set(reparto) != {1}:
        logger.error("Calificaciones según nº de módulos encontrados: %s "
                     "(se esperaba 1 para todas)", reparto)
        return False
    logger.info("Validado: las %d calificaciones encuentran su módulo "
                "(modulo + anyo + curso)", total)
    return True


# --- Programa principal --------------------------------------------------------

def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    try:
        db = conectar()
        vacias = [c for c in ("cursos", "contenidos", "alumnos")
                  if db[c].estimated_document_count() == 0]
        if vacias:
            logger.error("Colecciones vacías (%s): ejecuta antes "
                         "etl/cargar_mongodb.py", ", ".join(vacias))
            return 1

        logger.info("7.1 · Aplanar cursos")
        if not validar_cursos_planos(db, aplanar_cursos(db)):
            return 1

        logger.info("7.2 · Modelo mixto: modulos")
        if not validar_modulos(db, crear_modulos(db)):
            return 1

        logger.info("Verificación del modelo: calificaciones → modulos")
        if not validar_calificaciones(db):
            return 1

        logger.info("Ejemplo de modulos: %s", db.modulos.find_one(
            {"turno": {"$size": 2}, "nombre_modulo": {"$not": {"$regex": "^Tutor"}}},
            {"_id": 0}, sort=[("anyo", -1)]))
        logger.info("Modelo creado correctamente")
        return 0
    except RuntimeError as exc:          # conexión: mensaje ya explicado
        logger.error("%s", exc)
        return 1
    except ConnectionFailure as exc:
        logger.error("Se ha perdido la conexión con MongoDB: %s. Vuelve a "
                     "ejecutar el script cuando esté disponible", exc)
        return 1
    except DuplicateKeyError as exc:
        logger.error("No se puede crear el índice único de modulos: hay módulos "
                     "duplicados (%s)", exc.details.get("keyValue"))
        return 1
    except PyMongoError as exc:
        logger.error("Error de MongoDB: %s", exc)
        return 1
    except KeyboardInterrupt:
        logger.warning("Interrumpido por el usuario: vuelve a ejecutar el script "
                       "para regenerar el modelo")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
