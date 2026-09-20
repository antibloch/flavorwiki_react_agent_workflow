import os
import re
from difflib import SequenceMatcher
from typing import Any

import psycopg
import sqlfluff
from dotenv import load_dotenv
from psycopg.rows import dict_row
from sqlfluff.core import FluffConfig
from sqlglot import exp, parse
from sqlglot.optimizer.scope import traverse_scope


load_dotenv()
DATABASE_URL = os.getenv("POSTGRES_DB_LINK")


_SQLFLUFF_CONFIG = FluffConfig(
    configs={
        "core": {
            "dialect": "postgres",
            "templater": "placeholder",
            "rules": "LT01,LT02,LT04,LT05,LT09,LT12,LT13,CP01",
        },
        "templater": {"placeholder": {"param_style": "pyformat"}},
        "rules": {
            "capitalisation.keywords": {"capitalisation_policy": "upper"},
        },
    }
)


def repair_postgres_sql(
    query: str,
    database_url: str | None = None,
    parameters: Any = (),
) -> str:
    """Repair, format, and plan one read-only PostgreSQL query."""
    dialect, tables, foreign_keys = "postgres", {}, []
    database_url = database_url or DATABASE_URL
    if not database_url:
        raise RuntimeError(
            "Provide database_url or set POSTGRES_DB_LINK in the environment or .env."
        )

    statements = parse(query.strip(), read=dialect)
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise ValueError("Exactly one read-only SELECT or WITH query is required.")
    tree, original = statements[0], query.strip()
    write_nodes = tuple(
        kind
        for name in (
            "Insert",
            "Update",
            "Delete",
            "Create",
            "Drop",
            "Alter",
            "Merge",
            "Into",
        )
        if (kind := getattr(exp, name, None))
    )
    if any(isinstance(node, write_nodes) for node in tree.walk()):
        raise ValueError("Only read-only SELECT or WITH queries are allowed.")

    connection = psycopg.connect(
        database_url,
        row_factory=dict_row,
        options="-c default_transaction_read_only=on",
    )
    try:
        cursor = connection.cursor()
        cursor.execute(
            """SELECT table_name, column_name, data_type
               FROM information_schema.columns
               WHERE table_schema = 'public'
               ORDER BY table_name, ordinal_position"""
        )
        for row in cursor.fetchall():
            tables.setdefault(row["table_name"], {})[row["column_name"]] = row[
                "data_type"
            ]
        cursor.execute(
            """
            SELECT s.relname AS source_table, sa.attname AS source_column,
                   t.relname AS target_table, ta.attname AS target_column
            FROM pg_constraint c
            JOIN pg_class s ON s.oid = c.conrelid
            JOIN pg_namespace sn ON sn.oid = s.relnamespace
            JOIN pg_class t ON t.oid = c.confrelid
            JOIN pg_namespace tn ON tn.oid = t.relnamespace
            JOIN pg_attribute sa ON sa.attrelid = s.oid AND sa.attnum = c.conkey[1]
            JOIN pg_attribute ta ON ta.attrelid = t.oid AND ta.attnum = c.confkey[1]
            WHERE c.contype = 'f' AND cardinality(c.conkey) = 1
              AND sn.nspname = 'public' AND tn.nspname = 'public'
            """
        )
        foreign_keys = [
            (
                row["source_table"],
                row["source_column"],
                row["target_table"],
                row["target_column"],
            )
            for row in cursor.fetchall()
        ]

        def identifier(node: Any, default: str | None = None) -> str | None:
            if not isinstance(node, exp.Identifier):
                return default if node is None else None
            return str(node.this) if node.args.get("quoted") else str(node.this).lower()

        def table_key(node: exp.Table) -> str | None:
            schema = identifier(node.args.get("db"), "public")
            name = identifier(node.this)
            return name if schema == "public" and name in tables else None

        def real_column(table: str, node: exp.Identifier) -> str | None:
            matches = [
                name
                for name in tables[table]
                if name.lower() == str(node.this).lower()
            ]
            return matches[0] if len(matches) == 1 else None

        def scope_sources(scope: Any) -> list[dict[str, Any]]:
            result = []
            for node in scope.sources.values():
                if not isinstance(node, exp.Table) or not (table := table_key(node)):
                    continue
                alias = node.args.get("alias")
                alias_node = (
                    alias.this
                    if alias and isinstance(alias.this, exp.Identifier)
                    else node.this
                )
                result.append(
                    {"table": table, "alias": str(alias_node.this), "node": node}
                )
            return result

        def find_source(
            qualifier: Any,
            sources: list[dict[str, Any]],
            allow_base: bool = False,
        ):
            if not isinstance(qualifier, exp.Identifier):
                return None
            wanted = str(qualifier.this).lower()
            matches = [
                source
                for source in sources
                if source["alias"].lower() == wanted
                or allow_base
                and source["table"].lower() == wanted
            ]
            return matches[0] if len(matches) == 1 else None

        def owners(
            column: exp.Identifier,
            sources: list[dict[str, Any]],
            excluded=None,
        ):
            return [
                source
                for source in sources
                if source is not excluded and real_column(source["table"], column)
            ]

        def quoted(name: str) -> exp.Identifier:
            needs_quotes = name != name.lower() or not re.fullmatch(
                r"[a-z_][a-z0-9_$]*", name
            )
            return exp.to_identifier(name, quoted=needs_quotes)

        def set_column(
            column: exp.Column,
            name: str | None = None,
            source=None,
        ) -> None:
            if name:
                column.set("this", quoted(name))
            if source:
                column.set("table", quoted(source["alias"]))

        def keys_between(left: str, right: str):
            return [
                key for key in foreign_keys if {key[0], key[2]} == {left, right}
            ]

        def matches_key(left, left_source, right, right_source, keys) -> bool:
            actual = (
                left_source["table"],
                left.name,
                right_source["table"],
                right.name,
            )
            return any(
                actual == (key[0], key[1], key[2], key[3])
                or actual == (key[2], key[3], key[0], key[1])
                for key in keys
            )

        def typo(name: str, sources: list[dict[str, Any]]):
            if len(name) < 5:
                return None
            candidates = []
            for source in sources:
                for column in tables[source["table"]]:
                    score = SequenceMatcher(
                        None,
                        name.lower(),
                        column.lower(),
                    ).ratio()
                    if score >= 0.70 and abs(len(name) - len(column)) <= 2:
                        candidates.append((score, column, source))
            candidates.sort(key=lambda item: (-item[0], item[1]))
            if not candidates or (
                len(candidates) > 1
                and candidates[0][0] - candidates[1][0] < 0.08
            ):
                return None
            return candidates[0][2], candidates[0][1]

        def normalized_date(value: str) -> str | None:
            for pattern, order in (
                (r"(\d{4})/(\d{1,2})/(\d{1,2})", "ymd"),
                (r"(\d{1,2})/(\d{1,2})/(\d{4})", "mdy"),
            ):
                if not (match := re.fullmatch(pattern, value)):
                    continue
                first, second, third = map(int, match.groups())
                if order == "ymd":
                    year, month, day = first, second, third
                elif first > 12 >= second:
                    day, month, year = first, second, third
                elif second > 12 >= first:
                    month, day, year = first, second, third
                else:
                    return None
                if 1 <= month <= 12 and 1 <= day <= 31:
                    return f"{year:04d}-{month:02d}-{day:02d}"
                return None
            return None

        def repair_once() -> bool:
            for scope in traverse_scope(tree):
                sources = scope_sources(scope)
                for column in scope.columns:
                    if not isinstance(column.this, exp.Identifier):
                        continue
                    qualifier = column.args.get("table")
                    source = find_source(qualifier, sources)
                    if source:
                        if real := real_column(source["table"], column.this):
                            expected_quotes = real != real.lower()
                            if column.name != real or bool(
                                column.this.args.get("quoted")
                            ) != expected_quotes:
                                set_column(column, real)
                                return True
                            continue
                        other_owners = owners(column.this, sources, source)
                        if (
                            len(other_owners) == 1
                            and column.find_ancestor(exp.Join) is None
                        ):
                            set_column(column, source=other_owners[0])
                            return True
                        if match := typo(column.name, [source]):
                            set_column(column, match[1])
                            return True
                        continue
                    if qualifier is not None:
                        base = find_source(qualifier, sources, allow_base=True)
                        if base and (real := real_column(base["table"], column.this)):
                            set_column(column, real, base)
                            return True
                        current_owners = owners(column.this, sources)
                        if len(current_owners) == 1:
                            set_column(column, source=current_owners[0])
                            return True
                        missing = identifier(qualifier)
                        if missing in tables and real_column(missing, column.this):
                            links = [
                                (item, key)
                                for item in sources
                                for key in keys_between(item["table"], missing)
                            ]
                            if len(links) == 1 and isinstance(
                                scope.expression,
                                exp.Select,
                            ):
                                item, key = links[0]
                                if item["table"] == key[0]:
                                    left_name, right_name = key[1], key[3]
                                else:
                                    left_name, right_name = key[3], key[1]
                                condition = exp.EQ(
                                    this=exp.Column(
                                        this=quoted(left_name),
                                        table=quoted(item["alias"]),
                                    ),
                                    expression=exp.Column(
                                        this=quoted(right_name),
                                        table=quoted(missing),
                                    ),
                                )
                                scope.expression.append(
                                    "joins",
                                    exp.Join(
                                        this=exp.Table(
                                            this=quoted(missing),
                                            db=quoted("public"),
                                        ),
                                        on=condition,
                                    ),
                                )
                                return True
                        continue
                    current_owners = owners(column.this, sources)
                    if not current_owners and (
                        match := typo(column.name, sources)
                    ):
                        set_column(
                            column,
                            match[1],
                            match[0] if len(sources) > 1 else None,
                        )
                        return True

                for column in scope.columns:
                    if not isinstance(column.this, exp.Identifier):
                        continue
                    source = find_source(column.args.get("table"), sources)
                    current_owners = owners(column.this, sources)
                    source = source or (
                        current_owners[0] if len(current_owners) == 1 else None
                    )
                    real = (
                        real_column(source["table"], column.this) if source else None
                    )
                    if not real or not tables[source["table"]][real].lower().startswith(
                        ("date", "time")
                    ):
                        continue
                    parent = column.parent
                    other = (
                        parent.right
                        if isinstance(parent, exp.Binary) and parent.left is column
                        else None
                    )
                    if (
                        isinstance(other, exp.Literal)
                        and other.is_string
                        and (fixed := normalized_date(str(other.this)))
                    ):
                        other.set("this", fixed)
                        return True

                for join in scope.expression.args.get("joins") or []:
                    condition = join.args.get("on")
                    if not isinstance(condition, exp.EQ):
                        continue
                    left, right = condition.left, condition.right
                    if not isinstance(left, exp.Column) or not isinstance(
                        right,
                        exp.Column,
                    ):
                        continue
                    left_source = find_source(left.args.get("table"), sources)
                    right_source = find_source(right.args.get("table"), sources)
                    if not left_source or not right_source:
                        continue
                    keys = keys_between(
                        left_source["table"],
                        right_source["table"],
                    )
                    if (
                        len(keys) == 1
                        and left.name.lower() == right.name.lower() == "id"
                    ):
                        key = keys[0]
                        if key[0] != key[2] and not matches_key(
                            left,
                            left_source,
                            right,
                            right_source,
                            keys,
                        ):
                            if left_source["table"] == key[0]:
                                names = key[1], key[3]
                            else:
                                names = key[3], key[1]
                            set_column(left, names[0], left_source)
                            set_column(right, names[1], right_source)
                            return True
            return False

        changed = False
        for _ in range(8):
            if not repair_once():
                break
            changed = True
            tree = parse(tree.sql(dialect=dialect), read=dialect)[0]

        for scope in traverse_scope(tree):
            sources = scope_sources(scope)
            for join in scope.expression.args.get("joins") or []:
                condition = join.args.get("on")
                if condition is None and str(join.args.get("kind") or "").upper() != (
                    "CROSS"
                ):
                    raise ValueError("JOIN has no ON condition.")
                if not isinstance(condition, exp.EQ):
                    continue
                left, right = condition.left, condition.right
                if not isinstance(left, exp.Column) or not isinstance(
                    right,
                    exp.Column,
                ):
                    continue
                left_source = find_source(left.args.get("table"), sources)
                right_source = find_source(right.args.get("table"), sources)
                if left_source and right_source:
                    keys = keys_between(
                        left_source["table"],
                        right_source["table"],
                    )
                    if keys and not matches_key(
                        left,
                        left_source,
                        right,
                        right_source,
                        keys,
                    ):
                        raise ValueError(
                            f"Suspicious JOIN: {condition.sql(dialect=dialect)}"
                        )

        repaired = tree.sql(dialect=dialect, comments=False) if changed else original
        formatted = sqlfluff.fix(repaired, config=_SQLFLUFF_CONFIG)

        formatted_statements = parse(formatted.strip(), read=dialect)
        if len(formatted_statements) != 1 or not isinstance(
            formatted_statements[0],
            exp.Query,
        ):
            raise ValueError("SQLFluff did not return one read-only query.")
        if any(
            isinstance(node, write_nodes)
            for node in formatted_statements[0].walk()
        ):
            raise ValueError("Only read-only SELECT or WITH queries are allowed.")

        cursor.execute("SET LOCAL statement_timeout = 3000")
        cursor.execute(f"EXPLAIN (FORMAT JSON) {formatted}", parameters)
        cursor.fetchone()
        return formatted
    finally:
        connection.close()
