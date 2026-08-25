"""Seed the controlled reference tables from config.

Categories and locations live in YAML because they are editorial decisions, and in Postgres
because leads reference them by foreign key. This module keeps the two in step, idempotently:
running it twice changes nothing.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.ingestion.normalizers.category import get_taxonomy
from app.ingestion.normalizers.city import get_gazetteer, normalize_key
from app.models import Category, CategoryAlias, Location, LocationAlias

logger = get_logger(__name__)


def seed_categories(session: Session) -> dict[str, Category]:
    """Insert or update every vertical and its aliases; return them keyed by slug."""
    taxonomy = get_taxonomy()
    existing = {c.slug: c for c in session.scalars(select(Category)).all()}

    for slug, vertical in taxonomy.verticals.items():
        category = existing.get(slug)
        if category is None:
            category = Category(slug=slug, label=vertical.label)
            session.add(category)
            existing[slug] = category
        else:
            category.label = vertical.label
    session.flush()

    known_aliases = {a.alias_normalized for a in session.scalars(select(CategoryAlias)).all()}
    for alias_key, slug in taxonomy.alias_to_slug.items():
        if alias_key in known_aliases:
            continue
        session.add(
            CategoryAlias(
                alias_raw=alias_key,
                alias_normalized=alias_key,
                category_id=existing[slug].id,
            )
        )
    session.flush()
    logger.info("categories_seeded", verticals=len(existing), aliases=len(taxonomy.alias_to_slug))
    return existing


def seed_locations(session: Session) -> dict[str, Location]:
    """Insert or update every gazetteer location and alias; return them keyed by slug."""
    gazetteer = get_gazetteer()
    existing = {loc.slug: loc for loc in session.scalars(select(Location)).all()}

    for resolved in gazetteer.by_key.values():
        location = existing.get(resolved.slug)
        if location is None:
            location = Location(
                slug=resolved.slug,
                name=resolved.name,
                state=resolved.state,
                country_code=resolved.country_code,
            )
            session.add(location)
            existing[resolved.slug] = location
        else:
            location.name = resolved.name
            location.state = resolved.state
    session.flush()

    known_aliases = {a.alias_normalized for a in session.scalars(select(LocationAlias)).all()}
    for alias_key, resolved in gazetteer.by_key.items():
        if alias_key in known_aliases or alias_key == normalize_key(resolved.name):
            continue
        session.add(
            LocationAlias(
                alias_raw=alias_key,
                alias_normalized=alias_key,
                location_id=existing[resolved.slug].id,
            )
        )
    session.flush()
    logger.info("locations_seeded", locations=len(existing))
    return existing
