"""Exact metadata projections over the outage fixture's configured instances."""

from __future__ import annotations

import re
from uuid import NAMESPACE_URL, uuid5

from analytics_toolkit.sql.backends.ch.cluster_functions import cluster_arguments
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from sqlglot import exp, parse_one

_LEGACY_MEMBERSHIP = ("shard_num", "replica_num", "host_name", "port")


def _native_port(client, host):
    ports = {
        row[3]
        for members in [client.cluster.members, *client.cluster.members_by_cluster.values()]
        for row in members
        if isinstance(row, (tuple, list))
        and len(row) >= 4
        and row[2] == host
        and isinstance(row[3], int)
        and 0 < row[3] <= 65535
    }
    if len(ports) == 1:
        return next(iter(ports))
    if not ports and host == client.host:
        return 9000
    return None


def _instance(host, port):
    identity = uuid5(NAMESPACE_URL, f"https://fixture.invalid/outage/{host}/{port}")
    return {
        "hostName": host,
        "host_name": host,
        "host_address": host,
        "port": port,
        "serverUUID": str(identity),
        "tcpPort": port,
    }


def _members(client, cluster):
    return client.cluster.members_by_cluster.get(cluster, client.cluster.members)


def _origins(client, source):
    if not isinstance(source.this, exp.Anonymous):
        port = _native_port(client, client.host)
        return [(1, client.host, port)]
    name, _database, _relation, _trailing = cluster_arguments(source.this, "db")
    return [
        (position, row[2], row[3])
        for position, row in enumerate(_members(client, name), 1)
        if isinstance(row, (tuple, list))
        and len(row) >= 4
        and row[2] in client.cluster.alive
        and row[3] == _native_port(client, row[2])
    ]


def _cluster_names(statement):
    names = set()
    for condition in statement.find_all(exp.EQ):
        if isinstance(condition.this, exp.Column) and condition.this.name == "cluster":
            names.add(str(_project(condition.expression, {})))
    for condition in statement.find_all(exp.In):
        if isinstance(condition.this, exp.Column) and condition.this.name == "cluster":
            names.update(str(_project(item, {})) for item in condition.expressions)
    return names or {"core"}


def _cluster_records(client, origins, names):
    records = []
    for position, origin, native_port in origins:
        for name in sorted(names):
            for shard, replica, host, port in _members(client, name):
                records.append(
                    {
                        **_instance(host, port),
                        "_shard_num": position,
                        "cluster": name,
                        "shard_num": shard,
                        "replica_num": replica,
                        "is_local": int((host, port) == (origin, native_port)),
                    }
                )
    return records


def _table_records(client, origins):
    return [
        {
            **_instance(host, port),
            **_table_definition(value),
            "_shard_num": position,
        }
        for position, host, port in origins
        for (resource_host, _name), value in client.cluster.tables.items()
        if resource_host == host
    ]


def _table_definition(value):
    statement = parse_one(value.ddl, read=ClickHouseDialect)
    target = statement.this if isinstance(statement, exp.Create) else None
    target = target.this if isinstance(target, exp.Schema) else target
    if not isinstance(target, exp.Table):
        message = "Outage table metadata requires its actual CREATE TABLE identity."
        raise TypeError(message)
    return {
        "database": target.db or "db",
        "name": target.name,
        "engine": value.engine,
        "engine_full": value.engine_full,
        "create_table_query": value.ddl,
        "uuid": value.uuid,
    }


def _macro_records(client, origins):
    records = []
    for position, host, _port in origins:
        member = next(
            (
                row
                for row in client.cluster.members
                if isinstance(row, (tuple, list)) and len(row) >= 4 and row[2] == host
            ),
            (1, 1, host, _port),
        )
        macros = {
            "shard": str(member[0]),
            "replica": host,
            "cluster": "core",
            **client.cluster.host_macros.get(host, {}),
        }
        records.extend(
            {"_shard_num": position, "macro": name, "substitution": value}
            for name, value in macros.items()
        )
    return records


def _metadata_records(client, statement, table, origins, settings):
    if table == "clusters":
        names = _cluster_names(statement)
        if any(
            not isinstance(row, (tuple, list)) or len(row) != 4
            for name in names
            for row in _members(client, name)
        ):
            return None
        return _cluster_records(client, origins, names)
    if table == "tables":
        return _table_records(client, origins)
    if table == "macros":
        return _macro_records(client, origins)
    if table == "one":
        if isinstance(statement.args["from_"].this.this, exp.Anonymous):
            client.cluster.probe_requests.append(dict(settings or {}))
            if client.cluster.probe_failure:
                raise client.cluster.probe_failure
        return [
            {**_instance(host, port), "_shard_num": position}
            for position, host, port in origins
            if port is not None
        ]
    return None


def metadata_rows(client, statement, *, settings=None):
    if not isinstance(statement, exp.Select):
        return None
    fields = tuple(_field_name(item).casefold() for item in statement.expressions)
    source = statement.args.get("from_")
    if source is None:
        return _local_identity(client, fields)
    selected = _system_source(source.this)
    if selected is None:
        return None
    source, table, remote = selected
    if table == "clusters" and fields == _LEGACY_MEMBERSHIP and not remote:
        name = next(iter(_cluster_names(statement)))
        return list(_members(client, name))
    records = _metadata_records(client, statement, table, _origins(client, source), settings)
    if records is None:
        return None
    return _project_rows(statement, records)


def _local_identity(client, fields):
    if fields != ("serveruuid", "tcpport"):
        return None
    port = _native_port(client, client.host)
    return [(str(_instance(client.host, port)["serverUUID"]), port)] if port else []


def _system_source(source):
    if not isinstance(source, exp.Table):
        return None
    remote = isinstance(source.this, exp.Anonymous)
    if remote:
        if source.this.name.casefold() not in {"cluster", "clusterallreplicas"}:
            return None
        _cluster, database, table, _trailing = cluster_arguments(source.this, "db")
    else:
        database, table = source.db, source.name
    if database != "system" or table not in {"clusters", "one", "tables", "macros"}:
        return None
    return source, table, remote


def _project_rows(statement, records):
    where = statement.args.get("where")
    if where is not None:
        records = [record for record in records if _matches(where.this, record)]
    if len(statement.expressions) == 1 and isinstance(statement.expressions[0], exp.Count):
        return [(len(records),)]
    order = statement.args.get("order")
    if order is not None:
        records.sort(
            key=lambda record: tuple(_project(item.this, record) for item in order.expressions)
        )
    rows = [tuple(_project(item, record) for item in statement.expressions) for record in records]
    if statement.args.get("distinct") is not None:
        rows = [row for index, row in enumerate(rows) if row not in rows[:index]]
    return rows


def _field_name(expression):
    return expression.name or (
        expression.sql_name().replace("_", "") if isinstance(expression, exp.Func) else ""
    )


def _project(expression, record):
    if isinstance(expression, (exp.Alias, exp.Paren)):
        return _project(expression.this, record)
    if isinstance(expression, exp.Literal):
        return expression.to_py()
    if isinstance(expression, exp.Cast):
        return str(_project(expression.this, record))
    if isinstance(expression, exp.Anonymous) and expression.name.casefold() == "tostring":
        arguments = expression.expressions
        if len(arguments) != 1:
            message = "Unsupported outage metadata toString argument count."
            raise TypeError(message)
        return str(_project(arguments[0], record))
    name = _field_name(expression)
    return next(value for key, value in record.items() if key.casefold() == name.casefold())


def _matches(expression, record):
    if isinstance(expression, exp.Paren):
        return _matches(expression.this, record)
    if isinstance(expression, exp.And):
        return _matches(expression.this, record) and _matches(expression.expression, record)
    if isinstance(expression, exp.Or):
        return _matches(expression.this, record) or _matches(expression.expression, record)
    if isinstance(expression, exp.EQ):
        return _project(expression.this, record) == _project(expression.expression, record)
    if isinstance(expression, exp.In):
        return _project(expression.this, record) in [
            _project(item, record) for item in expression.expressions
        ]
    return _text_match(expression, record)


def _text_match(expression, record):
    if isinstance(expression, exp.Like):
        pattern = (
            re.escape(str(_project(expression.expression, record)))
            .replace("%", ".*")
            .replace("_", ".")
        )
        return re.fullmatch(pattern, str(_project(expression.this, record))) is not None
    if isinstance(expression, exp.StartsWith):
        return str(_project(expression.this, record)).startswith(
            str(_project(expression.expression, record))
        )
    if isinstance(expression, exp.Anonymous) and expression.name.casefold() == "startswith":
        left, right = expression.expressions
        return str(_project(left, record)).startswith(str(_project(right, record)))
    message = f"Unsupported outage metadata predicate: {expression}"
    raise TypeError(message)
