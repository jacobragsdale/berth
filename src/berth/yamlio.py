"""The only module that touches ruamel.yaml. Everything else sees plain trees typed as `object`."""

import re
from collections.abc import Mapping, Sequence
from io import StringIO

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.scalarstring import DoubleQuotedScalarString, LiteralScalarString

# kubectl and other Go tools parse YAML with libraries that keep YAML 1.1 rules, where `on`, `no` and `1:30`
# are not strings. ruamel writes YAML 1.2, so quote anything a 1.1 parser would read as another type.
_YAML11_NON_STRING = re.compile(r"^(?i:y|n|yes|no|on|off|true|false|null|~|[-+]?(\.?[0-9][0-9_:.eE+-]*|\.inf|\.nan))$")


class YamlError(ValueError):
    pass


def _yaml() -> YAML:
    yaml = YAML(typ="rt")
    yaml.default_flow_style = False
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def load(text: str, *, source: str) -> object:
    try:
        data: object = YAML(typ="safe", pure=True).load(text)
    except Exception as error:  # ruamel raises many unrelated exception types; all mean "not valid YAML"
        msg = f"{source}: not valid YAML: {error}"
        raise YamlError(msg) from error
    return data


def load_all(text: str, *, source: str) -> list[object]:
    try:
        documents: list[object] = list(YAML(typ="safe", pure=True).load_all(text))
    except Exception as error:  # see load()
        msg = f"{source}: not valid YAML: {error}"
        raise YamlError(msg) from error
    return documents


def _styled_str(value: str) -> str:
    # A literal block keeps multi-line text readable, but it cannot carry control characters; escape those.
    if "\n" in value and all(char == "\n" or char.isprintable() for char in value):
        return LiteralScalarString(value)
    if value == "" or not value.isprintable() or _YAML11_NON_STRING.match(value):
        return DoubleQuotedScalarString(value)
    return value


def _styled(value: object) -> object:
    if isinstance(value, str):
        return _styled_str(value)
    if isinstance(value, Mapping):
        return {str(key): _styled(item) for key, item in value.items()}
    if isinstance(value, Sequence):
        return [_styled(item) for item in value]
    return value


def dump_documents(documents: Sequence[Mapping[str, object]]) -> str:
    buffer = StringIO()
    _yaml().dump_all([_styled(document) for document in documents], buffer)
    return buffer.getvalue()


def dump(document: Mapping[str, object]) -> str:
    buffer = StringIO()
    _yaml().dump(_styled(document), buffer)
    return buffer.getvalue()


def set_path(text: str, path: tuple[str, ...], value: str, *, source: str) -> str:
    """Set one nested scalar, creating the mappings on the way, and keep the file's comments and layout, so that
    an edit by a bot or by `berth release` reads like one a person made."""
    yaml = _yaml()
    try:
        data: object = yaml.load(text)
    except Exception as error:  # see load()
        msg = f"{source}: not valid YAML: {error}"
        raise YamlError(msg) from error
    if not isinstance(data, CommentedMap):
        msg = f"{source}: expected a mapping at the top level"
        raise YamlError(msg)
    node = data
    for key in path[:-1]:
        child = node.get(key)
        if child is None:
            child = CommentedMap()
            node[key] = child
        if not isinstance(child, CommentedMap):
            msg = f"{source}: {key} is not a mapping"
            raise YamlError(msg)
        node = child
    node[path[-1]] = value
    buffer = StringIO()
    yaml.dump(data, buffer)
    return buffer.getvalue()
