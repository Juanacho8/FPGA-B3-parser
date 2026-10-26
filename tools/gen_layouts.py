"""Generate b3gen/layouts.py from B3's SBE schema.

Reads spec/b3-market-data-messages-2.3.1.xml, applies the SBE 1.0 layout
rules listed in spec/LAYOUTS.md ("How the layouts were verified") and writes
a plain, readable Python module with every template's field offsets, sizes,
types, presence and null values, plus enums, sets and the header layouts.

Usage (from feed_generator/):

    python3 tools/gen_layouts.py

The output is checked in. tests/test_layouts.py regenerates it in memory and
fails if the checked-in file differs, so edit this script, never the output.
Standard library only.
"""

import hashlib
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FEED_GENERATOR = REPO / "feed_generator"
XML_PATH = REPO / "spec" / "b3-market-data-messages-2.3.1.xml"
OUT_PATH = FEED_GENERATOR / "b3gen" / "layouts.py"

# The exact schema file every layout in spec/LAYOUTS.md was verified against.
EXPECTED_SHA256 = "b055b9a948756ab795f0f6f645a571094cacb85133e091e94440b812e17503c4"

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

    def constant(value):
        return "constant", {"name": name, "tag": tag, "type_name": type_name, "value": value}

    if type_name in types.simple:
        t = types.simple[type_name]
        # Rule: constants take 0 bytes, whether the field or the type says so.
        if field_presence == "constant" or t["presence"] == "constant":
            ref = elem.get("valueRef")
            return constant(types.value_ref(ref) if ref else t["const"])
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
            return constant(types.value_ref(elem.get("valueRef")))
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
    if digest != EXPECTED_SHA256:
        raise SchemaError(f"{xml_path}: SHA-256 {digest} does not match the verified "
                          f"schema {EXPECTED_SHA256}. Refusing to generate.")
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
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

HEADER = '''"""Support: field offsets and sizes for every B3 template.

GENERATED by tools/gen_layouts.py from spec/b3-market-data-messages-2.3.1.xml.
Do not edit by hand. Change the generator and run, from feed_generator/:

    python3 tools/gen_layouts.py

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
    schema = load_schema(xml_path)
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


def main():
    try:
        text = render()
    except SchemaError as exc:
        sys.exit(f"gen_layouts: {exc}")
    OUT_PATH.write_text(text, encoding="utf-8")
    print(f"wrote {OUT_PATH.relative_to(REPO)}")


if __name__ == "__main__":
    main()
