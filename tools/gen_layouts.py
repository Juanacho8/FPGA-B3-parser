"""Generate the field layouts of every B3 template from B3's SBE schema.

Reads spec/b3-market-data-messages-2.3.1.xml once, applies the SBE 1.0
layout rules listed in spec/LAYOUTS.md ("How the layouts were verified"),
and renders the parsed schema into one file per output target:

    feed_generator/b3gen/layouts.py      Python module for the feed generator
    cpp_baseline/include/b3/layouts.hpp  constexpr header for the C++ baseline
    cpp_baseline/include/b3/fields.hpp   field descriptor types over layouts.hpp,
                                         for decode.hpp's generic accessors

Each target is one render function in OUTPUTS. A SystemVerilog package for
the RTL is meant to become a third entry.

Usage (from the repo root):

    python3 tools/gen_layouts.py                 # rewrite both checked-in files
    python3 tools/gen_layouts.py --out-dir DIR   # write them under DIR instead
    make layouts / make layouts-check            # the same, via the Makefile

The outputs are checked in. `make layouts-check` (run by `make cpp-test`)
regenerates into a temporary folder and fails if a checked-in file differs,
and feed_generator/tests/test_layouts.py does the same for layouts.py. So
edit this script, never the outputs. Refuses to run unless the XML's SHA-256
equals the one recorded in spec/LAYOUTS.md. Standard library only.
"""

import argparse
import hashlib
import keyword
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
XML_PATH = REPO / "spec" / "b3-market-data-messages-2.3.1.xml"
LAYOUTS_MD = REPO / "spec" / "LAYOUTS.md"
PY_OUT = Path("feed_generator") / "b3gen" / "layouts.py"
CPP_OUT = Path("cpp_baseline") / "include" / "b3" / "layouts.hpp"
FIELDS_OUT = Path("cpp_baseline") / "include" / "b3" / "fields.hpp"
OUT_PATH = REPO / PY_OUT   # kept for callers of the old single-output script


def expected_sha256(layouts_md=LAYOUTS_MD):
    """The SHA-256 of the exact schema file every layout in spec/LAYOUTS.md
    was verified against, as LAYOUTS.md records it ("XML SHA-256: `...`")."""
    text = Path(layouts_md).read_text(encoding="utf-8")
    found = re.findall(r"^XML SHA-256: `([0-9a-f]{64})`$", text, re.M)
    if len(found) != 1:
        raise SchemaError(f"{layouts_md}: expected one 'XML SHA-256: `...`' line, "
                          f"found {len(found)}")
    return found[0]


# B3 facts with no value in the XML (spec/LAYOUTS.md sections 2 and 4).
ENCODING_TYPE = 0xEB50       # SBE 1.0 little-endian, framing header encodingType
MAX_PACKET_SIZE = 1400       # bytes per UDP datagram, packet header included

SBE_NS = {"sbe": "http://fixprotocol.io/2016/sbe"}

PRIMITIVE_SIZES = {
    "char": 1,
    "int8": 1, "uint8": 1,
    "int16": 2, "uint16": 2,
    "int32": 4, "uint32": 4,
    "int64": 8, "uint64": 8,
}

# Template 0 (HeaderMessage_0) only exists so code generators emit the packet
# and framing headers. B3 never sends it as a message.
SKIPPED_TEMPLATES = {0}


class SchemaError(Exception):
    """The XML is not the verified schema, or breaks a layout rule."""


# ---------------------------------------------------------------------------
# Null values
# ---------------------------------------------------------------------------

def default_null(primitive):
    """SBE 1.0 default null when a type declares no nullValue:
    char -> 0, unsigned -> max value, signed -> min value."""
    if primitive == "char":
        return 0
    bits = PRIMITIVE_SIZES[primitive] * 8
    if primitive.startswith("u"):
        return 2 ** bits - 1
    return -(2 ** (bits - 1))


def parse_value(text, primitive):
    """Constant and enum values: chars stay strings, numbers become ints."""
    text = text.strip()
    if primitive == "char":
        return text
    return int(text)


# ---------------------------------------------------------------------------
# Type table
# ---------------------------------------------------------------------------

class Types:
    """Every <type>, <composite>, <enum> and <set> in the schema, by name."""

    def __init__(self, types_elem):
        self.simple = {}
        self.composites = {}
        self.enums = {}
        self.sets = {}
        for elem in types_elem:
            kind = elem.tag
            name = elem.get("name")
            if kind == "type":
                self.simple[name] = self._simple(elem)
            elif kind == "composite":
                self.composites[name] = elem
            elif kind == "enum":
                self.enums[name] = self._enum(elem)
            elif kind == "set":
                self.sets[name] = self._set(elem)

    @staticmethod
    def _simple(elem):
        primitive = elem.get("primitiveType")
        length = int(elem.get("length", "1"))
        presence = elem.get("presence", "required")
        null_attr = elem.get("nullValue")
        return {
            "primitive": primitive,
            "length": length,
            # Rule: a constant takes 0 bytes on the wire. Otherwise the size
            # is primitive size x length (length > 1 makes char[N]).
            "size": 0 if presence == "constant" else PRIMITIVE_SIZES[primitive] * length,
            "presence": presence,
            "null": None if null_attr is None else parse_value(null_attr, "int64"),
            "const": None if presence != "constant" else parse_value(elem.text or "", primitive),
        }

    def _encoding(self, encoding_type):
        """Resolve an enum/set encodingType to (primitive, presence, null).
        It is either a primitive (required) or a named simple type such as
        UInt8NULL, whose presence and nullValue then apply."""
        if encoding_type in PRIMITIVE_SIZES:
            return encoding_type, "required", default_null(encoding_type)
        t = self.simple[encoding_type]
        null = t["null"] if t["null"] is not None else default_null(t["primitive"])
        return t["primitive"], t["presence"], null

    def _enum(self, elem):
        primitive, presence, null = self._encoding(elem.get("encodingType"))
        values = {v.get("name"): parse_value(v.text, primitive) for v in elem.findall("validValue")}
        return {"primitive": primitive, "presence": presence, "null": null, "values": values}

    def _set(self, elem):
        primitive, _, _ = self._encoding(elem.get("encodingType"))
        bits = {int(c.text): c.get("name") for c in elem.findall("choice")}
        return {"primitive": primitive, "bits": bits}

    def value_ref(self, ref):
        """Resolve valueRef="Enum.Value" to the enum value."""
        enum_name, value_name = ref.split(".")
        return self.enums[enum_name]["values"][value_name]

    def composite_members(self, name):
        """Members of a composite, packed in declaration order. Constant
        members (exponent, unit) take 0 bytes and are returned separately."""
        members = []
        constants = {}
        offset = 0
        for m in self.composites[name]:
            m_name = m.get("name")
            presence = m.get("presence", "required")
            primitive = m.get("primitiveType")
            # Rule: constants take 0 bytes. Value is valueRef or element text.
            if presence == "constant":
                ref = m.get("valueRef")
                constants[m_name] = self.value_ref(ref) if ref else parse_value(m.text, primitive)
                continue
            length = int(m.get("length", "1"))
            size = PRIMITIVE_SIZES[primitive] * length
            null_attr = m.get("nullValue")
            if null_attr is not None:
                null = parse_value(null_attr, "int64")
            elif presence == "optional":
                null = default_null(primitive)
            else:
                null = None
            members.append({"name": m_name, "offset": offset, "size": size,
                            "primitive": primitive, "null": null, "presence": presence})
            # Rule: fields (and composite members) are packed in order.
            offset += size
        return members, constants


# ---------------------------------------------------------------------------
# Field resolution
# ---------------------------------------------------------------------------

def resolve_field(elem, types):
    """Turn one <field> into ("constant", dict) or ("field", dict without offset)."""
    name = elem.get("name")
    tag = int(elem.get("id"))
    type_name = elem.get("type")
    field_presence = elem.get("presence")
    since = elem.get("sinceVersion")
    deprecated = elem.get("deprecated")
    common = {
        "since_version": int(since) if since else None,
        "deprecated": int(deprecated) if deprecated else None,
    }

    def constant(value, primitive):
        return "constant", {"name": name, "tag": tag, "type_name": type_name, "value": value,
                            "primitive": primitive}

    if type_name in types.simple:
        t = types.simple[type_name]
        # Rule: constants take 0 bytes, whether the field or the type says so.
        if field_presence == "constant" or t["presence"] == "constant":
            ref = elem.get("valueRef")
            if ref:
                return constant(types.value_ref(ref), types.enums[ref.split(".")[0]]["primitive"])
            return constant(t["const"], t["primitive"])
        # Rule: presence on the field overrides presence on the type.
        presence = field_presence or t["presence"]
        null = None
        if presence == "optional":
            # Rule: the type's nullValue if declared, else the SBE default.
            null = t["null"] if t["null"] is not None else default_null(t["primitive"])
        return "field", {"name": name, "tag": tag, "size": t["size"],
                         "primitive": t["primitive"], "type_name": type_name,
                         "presence": presence, "null": null, "exponent": None,
                         "members": (), **common}

    if type_name in types.enums or type_name in types.sets:
        is_enum = type_name in types.enums
        t = types.enums[type_name] if is_enum else types.sets[type_name]
        if field_presence == "constant":
            return constant(types.value_ref(elem.get("valueRef")), t["primitive"])
        # Rule: presence on the field overrides presence on the type. An
        # enum's own presence is that of its encodingType (e.g. PriceType is
        # encoded as UInt8NULL, so it is optional with null 0).
        presence = field_presence or (t["presence"] if is_enum else "required")
        null = t["null"] if (is_enum and presence == "optional") else None
        return "field", {"name": name, "tag": tag, "size": PRIMITIVE_SIZES[t["primitive"]],
                         "primitive": t["primitive"], "type_name": type_name,
                         "presence": presence, "null": null, "exponent": None,
                         "members": (), **common}

    if type_name in types.composites:
        members, constants = types.composite_members(type_name)
        presence = field_presence or "required"
        size = sum(m["size"] for m in members)
        if len(members) == 1:
            # Single-value composite (Price, Fixed8, UTCTimestampNanos, ...):
            # report the value's primitive plus the constant exponent, if any.
            m = members[0]
            null = None
            if presence == "optional":
                null = m["null"] if m["null"] is not None else default_null(m["primitive"])
            return "field", {"name": name, "tag": tag, "size": size,
                             "primitive": m["primitive"], "type_name": type_name,
                             "presence": presence, "null": null,
                             "exponent": constants.get("exponent"),
                             "members": (), **common}
        # Multi-member composite (MaturityMonthYear): each member keeps its own null.
        member_tuple = tuple(members)
        return "field", {"name": name, "tag": tag, "size": size,
                         "primitive": "composite", "type_name": type_name,
                         "presence": presence, "null": None, "exponent": None,
                         "members": member_tuple, **common}

    raise SchemaError(f"field {name}: unknown type {type_name}")


def layout_block(block_elem, types, where):
    """Lay out the fixed fields of a message root or a group entry.

    Returns (fields, constants, groups, var_data, end_offset)."""
    fields = []
    constants = []
    groups = []
    var_data = []
    offset = 0
    for child in block_elem:
        if child.tag == "field":
            if groups or var_data:
                # Rule: fixed fields come before any group or var data.
                raise SchemaError(f"{where}: field {child.get('name')} after a group or data")
            kind, data = resolve_field(child, types)
            if kind == "constant":
                # Rule: constants take 0 bytes and do not move the offset.
                constants.append(data)
                continue
            forced = child.get("offset")
            if forced is not None:
                # Rule: an offset attribute forces the position. The gap from
                # the previous field's end is padding.
                forced = int(forced)
                if forced < offset:
                    raise SchemaError(f"{where}: {data['name']} offset {forced} overlaps {offset}")
                offset = forced
            data["offset"] = offset
            fields.append(data)
            # Rule: fields are packed in declaration order.
            offset += data["size"]
        elif child.tag == "group":
            if var_data:
                raise SchemaError(f"{where}: group after var data")
            groups.append(layout_group(child, types, where))
        elif child.tag == "data":
            var_data.append(layout_data(child, types))
    return fields, constants, groups, var_data, offset


def forced_block_length(elem, end_offset, where):
    """Rule: a blockLength attribute forces trailing padding. Without it,
    blockLength is where the last field ends."""
    attr = elem.get("blockLength")
    if attr is None:
        return end_offset
    block_length = int(attr)
    if block_length < end_offset:
        raise SchemaError(f"{where}: blockLength {block_length} < fields end {end_offset}")
    return block_length


def layout_group(elem, types, where):
    """Rule: a repeating group comes after the root block, starts with a
    dimension header (GroupSizeEncoding: blockLength uint16, numInGroup
    uint8) and its entries are laid out with the same rules, offsets relative
    to each entry."""
    name = elem.get("name")
    gwhere = f"{where}.{name}"
    fields, constants, groups, var_data, end = layout_block(elem, types, gwhere)
    if groups or var_data:
        raise SchemaError(f"{gwhere}: nested groups or var data are not supported")
    dimension_type = elem.get("dimensionType")
    dimension, _ = types.composite_members(dimension_type)
    return {
        "name": name,
        "tag": int(elem.get("id")),
        "dimension_type": dimension_type,
        "dimension_size": sum(m["size"] for m in dimension),
        "dimension": dimension,
        "block_length": forced_block_length(elem, end, gwhere),
        "fields": fields,
        "constants": constants,
    }


def layout_data(elem, types):
    """Rule: variable-length data comes after the groups: a length prefix,
    then that many bytes. The prefix size is the size of the composite's
    `length` member."""
    type_name = elem.get("type")
    members, _ = types.composite_members(type_name)
    length = next(m for m in members if m["name"] == "length")
    return {
        "name": elem.get("name"),
        "tag": int(elem.get("id")),
        "type_name": type_name,
        "prefix_size": length["size"],
        "prefix_primitive": length["primitive"],
    }


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def load_schema(xml_path):
    data = Path(xml_path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    expected = expected_sha256()
    if digest != expected:
        raise SchemaError(f"{xml_path}: SHA-256 {digest} does not match the verified "
                          f"schema {expected} (spec/LAYOUTS.md). Refusing to generate.")
    root = ET.fromstring(data)
    types = Types(root.find("types"))

    templates = []
    for msg in root.findall("sbe:message", SBE_NS):
        template_id = int(msg.get("id"))
        if template_id in SKIPPED_TEMPLATES:
            continue
        name = msg.get("name")
        fields, constants, groups, var_data, end = layout_block(msg, types, name)
        since = msg.get("sinceVersion")
        deprecated = msg.get("deprecated")
        templates.append({
            "id": template_id,
            "name": name,
            "block_length": forced_block_length(msg, end, name),
            "since_version": int(since) if since else None,
            "deprecated": int(deprecated) if deprecated else None,
            "fields": fields,
            "constants": constants,
            "groups": groups,
            "var_data": var_data,
        })
    templates.sort(key=lambda t: t["id"])

    headers = {}
    for xml_name in ("PacketHeader", "FramingHeader", "messageHeader"):
        members, _ = types.composite_members(xml_name)
        headers[xml_name] = members

    return {
        "sha256": digest,
        "schema_id": int(root.get("id")),
        "schema_version": int(root.get("version")),
        "semantic_version": root.get("semanticVersion"),
        "templates": templates,
        "headers": headers,
        "enums": {n: e["values"] for n, e in types.enums.items()},
        "sets": {n: s["bits"] for n, s in types.sets.items()},
        # Full enum and set definitions (underlying type, presence, null) for
        # targets that need more than the values.
        "enum_types": types.enums,
        "set_types": types.sets,
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

HEADER = '''"""Support: field offsets and sizes for every B3 template.

GENERATED by tools/gen_layouts.py from spec/b3-market-data-messages-2.3.1.xml.
Do not edit by hand. Change the generator and run, from the repo root:

    python3 tools/gen_layouts.py        (or: make layouts)

Conventions:
* Field offsets are relative to the start of the message body (the byte after
  the 12-byte message header). Group field offsets are relative to each entry.
* null is the encoded value meaning "absent". It is None for required fields.
* Decimal fields report their int64 mantissa plus the constant exponent.
* Constants take 0 bytes on the wire and are listed with their value.
* Deprecated fields still occupy their bytes. Encoders write them as null.
* Decoders must still trust blockLength and version from each message header
  (spec/LAYOUTS.md section 5); these are the schema version {schema_version} layouts.

Must not: encode values or know about events and packets.
"""

from dataclasses import dataclass

XML_SHA256 = {sha256!r}
SCHEMA_ID = {schema_id!r}
SCHEMA_VERSION = {schema_version!r}
SEMANTIC_VERSION = {semantic_version!r}


@dataclass(frozen=True)
class Member:
    """One member of a header or multi-member composite."""
    name: str
    offset: int
    size: int
    primitive: str
    null: int | None


@dataclass(frozen=True)
class Field:
    """One fixed-position field of a message root block or group entry."""
    name: str
    tag: int
    offset: int
    size: int
    primitive: str            # "uint64", "int64", "char", ... or "composite"
    type_name: str            # schema type, e.g. "PriceOptional", "RptSeq"
    presence: str             # "required" or "optional"
    null: int | None          # None when required
    since_version: int | None
    deprecated: int | None    # schema version it was deprecated in
    exponent: int | None      # decimal composites only (-4, -7, -8)
    members: tuple            # multi-member composites only


@dataclass(frozen=True)
class Constant:
    """A constant field: 0 bytes on the wire, value fixed by the schema."""
    name: str
    tag: int
    type_name: str
    value: int | str


@dataclass(frozen=True)
class Group:
    """A repeating group: dimension header, then num_in_group entries."""
    name: str
    tag: int
    dimension_type: str
    dimension_size: int
    dimension: tuple          # Members of the dimension header
    block_length: int
    fields: tuple
    constants: tuple


@dataclass(frozen=True)
class VarData:
    """Variable-length data: a prefix_size-byte length, then the bytes."""
    name: str
    tag: int
    type_name: str
    prefix_size: int
    prefix_primitive: str


@dataclass(frozen=True)
class Template:
    id: int
    name: str
    block_length: int
    since_version: int | None
    deprecated: int | None
    fields: tuple
    constants: tuple
    groups: tuple
    var_data: tuple

'''

FIELD_KEYS = ("name", "tag", "offset", "size", "primitive", "type_name", "presence",
              "null", "since_version", "deprecated", "exponent", "members")
CONSTANT_KEYS = ("name", "tag", "type_name", "value")
VAR_DATA_KEYS = ("name", "tag", "type_name", "prefix_size", "prefix_primitive")
MEMBER_KEYS = ("name", "offset", "size", "primitive", "null")


def call(cls, data, keys):
    args = ", ".join(f"{k}={data[k]!r}" for k in keys)
    return f"{cls}({args})"


def render_field(field):
    """Like call(), but multi-member composite members render as Member(...)."""
    args = []
    for k in FIELD_KEYS:
        if k == "members":
            inner = ", ".join(call("Member", m, MEMBER_KEYS) for m in field[k])
            args.append(f"members=({inner},)" if field[k] else "members=()")
        else:
            args.append(f"{k}={field[k]!r}")
    return f"Field({', '.join(args)})"


def render_items(lines, label, items, indent):
    """Append `label=(` item lines `)` for a tuple of already-rendered items."""
    pad = " " * indent
    if not items:
        lines.append(f"{pad}{label}=(),")
        return
    lines.append(f"{pad}{label}=(")
    for item in items:
        lines.append(f"{pad}    {item},")
    lines.append(f"{pad}),")


def render(xml_path=XML_PATH):
    """Return the full text of layouts.py for the given schema file."""
    return render_python(load_schema(xml_path))


def render_python(schema):
    """Target 1: feed_generator/b3gen/layouts.py."""
    lines = [HEADER.format(**schema)]

    lines.append("# Header layouts (packet header, framing header, SBE message header).")
    for const_name, xml_name in (("PACKET_HEADER", "PacketHeader"),
                                 ("FRAMING_HEADER", "FramingHeader"),
                                 ("MESSAGE_HEADER", "messageHeader")):
        lines.append(f"{const_name} = (")
        for m in schema["headers"][xml_name]:
            lines.append(f"    {call('Member', m, MEMBER_KEYS)},")
        lines.append(")")
    lines.append("")

    lines.append("# Enums: enum name -> {value name: value}.")
    lines.append("ENUMS = {")
    for name, values in schema["enums"].items():
        lines.append(f"    {name!r}: {values!r},")
    lines.append("}")
    lines.append("")
    lines.append("# Sets (bitfields): set name -> {bit number: choice name}.")
    lines.append("SETS = {")
    for name, bits in schema["sets"].items():
        lines.append(f"    {name!r}: {bits!r},")
    lines.append("}")
    lines.append("")

    lines.append("# Templates by id (HeaderMessage_0 is excluded: B3 never sends it).")
    lines.append("TEMPLATES = {")
    for t in schema["templates"]:
        lines.append(f"    {t['id']}: Template(")
        lines.append(f"        id={t['id']!r},")
        lines.append(f"        name={t['name']!r},")
        lines.append(f"        block_length={t['block_length']!r},")
        lines.append(f"        since_version={t['since_version']!r},")
        lines.append(f"        deprecated={t['deprecated']!r},")
        render_items(lines, "fields", [render_field(f) for f in t["fields"]], 8)
        render_items(lines, "constants",
                     [call("Constant", c, CONSTANT_KEYS) for c in t["constants"]], 8)
        if t["groups"]:
            lines.append("        groups=(")
            for g in t["groups"]:
                lines.append("            Group(")
                for k in ("name", "tag", "dimension_type", "dimension_size"):
                    lines.append(f"                {k}={g[k]!r},")
                render_items(lines, "dimension",
                             [call("Member", m, MEMBER_KEYS) for m in g["dimension"]], 16)
                lines.append(f"                block_length={g['block_length']!r},")
                render_items(lines, "fields",
                             [render_field(f) for f in g["fields"]], 16)
                render_items(lines, "constants",
                             [call("Constant", c, CONSTANT_KEYS) for c in g["constants"]], 16)
                lines.append("            ),")
            lines.append("        ),")
        else:
            lines.append("        groups=(),")
        render_items(lines, "var_data",
                     [call("VarData", d, VAR_DATA_KEYS) for d in t["var_data"]], 8)
        lines.append("    ),")
    lines.append("}")
    lines.append("")
    lines.append("# Templates by schema name, e.g. TEMPLATES_BY_NAME['Order_MBO_50'].")
    lines.append("TEMPLATES_BY_NAME = {t.name: t for t in TEMPLATES.values()}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Target 2: C++ header
# ---------------------------------------------------------------------------

CPP_TYPES = {
    "char": "char",
    "int8": "std::int8_t", "uint8": "std::uint8_t",
    "int16": "std::int16_t", "uint16": "std::uint16_t",
    "int32": "std::int32_t", "uint32": "std::uint32_t",
    "int64": "std::int64_t", "uint64": "std::uint64_t",
}
CPP_PRIMITIVES = {"char": "character", "int8": "int8", "uint8": "uint8", "int16": "int16",
                  "uint16": "uint16", "int32": "int32", "uint32": "uint32", "int64": "int64",
                  "uint64": "uint64", "composite": "composite"}

# Names the generated namespaces declare themselves; a schema name equal to
# one of them would collide.
CPP_TEMPLATE_RESERVED = {"template_id", "name", "block_length", "since_version",
                         "deprecated_since"}
CPP_GROUP_RESERVED = {"tag", "dimension_size", "dimension", "block_length"}
CPP_ENUM_RESERVED = {"underlying_type", "optional", "null_value"}
CPP_SET_RESERVED = {"underlying_type", "bit", "mask"}
# C++20 keywords that Python's keyword module does not know.
CPP_KEYWORDS = set("""alignas alignof and and_eq asm auto bitand bitor bool break case catch
char char8_t char16_t char32_t class compl concept const consteval constexpr constinit
const_cast continue co_await co_return co_yield decltype default delete do double
dynamic_cast else enum explicit export extern false float for friend goto if inline int long
mutable namespace new noexcept not not_eq nullptr operator or or_eq private protected public
register reinterpret_cast requires return short signed sizeof static static_assert
static_cast struct switch template this thread_local throw true try typedef typeid typename
union unsigned using virtual void volatile wchar_t while xor xor_eq""".split())


def cpp_name(name, where, reserved=()):
    """A schema name used as a C++ identifier, checked."""
    if (not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name) or name in CPP_KEYWORDS
            or keyword.iskeyword(name) or name in reserved or "__" in name):
        raise SchemaError(f"{where}: {name!r} is not usable as a C++ identifier here")
    return name


def cpp_char(value):
    if value == "\\":
        return r"'\\'"
    if value == "'":
        return r"'\''"
    if len(value) != 1 or not 32 <= ord(value) < 127:
        raise SchemaError(f"char constant {value!r} is not one printable ASCII character")
    return f"'{value}'"


def cpp_literal(value, primitive):
    """A C++ expression of type CPP_TYPES[primitive] for value."""
    if primitive == "char":
        if isinstance(value, str):
            return cpp_char(value) if value else "'\\0'"
        return str(value)
    bits = PRIMITIVE_SIZES[primitive] * 8
    if primitive.startswith("u"):
        if not 0 <= value < 2 ** bits:
            raise SchemaError(f"{value} out of range for {primitive}")
        return f"{value}u" if bits <= 32 else f"{value}ull"
    if not -(2 ** (bits - 1)) <= value < 2 ** (bits - 1):
        raise SchemaError(f"{value} out of range for {primitive}")
    if value == -(2 ** (bits - 1)):
        return f"std::numeric_limits<{CPP_TYPES[primitive]}>::min()"
    return str(value)


class CppWriter:
    """Accumulates lines with namespace nesting."""

    def __init__(self):
        self.lines = []
        self.depth = 0

    def line(self, text=""):
        self.lines.append(("    " * self.depth + text) if text else "")

    def const(self, ctype, name, value):
        self.line(f"inline constexpr {ctype} {name} = {value};")

    def open(self, name, comment=None):
        self.line(f"namespace {name} {{" + (f"  // {comment}" if comment else ""))
        self.depth += 1

    def close(self, name):
        self.depth -= 1
        self.line(f"}}  // namespace {name}")


def cpp_member(w, m, where):
    """A header or composite member: offset, size, type, null if it has one."""
    w.open(cpp_name(m["name"], where))
    w.const("std::uint16_t", "offset", m["offset"])
    w.const("std::uint16_t", "size", m["size"])
    w.line(f"using type = {CPP_TYPES[m['primitive']]};")
    if m["null"] is not None:
        w.const("type", "null_value", cpp_literal(m["null"], m["primitive"]))
    w.close(m["name"])


def cpp_field(w, f, where):
    name = cpp_name(f["name"], where)
    w.open(name)
    w.const("std::uint16_t", "offset", f["offset"])
    w.const("std::uint16_t", "size", f["size"])
    w.const("Primitive", "primitive", f"Primitive::{CPP_PRIMITIVES[f['primitive']]}")
    if f["primitive"] != "composite":
        w.line(f"using type = {CPP_TYPES[f['primitive']]};")
        if f["primitive"] == "char":
            w.const("std::uint16_t", "length", f["size"])
    w.const("std::string_view", "type_name", f'"{f["type_name"]}"')
    w.const("std::uint16_t", "tag", f["tag"])
    w.const("std::uint16_t", "since_version", f["since_version"] or 0)
    w.const("bool", "deprecated", "true" if f["deprecated"] is not None else "false")
    w.const("std::uint16_t", "deprecated_since", f["deprecated"] or 0)
    w.const("bool", "constant", "false")
    w.const("bool", "optional", "true" if f["presence"] == "optional" else "false")
    if f["null"] is not None:
        w.const("type", "null_value", cpp_literal(f["null"], f["primitive"]))
    if f["exponent"] is not None:
        w.const("std::int8_t", "exponent", f["exponent"])
    if f["members"]:
        w.open("members", "offsets relative to the field")
        for m in f["members"]:
            cpp_member(w, m, f"{where}.{name}")
        w.close("members")
    w.close(name)


def cpp_constant(w, c, where):
    name = cpp_name(c["name"], where)
    w.open(name, "constant: 0 bytes on the wire")
    w.const("std::uint16_t", "size", 0)
    w.const("bool", "constant", "true")
    value, primitive = c["value"], c["primitive"]
    if primitive == "char" and isinstance(value, str) and len(value) != 1:
        w.const("std::string_view", "value", f'"{value}"')
    else:
        w.const(CPP_TYPES[primitive], "value", cpp_literal(value, primitive))
    w.const("std::string_view", "type_name", f'"{c["type_name"]}"')
    w.const("std::uint16_t", "tag", c["tag"])
    w.close(name)


def cpp_fields(w, fields, constants, where, reserved):
    seen = set(reserved)
    for f in fields:
        if f["name"] in seen:
            raise SchemaError(f"{where}: duplicate or reserved name {f['name']!r}")
        seen.add(f["name"])
        cpp_field(w, f, where)
    for c in constants:
        if c["name"] in seen:
            raise SchemaError(f"{where}: duplicate or reserved name {c['name']!r}")
        seen.add(c["name"])
        cpp_constant(w, c, where)
    return seen


CPP_BANNER = """\
// GENERATED FILE. DO NOT EDIT.
//
// Generated by tools/gen_layouts.py from spec/b3-market-data-messages-2.3.1.xml
// (SHA-256 {sha256}).
// Regenerate with `make layouts`. `make layouts-check` (run by `make cpp-test`)
// fails when this file is out of date.
//
// What it does:
//   Every B3 template's layout as plain constexpr values in b3::layout:
//   templateId, blockLength, and per field the offset, size, primitive type,
//   sinceVersion, deprecated flag and null value (optional fields only).
//   Constant fields are listed with their value and size 0. Also the schema
//   constants, the packet and message headers, groups, var data, enums and sets.
//
// Conventions (spec/LAYOUTS.md section 1):
//   * Field offsets are relative to the message body, the byte right after the
//     12-byte message header. Group field offsets are relative to each entry.
//   * since_version 0 means the field exists in every version. deprecated_since
//     is the version that deprecated it (0: not deprecated). Deprecated fields
//     still occupy their bytes.
//   * Decimals report the mantissa's type plus the constant exponent.
//
// What it must not do:
//   Hold logic, or replace the header's own blockLength and version: decoders
//   must still trust those (spec/LAYOUTS.md section 5). These are the schema
//   version {schema_version} layouts. Nothing here needs a .cpp file.
"""


def render_cpp(schema):
    """Target 2: cpp_baseline/include/b3/layouts.hpp."""
    w = CppWriter()
    w.lines.append(CPP_BANNER.format(**schema))
    for inc in ("#pragma once", "", "#include <array>", "#include <cstdint>",
                "#include <limits>", "#include <string_view>", ""):
        w.lines.append(inc)
    w.open("b3::layout")
    w.line()

    w.line("// ---- Schema ----")
    w.const("std::uint16_t", "schema_id", schema["schema_id"])
    w.const("std::uint16_t", "schema_version", schema["schema_version"])
    w.const("std::string_view", "semantic_version", f'"{schema["semantic_version"]}"')
    w.const("std::string_view", "xml_sha256", f'"{schema["sha256"]}"')
    w.const("std::uint16_t", "encoding_type", f"0x{ENCODING_TYPE:04X}")
    w.const("std::uint16_t", "max_packet_size", MAX_PACKET_SIZE)
    w.line()
    w.line("enum class Primitive : std::uint8_t {")
    w.line("    " + ", ".join(dict.fromkeys(CPP_PRIMITIVES.values())) + ",")
    w.line("};")
    w.line()

    w.line("// ---- Headers ----")
    headers = schema["headers"]
    packet = headers["PacketHeader"]
    w.open("packet_header", "once per datagram")
    w.const("std::uint16_t", "size", sum(m["size"] for m in packet))
    for m in packet:
        cpp_member(w, m, "PacketHeader")
    w.close("packet_header")
    # The 12-byte message header: framing header (SOFH) then SBE message header.
    framing = headers["FramingHeader"]
    framing_size = sum(m["size"] for m in framing)
    sbe = [dict(m, offset=m["offset"] + framing_size) for m in headers["messageHeader"]]
    w.open("message_header", "once per message: framing header + SBE header")
    w.const("std::uint16_t", "size", framing_size + sum(m["size"] for m in sbe))
    w.const("std::uint16_t", "framing_size", framing_size)
    for m in framing + sbe:
        cpp_member(w, m, "messageHeader")
    w.close("message_header")
    w.line()

    w.line("// ---- Enums: underlying type and values ----")
    w.open("enums")
    for name, e in schema["enum_types"].items():
        w.open(cpp_name(name, "enums"))
        w.line(f"using underlying_type = {CPP_TYPES[e['primitive']]};")
        optional = e["presence"] == "optional"
        w.const("bool", "optional", "true" if optional else "false")
        if optional:
            w.const("underlying_type", "null_value", cpp_literal(e["null"], e["primitive"]))
        for value_name, value in e["values"].items():
            w.const("underlying_type", cpp_name(value_name, f"enum {name}", CPP_ENUM_RESERVED),
                    cpp_literal(value, e["primitive"]))
        w.close(name)
    w.close("enums")
    w.line()

    w.line("// ---- Sets: bit positions and masks ----")
    w.open("sets")
    for name, st in schema["set_types"].items():
        w.open(cpp_name(name, "sets"))
        w.line(f"using underlying_type = {CPP_TYPES[st['primitive']]};")
        w.open("bit")
        for bit, choice in sorted(st["bits"].items()):
            w.const("unsigned", cpp_name(choice, f"set {name}", CPP_SET_RESERVED), bit)
        w.close("bit")
        w.open("mask")
        for bit, choice in sorted(st["bits"].items()):
            w.const("underlying_type", choice, cpp_literal(1 << bit, st["primitive"]))
        w.close("mask")
        w.close(name)
    w.close("sets")
    w.line()

    w.line("// ---- Templates (HeaderMessage_0 is excluded: B3 never sends it) ----")
    ids = [t["id"] for t in schema["templates"]]
    w.line(f"inline constexpr std::array<std::uint16_t, {len(ids)}> template_ids = {{")
    w.line("    " + ", ".join(str(i) for i in ids) + ",")
    w.line("};")
    for t in schema["templates"]:
        tname = cpp_name(t["name"], "templates")
        w.line()
        w.open(tname)
        w.const("std::uint16_t", "template_id", t["id"])
        w.const("std::string_view", "name", f'"{t["name"]}"')
        w.const("std::uint16_t", "block_length", t["block_length"])
        w.const("std::uint16_t", "since_version", t["since_version"] or 0)
        w.const("std::uint16_t", "deprecated_since", t["deprecated"] or 0)
        seen = cpp_fields(w, t["fields"], t["constants"], tname, CPP_TEMPLATE_RESERVED)
        for g in t["groups"]:
            gname = cpp_name(g["name"], tname)
            if gname in seen:
                raise SchemaError(f"{tname}: group {gname!r} collides with another name")
            seen.add(gname)
            w.open(gname, "repeating group: dimension header, then entries")
            w.const("std::uint16_t", "tag", g["tag"])
            w.const("std::uint16_t", "dimension_size", g["dimension_size"])
            w.const("std::uint16_t", "block_length", g["block_length"])
            w.open("dimension")
            for m in g["dimension"]:
                cpp_member(w, m, f"{tname}.{gname}.dimension")
            w.close("dimension")
            cpp_fields(w, g["fields"], g["constants"], f"{tname}.{gname}", CPP_GROUP_RESERVED)
            w.close(gname)
        for d in t["var_data"]:
            dname = cpp_name(d["name"], tname)
            if dname in seen:
                raise SchemaError(f"{tname}: var data {dname!r} collides with another name")
            seen.add(dname)
            w.open(dname, "var data: length prefix, then that many bytes")
            w.const("std::uint16_t", "tag", d["tag"])
            w.const("std::uint16_t", "prefix_size", d["prefix_size"])
            w.line(f"using prefix_type = {CPP_TYPES[d['prefix_primitive']]};")
            w.const("std::string_view", "type_name", f'"{d["type_name"]}"')
            w.close(dname)
        w.close(tname)
    w.line()
    w.close("b3::layout")
    return "\n".join(w.lines) + "\n"


# ---------------------------------------------------------------------------
# Target 3: C++ field descriptors (fields.hpp)
# ---------------------------------------------------------------------------

# Names fields.hpp declares itself inside a template namespace or a group struct.
FIELDS_TEMPLATE_RESERVED = {"field_list", "group_list", "var_data_list", "constant_list"}
FIELDS_GROUP_RESERVED = {"name", "dimension_size", "block_length", "block_length_offset",
                         "block_length_type", "num_in_group_offset", "num_in_group_type",
                         "fields"}

FIELDS_BANNER = """\
// GENERATED FILE. DO NOT EDIT.
//
// Generated by tools/gen_layouts.py from spec/b3-market-data-messages-2.3.1.xml
// (SHA-256 {sha256}).
// Regenerate with `make layouts`. `make layouts-check` (run by `make cpp-test`)
// fails when this file is out of date.
//
// What it does:
//   One descriptor type per field, group, var data field and constant of every
//   template, in b3::fields::<Template>, and b3::fields::Template<templateId>
//   listing them. Every value is read from layouts.hpp, never repeated, so the
//   two headers cannot disagree. decode.hpp's get<F>() turns a descriptor into
//   a field accessor; the full decode writer walks the lists.
//
// What it must not do:
//   Hold logic, or values of its own: the layouts live in layouts.hpp.
"""


def fields_kind(primitive, size):
    if primitive == "composite":
        return "composite"
    if primitive == "char":
        return "character" if size == 1 else "char_array"
    return "integer"


def fields_field(w, f, path, where):
    """A descriptor struct for one root or entry field. path: the layouts.hpp
    namespace holding the field's namespace, e.g. L::Order_MBO_50."""
    name = cpp_name(f["name"], where)
    src = f"{path}::{name}"
    w.line(f"struct {name} {{")
    w.depth += 1
    w.line(f'static constexpr std::string_view name = "{name}";')
    w.line(f"static constexpr decode::Kind kind = decode::Kind::{fields_kind(f['primitive'], f['size'])};")
    for member in ("offset", "size", "since_version"):
        w.line(f"static constexpr std::uint16_t {member} = {src}::{member};")
    w.line(f"static constexpr bool optional = {src}::optional;")
    if f["primitive"] == "composite":
        members = []
        for m in f["members"]:
            mname = cpp_name(m["name"], f"{where}.{name}")
            msrc = f"{src}::members::{mname}"
            w.line(f"struct {mname} {{")
            w.depth += 1
            w.line(f'static constexpr std::string_view name = "{mname}";')
            w.line("static constexpr decode::Kind kind = decode::Kind::integer;")
            w.line(f"static constexpr std::uint16_t offset = {msrc}::offset;")
            w.line(f"static constexpr std::uint16_t size = {msrc}::size;")
            w.line("static constexpr std::uint16_t since_version = 0;")
            w.line(f"using type = {msrc}::type;")
            w.line(f"static constexpr bool optional = {'true' if m['null'] is not None else 'false'};")
            if m["null"] is not None:
                w.line(f"static constexpr type null_value = {msrc}::null_value;")
            w.depth -= 1
            w.line("};")
            members.append(mname)
        w.line(f"using members = decode::List<{', '.join(members)}>;")
    else:
        w.line(f"using type = {src}::type;")
        if f["null"] is not None:
            w.line(f"static constexpr type null_value = {src}::null_value;")
    w.depth -= 1
    w.line("};")
    return name


def render_cpp_fields(schema):
    """Target 3: cpp_baseline/include/b3/fields.hpp."""
    w = CppWriter()
    w.lines.append(FIELDS_BANNER.format(**schema))
    for inc in ("#pragma once", "", "#include <cstdint>", "#include <string_view>", "",
                '#include "b3/decode.hpp"', '#include "b3/layouts.hpp"', ""):
        w.lines.append(inc)
    w.open("b3::fields")
    w.line("namespace L = b3::layout;")
    w.line()
    w.line("// Template<templateId>: the descriptor lists of one template.")
    w.line("template <std::uint16_t TemplateId>")
    w.line("struct Template;")
    for t in schema["templates"]:
        tname = cpp_name(t["name"], "templates")
        tpath = f"L::{tname}"
        w.line()
        w.open(tname)
        names = set()
        def check(n):
            if n in names or n in FIELDS_TEMPLATE_RESERVED:
                raise SchemaError(f"{tname}: {n!r} collides in fields.hpp")
            names.add(n)
        field_names = []
        for f in t["fields"]:
            check(f["name"])
            field_names.append(fields_field(w, f, tpath, tname))
        constant_names = []
        for c in t["constants"]:
            cname = cpp_name(c["name"], tname)
            check(cname)
            w.line(f"struct {cname} {{")
            w.depth += 1
            w.line(f'static constexpr std::string_view name = "{cname}";')
            w.line(f"static constexpr auto value = {tpath}::{cname}::value;   // schema constant, not on the wire")
            w.depth -= 1
            w.line("};")
            constant_names.append(cname)
        group_names = []
        for g in t["groups"]:
            gname = cpp_name(g["name"], tname)
            check(gname)
            gpath = f"{tpath}::{gname}"
            w.line(f"struct {gname} {{")
            w.depth += 1
            w.line(f'static constexpr std::string_view name = "{gname}";')
            w.line(f"static constexpr std::uint16_t dimension_size = {gpath}::dimension_size;")
            w.line(f"static constexpr std::uint16_t block_length = {gpath}::block_length;   // the schema's; trust the wire's")
            w.line(f"static constexpr std::uint16_t block_length_offset = {gpath}::dimension::blockLength::offset;")
            w.line(f"using block_length_type = {gpath}::dimension::blockLength::type;")
            w.line(f"static constexpr std::uint16_t num_in_group_offset = {gpath}::dimension::numInGroup::offset;")
            w.line(f"using num_in_group_type = {gpath}::dimension::numInGroup::type;")
            entry_names = []
            seen = set()
            for f in g["fields"]:
                if f["name"] in FIELDS_GROUP_RESERVED or f["name"] in seen:
                    raise SchemaError(f"{tname}.{gname}: {f['name']!r} collides in fields.hpp")
                seen.add(f["name"])
                entry_names.append(fields_field(w, f, gpath, f"{tname}.{gname}"))
            w.line(f"using fields = decode::List<{', '.join(entry_names)}>;")
            w.depth -= 1
            w.line("};")
            group_names.append(gname)
        var_names = []
        for d in t["var_data"]:
            dname = cpp_name(d["name"], tname)
            check(dname)
            w.line(f"struct {dname} {{")
            w.depth += 1
            w.line(f'static constexpr std::string_view name = "{dname}";')
            w.line(f"static constexpr std::uint16_t prefix_size = {tpath}::{dname}::prefix_size;")
            w.line(f"using prefix_type = {tpath}::{dname}::prefix_type;")
            w.depth -= 1
            w.line("};")
            var_names.append(dname)
        w.line(f"using field_list = decode::List<{', '.join(field_names)}>;")
        w.line(f"using constant_list = decode::List<{', '.join(constant_names)}>;")
        w.line(f"using group_list = decode::List<{', '.join(group_names)}>;")
        w.line(f"using var_data_list = decode::List<{', '.join(var_names)}>;")
        w.close(tname)
        w.line("template <>")
        w.line(f"struct Template<{tpath}::template_id> {{")
        w.depth += 1
        w.line(f"static constexpr std::uint16_t template_id = {tpath}::template_id;")
        w.line(f"static constexpr std::string_view name = {tpath}::name;")
        w.line(f"static constexpr std::uint16_t block_length = {tpath}::block_length;   // the schema's")
        for alias, kind in (("fields", "field"), ("constants", "constant"), ("groups", "group"),
                            ("var_data", "var_data")):
            w.line(f"using {alias} = {tname}::{kind}_list;")
        w.depth -= 1
        w.line("};")
    w.line()
    w.close("b3::fields")
    return "\n".join(w.lines) + "\n"


# ---------------------------------------------------------------------------
# Output targets
# ---------------------------------------------------------------------------

# (path relative to the repo root, render function taking the parsed schema).
# A SystemVerilog package for the RTL becomes a third entry here.
OUTPUTS = (
    (PY_OUT, render_python),
    (CPP_OUT, render_cpp),
    (FIELDS_OUT, render_cpp_fields),
)


def render_all(xml_path=XML_PATH):
    """{relative output path: text} for every target, from one parse."""
    schema = load_schema(xml_path)
    return {path: render_fn(schema) for path, render_fn in OUTPUTS}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate B3 layouts from the SBE schema.")
    ap.add_argument("--out-dir", type=Path, default=REPO,
                    help="root to write the outputs under (default: the repo, in place)")
    args = ap.parse_args(argv)
    try:
        outputs = render_all()
    except SchemaError as exc:
        sys.exit(f"gen_layouts: {exc}")
    for rel, text in outputs.items():
        path = args.out_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n": byte-identical output on every OS.
        path.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
