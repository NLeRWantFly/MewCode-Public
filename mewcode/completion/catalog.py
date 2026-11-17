from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


MemberKind = Literal["method", "field"]


@dataclass(frozen=True)
class JavaMember:
    declaring_type: str
    name: str
    kind: MemberKind
    return_type: str
    parameters: tuple[str, ...] = ()
    priority: float = 0.5
    is_static: bool = False
    source: Literal["jdk_catalog", "project_symbol", "llm"] = "jdk_catalog"

    @property
    def label(self) -> str:
        if self.kind == "field":
            return self.name
        return f"{self.name}({', '.join(self.parameters)})"

    @property
    def insert_text(self) -> str:
        return self.name if self.kind == "field" else f"{self.name}()"

    @property
    def candidate_key(self) -> str:
        return f"{self.declaring_type}#{self.kind}:{self.label}"

    @property
    def detail(self) -> str:
        return f"{self.return_type} {self.declaring_type}.{self.label}"


def _method(
    owner: str,
    name: str,
    return_type: str,
    *parameters: str,
    priority: float = 0.5,
    is_static: bool = False,
) -> JavaMember:
    return JavaMember(
        declaring_type=owner,
        name=name,
        kind="method",
        return_type=return_type,
        parameters=parameters,
        priority=priority,
        is_static=is_static,
    )


def _field(
    owner: str,
    name: str,
    field_type: str,
    *,
    priority: float = 0.5,
    is_static: bool = False,
) -> JavaMember:
    return JavaMember(
        declaring_type=owner,
        name=name,
        kind="field",
        return_type=field_type,
        priority=priority,
        is_static=is_static,
    )


_SCANNER = (
    _method("Scanner", "next", "String", priority=0.96),
    _method("Scanner", "nextLine", "String", priority=0.94),
    _method("Scanner", "nextInt", "int", priority=0.93),
    _method("Scanner", "nextLong", "long", priority=0.88),
    _method("Scanner", "nextDouble", "double", priority=0.86),
    _method("Scanner", "nextFloat", "float", priority=0.72),
    _method("Scanner", "nextBoolean", "boolean", priority=0.68),
    _method("Scanner", "nextByte", "byte", priority=0.58),
    _method("Scanner", "nextShort", "short", priority=0.58),
    _method("Scanner", "hasNext", "boolean", priority=0.82),
    _method("Scanner", "hasNextLine", "boolean", priority=0.78),
    _method("Scanner", "hasNextInt", "boolean", priority=0.78),
    _method("Scanner", "hasNextLong", "boolean", priority=0.68),
    _method("Scanner", "hasNextDouble", "boolean", priority=0.68),
    _method("Scanner", "useDelimiter", "Scanner", "String", priority=0.55),
    _method("Scanner", "delimiter", "Pattern", priority=0.45),
    _method("Scanner", "locale", "Locale", priority=0.38),
    _method("Scanner", "radix", "int", priority=0.38),
    _method("Scanner", "close", "void", priority=0.5),
    _method("Scanner", "ioException", "IOException", priority=0.3),
)

_STRING = (
    _method("String", "length", "int", priority=0.98),
    _method("String", "isEmpty", "boolean", priority=0.86),
    _method("String", "charAt", "char", "int", priority=0.9),
    _method("String", "substring", "String", "int", priority=0.9),
    _method("String", "contains", "boolean", "CharSequence", priority=0.82),
    _method("String", "startsWith", "boolean", "String", priority=0.72),
    _method("String", "endsWith", "boolean", "String", priority=0.72),
    _method("String", "indexOf", "int", "String", priority=0.78),
    _method("String", "lastIndexOf", "int", "String", priority=0.6),
    _method("String", "substring", "String", "int", "int", priority=0.84),
    _method("String", "split", "String[]", "String", priority=0.76),
    _method("String", "replace", "String", "CharSequence", "CharSequence", priority=0.7),
    _method("String", "trim", "String", priority=0.66),
    _method("String", "strip", "String", priority=0.66),
    _method("String", "toLowerCase", "String", priority=0.62),
    _method("String", "toUpperCase", "String", priority=0.62),
    _method("String", "equals", "boolean", "Object", priority=0.8),
    _method("String", "equalsIgnoreCase", "boolean", "String", priority=0.62),
    _method("String", "compareTo", "int", "String", priority=0.55),
    _method("String", "toCharArray", "char[]", priority=0.62),
)

_LIST = (
    _method("List", "add", "boolean", "E", priority=0.94),
    _method("List", "get", "E", "int", priority=0.96),
    _method("List", "set", "E", "int", "E", priority=0.78),
    _method("List", "size", "int", priority=0.98),
    _method("List", "isEmpty", "boolean", priority=0.86),
    _method("List", "contains", "boolean", "Object", priority=0.75),
    _method("List", "remove", "E", "int", priority=0.72),
    _method("List", "clear", "void", priority=0.62),
    _method("List", "sort", "void", "Comparator", priority=0.58),
    _method("List", "stream", "Stream", priority=0.64),
    _method("List", "iterator", "Iterator", priority=0.55),
)

_MAP = (
    _method("Map", "put", "V", "K", "V", priority=0.94),
    _method("Map", "get", "V", "Object", priority=0.96),
    _method("Map", "getOrDefault", "V", "Object", "V", priority=0.82),
    _method("Map", "containsKey", "boolean", "Object", priority=0.84),
    _method("Map", "remove", "V", "Object", priority=0.7),
    _method("Map", "size", "int", priority=0.88),
    _method("Map", "isEmpty", "boolean", priority=0.76),
    _method("Map", "keySet", "Set<K>", priority=0.72),
    _method("Map", "values", "Collection<V>", priority=0.68),
    _method("Map", "entrySet", "Set<Entry<K, V>>", priority=0.68),
    _method("Map", "computeIfAbsent", "V", "K", "Function", priority=0.56),
)

_SET = (
    _method("Set", "add", "boolean", "E", priority=0.94),
    _method("Set", "contains", "boolean", "Object", priority=0.92),
    _method("Set", "remove", "boolean", "Object", priority=0.78),
    _method("Set", "size", "int", priority=0.9),
    _method("Set", "isEmpty", "boolean", priority=0.78),
    _method("Set", "clear", "void", priority=0.58),
    _method("Set", "stream", "Stream", priority=0.62),
    _method("Set", "iterator", "Iterator", priority=0.58),
)

_STRING_BUILDER = (
    _method("StringBuilder", "append", "StringBuilder", "Object", priority=0.98),
    _method("StringBuilder", "toString", "String", priority=0.96),
    _method("StringBuilder", "length", "int", priority=0.82),
    _method("StringBuilder", "setLength", "void", "int", priority=0.58),
    _method("StringBuilder", "reverse", "StringBuilder", priority=0.72),
    _method("StringBuilder", "insert", "StringBuilder", "int", "Object", priority=0.65),
    _method("StringBuilder", "delete", "StringBuilder", "int", "int", priority=0.6),
    _method("StringBuilder", "deleteCharAt", "StringBuilder", "int", priority=0.58),
    _method("StringBuilder", "charAt", "char", "int", priority=0.62),
)

_OPTIONAL = (
    _method("Optional", "get", "T", priority=0.62),
    _method("Optional", "orElse", "T", "T", priority=0.9),
    _method("Optional", "orElseGet", "T", "Supplier", priority=0.76),
    _method("Optional", "isPresent", "boolean", priority=0.86),
    _method("Optional", "isEmpty", "boolean", priority=0.7),
    _method("Optional", "ifPresent", "void", "Consumer", priority=0.65),
    _method("Optional", "map", "Optional<U>", "Function", priority=0.72),
    _method("Optional", "filter", "Optional<T>", "Predicate", priority=0.62),
)

JAVA_TYPE_MEMBERS: dict[str, tuple[JavaMember, ...]] = {
    "Scanner": _SCANNER,
    "String": _STRING,
    "List": _LIST,
    "ArrayList": _LIST,
    "LinkedList": _LIST,
    "Map": _MAP,
    "HashMap": _MAP,
    "TreeMap": _MAP,
    "Set": _SET,
    "HashSet": _SET,
    "TreeSet": _SET,
    "StringBuilder": _STRING_BUILDER,
    "StringBuffer": _STRING_BUILDER,
    "Optional": _OPTIONAL,
}

COMMON_JAVA_ALIASES: dict[str, str] = {
    "in": "Scanner",
    "input": "Scanner",
    "scanner": "Scanner",
    "sc": "Scanner",
    "str": "String",
    "text": "String",
    "list": "List",
    "arr": "ArrayList",
    "map": "Map",
    "set": "Set",
    "sb": "StringBuilder",
}


def simple_type_name(type_name: str) -> str:
    """Normalize a Java type token to its simple, non-generic base name."""

    value = type_name.strip()
    generic_at = value.find("<")
    if generic_at >= 0:
        value = value[:generic_at]
    value = value.replace("[]", "").strip()
    return value.rsplit(".", 1)[-1]


def catalog_members(type_name: str) -> tuple[JavaMember, ...]:
    return JAVA_TYPE_MEMBERS.get(simple_type_name(type_name), ())


def catalog_has_type(type_name: str) -> bool:
    return simple_type_name(type_name) in JAVA_TYPE_MEMBERS
