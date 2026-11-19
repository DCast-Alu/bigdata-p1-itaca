#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Carga la zona raw de ITACA en MongoDB (base ``itaca``). Bloque 2, apartado 04.

Sigue el script ``etl/cargar_mongodb.py`` del enunciado, con las funciones
``cargar_contenidos()``, ``cargar_alumnos()`` y ``cargar_calificaciones()``, más
``cargar_cursos()``, que hace falta para aplanar la jerarquía de cursos (7.1).

Lee el JSON más reciente de cada tabla en ``data/raw/<curso>/<tabla>_<fecha>.json``
(los genera ``ingesta/ingesta_itaca_xml.py``) y crea estas colecciones:

    contenidos      anyo, curso, modulo, nombre_modulo, ...  (base de ``modulos``, 7.2)
    alumnos         nia, anyo, curso, grupo, turno, ...       (un documento por matrícula)
    calificaciones  nia, anyo, curso, modulo, nota, evaluacion
    cursos          anyo, codigo, nombre_cas, abreviatura, padre, ...

Antes de cargar se vacía cada colección, así se puede repetir sin duplicar.

Tolerancia a errores: un fichero ausente o corrupto, o un registro incompleto,
se registra y se salta, y la carga sigue con el resto. Solo se aborta si no se
puede conectar con MongoDB o se pierde la conexión. El script termina con
código 1 si hubo algún error y 0 si todo fue bien.

Uso:
    python etl/cargar_mongodb.py                 # todos los cursos
    python etl/cargar_mongodb.py 2025-26 2024-25 # solo esos cursos

La contraseña se lee de la variable MONGO_PASSWORD (o del fichero .env).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

from pymongo import ASCENDING, MongoClient
from pymongo.database import Database
from pymongo.errors import (BulkWriteError, ConnectionFailure, DuplicateKeyError,
                            OperationFailure, PyMongoError)

RAIZ_PROYECTO = Path(__file__).resolve().parents[1]
DIRECTORIO_RAW = RAIZ_PROYECTO / "data" / "raw"
FICHERO_ENV = RAIZ_PROYECTO / ".env"
BASE_DATOS = "itaca"

# ITACA usa códigos de evaluación; las ordinarias se traducen a "1ª"/"2ª" como
# en el enunciado. El resto (FI, EX, FO, ...) se deja igual.
EVALUACIONES = {"01": "1ª", "02": "2ª", "03": "3ª"}

logger = logging.getLogger("cargar_mongodb")


# --- Conexión ----------------------------------------------------------------

def leer_env(ruta: Path) -> None:
    """Carga variables CLAVE=valor de .env sin pisar las ya definidas."""
    if not ruta.exists():
        return
    try:
        lineas = ruta.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("No se puede leer %s: %s", ruta, exc)
        return
    for linea in lineas:
        linea = linea.strip()
        if linea and not linea.startswith("#") and "=" in linea:
            clave, valor = linea.split("=", 1)
            os.environ.setdefault(clave.strip(), valor.strip().strip("\"'"))


def uri_mongo() -> str:
    """URI de conexión. Las credenciales salen de .env, no del código."""
    leer_env(FICHERO_ENV)
    usuario = os.environ.get("MONGO_USER", "admin")
    password = os.environ.get("MONGO_PASSWORD")
    host = os.environ.get("MONGO_HOST", "localhost:27017")
    if not password:
        raise RuntimeError("Falta MONGO_PASSWORD (variable de entorno o .env)")
    return f"mongodb://{usuario}:{password}@{host}/"


def conectar() -> Database:
    """Devuelve la base ``itaca`` comprobando antes que el servidor responde.

    Traduce los fallos típicos a un RuntimeError con un mensaje claro.
    """
    uri = uri_mongo()
    host = uri.rsplit("@", 1)[-1].rstrip("/")
    cliente = MongoClient(uri, serverSelectionTimeoutMS=5000)
    try:
        cliente.admin.command("ping")
    except ConnectionFailure as exc:
        raise RuntimeError(f"MongoDB no responde en {host}. ¿Está arrancado? "
                           "Prueba con: docker compose up -d") from exc
    except OperationFailure as exc:
        if exc.code == 18:   # AuthenticationFailed
            raise RuntimeError("Usuario o contraseña de MongoDB incorrectos: "
                               "revisa MONGO_USER / MONGO_PASSWORD en .env") from exc
        raise
    return cliente[BASE_DATOS]


# --- Lectura de raw ----------------------------------------------------------

def ruta_legible(ruta: Path) -> str:
    """Ruta relativa al proyecto si se puede (para el log); si no, absoluta."""
    try:
        return str(ruta.relative_to(RAIZ_PROYECTO))
    except ValueError:
        return str(ruta)


def fichero_mas_reciente(directorio: Path, tabla: str) -> Path:
    """Último <tabla>_<fecha>.json del curso (la fecha ISO ordena bien)."""
    candidatos = sorted(directorio.glob(f"{tabla}_*.json"))
    if not candidatos:
        raise FileNotFoundError(f"no hay {tabla}_<fecha>.json en {directorio}")
    return candidatos[-1]


def leer_registros(ruta: Path) -> List[Dict[str, str]]:
    """Lee un JSON de raw. Debe ser una lista de objetos."""
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    if not isinstance(datos, list):
        raise ValueError("el JSON no es una lista de registros")
    return datos


def transformar_registros(registros: List[Dict[str, str]],
                          transformar: Callable[[Dict[str, str]], Dict],
                          origen: str) -> List[Dict]:
    """Aplica ``transformar`` a cada registro y salta los que estén incompletos."""
    documentos: List[Dict] = []
    descartados = 0
    for posicion, registro in enumerate(registros):
        try:
            documentos.append(transformar(registro))
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            descartados += 1
            if descartados == 1:
                logger.warning("%s: registro %d descartado (%s: %s)", origen,
                               posicion, type(exc).__name__, exc)
    if descartados:
        logger.warning("%s: %d de %d registros descartados por estar incompletos",
                       origen, descartados, len(registros))
    return documentos


def cargar_coleccion(db: Database, cursos: List[Path], tabla: str, coleccion: str,
                     transformar: Callable[[Dict[str, str]], Dict]) -> int:
    """Vacía ``coleccion`` y la llena con ``tabla`` de cada curso.

    Devuelve el nº de errores. Los errores de un fichero no detienen la carga
    del resto; una pérdida de conexión (ConnectionFailure) sí, y se propaga.
    """
    try:
        db[coleccion].drop()
    except ConnectionFailure:
        raise
    except PyMongoError as exc:
        logger.error("No se puede vaciar la colección %s: %s. Se omite su carga",
                     coleccion, exc)
        return 1

    errores = 0
    for carpeta in cursos:
        origen = f"{carpeta.name}/{tabla}"
        try:
            ruta = fichero_mas_reciente(carpeta, tabla)
            registros = leer_registros(ruta)
        except FileNotFoundError as exc:
            errores += 1
            logger.error("%s: %s", origen, exc)
            continue
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            errores += 1
            logger.error("%s: no se puede leer %s (%s)", origen,
                         ruta_legible(ruta), exc)
            continue

        documentos = transformar_registros(registros, transformar, origen)
        if len(documentos) < len(registros):
            errores += 1
        if not documentos:
            logger.warning("%s: no hay documentos que cargar", origen)
            continue

        try:
            db[coleccion].insert_many(documentos)
        except ConnectionFailure:
            raise
        except BulkWriteError as exc:
            errores += 1
            logger.error("%s: inserción parcial en %s (%d de %d documentos)",
                         origen, coleccion, exc.details.get("nInserted", 0),
                         len(documentos))
            continue
        except PyMongoError as exc:
            errores += 1
            logger.error("%s: error de MongoDB al insertar en %s: %s",
                         origen, coleccion, exc)
            continue
        logger.info("%s → %s: %d documentos", ruta_legible(ruta), coleccion,
                    len(documentos))
    return errores


def a_nota(valor: str) -> Optional[int]:
    valor = valor.strip()
    return int(valor) if valor.isdigit() else None


# --- Funciones de carga del enunciado -----------------------------------------

def cargar_contenidos(db: Database, cursos: List[Path]) -> int:
    """Contenidos (módulos). Nombres de campo del enunciado: modulo, nombre_modulo."""
    def transformar(r: Dict[str, str]) -> Dict:
        return {
            "anyo": r["Anyo"],
            "curso": r["curso"],
            "modulo": r["codigo"],
            "nombre_modulo": r["nombre_cas"],
            "nombre_modulo_val": r["nombre_val"],
            "ensenanza": r["ensenanza"],
        }
    return cargar_coleccion(db, cursos, "contenidos", "contenidos", transformar)


def cargar_alumnos(db: Database, cursos: List[Path]) -> int:
    """Alumnos: nia, anyo, curso, turno, grupo + el resto de campos del paso 3 de la P1.

    No hay fecha_nac ni ningún otro dato personal (no llegan a raw).
    """
    def transformar(r: Dict[str, str]) -> Dict:
        doc = {"nia": r["NIA"], "anyo": r["Anyo"], "curso": r["curso"],
               "turno": r["turno"], "grupo": r["grupo"]}
        doc.update({k: v for k, v in r.items()
                    if k not in ("NIA", "Anyo", "curso", "turno", "grupo")})
        return doc
    return cargar_coleccion(db, cursos, "alumnos", "alumnos", transformar)


def cargar_calificaciones(db: Database, cursos: List[Path]) -> int:
    """Calificaciones: nia, anyo, curso, modulo, nota (entero o null), evaluacion."""
    def transformar(r: Dict[str, str]) -> Dict:
        return {
            "nia": r["alumno"],
            "anyo": r["Anyo"],
            "curso": r["curso"],
            "modulo": r["contenido"],
            "nota": a_nota(r["nota_numerica"]),
            "evaluacion": EVALUACIONES.get(r["evaluacion"], r["evaluacion"]),
            "tipo_nota": r["tipo_nota"],
        }
    return cargar_coleccion(db, cursos, "calificaciones", "calificaciones", transformar)


def cargar_cursos(db: Database, cursos: List[Path]) -> int:
    """Cursos tal cual + anyo. Se usa para aplanar la jerarquía (7.1)."""
    def transformar(r: Dict[str, str]) -> Dict:
        doc = {"anyo": r["Anyo"]}
        doc.update({k: v for k, v in r.items() if k != "Anyo"})
        return doc
    return cargar_coleccion(db, cursos, "cursos", "cursos", transformar)


# --- Índices -----------------------------------------------------------------

# El enunciado pide (nia, anyo) único en alumnos, pero es imposible con los
# datos: un alumno puede tener en el mismo año matrículas de baja ("B") junto a
# la activa ("M"), e incluso dos activas en cursos distintos. Se garantiza una
# sola matrícula ACTIVA por alumno, año y curso con un índice único parcial.
INDICES = [
    ("calificaciones",
     [("curso", ASCENDING), ("anyo", ASCENDING), ("evaluacion", ASCENDING)], {}),
    ("calificaciones", [("nia", ASCENDING)], {}),
    ("alumnos",
     [("nia", ASCENDING), ("anyo", ASCENDING), ("curso", ASCENDING)],
     {"unique": True, "name": "nia_anyo_curso_activa",
      "partialFilterExpression": {"estado_matricula": "M"}}),
    ("contenidos",
     [("modulo", ASCENDING), ("anyo", ASCENDING), ("curso", ASCENDING)],
     {"unique": True}),
    ("cursos", [("codigo", ASCENDING), ("anyo", ASCENDING)], {"unique": True}),
]


def crear_indices(db: Database) -> int:
    """Crea cada índice por separado: si uno falla, se crean los demás."""
    errores = 0
    for coleccion, claves, opciones in INDICES:
        campos = ", ".join(campo for campo, _ in claves)
        try:
            db[coleccion].create_index(claves, **opciones)
        except ConnectionFailure:
            raise
        except DuplicateKeyError as exc:
            errores += 1
            logger.error("Índice único %s (%s) no creado: hay valores duplicados "
                         "(%s)", coleccion, campos, exc.details.get("keyValue"))
        except PyMongoError as exc:
            errores += 1
            logger.error("Índice %s (%s) no creado: %s", coleccion, campos, exc)
    if not errores:
        logger.info("Índices creados")
    return errores


# --- Programa principal --------------------------------------------------------

def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    filtro = set(sys.argv[1:])

    if not DIRECTORIO_RAW.is_dir():
        logger.error("No existe %s: ejecuta antes ingesta/ingesta_itaca_xml.py",
                     DIRECTORIO_RAW)
        return 1
    cursos = sorted(p for p in DIRECTORIO_RAW.iterdir()
                    if p.is_dir() and (not filtro or p.name in filtro))
    if filtro - {c.name for c in cursos}:
        logger.warning("Cursos pedidos que no existen en raw: %s",
                       ", ".join(sorted(filtro - {c.name for c in cursos})))
    if not cursos:
        logger.error("No hay cursos que cargar en %s", DIRECTORIO_RAW)
        return 1
    logger.info("Cursos a cargar: %s", ", ".join(c.name for c in cursos))

    try:
        db = conectar()
        errores = 0
        errores += cargar_contenidos(db, cursos)
        errores += cargar_alumnos(db, cursos)
        errores += cargar_calificaciones(db, cursos)
        errores += cargar_cursos(db, cursos)
        errores += crear_indices(db)

        for coleccion in ("contenidos", "alumnos", "calificaciones", "cursos"):
            logger.info("Total %s: %d", coleccion, db[coleccion].count_documents({}))
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 1
    except ConnectionFailure as exc:
        logger.error("Se ha perdido la conexión con MongoDB: %s. Carga incompleta: "
                     "vuelve a ejecutar el script cuando esté disponible", exc)
        return 1
    except PyMongoError as exc:
        logger.error("Error de MongoDB: %s", exc)
        return 1
    except KeyboardInterrupt:
        logger.warning("Carga interrumpida por el usuario: los datos pueden haber "
                       "quedado incompletos, vuelve a ejecutar el script")
        return 130

    if errores:
        logger.warning("Carga completada con %d errores (revisa los mensajes "
                       "anteriores)", errores)
        return 1
    logger.info("Carga completada con 0 errores")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
