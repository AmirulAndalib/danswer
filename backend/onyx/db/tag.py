from typing import Any

from sqlalchemy import and_
from sqlalchemy import delete
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.orm import Session

from onyx.configs.constants import DocumentSource
from onyx.db.models import Document
from onyx.db.models import Document__Tag
from onyx.db.models import Tag
from onyx.utils.logger import setup_logger

logger = setup_logger()


def check_tag_validity(tag_key: str, tag_value: str) -> bool:
    """If a tag is too long, it should not be used (it will cause an error in Postgres
    as the unique constraint can only apply to entries that are less than 2704 bytes).

    Additionally, extremely long tags are not really usable / useful."""
    if len(tag_key) + len(tag_value) > 255:
        logger.error(
            f"Tag with key '{tag_key}' and value '{tag_value}' is too long, cannot be used"
        )
        return False

    return True


def create_or_add_document_tag(
    tag_key: str,
    tag_value: str,
    source: DocumentSource,
    document_id: str,
    db_session: Session,
) -> Tag | None:
    if not check_tag_validity(tag_key, tag_value):
        return None

    document = db_session.get(Document, document_id)
    if not document:
        raise ValueError("Invalid Document, cannot attach Tags")

    tag_stmt = select(Tag).where(
        Tag.tag_key == tag_key,
        Tag.tag_value == tag_value,
        Tag.source == source,
    )
    tag = db_session.execute(tag_stmt).scalar_one_or_none()

    if not tag:
        tag = Tag(tag_key=tag_key, tag_value=tag_value, source=source)
        db_session.add(tag)

    if tag not in document.tags:
        document.tags.append(tag)

    db_session.commit()
    return tag


def create_or_add_document_tag_list(
    tag_key: str,
    tag_values: list[str],
    source: DocumentSource,
    document_id: str,
    db_session: Session,
) -> list[Tag]:
    valid_tag_values = [
        tag_value for tag_value in tag_values if check_tag_validity(tag_key, tag_value)
    ]
    if not valid_tag_values:
        return []

    document = db_session.get(Document, document_id)
    if not document:
        raise ValueError("Invalid Document, cannot attach Tags")

    existing_tags_stmt = select(Tag).where(
        Tag.tag_key == tag_key,
        Tag.tag_value.in_(valid_tag_values),
        Tag.source == source,
    )
    existing_tags = list(db_session.execute(existing_tags_stmt).scalars().all())
    existing_tag_values = {tag.tag_value for tag in existing_tags}

    new_tags = []
    for tag_value in valid_tag_values:
        if tag_value not in existing_tag_values:
            new_tag = Tag(tag_key=tag_key, tag_value=tag_value, source=source)
            db_session.add(new_tag)
            new_tags.append(new_tag)
            existing_tag_values.add(tag_value)

    if new_tags:
        logger.debug(
            f"Created new tags: {', '.join([f'{tag.tag_key}:{tag.tag_value}' for tag in new_tags])}"
        )

    all_tags = existing_tags + new_tags

    for tag in all_tags:
        if tag not in document.tags:
            document.tags.append(tag)

    db_session.commit()
    return all_tags


def find_tags(
    tag_key_prefix: str | None,
    tag_value_prefix: str | None,
    sources: list[DocumentSource] | None,
    limit: int | None,
    db_session: Session,
    # if set, both tag_key_prefix and tag_value_prefix must be a match
    require_both_to_match: bool = False,
) -> list[Tag]:
    query = select(Tag)

    if tag_key_prefix or tag_value_prefix:
        conditions = []
        if tag_key_prefix:
            conditions.append(Tag.tag_key.ilike(f"{tag_key_prefix}%"))
        if tag_value_prefix:
            conditions.append(Tag.tag_value.ilike(f"{tag_value_prefix}%"))

        final_prefix_condition = (
            and_(*conditions) if require_both_to_match else or_(*conditions)
        )
        query = query.where(final_prefix_condition)

    if sources:
        query = query.where(Tag.source.in_(sources))

    if limit:
        query = query.limit(limit)

    result = db_session.execute(query)

    tags = result.scalars().all()
    return list(tags)


def get_structured_tags_for_document(
    document_id: str, db_session: Session
) -> dict[str, str | list[str]]:
    document = db_session.get(Document, document_id)
    if not document:
        raise ValueError("Invalid Document, cannot find tags")

    document_metadata: dict[str, Any] = {}
    for tag in document.tags:
        if tag.tag_key in document_metadata:
            # NOTE: we convert to list if there are multiple values for the same key
            # Thus, it won't know if a tag is a list if it only contains one value
            if isinstance(document_metadata[tag.tag_key], str):
                document_metadata[tag.tag_key] = [
                    document_metadata[tag.tag_key],
                    tag.tag_value,
                ]
            else:
                document_metadata[tag.tag_key].append(tag.tag_value)
        else:
            document_metadata[tag.tag_key] = tag.tag_value
    return document_metadata


def delete_document_tags_for_documents__no_commit(
    document_ids: list[str], db_session: Session
) -> None:
    stmt = delete(Document__Tag).where(Document__Tag.document_id.in_(document_ids))
    db_session.execute(stmt)


def delete_orphan_tags__no_commit(db_session: Session) -> None:
    orphan_tags_query = select(Tag.id).where(
        ~db_session.query(Document__Tag.tag_id)
        .filter(Document__Tag.tag_id == Tag.id)
        .exists()
    )

    orphan_tags = db_session.execute(orphan_tags_query).scalars().all()

    if orphan_tags:
        delete_orphan_tags_stmt = delete(Tag).where(Tag.id.in_(orphan_tags))
        db_session.execute(delete_orphan_tags_stmt)
