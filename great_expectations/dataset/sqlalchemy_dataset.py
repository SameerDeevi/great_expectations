import inspect
import logging
import uuid
import warnings
from datetime import datetime
from functools import wraps
from importlib import import_module
from typing import List

import numpy as np
import pandas as pd
from dateutil.parser import parse

from great_expectations.data_asset import DataAsset
from great_expectations.data_asset.util import DocInherit, parse_result_format
from .dataset import Dataset
from .pandas_dataset import PandasDataset

logger = logging.getLogger(__name__)

try:
    import sqlalchemy as sa
    from sqlalchemy.engine import reflection
    from sqlalchemy.sql.expression import BinaryExpression, literal
    from sqlalchemy.sql.operators import custom_op
except ImportError:
    logger.debug(
        "Unable to load SqlAlchemy context; install optional sqlalchemy dependency for support"
    )

try:
    import sqlalchemy_redshift.dialect
except ImportError:
    sqlalchemy_redshift = None

try:
    import snowflake.sqlalchemy.snowdialect
except ImportError:
    snowflake = None

try:
    import pybigquery.sqlalchemy_bigquery

    try:
        getattr(pybigquery.sqlalchemy_bigquery, "INTEGER")
        bigquery_types_tuple = None
    except AttributeError:
        # In older versions of the pybigquery driver, types were not exported, so we use a hack
        logger.warning(
            "Old pybigquery driver version detected. Consider upgrading to 0.4.14 or later."
        )
        from collections import namedtuple

        BigQueryTypes = namedtuple(
            "BigQueryTypes", sorted(pybigquery.sqlalchemy_bigquery._type_map)
        )
        bigquery_types_tuple = BigQueryTypes(**pybigquery.sqlalchemy_bigquery._type_map)
except ImportError:
    bigquery_types_tuple = None
    pybigquery = None


class SqlAlchemyBatchReference(object):
    def __init__(self, engine, table_name=None, schema=None, query=None):
        self._engine = engine
        if table_name is None and query is None:
            raise ValueError("Table_name or query must be specified")

        self._table_name = table_name
        self._schema = schema
        self._query = query

    def get_init_kwargs(self):
        if self._table_name and self._query:
            # This is allowed in BigQuery where a temporary table name must be provided *with* the
            # custom sql to execute.
            kwargs = {
                "engine": self._engine,
                "table_name": self._table_name,
                "custom_sql": self._query,
            }
        elif self._table_name:
            kwargs = {"engine": self._engine, "table_name": self._table_name}
        else:
            kwargs = {"engine": self._engine, "custom_sql": self._query}
        if self._schema:
            kwargs["schema"] = self._schema

        return kwargs


class MetaSqlAlchemyDataset(Dataset):
    def __init__(self, *args, **kwargs):
        super(MetaSqlAlchemyDataset, self).__init__(*args, **kwargs)

    @classmethod
    def column_map_expectation(cls, func):
        """For SqlAlchemy, this decorator allows individual column_map_expectations to simply return the filter
        that describes the expected condition on their data.

        The decorator will then use that filter to obtain unexpected elements, relevant counts, and return the formatted
        object.
        """
        argspec = inspect.getfullargspec(func)[0][1:]

        @cls.expectation(argspec)
        @wraps(func)
        def inner_wrapper(
            self, column, mostly=None, result_format=None, *args, **kwargs
        ):
            if result_format is None:
                result_format = self.default_expectation_args["result_format"]

            result_format = parse_result_format(result_format)

            if result_format["result_format"] == "COMPLETE":
                warnings.warn(
                    "Setting result format to COMPLETE for a SqlAlchemyDataset can be dangerous because it will not limit the number of returned results."
                )
                unexpected_count_limit = None
            else:
                unexpected_count_limit = result_format["partial_unexpected_count"]

            expected_condition = func(self, column, *args, **kwargs)

            # Added to prepare for when an ignore_values argument is added to the expectation
            ignore_values = [None]
            if func.__name__ in [
                "expect_column_values_to_not_be_null",
                "expect_column_values_to_be_null",
            ]:
                ignore_values = []
                # Counting the number of unexpected values can be expensive when there is a large
                # number of np.nan values.
                # This only happens on expect_column_values_to_not_be_null expectations.
                # Since there is no reason to look for most common unexpected values in this case,
                # we will instruct the result formatting method to skip this step.
                result_format["partial_unexpected_count"] = 0

            ignore_values_conditions = []
            if (
                len(ignore_values) > 0
                and None not in ignore_values
                or len(ignore_values) > 1
                and None in ignore_values
            ):
                ignore_values_conditions += [
                    sa.column(column).in_(
                        [val for val in ignore_values if val is not None]
                    )
                ]
            if None in ignore_values:
                ignore_values_conditions += [sa.column(column).is_(None)]

            if len(ignore_values_conditions) > 1:
                ignore_values_condition = sa.or_(*ignore_values_conditions)
            elif len(ignore_values_conditions) == 1:
                ignore_values_condition = ignore_values_conditions[0]
            else:
                ignore_values_condition = sa.literal(False)

            count_query = sa.select(
                [
                    sa.func.count().label("element_count"),
                    sa.func.sum(sa.case([(ignore_values_condition, 1)], else_=0)).label(
                        "null_count"
                    ),
                    sa.func.sum(
                        sa.case(
                            [
                                (
                                    sa.and_(
                                        sa.not_(expected_condition),
                                        sa.not_(ignore_values_condition),
                                    ),
                                    1,
                                )
                            ],
                            else_=0,
                        )
                    ).label("unexpected_count"),
                ]
            ).select_from(self._table)

            count_results = dict(self.engine.execute(count_query).fetchone())

            # Handle case of empty table gracefully:
            if (
                "element_count" not in count_results
                or count_results["element_count"] is None
            ):
                count_results["element_count"] = 0
            if "null_count" not in count_results or count_results["null_count"] is None:
                count_results["null_count"] = 0
            if (
                "unexpected_count" not in count_results
                or count_results["unexpected_count"] is None
            ):
                count_results["unexpected_count"] = 0

            # Retrieve unexpected values
            unexpected_query_results = self.engine.execute(
                sa.select([sa.column(column)])
                .select_from(self._table)
                .where(
                    sa.and_(
                        sa.not_(expected_condition), sa.not_(ignore_values_condition)
                    )
                )
                .limit(unexpected_count_limit)
            )

            nonnull_count = count_results["element_count"] - count_results["null_count"]

            if "output_strftime_format" in kwargs:
                output_strftime_format = kwargs["output_strftime_format"]
                maybe_limited_unexpected_list = []
                for x in unexpected_query_results.fetchall():
                    if isinstance(x[column], str):
                        col = parse(x[column])
                    else:
                        col = x[column]
                    maybe_limited_unexpected_list.append(
                        datetime.strftime(col, output_strftime_format)
                    )
            else:
                maybe_limited_unexpected_list = [
                    x[column] for x in unexpected_query_results.fetchall()
                ]

            success_count = nonnull_count - count_results["unexpected_count"]
            success, percent_success = self._calc_map_expectation_success(
                success_count, nonnull_count, mostly
            )

            return_obj = self._format_map_output(
                result_format,
                success,
                count_results["element_count"],
                nonnull_count,
                count_results["unexpected_count"],
                maybe_limited_unexpected_list,
                None,
            )

            if func.__name__ in [
                "expect_column_values_to_not_be_null",
                "expect_column_values_to_be_null",
            ]:
                # These results are unnecessary for the above expectations
                del return_obj["result"]["unexpected_percent_nonmissing"]
                del return_obj["result"]["missing_count"]
                del return_obj["result"]["missing_percent"]
                try:
                    del return_obj["result"]["partial_unexpected_counts"]
                    del return_obj["result"]["partial_unexpected_list"]
                except KeyError:
                    pass

            return return_obj

        inner_wrapper.__name__ = func.__name__
        inner_wrapper.__doc__ = func.__doc__

        return inner_wrapper


class SqlAlchemyDataset(MetaSqlAlchemyDataset):
    """
Feature: postgresql
-> production
API Stability: High
Implementation Completeness: Complete
Unit Test Coverage: Complete
Infrastructure Coverage: Complete
Documentation Completeness: Medium [doesn’t have “specific” how-to, but we say ‘easy to use’ overall]
Bug Risk: Low
Expectation Completeness: Moderate+ (all sqlalchemy works)

Bigquery
	• API Stability: Unstable
		○ Table generator inability to work with triple-dotted
		○ Temp table usability
		○ Init flow calls setup "other"
	• Implementation Completeness
	• Unit test: Partial
		○ No test coverage for temp table creation, e.g.
	• Infrastructure: Minimal (optional)
	• Documentation: Partial
		○ How-to does not cover all cases
	• Bug frequency: Expected
		○ We *know* of several bugs, including inability to list tables
		○ Sqlalchemy URL incomplete
Bug Frequency -> Bug Risk == H/M/L (footnote?)
Thorough -> Complete
Bug Frequency: "Expected" == "Expected and/or Known"
Infrastructure test "Optional" == "minimal"

Feature: redshift
->
API Stability: Moderate [unresolved driver recommendation; potential metadata/introspection method special handling for performance]
Implementation Completeness: Complete
Unit Test Coverage: Minimal
Infrastructure Coverage: Minimal [None (not automated)]
Documentation Completeness: moderate
Bug Risk: Moderate [sqlalchemy driver may be difficult to find, and we do not have a clear recommendation for the correct driver]
Expectation Completeness: Moderate+ (all sqlalchemy works)


Feature: snowflake
->
API Stability:
Implementation Completeness:
Unit Test Coverage:
Infrastructure Coverage:
Documentation Completeness:
Bug Risk:
Expectation Completeness:

Feature: mssql
-> Experimental
API Stability: High
Implementation Completeness: Moderate
Unit Test Coverage: Minimal [None]
Infrastructure Coverage: Minimal [None]
Documentation Completeness: Minimal
Bug Risk: High
Expectation Completeness: Low [some required queries do not generate properly, such as related to nullity]

Feature: mysql
-> experimental
API Stability: Low [no consideration for temp tables]
Implementation Completeness: Low [no consideration for temp tables]
Unit Test Coverage: Minimal [None]
Infrastructure Coverage: Minimal [None]
Documentation Completeness:  Minimal [None]
Bug Risk: [Unknown]
Expectation Completeness: [Unknown]

Feature: mariadb
-> experimental
API Stability: Low [no consideration for temp tables]
Implementation Completeness: Low [no consideration for temp tables]
Unit Test Coverage: Minimal [None]
Infrastructure Coverage: Minimal [None]
Documentation Completeness:  Minimal [None]
Bug Risk: [Unknown]
Expectation Completeness: [Unknown]


Feature: validation_engine_sqlalchemy
API Stability: High
Implementation Completeness: Moderate [temp table handling / permissions not universal]
Unit Test Coverage: High
Infrastructure Coverage: N/A
Documentation Completeness:  Minimal [None]
Bug Risk: Low



    """

    @classmethod
    def from_dataset(cls, dataset=None):
        if isinstance(dataset, SqlAlchemyDataset):
            return cls(table_name=str(dataset._table.name), engine=dataset.engine)
        else:
            raise ValueError("from_dataset requires a SqlAlchemy dataset")

    def __init__(
        self,
        table_name=None,
        engine=None,
        connection_string=None,
        custom_sql=None,
        schema=None,
        *args,
        **kwargs
    ):

        if custom_sql and not table_name:
            # NOTE: Eugene 2020-01-31: @James, this is a not a proper fix, but without it the "public" schema
            # was used for a temp table and raising an error
            schema = None
            table_name = "ge_tmp_" + str(uuid.uuid4())[:8]
            # mssql expects all temporary table names to have a prefix '#'
            if engine.dialect.name.lower() == "mssql":
                table_name = "#" + table_name
            generated_table_name = table_name
        else:
            generated_table_name = None

        if table_name is None:
            raise ValueError("No table_name provided.")

        if engine is None and connection_string is None:
            raise ValueError("Engine or connection_string must be provided.")

        if engine is not None:
            self.engine = engine
        else:
            try:
                self.engine = sa.create_engine(connection_string)
            except Exception as err:
                # Currently we do no error handling if the engine doesn't work out of the box.
                raise err

        if self.engine.dialect.name.lower() == "bigquery":
            # In BigQuery the table name is already qualified with its schema name
            self._table = sa.Table(table_name, sa.MetaData(), schema=None)
        else:
            self._table = sa.Table(table_name, sa.MetaData(), schema=schema)

        # Get the dialect **for purposes of identifying types**
        if self.engine.dialect.name.lower() in [
            "postgresql",
            "mysql",
            "sqlite",
            "oracle",
            "mssql",
            "oracle",
        ]:
            # These are the officially included and supported dialects by sqlalchemy
            self.dialect = import_module(
                "sqlalchemy.dialects." + self.engine.dialect.name
            )

            if engine and engine.dialect.name.lower() in ["sqlite", "mssql"]:
                # sqlite/mssql temp tables only persist within a connection so override the engine
                self.engine = engine.connect()
        elif self.engine.dialect.name.lower() == "snowflake":
            self.dialect = import_module("snowflake.sqlalchemy.snowdialect")
        elif self.engine.dialect.name.lower() == "redshift":
            self.dialect = import_module("sqlalchemy_redshift.dialect")
        elif self.engine.dialect.name.lower() == "bigquery":
            self.dialect = import_module("pybigquery.sqlalchemy_bigquery")
        else:
            self.dialect = None

        if schema is not None and custom_sql is not None:
            # temporary table will be written to temp schema, so don't allow
            # a user-defined schema
            # NOTE: 20200306 - JPC - Previously, this would disallow both custom_sql (a query) and a schema, but
            # that is overly restrictive -- snowflake could have had a schema specified, for example, in which to create
            # a temporary table.
            # raise ValueError("Cannot specify both schema and custom_sql.")
            pass

        if custom_sql is not None and self.engine.dialect.name.lower() == "bigquery":
            if (
                generated_table_name is not None
                and self.engine.dialect.dataset_id is None
            ):
                raise ValueError(
                    "No BigQuery dataset specified. Use bigquery_temp_table batch_kwarg or a specify a "
                    "default dataset in engine url"
                )

        if (
            custom_sql is not None
            and self.engine.dialect.name.lower() == "snowflake"
            and generated_table_name is not None
        ):
            raise ValueError(
                "No snowflake_transient_table specified. Snowflake with a query batch_kwarg will create "
                "a transient table, so you must provide a user-selected name."
            )

        if custom_sql:
            self.create_temporary_table(table_name, custom_sql, schema_name=schema)

            if (
                generated_table_name is not None
                and self.engine.dialect.name.lower() == "bigquery"
            ):
                logger.warning(
                    "Created permanent table {table_name}".format(table_name=table_name)
                )

        try:
            insp = reflection.Inspector.from_engine(self.engine)
            self.columns = insp.get_columns(table_name, schema=schema)
        except KeyError:
            # we will get a KeyError for temporary tables, since
            # reflection will not find the temporary schema
            self.columns = self.column_reflection_fallback()

        # Use fallback because for mssql reflection doesn't throw an error but returns an empty list
        if len(self.columns) == 0:
            self.columns = self.column_reflection_fallback()

        # Only call super once connection is established and table_name and columns known to allow autoinspection
        super(SqlAlchemyDataset, self).__init__(*args, **kwargs)

    def head(self, n=5):
        """Returns a *PandasDataset* with the first *n* rows of the given Dataset"""

        try:
            df = next(
                pd.read_sql_table(
                    table_name=self._table.name,
                    schema=self._table.schema,
                    con=self.engine,
                    chunksize=n,
                )
            )
        except (ValueError, NotImplementedError):
            # it looks like MetaData that is used by pd.read_sql_table
            # cannot work on a temp table.
            # If it fails, we are trying to get the data using read_sql
            head_sql_str = "select * from "
            if self._table.schema and self.engine.dialect.name.lower() != "bigquery":
                head_sql_str += self._table.schema + "."
            elif self.engine.dialect.name.lower() == "bigquery":
                head_sql_str += "`" + self._table.name + "`"
            else:
                head_sql_str += self._table.name
            head_sql_str += " limit {0:d}".format(n)

            # Limit is unknown in mssql! Use top instead!
            if self.engine.dialect.name.lower() == "mssql":
                head_sql_str = "select top({n}) * from {table}".format(
                    n=n, table=self._table.name
                )

            df = pd.read_sql(head_sql_str, con=self.engine)

        return PandasDataset(
            df,
            expectation_suite=self.get_expectation_suite(
                discard_failed_expectations=False,
                discard_result_format_kwargs=False,
                discard_catch_exceptions_kwargs=False,
                discard_include_config_kwargs=False,
            ),
        )

    def get_row_count(self):
        count_query = sa.select([sa.func.count()]).select_from(self._table)
        return int(self.engine.execute(count_query).scalar())

    def get_column_count(self):
        return len(self.columns)

    def get_table_columns(self) -> List[str]:
        return [col["name"] for col in self.columns]

    def get_column_nonnull_count(self, column):
        ignore_values = [None]
        count_query = sa.select(
            [
                sa.func.count().label("element_count"),
                sa.func.sum(
                    sa.case(
                        [
                            (
                                sa.or_(
                                    sa.column(column).in_(ignore_values),
                                    # Below is necessary b/c sa.in_() uses `==` but None != None
                                    # But we only consider this if None is actually in the list of ignore values
                                    sa.column(column).is_(None)
                                    if None in ignore_values
                                    else False,
                                ),
                                1,
                            )
                        ],
                        else_=0,
                    )
                ).label("null_count"),
            ]
        ).select_from(self._table)
        count_results = dict(self.engine.execute(count_query).fetchone())
        element_count = int(count_results.get("element_count") or 0)
        null_count = int(count_results.get("null_count") or 0)
        return element_count - null_count

    def get_column_sum(self, column):
        return self.engine.execute(
            sa.select([sa.func.sum(sa.column(column))]).select_from(self._table)
        ).scalar()

    def get_column_max(self, column, parse_strings_as_datetimes=False):
        if parse_strings_as_datetimes:
            raise NotImplementedError
        return self.engine.execute(
            sa.select([sa.func.max(sa.column(column))]).select_from(self._table)
        ).scalar()

    def get_column_min(self, column, parse_strings_as_datetimes=False):
        if parse_strings_as_datetimes:
            raise NotImplementedError
        return self.engine.execute(
            sa.select([sa.func.min(sa.column(column))]).select_from(self._table)
        ).scalar()

    def get_column_value_counts(self, column, sort="value", collate=None):
        if sort not in ["value", "count", "none"]:
            raise ValueError("sort must be either 'value', 'count', or 'none'")

        query = (
            sa.select(
                [
                    sa.column(column).label("value"),
                    sa.func.count(sa.column(column)).label("count"),
                ]
            )
            .where(sa.column(column) != None)
            .group_by(sa.column(column))
        )
        if sort == "value":
            # NOTE: depending on the way the underlying database collates columns,
            # ordering can vary. postgresql collate "C" matches default sort
            # for python and most other systems, but is not universally supported,
            # so we use the default sort for the system, unless specifically overridden
            if collate is not None:
                query = query.order_by(sa.column(column).collate(collate))
            else:
                query = query.order_by(sa.column(column))
        elif sort == "count":
            query = query.order_by(sa.column("count").desc())
        results = self.engine.execute(query.select_from(self._table)).fetchall()
        series = pd.Series(
            [row[1] for row in results],
            index=pd.Index(data=[row[0] for row in results], name="value"),
            name="count",
        )
        return series

    def get_column_mean(self, column):
        return self.engine.execute(
            sa.select([sa.func.avg(sa.column(column))]).select_from(self._table)
        ).scalar()

    def get_column_unique_count(self, column):
        return self.engine.execute(
            sa.select([sa.func.count(sa.func.distinct(sa.column(column)))]).select_from(
                self._table
            )
        ).scalar()

    def get_column_median(self, column):
        nonnull_count = self.get_column_nonnull_count(column)
        element_values = self.engine.execute(
            sa.select([sa.column(column)])
            .order_by(sa.column(column))
            .where(sa.column(column) != None)
            .offset(max(nonnull_count // 2 - 1, 0))
            .limit(2)
            .select_from(self._table)
        )

        column_values = list(element_values.fetchall())

        if len(column_values) == 0:
            column_median = None
        elif nonnull_count % 2 == 0:
            # An even number of column values: take the average of the two center values
            column_median = (
                float(
                    column_values[0][0]
                    + column_values[1][0]  # left center value  # right center value
                )
                / 2.0
            )  # Average center values
        else:
            # An odd number of column values, we can just take the center value
            column_median = column_values[1][0]  # True center value
        return column_median

    def get_column_quantiles(self, column, quantiles, allow_relative_error=False):
        selects = None
        if self.engine.dialect.name.lower() == "mssql":
            # mssql requires over(), so we add an empty over() clause
            selects = [
                sa.func.percentile_disc(quantile)
                .within_group(sa.column(column).asc())
                .over()
                for quantile in quantiles
            ]
        elif self.engine.dialect.name.lower() == "bigquery":
            # BigQuery does not support "WITHIN", so we need a special case for it
            selects = [
                sa.func.percentile_disc(sa.column(column), quantile).over()
                for quantile in quantiles
            ]
        else:
            try:
                if isinstance(
                    self.engine.dialect, sqlalchemy_redshift.dialect.RedshiftDialect
                ):
                    # Redshift does not have a percentile_disc method, but does support an approximate version
                    if allow_relative_error is True:
                        selects = [
                            sa.text(
                                ", ".join(
                                    [
                                        "approximate "
                                        + str(
                                            stmt.compile(
                                                dialect=self.engine.dialect,
                                                compile_kwargs={"literal_binds": True},
                                            )
                                        )
                                        for stmt in selects
                                    ]
                                )
                            )
                        ]
                    else:
                        raise ValueError(
                            "Redshift does not support computing quantiles without approximation error; "
                            "set allow_relative_error to True to allow approximate quantiles."
                        )
            except (AttributeError, TypeError):
                pass

        if not selects:
            selects = [
                sa.func.percentile_disc(quantile).within_group(sa.column(column).asc())
                for quantile in quantiles
            ]

        quantiles = self.engine.execute(
            sa.select(selects).select_from(self._table)
        ).fetchone()
        return list(quantiles)

    def get_column_stdev(self, column):
        if self.engine.dialect.name.lower() != "mssql":
            res = self.engine.execute(
                sa.select([sa.func.stddev_samp(sa.column(column))])
                .select_from(self._table)
                .where(sa.column(column) != None)
            ).fetchone()
        else:
            # stdev_samp is not a recognized built-in function name but stdevp does exist for mssql!
            res = self.engine.execute(
                sa.select([sa.func.stdevp(sa.column(column))])
                .select_from(self._table)
                .where(sa.column(column) != None)
            ).fetchone()
        return float(res[0])

    def get_column_hist(self, column, bins):
        """return a list of counts corresponding to bins

        Args:
            column: the name of the column for which to get the histogram
            bins: tuple of bin edges for which to get histogram values; *must* be tuple to support caching
        """
        case_conditions = []
        idx = 0
        bins = list(bins)

        # If we have an infinte lower bound, don't express that in sql
        if (bins[0] == -np.inf) or (bins[0] == -float("inf")):
            case_conditions.append(
                sa.func.sum(
                    sa.case([(sa.column(column) < bins[idx + 1], 1)], else_=0)
                ).label("bin_" + str(idx))
            )
            idx += 1

        for idx in range(idx, len(bins) - 2):
            case_conditions.append(
                sa.func.sum(
                    sa.case(
                        [
                            (
                                sa.and_(
                                    bins[idx] <= sa.column(column),
                                    sa.column(column) < bins[idx + 1],
                                ),
                                1,
                            )
                        ],
                        else_=0,
                    )
                ).label("bin_" + str(idx))
            )

        if (bins[-1] == np.inf) or (bins[-1] == float("inf")):
            case_conditions.append(
                sa.func.sum(
                    sa.case([(bins[-2] <= sa.column(column), 1)], else_=0)
                ).label("bin_" + str(len(bins) - 1))
            )
        else:
            case_conditions.append(
                sa.func.sum(
                    sa.case(
                        [
                            (
                                sa.and_(
                                    bins[-2] <= sa.column(column),
                                    sa.column(column) <= bins[-1],
                                ),
                                1,
                            )
                        ],
                        else_=0,
                    )
                ).label("bin_" + str(len(bins) - 1))
            )

        query = (
            sa.select(case_conditions)
            .where(sa.column(column) != None,)
            .select_from(self._table)
        )

        hist = list(self.engine.execute(query).fetchone())
        return hist

    def get_column_count_in_range(
        self, column, min_val=None, max_val=None, strict_min=False, strict_max=True
    ):
        if min_val is None and max_val is None:
            raise ValueError("Must specify either min or max value")
        if min_val is not None and max_val is not None and min_val > max_val:
            raise ValueError("Min value must be <= to max value")

        min_condition = None
        max_condition = None
        if min_val is not None:
            if strict_min:
                min_condition = sa.column(column) > min_val
            else:
                min_condition = sa.column(column) >= min_val
        if max_val is not None:
            if strict_max:
                max_condition = sa.column(column) < max_val
            else:
                max_condition = sa.column(column) <= max_val

        if min_condition is not None and max_condition is not None:
            condition = sa.and_(min_condition, max_condition)
        elif min_condition is not None:
            condition = min_condition
        else:
            condition = max_condition

        query = query = (
            sa.select([sa.func.count((sa.column(column)))])
            .where(sa.and_(sa.column(column) != None, condition))
            .select_from(self._table)
        )

        return self.engine.execute(query).scalar()

    def create_temporary_table(self, table_name, custom_sql, schema_name=None):
        """
        Create Temporary table based on sql query. This will be used as a basis for executing expectations.
        WARNING: this feature is new in v0.4.
        It hasn't been tested in all SQL dialects, and may change based on community feedback.
        :param custom_sql:
        """

        ###
        # NOTE: 20200310 - The update to support snowflake transient table creation revealed several
        # import cases that are not fully handled.
        # The snowflake-related change updated behavior to allow both custom_sql and schema to be specified. But
        # the underlying incomplete handling of schema remains.
        #
        # Several cases we need to consider:
        #
        # 1. Distributed backends (e.g. Snowflake and BigQuery) often use a `<database>.<schema>.<table>`
        # syntax, but currently we are biased towards only allowing schema.table
        #
        # 2. In the wild, we see people using several ways to declare the schema they want to use:
        # a. In the connection string, the original RFC only specifies database, but schema is supported by some
        # backends (Snowflake) as a query parameter.
        # b. As a default for a user (the equivalent of USE SCHEMA being provided at the beginning of a session)
        # c. As part of individual queries.
        #
        # 3. We currently don't make it possible to select from a table in one query, but create a temporary table in
        # another schema, except for with BigQuery and (now) snowflake, where you can specify the table name (and
        # potentially trip of database, schema, table) in the batch_kwargs.
        #
        # The SqlAlchemyDataset interface essentially predates the batch_kwargs concept and so part of what's going
        # on, I think, is a mismatch between those. I think we should rename custom_sql -> "temp_table_query" or
        # similar, for example.
        ###

        if self.engine.dialect.name.lower() == "bigquery":
            stmt = "CREATE OR REPLACE TABLE `{table_name}` AS {custom_sql}".format(
                table_name=table_name, custom_sql=custom_sql
            )
        elif self.engine.dialect.name.lower() == "snowflake":
            logger.info("Creating transient table %s" % table_name)
            if schema_name is not None:
                table_name = schema_name + "." + table_name
            stmt = "CREATE OR REPLACE TRANSIENT TABLE {table_name} AS {custom_sql}".format(
                table_name=table_name, custom_sql=custom_sql
            )
        elif self.engine.dialect.name == "mysql":
            stmt = "CREATE TEMPORARY TABLE {table_name} AS {custom_sql}".format(
                table_name=table_name, custom_sql=custom_sql
            )
        elif self.engine.dialect.name == "mssql":
            # Insert "into #{table_name}" in the custom sql query right before the "from" clause
            # Split is case sensitive so detect case.
            # Note: transforming custom_sql to uppercase/lowercase has uninteded consequences (i.e., changing column names), so this is not an option!
            if "from" in custom_sql:
                strsep = "from"
            else:
                strsep = "FROM"
            custom_sqlmod = custom_sql.split(strsep, maxsplit=1)
            stmt = (
                custom_sqlmod[0] + "into {table_name} from" + custom_sqlmod[1]
            ).format(table_name=table_name)
        else:
            stmt = 'CREATE TEMPORARY TABLE "{table_name}" AS {custom_sql}'.format(
                table_name=table_name, custom_sql=custom_sql
            )
        self.engine.execute(stmt)

    def column_reflection_fallback(self):
        """If we can't reflect the table, use a query to at least get column names."""
        if self.engine.dialect.name.lower() != "mssql":
            sql = sa.select([sa.text("*")]).select_from(self._table).limit(1)
            col_names = self.engine.execute(sql).keys()
            col_dict = [{"name": col_name} for col_name in col_names]
        else:
            type_module = self._get_dialect_type_module()
            # Get column names and types from the database
            # StackOverflow to the rescue: https://stackoverflow.com/a/38634368
            col_info = self.engine.execute(
                "SELECT cols.NAME,ty.NAME FROM tempdb.sys.columns cols JOIN sys.types ty ON cols.user_type_id = ty.user_type_id WHERE object_id = OBJECT_ID('tempdb..{}')".format(
                    self._table
                )
            ).fetchall()
            col_dict = [
                {"name": col_name, "type": getattr(type_module, col_type.upper())()}
                for col_name, col_type in col_info
            ]
        return col_dict

    ###
    ###
    ###
    #
    # Column Map Expectation Implementations
    #
    ###
    ###
    ###

    @DocInherit
    @MetaSqlAlchemyDataset.column_map_expectation
    def expect_column_values_to_be_null(
        self,
        column,
        mostly=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):

        return sa.column(column) == None

    @DocInherit
    @MetaSqlAlchemyDataset.column_map_expectation
    def expect_column_values_to_not_be_null(
        self,
        column,
        mostly=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):

        return sa.column(column) != None

    def _get_dialect_type_module(self):
        if self.dialect is None:
            logger.warning(
                "No sqlalchemy dialect found; relying in top-level sqlalchemy types."
            )
            return sa
        try:
            # Redshift does not (yet) export types to top level; only recognize base SA types
            if isinstance(
                self.engine.dialect, sqlalchemy_redshift.dialect.RedshiftDialect
            ):
                return self.dialect.sa
        except (TypeError, AttributeError):
            pass

        # Bigquery works with newer versions, but use a patch if we had to define bigquery_types_tuple
        try:
            if (
                isinstance(
                    self.engine.dialect, pybigquery.sqlalchemy_bigquery.BigQueryDialect
                )
                and bigquery_types_tuple is not None
            ):
                return bigquery_types_tuple
        except (TypeError, AttributeError):
            pass

        return self.dialect

    @DocInherit
    @DataAsset.expectation(["column", "type_", "mostly"])
    def expect_column_values_to_be_of_type(
        self,
        column,
        type_,
        mostly=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        if mostly is not None:
            raise ValueError(
                "SqlAlchemyDataset does not support column map semantics for column types"
            )

        try:
            col_data = [col for col in self.columns if col["name"] == column][0]
            col_type = type(col_data["type"])
        except IndexError:
            raise ValueError("Unrecognized column: %s" % column)
        except KeyError:
            raise ValueError("No database type data available for column: %s" % column)

        try:
            # Our goal is to be as explicit as possible. We will match the dialect
            # if that is possible. If there is no dialect available, we *will*
            # match against a top-level SqlAlchemy type if that's possible.
            #
            # This is intended to be a conservative approach.
            #
            # In particular, we *exclude* types that would be valid under an ORM
            # such as "float" for postgresql with this approach

            if type_ is None:
                # vacuously true
                success = True
            else:
                type_module = self._get_dialect_type_module()
                success = issubclass(col_type, getattr(type_module, type_))

            return {"success": success, "result": {"observed_value": col_type.__name__}}

        except AttributeError:
            raise ValueError("Type not recognized by current driver: %s" % type_)

    @DocInherit
    @DataAsset.expectation(["column", "type_", "mostly"])
    def expect_column_values_to_be_in_type_list(
        self,
        column,
        type_list,
        mostly=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        if mostly is not None:
            raise ValueError(
                "SqlAlchemyDataset does not support column map semantics for column types"
            )

        try:
            col_data = [col for col in self.columns if col["name"] == column][0]
            col_type = type(col_data["type"])
        except IndexError:
            raise ValueError("Unrecognized column: %s" % column)
        except KeyError:
            raise ValueError("No database type data available for column: %s" % column)

        # Our goal is to be as explicit as possible. We will match the dialect
        # if that is possible. If there is no dialect available, we *will*
        # match against a top-level SqlAlchemy type.
        #
        # This is intended to be a conservative approach.
        #
        # In particular, we *exclude* types that would be valid under an ORM
        # such as "float" for postgresql with this approach

        if type_list is None:
            success = True
        else:
            types = []
            type_module = self._get_dialect_type_module()
            for type_ in type_list:
                try:
                    type_class = getattr(type_module, type_)
                    types.append(type_class)
                except AttributeError:
                    logger.debug("Unrecognized type: %s" % type_)
            if len(types) == 0:
                logger.warning(
                    "No recognized sqlalchemy types in type_list for current dialect."
                )
            types = tuple(types)
            success = issubclass(col_type, types)

        return {"success": success, "result": {"observed_value": col_type.__name__}}

    @DocInherit
    @MetaSqlAlchemyDataset.column_map_expectation
    def expect_column_values_to_be_in_set(
        self,
        column,
        value_set,
        mostly=None,
        parse_strings_as_datetimes=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        if value_set is None:
            # vacuously true
            return True

        if parse_strings_as_datetimes:
            parsed_value_set = self._parse_value_set(value_set)
        else:
            parsed_value_set = value_set
        return sa.column(column).in_(tuple(parsed_value_set))

    @DocInherit
    @MetaSqlAlchemyDataset.column_map_expectation
    def expect_column_values_to_not_be_in_set(
        self,
        column,
        value_set,
        mostly=None,
        parse_strings_as_datetimes=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        if parse_strings_as_datetimes:
            parsed_value_set = self._parse_value_set(value_set)
        else:
            parsed_value_set = value_set
        return sa.column(column).notin_(tuple(parsed_value_set))

    @DocInherit
    @MetaSqlAlchemyDataset.column_map_expectation
    def expect_column_values_to_be_between(
        self,
        column,
        min_value=None,
        max_value=None,
        strict_min=False,
        strict_max=False,
        allow_cross_type_comparisons=None,
        parse_strings_as_datetimes=None,
        output_strftime_format=None,
        mostly=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        if parse_strings_as_datetimes:
            if min_value:
                min_value = parse(min_value)

            if max_value:
                max_value = parse(max_value)

        if min_value is not None and max_value is not None and min_value > max_value:
            raise ValueError("min_value cannot be greater than max_value")

        if min_value is None and max_value is None:
            raise ValueError("min_value and max_value cannot both be None")

        if min_value is None:
            if strict_max:
                return sa.column(column) < max_value
            else:
                return sa.column(column) <= max_value

        elif max_value is None:
            if strict_min:
                return min_value < sa.column(column)
            else:
                return min_value <= sa.column(column)

        else:
            if strict_min and strict_max:
                return sa.and_(
                    min_value < sa.column(column), sa.column(column) < max_value
                )
            elif strict_min:
                return sa.and_(
                    min_value < sa.column(column), sa.column(column) <= max_value
                )
            elif strict_max:
                return sa.and_(
                    min_value <= sa.column(column), sa.column(column) < max_value
                )
            else:
                return sa.and_(
                    min_value <= sa.column(column), sa.column(column) <= max_value
                )

    @DocInherit
    @MetaSqlAlchemyDataset.column_map_expectation
    def expect_column_value_lengths_to_equal(
        self,
        column,
        value,
        mostly=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        return sa.func.length(sa.column(column)) == value

    @DocInherit
    @MetaSqlAlchemyDataset.column_map_expectation
    def expect_column_value_lengths_to_be_between(
        self,
        column,
        min_value=None,
        max_value=None,
        mostly=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):

        if min_value is None and max_value is None:
            raise ValueError("min_value and max_value cannot both be None")

        # Assert that min_value and max_value are integers
        try:
            if min_value is not None and not float(min_value).is_integer():
                raise ValueError("min_value and max_value must be integers")

            if max_value is not None and not float(max_value).is_integer():
                raise ValueError("min_value and max_value must be integers")

        except ValueError:
            raise ValueError("min_value and max_value must be integers")

        if min_value is not None and max_value is not None:
            return sa.and_(
                sa.func.length(sa.column(column)) >= min_value,
                sa.func.length(sa.column(column)) <= max_value,
            )

        elif min_value is None and max_value is not None:
            return sa.func.length(sa.column(column)) <= max_value

        elif min_value is not None and max_value is None:
            return sa.func.length(sa.column(column)) >= min_value

    @MetaSqlAlchemyDataset.column_map_expectation
    def expect_column_values_to_be_unique(
        self,
        column,
        mostly=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        # Duplicates are found by filtering a group by query
        dup_query = (
            sa.select([sa.column(column)])
            .select_from(self._table)
            .group_by(sa.column(column))
            .having(sa.func.count(sa.column(column)) > 1)
        )

        return sa.column(column).notin_(dup_query)

    def _get_dialect_regex_expression(self, column, regex, positive=True):
        try:
            # postgres
            if isinstance(self.engine.dialect, sa.dialects.postgresql.dialect):
                if positive:
                    return BinaryExpression(
                        sa.column(column), literal(regex), custom_op("~")
                    )
                else:
                    return BinaryExpression(
                        sa.column(column), literal(regex), custom_op("!~")
                    )
        except AttributeError:
            pass

        try:
            # redshift
            if isinstance(
                self.engine.dialect, sqlalchemy_redshift.dialect.RedshiftDialect
            ):
                if positive:
                    return BinaryExpression(
                        sa.column(column), literal(regex), custom_op("~")
                    )
                else:
                    return BinaryExpression(
                        sa.column(column), literal(regex), custom_op("!~")
                    )
        except (
            AttributeError,
            TypeError,
        ):  # TypeError can occur if the driver was not installed and so is None
            pass
        try:
            # Mysql
            if isinstance(self.engine.dialect, sa.dialects.mysql.dialect):
                if positive:
                    return BinaryExpression(
                        sa.column(column), literal(regex), custom_op("REGEXP")
                    )
                else:
                    return BinaryExpression(
                        sa.column(column), literal(regex), custom_op("NOT REGEXP")
                    )
        except AttributeError:
            pass

        try:
            # Snowflake
            if isinstance(
                self.engine.dialect, snowflake.sqlalchemy.snowdialect.SnowflakeDialect
            ):
                if positive:
                    return BinaryExpression(
                        sa.column(column), literal(regex), custom_op("RLIKE")
                    )
                else:
                    return BinaryExpression(
                        sa.column(column), literal(regex), custom_op("NOT RLIKE")
                    )
        except (
            AttributeError,
            TypeError,
        ):  # TypeError can occur if the driver was not installed and so is None
            pass

        try:
            # Bigquery
            if isinstance(
                self.engine.dialect, pybigquery.sqlalchemy_bigquery.BigQueryDialect
            ):
                if positive:
                    return sa.func.REGEXP_CONTAINS(sa.column(column), literal(regex))
                else:
                    return sa.not_(
                        sa.func.REGEXP_CONTAINS(sa.column(column), literal(regex))
                    )
        except (
            AttributeError,
            TypeError,
        ):  # TypeError can occur if the driver was not installed and so is None
            pass

    @MetaSqlAlchemyDataset.column_map_expectation
    def expect_column_values_to_match_regex(
        self,
        column,
        regex,
        mostly=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        regex_expression = self._get_dialect_regex_expression(column, regex)
        if regex_expression is None:
            logger.warning(
                "Regex is not supported for dialect %s" % str(self.engine.dialect)
            )
            raise NotImplementedError

        return regex_expression

    @MetaSqlAlchemyDataset.column_map_expectation
    def expect_column_values_to_not_match_regex(
        self,
        column,
        regex,
        mostly=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        regex_expression = self._get_dialect_regex_expression(
            column, regex, positive=False
        )
        if regex_expression is None:
            logger.warning(
                "Regex is not supported for dialect %s" % str(self.engine.dialect)
            )
            raise NotImplementedError

        return regex_expression

    @MetaSqlAlchemyDataset.column_map_expectation
    def expect_column_values_to_match_regex_list(
        self,
        column,
        regex_list,
        match_on="any",
        mostly=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):

        if match_on not in ["any", "all"]:
            raise ValueError("match_on must be any or all")

        if len(regex_list) == 0:
            raise ValueError("At least one regex must be supplied in the regex_list.")

        regex_expression = self._get_dialect_regex_expression(column, regex_list[0])
        if regex_expression is None:
            logger.warning(
                "Regex is not supported for dialect %s" % str(self.engine.dialect)
            )
            raise NotImplementedError

        if match_on == "any":
            condition = sa.or_(
                *[
                    self._get_dialect_regex_expression(column, regex)
                    for regex in regex_list
                ]
            )
        else:
            condition = sa.and_(
                *[
                    self._get_dialect_regex_expression(column, regex)
                    for regex in regex_list
                ]
            )
        return condition

    @MetaSqlAlchemyDataset.column_map_expectation
    def expect_column_values_to_not_match_regex_list(
        self,
        column,
        regex_list,
        mostly=None,
        result_format=None,
        include_config=True,
        catch_exceptions=None,
        meta=None,
    ):
        if len(regex_list) == 0:
            raise ValueError("At least one regex must be supplied in the regex_list.")

        regex_expression = self._get_dialect_regex_expression(
            column, regex_list[0], positive=False
        )
        if regex_expression is None:
            logger.warning(
                "Regex is not supported for dialect %s" % str(self.engine.dialect)
            )
            raise NotImplementedError

        return sa.and_(
            *[
                self._get_dialect_regex_expression(column, regex, positive=False)
                for regex in regex_list
            ]
        )
