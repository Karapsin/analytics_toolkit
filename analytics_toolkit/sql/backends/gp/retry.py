from __future__ import annotations

import re

_TERMINAL_SQLSTATE_CLASSES = frozenset(
    {"0A", "21", "22", "23", "28", "3D", "3F", "42", "44", "54", "F0"}
)
_TERMINAL_SQLSTATES = frozenset({"57014", "XX001", "XX002"})
_RESOURCE_SQLSTATES = frozenset({"53100", "53200", "53400"})
_UNKNOWN_OUTCOME_SQLSTATES = frozenset({"08007", "40003"})
_LIBPQ_STARTUP_PREFIX = r"^[ \t]*(?:connection to server[^\r\n]*failed:[ \t]*)?"
_STARTUP_AUTHENTICATION_PATTERN = re.compile(
    _LIBPQ_STARTUP_PREFIX
    + r"(?:FATAL:[ \t]*(?:password authentication failed for user\b"
    + r'|role "[^\r\n]*" does not exist\b|no pg_hba\.conf entry for host\b)'
    + r"|fe_sendauth:[ \t]*no password supplied\b)",
    re.MULTILINE,
)
_STARTUP_DATABASE_PATTERN = re.compile(
    _LIBPQ_STARTUP_PREFIX + r'FATAL:[ \t]*database "[^\r\n]*" does not exist\b',
    re.MULTILINE,
)
_STARTUP_CERTIFICATE_PATTERN = re.compile(
    _LIBPQ_STARTUP_PREFIX
    + r"(?:SSL error:[ \t]*(?:certificate verify failed\b|hostname mismatch\b"
    + r"|certificate has expired\b|certificate expired\b)"
    + r"|server certificate for [^\r\n]+ does not match host name\b)",
    re.MULTILINE,
)
_CLASS_SQLSTATES = {
    "FeatureNotSupported": "0A000",
    "CardinalityViolation": "21000",
    "StringDataRightTruncation": "22001",
    "NumericValueOutOfRange": "22003",
    "InvalidDatetimeFormat": "22007",
    "DatetimeFieldOverflow": "22008",
    "DivisionByZero": "22012",
    "CharacterNotInRepertoire": "22021",
    "InvalidParameterValue": "22023",
    "InvalidTextRepresentation": "22P02",
    "InvalidBinaryRepresentation": "22P03",
    "BadCopyFileFormat": "22P04",
    "UntranslatableCharacter": "22P05",
    "NotNullViolation": "23502",
    "ForeignKeyViolation": "23503",
    "UniqueViolation": "23505",
    "CheckViolation": "23514",
    "ExclusionViolation": "23P01",
    "InvalidAuthorizationSpecification": "28000",
    "InvalidPassword": "28P01",
    "InvalidCatalogName": "3D000",
    "InvalidSchemaName": "3F000",
    "SyntaxError": "42601",
    "InsufficientPrivilege": "42501",
    "CannotCoerce": "42846",
    "GroupingError": "42803",
    "DatatypeMismatch": "42804",
    "UndefinedColumn": "42703",
    "UndefinedFunction": "42883",
    "UndefinedTable": "42P01",
    "UndefinedObject": "42704",
    "DuplicateTable": "42P07",
    "AmbiguousColumn": "42702",
    "InvalidColumnReference": "42P10",
    "InvalidTableDefinition": "42P16",
    "WithCheckOptionViolation": "44000",
    "ProgramLimitExceeded": "54000",
    "StatementTooComplex": "54001",
    "TooManyColumns": "54011",
    "TooManyArguments": "54023",
    "QueryCanceled": "57014",
    "ConfigFileError": "F0000",
    "LockFileExists": "F0001",
    "DataCorrupted": "XX001",
    "IndexCorrupted": "XX002",
    "DiskFull": "53100",
    "OutOfMemory": "53200",
    "ConfigurationLimitExceeded": "53400",
    "TransactionResolutionUnknown": "08007",
    "StatementCompletionUnknown": "40003",
    "SerializationFailure": "40001",
    "DeadlockDetected": "40P01",
    "LockNotAvailable": "55P03",
    "TooManyConnections": "53300",
    "AdminShutdown": "57P01",
    "CrashShutdown": "57P02",
    "CannotConnectNow": "57P03",
}


def classify_error(exc: BaseException) -> tuple[bool, str, str | None] | None:
    """Classify PostgreSQL SQLSTATEs without treating driver bases as transient."""
    diagnostic = getattr(exc, "diag", None)
    for value in (
        getattr(exc, "pgcode", None),
        getattr(exc, "sqlstate", None),
        getattr(diagnostic, "sqlstate", None),
    ):
        if isinstance(value, str):
            sqlstate = value.strip().upper()
            if re.fullmatch(r"[0-9A-Z]{5}", sqlstate):
                return _classify_sqlstate(sqlstate)

    for error_class in type(exc).__mro__:
        class_sqlstate = _CLASS_SQLSTATES.get(error_class.__name__)
        if class_sqlstate is not None:
            retryable, reason, code = _classify_sqlstate(class_sqlstate)
            if reason == "gp_sqlstate_terminal":
                reason = "gp_exception_class_terminal"
            elif retryable:
                reason = "gp_exception_class_retryable"
            return retryable, reason, code
        if error_class.__module__.startswith("psycopg2") and error_class.__name__ in {
            "DataError",
            "IntegrityError",
            "ProgrammingError",
            "NotSupportedError",
        }:
            return False, "gp_exception_class_terminal", None
    return _classify_startup_error(exc)


def _classify_startup_error(exc: BaseException) -> tuple[bool, str, None] | None:
    if not any(
        error_class.__module__.startswith("psycopg2") and error_class.__name__ == "OperationalError"
        for error_class in type(exc).__mro__
    ):
        return None
    message = str(exc)
    if _STARTUP_AUTHENTICATION_PATTERN.search(message):
        return False, "gp_startup_authentication", None
    if _STARTUP_DATABASE_PATTERN.search(message):
        return False, "gp_startup_configuration", None
    if _STARTUP_CERTIFICATE_PATTERN.search(message):
        return False, "gp_tls_certificate", None
    return None


def _classify_sqlstate(sqlstate: str) -> tuple[bool, str, str]:
    if sqlstate in _UNKNOWN_OUTCOME_SQLSTATES:
        return False, "gp_outcome_unknown", sqlstate
    if sqlstate in _RESOURCE_SQLSTATES:
        return False, "gp_resource_exhausted", sqlstate
    if sqlstate[:2] in _TERMINAL_SQLSTATE_CLASSES or sqlstate in _TERMINAL_SQLSTATES:
        return False, "gp_sqlstate_terminal", sqlstate
    return True, "gp_sqlstate_retryable", sqlstate
