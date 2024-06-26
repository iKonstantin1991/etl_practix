from typing import Any, Dict, List, Iterator, Optional, Callable
from uuid import UUID
from datetime import datetime
from enum import Enum

from psycopg.rows import dict_row
from psycopg.errors import OperationalError
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from etl.search_engine import SearchEngineFilmwork, SearchEngineGenre, SearchEnginePerson
from etl.logger import logger
from etl.backoff import backoff
from etl.settings import settings
from etl import state

_CHUNK_SIZE = 100

conninfo = ("postgresql://"
            f"{settings.postgres_user}:{settings.postgres_password}@"
            f"{settings.postgres_host}:{settings.postgres_port}/"
            f"{settings.postgres_db}")
conn_pool = ConnectionPool(conninfo, min_size=1, max_size=1)


class Entity(str, Enum):
    FILMWORK = "filmwork"
    PERSON = "person"
    GENRE = "genre"


def get_updated_genres() -> Iterator[List[SearchEngineGenre]]:
    yield from _get_updated_entity(Entity.GENRE)


def get_updated_personas() -> Iterator[List[SearchEnginePerson]]:
    yield from _get_updated_entity(Entity.PERSON)


def get_updated_filmworks() -> Iterator[List[SearchEngineFilmwork]]:
    yield from _get_filmorks_with_updated_personas()
    yield from _get_filmorks_with_updated_genres()
    yield from _get_updated_entity(Entity.FILMWORK)


def _get_filmorks_with_updated_personas() -> Iterator[List[SearchEngineFilmwork]]:
    logger.info("Getting filmworks with updated personas")
    yield from _get_filmorks_with_updated_related_entities(Entity.PERSON)


def _get_filmorks_with_updated_genres() -> Iterator[List[SearchEngineFilmwork]]:
    logger.info("Getting filmworks with updated genres")
    yield from _get_filmorks_with_updated_related_entities(Entity.GENRE)


def _get_updated_entity(entity: Entity) -> Iterator[BaseModel]:
    logger.info("Getting updated %s", entity)
    state_key = _get_state_key(entity)
    sql_builder = _get_sql_builder(entity)
    seen_entities_count = 0
    should_generate = True
    while should_generate:
        last_seen_modified = state.get(state_key)
        entities = _db_execute(sql_builder(last_seen_modified=last_seen_modified))
        if entities:
            state.save(state_key, entities[-1]["modified"])
            seen_entities_count += len(entities)
            yield _normalize(entities, _get_model(entity))
        else:
            should_generate = False
            logger.info("Seen %s updates for %s: %s", seen_entities_count, entity, last_seen_modified)


def _get_filmorks_with_updated_related_entities(entity: Entity) -> Iterator[List[SearchEngineFilmwork]]:
    for entity_ids_ids in _get_updated_related_entities_ids(entity):
        for filmwork_ids in _get_filmworks_ids_with_related_entities(entity, entity_ids_ids):
            yield _get_filmworks_by_ids(filmwork_ids)


def _get_updated_related_entities_ids(entity: Entity) -> Iterator[List[UUID]]:
    state_key = _get_state_key(f"{entity}_related")
    seen_entities_count = 0
    should_generate = True
    while should_generate:
        last_seen_modified = state.get(state_key)
        cmd = _build_sql_requesting_entity(entity, last_seen_modified)
        entities = _db_execute(cmd)
        if entities:
            state.save(state_key, entities[-1]["modified"])
            seen_entities_count += len(entities)
            yield [e["id"] for e in entities]
        else:
            should_generate = False
            logger.info("Seen %s updates for %s: %s", seen_entities_count, entity, last_seen_modified)


def _get_filmworks_ids_with_related_entities(entity: Entity, entity_ids: List[UUID]) -> Iterator[List[UUID]]:
    should_generate = True
    state_key = _get_state_key(f"{entity}_filmwork")
    while should_generate:
        last_seen_modified = state.get(state_key)
        cmd = _build_sql_requesting_filmworks_ids_with_entity(entity, entity_ids, last_seen_modified)
        filmworks = _db_execute(cmd)
        if filmworks:
            state.save(state_key, filmworks[-1]["modified"])
            yield [fw["id"] for fw in filmworks]
        else:
            state.reset(state_key)
            should_generate = False


def _get_filmworks_by_ids(filmwork_ids: List[UUID]) -> List[SearchEngineFilmwork]:
    cmd = _build_sql_requesting_filmworks(filmwork_ids)
    return _normalize(_db_execute(cmd), SearchEngineFilmwork)


@backoff(exceptions=(OperationalError,))
def _db_execute(cmd: str) -> List[Dict[str, Any]]:
    with conn_pool.connection() as conn:
        conn.row_factory=dict_row
        return conn.execute(cmd).fetchall()


def _get_model(entity: Entity) -> BaseModel:
    if entity == Entity.FILMWORK:
        model = SearchEngineFilmwork
    elif entity == Entity.GENRE:
        model = SearchEngineGenre
    else:
        model = SearchEnginePerson
    return model


def _normalize(data: List[Dict[str, Any]], model: BaseModel) -> List[BaseModel]:
    return [model.parse_obj(i) for i in data]


def _build_sql_requesting_entity(entity: Entity, last_seen_modified: datetime) -> str:
    return f"""
        SELECT DISTINCT id, modified
        FROM content.{entity}
        WHERE modified > '{last_seen_modified.isoformat()}'
        ORDER BY modified
        LIMIT {_CHUNK_SIZE}
    """


def _build_sql_requesting_filmworks_ids_with_entity(
        entity: Entity, entity_ids: List[UUID], last_seen_modified: datetime) -> str:
    normalized_entity_ids = ' ,'.join([f"'{str(id)}'" for id in entity_ids])
    return f"""
        SELECT DISTINCT fw.id, fw.modified
        FROM content.film_work fw
        LEFT JOIN content.{entity}_film_work efw ON efw.film_work_id = fw.id
        WHERE efw.{entity}_id IN ({normalized_entity_ids}) AND
              fw.modified > '{last_seen_modified.isoformat()}'
        ORDER BY fw.modified
        LIMIT {_CHUNK_SIZE}
    """


def _build_sql_requesting_filmworks(ids: Optional[List[UUID]] = None,
                                    last_seen_modified: Optional[datetime] = None) -> str:
    if ids:
        normalized_ids = ' ,'.join([f"'{str(p)}'" for p in ids])
        condition, limit = f"WHERE fw.id IN ({normalized_ids})", ""
    else:
        assert last_seen_modified is not None
        condition, limit = f"WHERE fw.modified > '{last_seen_modified.isoformat()}'", f"LIMIT {_CHUNK_SIZE}"
    return f"""
        SELECT fw.id,
               fw.title,
               fw.description,
               fw.rating as imdb_rating,
               fw.modified,
               COALESCE(json_agg(DISTINCT jsonb_build_object('role', pfw.role,
                                                             'id', p.id,
                                                             'name', p.full_name)) FILTER (WHERE p.id is not null),
                        '[]') as personas,
               COALESCE(json_agg(DISTINCT jsonb_build_object('id', g.id,
                                                             'name', g.name)) FILTER (WHERE g.id is not null),
                        '[]') as genres
        FROM content.film_work fw
        LEFT JOIN content.person_film_work pfw ON pfw.film_work_id = fw.id
        LEFT JOIN content.person p ON p.id = pfw.person_id
        LEFT JOIN content.genre_film_work gfw ON gfw.film_work_id = fw.id
        LEFT JOIN content.genre g ON g.id = gfw.genre_id
        {condition}
        GROUP BY fw.id
        ORDER BY fw.modified
        {limit}
    """


def _build_sql_requesting_genres(last_seen_modified: datetime) -> str:
    return f"""
        SELECT id, name, modified
        FROM content.genre
        WHERE modified > '{last_seen_modified.isoformat()}'
        ORDER BY modified
        LIMIT {_CHUNK_SIZE}
    """


def _build_sql_requesting_personas(last_seen_modified: datetime) -> str:
    return f"""
        SELECT p.id,
               p.full_name,
               p.modified,
               json_agg(DISTINCT jsonb_build_object('id', pfw.film_work_id, 'role', pfw.role)) as films
        FROM content.person p
        LEFT JOIN content.person_film_work pfw ON p.id = pfw.person_id
        WHERE p.modified > '{last_seen_modified.isoformat()}'
        GROUP BY p.id
        ORDER BY p.modified
        LIMIT {_CHUNK_SIZE}
    """


def _get_sql_builder(entity: Entity) -> Callable[[datetime], str]:
    if entity == Entity.FILMWORK:
        builder = _build_sql_requesting_filmworks
    elif entity == Entity.GENRE:
        builder = _build_sql_requesting_genres
    else:
        builder = _build_sql_requesting_personas
    return builder


def _get_state_key(prefix: str) -> str:
    return f"{prefix}_last_seen_modified"
