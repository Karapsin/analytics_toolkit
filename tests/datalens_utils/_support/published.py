"""Published metadata envelopes for synthetic SDK read models."""

import copy
from dataclasses import asdict, replace
from types import SimpleNamespace


def published_client(entities, sdk=None):
    """Expose inventory and getter revisions for independently authored fixture models."""

    def get(**kwargs):
        entity = entities[kwargs["by_id"]]
        if type(entity).__name__ == "Connection":
            return entity
        snapshot = copy.deepcopy(entity.response_snapshot)
        if type(entity).__name__ == "Dataset":
            snapshot["dataset"] = {
                **snapshot.get("dataset", {}),
                "sources": [asdict(source) for source in entity.sources],
                "result_schema": list(entity.result_schema),
                "source_avatars": list(entity.source_avatars),
            }
        else:
            snapshot.setdefault("entry", {})["data"] = copy.deepcopy(entity.data)
        revision = (
            {"rev_id": entity.published_id}
            if type(entity).__name__ in {"Dashboard", "Dataset"}
            else {}
        )
        return replace(entity, **revision, response_snapshot=snapshot)

    def entries(**kwargs):
        for identifier in kwargs["ids"]:
            entity = entities[identifier]
            yield SimpleNamespace(
                id=identifier,
                name=entity.name,
                key=entity.key,
                workbook_id=entity.workbook_id,
                scope={"Dashboard": "dash", "Dataset": "dataset", "Connection": "connection"}.get(
                    type(entity).__name__, "widget"
                ),
                type=getattr(entity, "wire_type", None),
                published_id=getattr(entity, "published_id", None),
                saved_id=getattr(entity, "saved_id", None),
            )

    client = SimpleNamespace(
        navigation=SimpleNamespace(get_entries=entries),
        get=SimpleNamespace(**dict.fromkeys(("dashboard", "chart", "dataset", "connection"), get)),
    )
    if sdk:
        for name in ("create", "capabilities", "INSTALLATION"):
            setattr(client, name, getattr(sdk, name))
    return client
